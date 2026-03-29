#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Mubo (無貌) — One-shot Local LLM Bootstrapper
# Automated environment setup script based on spec.md
# ============================================================

# ---------- Colored Output Helpers ----------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()  { printf "${BLUE}[INFO]${NC}  %s\n" "$*"; }
ok()    { printf "${GREEN}[OK]${NC}    %s\n" "$*"; }
warn()  { printf "${YELLOW}[WARN]${NC}  %s\n" "$*"; }
err()   { printf "${RED}[ERROR]${NC} %s\n" "$*"; }
step()  { printf "\n${BOLD}${CYAN}── %s ──${NC}\n" "$*"; }

# ---------- Global Variables ----------
OS=""
ARCH=""
RAM_GB=0
GPU_TYPE="none"       # none | apple | nvidia | amd
GPU_VRAM_GB=0
UNIFIED_MEM=false
HAS_NPU=false
NVIDIA_DOCKER_OK=false
OLLAMA_INSTALLED=false
BASE_MODEL="nemotron-3-nano:4b"
DERIVED_MODEL="nemotron-3-nano:4b"
CTX_LENGTH=262144      # Default 64K

# ============================================================
# Phase 1: Environment Detection
# ============================================================
detect_environment() {
    step "Phase 1: Environment Detection"

    # --- OS ---
    case "$(uname -s)" in
        Darwin) OS="macos" ;;
        Linux)  OS="linux" ;;
        *)
            err "Unsupported OS: $(uname -s)"
            err "macOS or Linux is required"
            exit 1
            ;;
    esac
    info "OS: ${OS}"

    # --- Architecture ---
    ARCH="$(uname -m)"
    info "Arch: ${ARCH}"

    # --- RAM ---
    if [[ "$OS" == "macos" ]]; then
        RAM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ))
    else
        RAM_GB=$(awk '/MemTotal/ {printf "%d", $2/1048576}' /proc/meminfo)
    fi
    info "RAM: ${RAM_GB} GB"

    # --- GPU ---
    detect_gpu

    # --- Summary ---
    ok "Environment detection complete: OS=${OS}, Arch=${ARCH}, RAM=${RAM_GB}GB, GPU=${GPU_TYPE}"
    if [[ "$GPU_TYPE" == "nvidia" ]]; then
        info "NVIDIA VRAM: ${GPU_VRAM_GB} GB"
    fi
    if [[ "$UNIFIED_MEM" == true ]]; then
        info "Apple Silicon unified memory detected"
    fi
    if [[ "$HAS_NPU" == true ]]; then
        info "NPU (Neural Engine) detected"
    fi
}

detect_gpu() {
    if [[ "$OS" == "macos" ]]; then
        # Apple Silicon check
        if [[ "$ARCH" == "arm64" ]]; then
            GPU_TYPE="apple"
            UNIFIED_MEM=true
            HAS_NPU=true
            GPU_VRAM_GB=$RAM_GB  # Unified memory: RAM=VRAM
        else
            # Intel Mac — external GPU detection is best-effort
            if system_profiler SPDisplaysDataType 2>/dev/null | grep -qi "AMD\|Radeon"; then
                GPU_TYPE="amd"
            fi
        fi
    elif [[ "$OS" == "linux" ]]; then
        if command -v nvidia-smi &>/dev/null; then
            GPU_TYPE="nvidia"
            GPU_VRAM_GB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null \
                | head -1 | awk '{printf "%d", $1/1024}') || GPU_VRAM_GB=0
        elif [[ -d /sys/class/drm ]] && ls /sys/class/drm/card*/device/vendor 2>/dev/null | xargs grep -l 0x1002 &>/dev/null; then
            GPU_TYPE="amd"
        fi
    fi
}

# ============================================================
# Phase 2: Ollama Installation & Connectivity Check
# ============================================================
setup_ollama() {
    step "Phase 2: Ollama Installation & Connectivity Check"

    # --- Installation check ---
    if command -v ollama &>/dev/null; then
        OLLAMA_INSTALLED=true
        ok "Ollama is already installed: $(ollama --version 2>/dev/null || echo 'version unknown')"
    else
        info "Ollama not found. Installing..."
        install_ollama
    fi

    # --- Server startup check ---
    ensure_ollama_running
}

install_prerequisites() {
    if [[ "$OS" == "linux" ]]; then
        # zstd is required by the Ollama installer
        if ! command -v zstd &>/dev/null; then
            info "Installing zstd (required for Ollama installation)..."
            if command -v apt-get &>/dev/null; then
                sudo apt-get install -y zstd
            elif command -v dnf &>/dev/null; then
                sudo dnf install -y zstd
            elif command -v yum &>/dev/null; then
                sudo yum install -y zstd
            elif command -v pacman &>/dev/null; then
                sudo pacman -S --noconfirm zstd
            else
                warn "Cannot auto-install zstd. Please install it manually"
            fi
        fi
    fi
}

install_docker() {
    if [[ "$OS" == "macos" ]]; then
        if command -v brew &>/dev/null; then
            info "Installing Docker via Homebrew..."
            brew install --cask docker
            ok "Docker Desktop installation complete"
            info "Starting Docker Desktop..."
            open -a Docker 2>/dev/null || true
            # Wait for startup
            local dw=0
            while ! docker info &>/dev/null 2>&1; do
                sleep 2
                dw=$((dw + 2))
                if [[ $dw -ge 60 ]]; then
                    warn "Docker Desktop is taking a long time to start"
                    break
                fi
            done
        else
            err "Homebrew is required to install Docker Desktop"
            info "  Homebrew: https://brew.sh"
            info "  Or install manually: https://docs.docker.com/desktop/install/mac-install/"
            return 1
        fi
    elif [[ "$OS" == "linux" ]]; then
        info "Installing Docker Engine..."
        if command -v apt-get &>/dev/null; then
            # Debian/Ubuntu
            sudo apt-get update -y
            sudo apt-get install -y ca-certificates curl gnupg
            sudo install -m 0755 -d /etc/apt/keyrings
            curl -fsSL https://download.docker.com/linux/$(. /etc/os-release && echo "$ID")/gpg \
                | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null || true
            sudo chmod a+r /etc/apt/keyrings/docker.gpg
            echo \
                "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$(. /etc/os-release && echo "$ID") \
                $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
                sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
            sudo apt-get update -y
            sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
        elif command -v dnf &>/dev/null; then
            # Fedora/RHEL
            sudo dnf -y install dnf-plugins-core
            sudo dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo 2>/dev/null || true
            sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
        else
            # Fallback: official convenience script
            info "Installing Docker via official install script..."
            curl -fsSL https://get.docker.com | sh
        fi
        # Start daemon & add current user to docker group
        sudo systemctl enable docker 2>/dev/null || true
        sudo systemctl start docker 2>/dev/null || true
        sudo usermod -aG docker "$USER" 2>/dev/null || true
        ok "Docker Engine installation complete"
        info "You may need to re-login for docker group membership to take effect"
    fi
}

install_ollama() {
    install_prerequisites

    if [[ "$OS" == "macos" ]]; then
        if command -v brew &>/dev/null; then
            info "Installing Ollama via Homebrew..."
            brew install ollama
        else
            info "Installing Ollama via official install script..."
            curl -fsSL https://ollama.com/install.sh | sh
        fi
    elif [[ "$OS" == "linux" ]]; then
        info "Installing Ollama via official install script..."
        curl -fsSL https://ollama.com/install.sh | sh
    fi

    if command -v ollama &>/dev/null; then
        OLLAMA_INSTALLED=true
        ok "Ollama installation complete"
    else
        err "Ollama installation failed"
        err "Manual installation: https://ollama.com/download"
        exit 1
    fi
}

ensure_ollama_running() {
    local max_wait=30
    local waited=0

    # Check if already running
    if curl -s -o /dev/null -w "%{http_code}" http://localhost:11434/api/tags 2>/dev/null | grep -q "200"; then
        ok "Ollama server is running"
        return
    fi

    info "Starting Ollama server..."
    if [[ "$OS" == "macos" ]]; then
        # macOS: launch as app or background
        if [[ -d "/Applications/Ollama.app" ]]; then
            open -a Ollama
        else
            ollama serve &>/dev/null &
        fi
    else
        # Linux: systemd or background
        if systemctl is-enabled ollama &>/dev/null 2>&1; then
            sudo systemctl start ollama 2>/dev/null || ollama serve &>/dev/null &
        else
            ollama serve &>/dev/null &
        fi
    fi

    # Wait for connectivity
    info "Waiting for Ollama server to start..."
    while ! curl -s -o /dev/null http://localhost:11434/api/tags 2>/dev/null; do
        sleep 1
        waited=$((waited + 1))
        if [[ $waited -ge $max_wait ]]; then
            err "Ollama server startup timed out (${max_wait}s)"
            err "Please run 'ollama serve' manually in another terminal"
            exit 1
        fi
    done
    ok "Ollama server connectivity confirmed"
}

# ============================================================
# Phase 3: Model Download
# ============================================================
pull_model() {
    step "Phase 3: Model Download (${BASE_MODEL})"

    # Check if already downloaded
    if ollama list 2>/dev/null | grep -q "${BASE_MODEL}"; then
        ok "${BASE_MODEL} is already downloaded"
        return
    fi

    info "Downloading ${BASE_MODEL}... (this may take a while due to large file size)"
    if ollama pull "${BASE_MODEL}"; then
        ok "${BASE_MODEL} download complete"
    else
        err "${BASE_MODEL} download failed"
        err "Please check your network connection"
        exit 1
    fi
}

# ============================================================
# Phase 4: Extended Context Derived Model Creation
# ============================================================
create_extended_model() {
    step "Phase 4: Extended Context Model Creation"

    # Determine context length based on RAM
    decide_context_length

    # Skip if already exists
    if ollama list 2>/dev/null | grep -q "${DERIVED_MODEL}"; then
        warn "${DERIVED_MODEL} already exists. Recreating..."
    fi

    local modelfile
    modelfile=$(mktemp /tmp/mubo-modelfile.XXXXXX)

    cat > "$modelfile" <<EOF
FROM ${BASE_MODEL}
PARAMETER num_ctx ${CTX_LENGTH}
PARAMETER num_gpu 999
EOF

    info "Modelfile generated: ctx=${CTX_LENGTH} ($(( CTX_LENGTH / 1024 ))K)"
    info "Creating derived model ${DERIVED_MODEL}..."

    if ollama create "${DERIVED_MODEL}" -f "$modelfile"; then
        ok "${DERIVED_MODEL} created (ctx=$(( CTX_LENGTH / 1024 ))K)"
    else
        err "Derived model creation failed"
        warn "Base model ${BASE_MODEL} is still available"
    fi

    rm -f "$modelfile"
}

decide_context_length() {
    # Apple Silicon unified memory or large VRAM -> aim for 128K
    local available_mem=$RAM_GB
    if [[ "$GPU_TYPE" == "nvidia" ]] && (( GPU_VRAM_GB > 0 )); then
        available_mem=$GPU_VRAM_GB
    fi

    if (( available_mem >= 64 )); then
        CTX_LENGTH=262144   # 128K
        info "Sufficient memory (${available_mem}GB): setting 128K context"
    elif (( available_mem >= 32 )); then
        CTX_LENGTH=262144    # 64K
        info "Moderate memory (${available_mem}GB): setting 64K context"
    elif (( available_mem >= 16 )); then
        CTX_LENGTH=32768    # 32K
        warn "Limited memory (${available_mem}GB): restricting to 32K context"
    else
        CTX_LENGTH=16384    # 16K
        warn "Low memory (${available_mem}GB): restricting to 16K context"
    fi
}

# ============================================================
# Phase 5: Additional Environment (optional, failures do not affect core setup)
# ============================================================
setup_extras() {
    step "Phase 5: Additional Environment Setup (optional)"

    info "Even if additional setup fails, Ollama + ${DERIVED_MODEL} remains available"

    # --- uv (Python package manager) ---
    setup_uv

    # --- vLLM (Linux + NVIDIA GPU only) ---
    if [[ "$OS" == "linux" && "$GPU_TYPE" == "nvidia" ]]; then
        setup_vllm
    else
        info "vLLM: skipped (Linux + NVIDIA GPU only)"
    fi

    # --- MLX (macOS Apple Silicon only) ---
    if [[ "$OS" == "macos" && "$ARCH" == "arm64" ]]; then
        setup_mlx
    else
        info "MLX: skipped (macOS Apple Silicon only)"
    fi

    # --- Docker ---
    setup_docker_check
}

setup_uv() {
    if command -v uv &>/dev/null; then
        ok "uv is already installed"
        return
    fi

    info "Installing uv..."
    if curl -LsSf https://astral.sh/uv/install.sh | sh 2>/dev/null; then
        ok "uv installation complete"
    else
        warn "uv installation failed (does not affect core setup)"
    fi
}

setup_vllm() {
    info "Attempting vLLM setup..."

    if ! command -v python3 &>/dev/null; then
        warn "Python3 not found. Skipping vLLM"
        return
    fi

    if python3 -c "import vllm" 2>/dev/null; then
        ok "vLLM is already installed"
        return
    fi

    if pip3 install vllm 2>/dev/null || pip install vllm 2>/dev/null; then
        ok "vLLM installation complete"
    else
        warn "vLLM installation failed (does not affect core setup)"
    fi
}

setup_mlx() {
    info "Attempting MLX setup..."

    if ! command -v python3 &>/dev/null; then
        warn "Python3 not found. Skipping MLX"
        return
    fi

    if python3 -c "import mlx" 2>/dev/null; then
        ok "MLX is already installed"
        return
    fi

    if pip3 install mlx mlx-lm 2>/dev/null || pip install mlx mlx-lm 2>/dev/null; then
        ok "MLX installation complete"
    else
        warn "MLX installation failed (does not affect core setup)"
    fi
}

setup_docker_check() {
    if ! command -v docker &>/dev/null; then
        info "Docker not found. Installing..."
        install_docker
    fi

    if ! command -v docker &>/dev/null; then
        warn "Docker installation failed (does not affect core setup)"
        return
    fi

    # Docker daemon startup check
    if ! docker info &>/dev/null 2>&1; then
        info "Starting Docker daemon..."
        if [[ "$OS" == "macos" ]]; then
            open -a Docker 2>/dev/null || true
            # Wait up to 60 seconds for Docker Desktop to start
            local dw=0
            while ! docker info &>/dev/null 2>&1; do
                sleep 2
                dw=$((dw + 2))
                if [[ $dw -ge 60 ]]; then
                    warn "Docker daemon startup timed out"
                    return
                fi
            done
        elif [[ "$OS" == "linux" ]]; then
            sudo systemctl start docker 2>/dev/null || sudo service docker start 2>/dev/null || true
            sleep 2
        fi
    fi

    if docker info &>/dev/null 2>&1; then
        ok "Docker daemon is running"
    else
        warn "Docker is installed but the daemon is not running"
        return
    fi

    # --- NVIDIA Container Runtime / nvidia-docker test ---
    if [[ "$OS" != "linux" || "$GPU_TYPE" != "nvidia" ]]; then
        info "nvidia-docker test: skipped (Linux + NVIDIA GPU only)"
        return
    fi

    test_nvidia_docker
}

test_nvidia_docker() {
    info "Running nvidia-docker functionality test..."

    # 1. Check if nvidia-container-toolkit / nvidia-docker2 is installed
    local has_runtime=false
    if docker info 2>/dev/null | grep -qi "nvidia"; then
        has_runtime=true
    elif command -v nvidia-container-cli &>/dev/null; then
        has_runtime=true
    elif dpkg -l nvidia-container-toolkit &>/dev/null 2>&1; then
        has_runtime=true
    elif rpm -q nvidia-container-toolkit &>/dev/null 2>&1; then
        has_runtime=true
    fi

    if [[ "$has_runtime" == false ]]; then
        warn "nvidia-container-toolkit not found"
        warn "Installation guide: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html"
        return
    fi
    ok "nvidia-container-toolkit detected"

    # 2. Test if nvidia-smi runs with --gpus flag (live functionality test)
    info "Running nvidia-smi inside container to verify GPU access..."
    local test_output
    if test_output=$(docker run --rm --gpus all nvidia/cuda:12.6.3-base-ubuntu24.04 nvidia-smi 2>&1); then
        ok "nvidia-docker verified: GPU is accessible from containers"
        NVIDIA_DOCKER_OK=true

        # Display GPU name and driver version
        local driver_ver
        driver_ver=$(echo "$test_output" | grep "Driver Version" | sed 's/.*Driver Version: *\([^ ]*\).*/\1/' | head -1)
        local gpu_name
        gpu_name=$(echo "$test_output" | grep -oP '\| +\K[A-Za-z0-9 ._-]+(?= +[A-Za-z])' | head -1 | xargs)
        if [[ -n "$driver_ver" ]]; then
            info "  Driver: ${driver_ver}"
        fi
        if [[ -n "$gpu_name" ]]; then
            info "  GPU:    ${gpu_name}"
        fi
    else
        warn "nvidia-docker test failed: GPU is not accessible from containers"
        warn "Error details:"
        echo "$test_output" | tail -5 | while IFS= read -r line; do
            warn "  $line"
        done
        warn ""
        warn "Troubleshooting:"
        warn "  1. Reinstall nvidia-container-toolkit:"
        warn "     sudo apt-get install -y nvidia-container-toolkit"
        warn "     sudo nvidia-ctk runtime configure --runtime=docker"
        warn "     sudo systemctl restart docker"
        warn "  2. Restart Docker daemon: sudo systemctl restart docker"
        warn "  3. Verify NVIDIA driver is working: nvidia-smi"
    fi
}

# ============================================================
# Phase 6: Mubo Agent (Self-Rewriting AI) Deploy & Launch
# ============================================================
generate_agent_identity() {
    local agent_dir="$1"
    local config_file="${agent_dir}/config.json"

    # Skip if config.json already exists
    if [[ -f "$config_file" ]]; then
        ok "Agent configuration already exists"
        return
    fi

    info "Asking Ollama to generate agent name and color scheme..."

    # Collect machine information
    local hostname
    hostname="$(hostname 2>/dev/null || echo 'unknown')"
    local username
    username="$(whoami 2>/dev/null || echo 'user')"
    local machine_model=""
    if [[ "$OS" == "macos" ]]; then
        machine_model="$(sysctl -n hw.model 2>/dev/null || echo '')"
    elif [[ -f /sys/devices/virtual/dmi/id/product_name ]]; then
        machine_model="$(cat /sys/devices/virtual/dmi/id/product_name 2>/dev/null || echo '')"
    fi
    local shell_name
    shell_name="$(basename "${SHELL:-bash}")"
    local locale_info
    locale_info="$(echo "${LANG:-${LC_ALL:-en_US.UTF-8}}")"
    local current_hour
    current_hour="$(date +%H)"
    local term_program
    term_program="${TERM_PROGRAM:-unknown}"
    # macOS: dark mode detection
    local os_appearance="unknown"
    if [[ "$OS" == "macos" ]]; then
        if defaults read -g AppleInterfaceStyle &>/dev/null 2>&1; then
            os_appearance="dark"
        else
            os_appearance="light"
        fi
    fi

    local prompt
    prompt="あなたはAIエージェントの命名とUIデザインの専門家です。
以下のマシン情報から、このマシンに住むAIエージェントにふさわしい名前とカラースキームを考えてください。

マシン情報:
- ホスト名: ${hostname}
- ユーザー名: ${username}
- OS: ${OS} (${ARCH})
- マシンモデル: ${machine_model:-不明}
- RAM: ${RAM_GB}GB
- GPU: ${GPU_TYPE}
- シェル: ${shell_name}
- ロケール: ${locale_info}
- 現在時刻: ${current_hour}時
- ターミナル: ${term_program}
- OS外観モード: ${os_appearance}

Respond ONLY with the following JSON. No other text.
All \"strings\" values MUST be translated to match the locale (${locale_info}).
If the locale is ja_JP, translate to Japanese. If en_US, keep English. If fr_FR, translate to French. Etc.
{
  \"agent_name\": \"A short name fitting this machine (2-4 chars, native language or English)\",
  \"agent_name_en\": \"English name\",
  \"theme\": \"dark or light (based on OS appearance and machine vibe)\",
  \"colors\": {
    \"bg\": \"background color (hex)\",
    \"bg_secondary\": \"secondary background (hex)\",
    \"header_bg\": \"header background (CSS gradient OK)\",
    \"text\": \"text color (hex)\",
    \"text_secondary\": \"secondary text color (hex)\",
    \"accent\": \"accent color (hex)\",
    \"accent_secondary\": \"secondary accent (hex)\",
    \"user_msg_bg\": \"user message background (hex)\",
    \"user_msg_border\": \"user message border (hex)\",
    \"assistant_msg_bg\": \"assistant message background (hex)\",
    \"assistant_msg_border\": \"assistant message border (hex)\",
    \"input_bg\": \"input background (hex)\",
    \"input_border\": \"input border (hex)\",
    \"button_bg\": \"send button background (CSS gradient OK)\",
    \"system_msg_bg\": \"system message background (hex)\",
    \"system_msg_border\": \"system message border (hex)\",
    \"system_msg_text\": \"system message text color (hex)\"
  },
  \"personality\": \"One sentence describing this agent personality (in locale language)\",
  \"strings\": {
    \"plugins\": \"Plugins (translated to locale)\",
    \"history\": \"History (translated)\",
    \"undo\": \"Undo (translated)\",
    \"reset\": \"Reset (translated)\",
    \"send\": \"Send (translated)\",
    \"close\": \"Close (translated)\",
    \"placeholder\": \"Type a message... (translated)\",
    \"history_title\": \"Git History (agent/app.py) (translated)\",
    \"plugins_title\": \"Plugins (translated)\",
    \"no_history\": \"No history available (translated)\",
    \"no_plugins\": \"No plugins yet. Ask in chat to create one. (translated)\",
    \"confirm_reset\": \"Reset to initial state? (translated)\",
    \"confirm_revert\": \"Revert to this version? (translated)\",
    \"confirm_delete_plugin\": \"Delete plugin? (translated)\",
    \"revert_btn\": \"Restore (translated)\",
    \"delete_btn\": \"Delete (translated)\",
    \"no_description\": \"No description (translated)\",
    \"code_rewritten\": \"Code rewritten. Server restarting. (translated)\",
    \"error_prefix\": \"Error (translated)\",
    \"error_ollama\": \"Cannot connect to Ollama server. Run ollama serve. (translated)\",
    \"restored_msg\": \"Restored. Server restarting. (translated)\",
    \"no_previous\": \"No previous state (translated)\",
    \"plugin_created\": \"Plugin created (translated)\",
    \"initial_not_found\": \"Initial commit not found (translated)\"
  }
}"

    # Build the JSON request body via python to avoid shell escaping issues
    local request_body
    request_body=$(python3 -c "
import json, sys
prompt = sys.stdin.read()
body = {'model': '${BASE_MODEL}', 'prompt': prompt, 'stream': False, 'options': {'temperature': 0.8}}
print(json.dumps(body))
" <<< "$prompt" 2>/dev/null)

    if [[ -z "$request_body" ]]; then
        warn "Failed to build request. Using default configuration"
        _write_default_config "$config_file"
        return
    fi

    local response
    response=$(curl -s --max-time 180 "${OLLAMA_BASE:-http://localhost:11434}/api/generate" \
        -H "Content-Type: application/json" \
        -d "$request_body" \
        2>/dev/null)

    if [[ -z "$response" ]]; then
        warn "No response from Ollama. Using default configuration"
        _write_default_config "$config_file"
        return
    fi

    # Extract JSON from Ollama's response field
    local llm_output
    llm_output=$(python3 -c "
import sys, json
try:
    raw = sys.stdin.buffer.read()
    data = json.loads(raw, strict=False)
    text = data.get('response', '')
    start = text.find('{')
    end = text.rfind('}') + 1
    if start >= 0 and end > start:
        config = json.loads(text[start:end], strict=False)
        print(json.dumps(config, ensure_ascii=False, indent=2))
    else:
        sys.exit(1)
except:
    sys.exit(1)
" 2>/dev/null <<< "$response")

    if [[ $? -eq 0 && -n "$llm_output" ]]; then
        echo "$llm_output" > "$config_file"
        local agent_name
        agent_name=$(python3 -c "import json; d=json.load(open('${config_file}')); print(d.get('agent_name','Mubo'))" 2>/dev/null)
        ok "Agent name: ${agent_name}"
        info "Color scheme generation complete"
    else
        warn "Failed to parse LLM output. Using default configuration"
        _write_default_config "$config_file"
    fi
}

_write_default_config() {
    cat > "$1" <<'DEFAULTCFG'
{
  "agent_name": "Mubo",
  "agent_name_en": "Mubo",
  "theme": "dark",
  "colors": {
    "bg": "#0a0a0f",
    "bg_secondary": "#0d1117",
    "header_bg": "linear-gradient(135deg, #1a0a2e, #0d1117)",
    "text": "#e0e0e0",
    "text_secondary": "#666666",
    "accent": "#ff6b35",
    "accent_secondary": "#f7c948",
    "user_msg_bg": "#1a3a5c",
    "user_msg_border": "#2a5a8c",
    "assistant_msg_bg": "#1a1a2e",
    "assistant_msg_border": "#2a2a4e",
    "input_bg": "#1a1a2e",
    "input_border": "#2a2a4e",
    "button_bg": "linear-gradient(135deg, #ff6b35, #e55a2b)",
    "system_msg_bg": "#2a1a0e",
    "system_msg_border": "#ff6b3530",
    "system_msg_text": "#f7c948"
  },
  "personality": "A passionate guardian who ignites the light of knowledge",
  "strings": {
    "plugins": "Plugins",
    "history": "History",
    "undo": "Undo",
    "reset": "Reset",
    "send": "Send",
    "close": "Close",
    "placeholder": "Type a message...",
    "history_title": "Git History (agent/app.py)",
    "plugins_title": "Plugins",
    "no_history": "No history available",
    "no_plugins": "No plugins yet. Ask in chat to create one.",
    "confirm_reset": "Reset to initial state?",
    "confirm_revert": "Revert to this version?",
    "confirm_delete_plugin": "Delete plugin?",
    "revert_btn": "Restore",
    "delete_btn": "Delete",
    "no_description": "No description",
    "code_rewritten": "Code rewritten. Server restarting.",
    "error_prefix": "Error",
    "error_ollama": "Cannot connect to Ollama server. Run: ollama serve",
    "restored_msg": "Restored. Server restarting.",
    "no_previous": "No previous state",
    "plugin_created": "Plugin created",
    "initial_not_found": "Initial commit not found"
  }
}
DEFAULTCFG
}

setup_agent() {
    step "Phase 6: Mubo Agent Deploy"

    local agent_dir
    agent_dir="$(cd "$(dirname "$0")" && pwd)/agent"

    if [[ ! -f "${agent_dir}/app.py" ]]; then
        err "agent/app.py not found"
        return
    fi

    # Generate agent name and color scheme using Ollama
    generate_agent_identity "$agent_dir"

    # Check if uv is available
    # PATH may not be updated immediately after installation
    if ! command -v uv &>/dev/null; then
        if [[ -f "$HOME/.local/bin/uv" ]]; then
            export PATH="$HOME/.local/bin:$PATH"
        elif [[ -f "$HOME/.cargo/bin/uv" ]]; then
            export PATH="$HOME/.cargo/bin:$PATH"
        fi
    fi

    if ! command -v uv &>/dev/null; then
        warn "uv not found. Skipping agent launch"
        return
    fi

    info "Installing dependencies..."
    cd "$agent_dir"
    uv sync 2>/dev/null || uv pip install -r <(python3 -c "
import tomllib, pathlib
d = tomllib.loads(pathlib.Path('pyproject.toml').read_text())
for dep in d['project']['dependencies']:
    print(dep)
") 2>/dev/null || {
        warn "Dependency installation failed"
        cd - > /dev/null
        return
    }
    cd - > /dev/null

    # Launch Agent in the background
    local port="${MUBO_PORT:-8392}"
    info "Starting Mubo Agent (port: ${port})..."

    MUBO_MODEL="${DERIVED_MODEL}" MUBO_PORT="${port}" \
        uv run --project "${agent_dir}" python "${agent_dir}/app.py" &
    local agent_pid=$!

    # Verify startup
    local aw=0
    while ! curl -s -o /dev/null http://localhost:${port}/ 2>/dev/null; do
        sleep 1
        aw=$((aw + 1))
        if [[ $aw -ge 15 ]]; then
            warn "Mubo Agent startup timed out"
            warn "Manual start: cd agent && uv run python app.py"
            return
        fi
        # Check if process is still alive
        if ! kill -0 $agent_pid 2>/dev/null; then
            warn "Mubo Agent failed to start"
            warn "Manual start: cd agent && MUBO_MODEL=${DERIVED_MODEL} uv run python app.py"
            return
        fi
    done

    ok "Mubo Agent started: http://localhost:${port}"
}

# ============================================================
# Final Report
# ============================================================
print_summary() {
    step "Setup Complete"

    echo ""
    printf "${BOLD}┌─────────────────────────────────────────┐${NC}\n"
    printf "${BOLD}│         Mubo Setup Complete              │${NC}\n"
    printf "${BOLD}└─────────────────────────────────────────┘${NC}\n"
    echo ""
    printf "  ${CYAN}Env:${NC}     %s / %s / RAM %dGB / GPU: %s\n" "$OS" "$ARCH" "$RAM_GB" "$GPU_TYPE"
    printf "  ${CYAN}Ollama:${NC}  http://localhost:11434\n"
    printf "  ${CYAN}Model:${NC}   %s (ctx %dK)\n" "$DERIVED_MODEL" "$(( CTX_LENGTH / 1024 ))"
    if [[ "$OS" == "linux" && "$GPU_TYPE" == "nvidia" ]]; then
        if [[ "$NVIDIA_DOCKER_OK" == true ]]; then
            printf "  ${CYAN}nvidia-docker:${NC} ${GREEN}OK${NC}\n"
        else
            printf "  ${CYAN}nvidia-docker:${NC} ${RED}Not working / Not detected${NC}\n"
        fi
    fi
    local port="${MUBO_PORT:-8392}"
    if curl -s -o /dev/null http://localhost:${port}/ 2>/dev/null; then
        printf "  ${CYAN}Agent:${NC}   ${GREEN}http://localhost:${port}${NC}\n"
    fi
    echo ""
    printf "  ${GREEN}Usage:${NC}\n"
    printf "    Open http://localhost:${port} in your browser (Mubo Agent)\n"
    printf "    ollama run %s (CLI)\n" "$DERIVED_MODEL"
    echo ""
    printf "  ${GREEN}Manual Agent Start:${NC}\n"
    printf "    cd agent && MUBO_MODEL=%s uv run python app.py\n" "$DERIVED_MODEL"
    echo ""
    printf "  ${GREEN}API Usage:${NC}\n"
    printf "    curl http://localhost:11434/api/chat -d '{\n"
    printf "      \"model\": \"%s\",\n" "$DERIVED_MODEL"
    printf "      \"messages\": [{\"role\": \"user\", \"content\": \"Hello\"}]\n"
    printf "    }'\n"
    echo ""
}

# ============================================================
# Main Execution
# ============================================================
install_git() {
    if command -v git &>/dev/null; then
        return
    fi
    info "git not found. Installing..."
    case "$(uname -s)" in
        Darwin)
            if command -v brew &>/dev/null; then
                brew install git
            else
                info "Installing git via Xcode Command Line Tools..."
                xcode-select --install 2>/dev/null || true
                # xcode-select is interactive, wait for completion
                until command -v git &>/dev/null; do
                    sleep 3
                done
            fi
            ;;
        Linux)
            if command -v apt-get &>/dev/null; then
                sudo apt-get update -y && sudo apt-get install -y git
            elif command -v dnf &>/dev/null; then
                sudo dnf install -y git
            elif command -v yum &>/dev/null; then
                sudo yum install -y git
            elif command -v pacman &>/dev/null; then
                sudo pacman -S --noconfirm git
            elif command -v apk &>/dev/null; then
                sudo apk add git
            else
                err "Cannot auto-install git. Please install it manually"
                exit 1
            fi
            ;;
    esac
    if command -v git &>/dev/null; then
        ok "git installation complete"
    else
        err "git installation failed"
        exit 1
    fi
}

check_for_updates() {
    # Skip if not inside a git repository
    if ! git rev-parse --is-inside-work-tree &>/dev/null 2>&1; then
        return
    fi

    local repo_dir
    repo_dir="$(git rev-parse --show-toplevel 2>/dev/null)"
    if [[ -z "$repo_dir" ]]; then
        return
    fi

    info "Checking for updates..."

    # Fetch latest from remote (ignore connection errors)
    if ! git -C "$repo_dir" fetch origin --quiet 2>/dev/null; then
        warn "Failed to connect to remote. Continuing offline"
        return
    fi

    # Always compare local main with remote main (not HEAD, which may be a machine branch)
    local local_main remote_main
    local_main=$(git -C "$repo_dir" rev-parse refs/heads/main 2>/dev/null || \
                 git -C "$repo_dir" rev-parse refs/heads/master 2>/dev/null || echo "")
    remote_main=$(git -C "$repo_dir" rev-parse origin/main 2>/dev/null || \
                  git -C "$repo_dir" rev-parse origin/master 2>/dev/null || echo "")

    if [[ -z "$remote_main" ]]; then
        warn "Remote branch not found. Skipping"
        return
    fi

    if [[ "$local_main" == "$remote_main" ]]; then
        ok "Already up to date"
        return
    fi

    # Show number of commits behind
    local behind
    behind=$(git -C "$repo_dir" rev-list --count refs/heads/main..origin/main 2>/dev/null || echo "?")
    info "A newer version is available (${behind} commits behind)"
    info "Updating..."

    # Remember current branch to restore later
    local current_branch
    current_branch=$(git -C "$repo_dir" branch --show-current 2>/dev/null)

    # Protect user's local changes
    local has_changes=false
    if ! git -C "$repo_dir" diff --quiet 2>/dev/null || \
       ! git -C "$repo_dir" diff --cached --quiet 2>/dev/null; then
        has_changes=true
        info "Stashing local changes..."
        git -C "$repo_dir" stash push -m "mubo-auto-update-$(date +%Y%m%d_%H%M%S)" --quiet 2>/dev/null || true
    fi

    # Switch to main, update, then switch back
    if [[ "$current_branch" != "main" && "$current_branch" != "master" ]]; then
        git -C "$repo_dir" checkout main --quiet 2>/dev/null || \
            git -C "$repo_dir" checkout master --quiet 2>/dev/null || true
    fi

    # Try ff-only first, fall back to reset
    if git -C "$repo_dir" pull --ff-only origin main --quiet 2>/dev/null; then
        ok "Update complete"
    else
        info "Fast-forward not possible. Resetting main to latest..."
        git -C "$repo_dir" reset --hard origin/main --quiet 2>/dev/null || true
        ok "Update complete (reset to latest)"
    fi

    # Switch back to machine branch if we were on one
    if [[ -n "$current_branch" && "$current_branch" != "main" && "$current_branch" != "master" ]]; then
        git -C "$repo_dir" checkout "$current_branch" --quiet 2>/dev/null || true
        # Merge updated main into machine branch
        info "Merging updates into ${current_branch}..."
        if git -C "$repo_dir" merge main --no-edit --quiet 2>/dev/null; then
            ok "Machine branch updated"
        else
            warn "Merge conflict. Resetting machine branch to main..."
            git -C "$repo_dir" reset --hard main --quiet 2>/dev/null || true
            ok "Machine branch reset to latest main"
        fi
    fi

    # Restore stashed changes
    if [[ "$has_changes" == true ]]; then
        if git -C "$repo_dir" stash pop --quiet 2>/dev/null; then
            ok "Local changes restored"
        else
            warn "Conflict occurred while restoring local changes"
            warn "Please resolve manually: git stash pop"
        fi
    fi

    # setup.sh itself may have been updated, so re-execute
    info "Restarting with updated script..."
    exec bash "$repo_dir/setup.sh" "$@"
}

ensure_repo() {
    # If run via curl | bash, clone the repository to current directory and re-execute
    if [[ ! -f "$(dirname "$0")/agent/app.py" ]] && [[ ! -f "./agent/app.py" ]]; then
        install_git
        info "Cloning mubo into current directory..."
        if [[ -d "./mubo" ]]; then
            info "mubo/ already exists, using it"
        else
            git clone https://github.com/shi3z/mubo.git ./mubo
        fi
        cd ./mubo
        exec bash ./setup.sh "$@"
    fi
}

setup_machine_branch() {
    # Skip if not inside a git repository
    if ! git rev-parse --is-inside-work-tree &>/dev/null 2>&1; then
        return
    fi

    local repo_dir
    repo_dir="$(git rev-parse --show-toplevel 2>/dev/null)"
    if [[ -z "$repo_dir" ]]; then
        return
    fi

    # Generate machine-specific branch name
    local hostname
    hostname="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo 'unknown')"
    # Remove characters not allowed in branch names
    hostname="$(echo "$hostname" | tr -c 'a-zA-Z0-9_-' '-')"
    local branch_name="machine/${hostname}"

    local current_branch
    current_branch="$(git -C "$repo_dir" branch --show-current 2>/dev/null)"

    # Already on the machine branch
    if [[ "$current_branch" == "$branch_name" ]]; then
        ok "Machine branch: ${branch_name}"
        return
    fi

    # Check if machine branch exists
    if git -C "$repo_dir" show-ref --verify --quiet "refs/heads/${branch_name}" 2>/dev/null; then
        info "Switching to existing machine branch: ${branch_name}"
        git -C "$repo_dir" checkout "$branch_name" --quiet 2>/dev/null
        # Merge updates from main
        git -C "$repo_dir" merge main --no-edit --quiet 2>/dev/null || true
    else
        info "Creating machine branch: ${branch_name}"
        git -C "$repo_dir" checkout -b "$branch_name" --quiet 2>/dev/null

        # Set temporary git user config if not configured (required for commits)
        if ! git -C "$repo_dir" config user.name &>/dev/null; then
            git -C "$repo_dir" config user.name "mubo-agent"
        fi
        if ! git -C "$repo_dir" config user.email &>/dev/null; then
            git -C "$repo_dir" config user.email "mubo@localhost"
        fi

        # Initial commit (tag as baseline)
        git -C "$repo_dir" add -A 2>/dev/null || true
        git -C "$repo_dir" commit --allow-empty -m "mubo: initial state for ${hostname}" --quiet 2>/dev/null || true
        git -C "$repo_dir" tag -f "mubo-initial-${hostname}" --quiet 2>/dev/null || true
    fi

    ok "Machine branch: ${branch_name}"
}

main() {
    echo ""
    printf "${BOLD}${CYAN}"
    echo "  __  __       _            "
    echo " |  \\/  |_   _| |__   ___  "
    echo " | |\\/| | | | | '_ \\ / _ \\ "
    echo " | |  | | |_| | |_) | (_) |"
    echo " |_|  |_|\\__,_|_.__/ \\___/ "
    printf "${NC}\n"
    echo "  Mubo — Local LLM Bootstrapper"
    echo ""

    ensure_repo "$@"
    check_for_updates "$@"
    setup_machine_branch
    detect_environment
    setup_ollama
    pull_model
    create_extended_model
    setup_extras
    setup_agent
    print_summary
}

main "$@"
