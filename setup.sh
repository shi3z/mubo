#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Mubo (無貌) — One-shot Local LLM Bootstrapper
# spec.md に基づく自動環境セットアップスクリプト
# ============================================================

# ---------- 色付き出力ヘルパー ----------
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

# ---------- グローバル変数 ----------
OS=""
ARCH=""
RAM_GB=0
GPU_TYPE="none"       # none | apple | nvidia | amd
GPU_VRAM_GB=0
UNIFIED_MEM=false
HAS_NPU=false
NVIDIA_DOCKER_OK=false
OLLAMA_INSTALLED=false
BASE_MODEL="gpt-oss:20b"
DERIVED_MODEL="gpt-oss:20b-long"
CTX_LENGTH=65536      # デフォルト 64K

# ============================================================
# Phase 1: 環境調査
# ============================================================
detect_environment() {
    step "Phase 1: 環境調査"

    # --- OS ---
    case "$(uname -s)" in
        Darwin) OS="macos" ;;
        Linux)  OS="linux" ;;
        *)
            err "未対応の OS: $(uname -s)"
            err "macOS または Linux が必要です"
            exit 1
            ;;
    esac
    info "OS: ${OS}"

    # --- アーキテクチャ ---
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

    # --- サマリ ---
    ok "環境調査完了: OS=${OS}, Arch=${ARCH}, RAM=${RAM_GB}GB, GPU=${GPU_TYPE}"
    if [[ "$GPU_TYPE" == "nvidia" ]]; then
        info "NVIDIA VRAM: ${GPU_VRAM_GB} GB"
    fi
    if [[ "$UNIFIED_MEM" == true ]]; then
        info "Apple Silicon ユニファイドメモリ検出"
    fi
    if [[ "$HAS_NPU" == true ]]; then
        info "NPU (Neural Engine) 検出"
    fi
}

detect_gpu() {
    if [[ "$OS" == "macos" ]]; then
        # Apple Silicon チェック
        if [[ "$ARCH" == "arm64" ]]; then
            GPU_TYPE="apple"
            UNIFIED_MEM=true
            HAS_NPU=true
            GPU_VRAM_GB=$RAM_GB  # ユニファイドメモリなので RAM=VRAM
        else
            # Intel Mac — 外部GPU検出は best-effort
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
# Phase 2: Ollama 導入 & 疎通確認
# ============================================================
setup_ollama() {
    step "Phase 2: Ollama 導入 & 疎通確認"

    # --- インストール確認 ---
    if command -v ollama &>/dev/null; then
        OLLAMA_INSTALLED=true
        ok "Ollama は既にインストール済み: $(ollama --version 2>/dev/null || echo 'version unknown')"
    else
        info "Ollama が見つかりません。インストールします..."
        install_ollama
    fi

    # --- サーバー起動確認 ---
    ensure_ollama_running
}

install_prerequisites() {
    if [[ "$OS" == "linux" ]]; then
        # zstd は Ollama インストーラーが必要とする
        if ! command -v zstd &>/dev/null; then
            info "zstd をインストール中 (Ollama インストールに必要)..."
            if command -v apt-get &>/dev/null; then
                sudo apt-get install -y zstd
            elif command -v dnf &>/dev/null; then
                sudo dnf install -y zstd
            elif command -v yum &>/dev/null; then
                sudo yum install -y zstd
            elif command -v pacman &>/dev/null; then
                sudo pacman -S --noconfirm zstd
            else
                warn "zstd を自動インストールできません。手動でインストールしてください"
            fi
        fi
    fi
}

install_docker() {
    if [[ "$OS" == "macos" ]]; then
        if command -v brew &>/dev/null; then
            info "Homebrew 経由で Docker をインストール中..."
            brew install --cask docker
            ok "Docker Desktop インストール完了"
            info "Docker Desktop を起動中..."
            open -a Docker 2>/dev/null || true
            # 起動を待つ
            local dw=0
            while ! docker info &>/dev/null 2>&1; do
                sleep 2
                dw=$((dw + 2))
                if [[ $dw -ge 60 ]]; then
                    warn "Docker Desktop の起動に時間がかかっています"
                    break
                fi
            done
        else
            err "Docker Desktop のインストールには Homebrew が必要です"
            info "  Homebrew: https://brew.sh"
            info "  または手動: https://docs.docker.com/desktop/install/mac-install/"
            return 1
        fi
    elif [[ "$OS" == "linux" ]]; then
        info "Docker Engine をインストール中..."
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
            # フォールバック: 公式convenience script
            info "公式インストールスクリプトで Docker をインストール中..."
            curl -fsSL https://get.docker.com | sh
        fi
        # デーモン起動 & 現ユーザーをdockerグループに追加
        sudo systemctl enable docker 2>/dev/null || true
        sudo systemctl start docker 2>/dev/null || true
        sudo usermod -aG docker "$USER" 2>/dev/null || true
        ok "Docker Engine インストール完了"
        info "dockerグループの反映には再ログインが必要な場合があります"
    fi
}

install_ollama() {
    install_prerequisites

    if [[ "$OS" == "macos" ]]; then
        if command -v brew &>/dev/null; then
            info "Homebrew 経由で Ollama をインストール中..."
            brew install ollama
        else
            info "公式インストールスクリプトで Ollama をインストール中..."
            curl -fsSL https://ollama.com/install.sh | sh
        fi
    elif [[ "$OS" == "linux" ]]; then
        info "公式インストールスクリプトで Ollama をインストール中..."
        curl -fsSL https://ollama.com/install.sh | sh
    fi

    if command -v ollama &>/dev/null; then
        OLLAMA_INSTALLED=true
        ok "Ollama インストール完了"
    else
        err "Ollama のインストールに失敗しました"
        err "手動インストール: https://ollama.com/download"
        exit 1
    fi
}

ensure_ollama_running() {
    local max_wait=30
    local waited=0

    # 既に動作中か確認
    if curl -s -o /dev/null -w "%{http_code}" http://localhost:11434/api/tags 2>/dev/null | grep -q "200"; then
        ok "Ollama サーバーは稼働中"
        return
    fi

    info "Ollama サーバーを起動します..."
    if [[ "$OS" == "macos" ]]; then
        # macOS: アプリとして起動 or バックグラウンド
        if [[ -d "/Applications/Ollama.app" ]]; then
            open -a Ollama
        else
            ollama serve &>/dev/null &
        fi
    else
        # Linux: systemd or バックグラウンド
        if systemctl is-enabled ollama &>/dev/null 2>&1; then
            sudo systemctl start ollama 2>/dev/null || ollama serve &>/dev/null &
        else
            ollama serve &>/dev/null &
        fi
    fi

    # 疎通待ち
    info "Ollama サーバーの起動を待機中..."
    while ! curl -s -o /dev/null http://localhost:11434/api/tags 2>/dev/null; do
        sleep 1
        waited=$((waited + 1))
        if [[ $waited -ge $max_wait ]]; then
            err "Ollama サーバーの起動がタイムアウトしました (${max_wait}秒)"
            err "'ollama serve' を別ターミナルで手動実行してください"
            exit 1
        fi
    done
    ok "Ollama サーバー疎通確認完了"
}

# ============================================================
# Phase 3: モデル取得
# ============================================================
pull_model() {
    step "Phase 3: モデル取得 (${BASE_MODEL})"

    # 既にダウンロード済みか確認
    if ollama list 2>/dev/null | grep -q "${BASE_MODEL}"; then
        ok "${BASE_MODEL} は既にダウンロード済み"
        return
    fi

    info "${BASE_MODEL} をダウンロード中... (サイズが大きいため時間がかかります)"
    if ollama pull "${BASE_MODEL}"; then
        ok "${BASE_MODEL} のダウンロード完了"
    else
        err "${BASE_MODEL} のダウンロードに失敗しました"
        err "ネットワーク接続を確認してください"
        exit 1
    fi
}

# ============================================================
# Phase 4: コンテキスト長拡張の派生モデル生成
# ============================================================
create_extended_model() {
    step "Phase 4: コンテキスト長拡張モデル生成"

    # RAM に応じてコンテキスト長を決定
    decide_context_length

    # 既に存在する場合はスキップ
    if ollama list 2>/dev/null | grep -q "${DERIVED_MODEL}"; then
        warn "${DERIVED_MODEL} は既に存在します。再作成します..."
    fi

    local modelfile
    modelfile=$(mktemp /tmp/mubo-modelfile.XXXXXX)

    cat > "$modelfile" <<EOF
FROM ${BASE_MODEL}
PARAMETER num_ctx ${CTX_LENGTH}
PARAMETER num_gpu 999
EOF

    info "Modelfile 生成: ctx=${CTX_LENGTH} ($(( CTX_LENGTH / 1024 ))K)"
    info "派生モデル ${DERIVED_MODEL} を作成中..."

    if ollama create "${DERIVED_MODEL}" -f "$modelfile"; then
        ok "${DERIVED_MODEL} 作成完了 (ctx=$(( CTX_LENGTH / 1024 ))K)"
    else
        err "派生モデルの作成に失敗しました"
        warn "ベースモデル ${BASE_MODEL} はそのまま利用可能です"
    fi

    rm -f "$modelfile"
}

decide_context_length() {
    # Apple Silicon ユニファイドメモリ or 大容量 VRAM → 128K を狙う
    local available_mem=$RAM_GB
    if [[ "$GPU_TYPE" == "nvidia" ]] && (( GPU_VRAM_GB > 0 )); then
        available_mem=$GPU_VRAM_GB
    fi

    if (( available_mem >= 64 )); then
        CTX_LENGTH=131072   # 128K
        info "メモリ十分 (${available_mem}GB): 128K コンテキストを設定"
    elif (( available_mem >= 32 )); then
        CTX_LENGTH=65536    # 64K
        info "メモリ中程度 (${available_mem}GB): 64K コンテキストを設定"
    elif (( available_mem >= 16 )); then
        CTX_LENGTH=32768    # 32K
        warn "メモリが限定的 (${available_mem}GB): 32K コンテキストに制限"
    else
        CTX_LENGTH=16384    # 16K
        warn "メモリが少ない (${available_mem}GB): 16K コンテキストに制限"
    fi
}

# ============================================================
# Phase 5: 追加環境 (任意・失敗しても本線に影響なし)
# ============================================================
setup_extras() {
    step "Phase 5: 追加環境セットアップ (任意)"

    info "追加環境は失敗しても Ollama + ${DERIVED_MODEL} は利用可能です"

    # --- uv (Python パッケージマネージャ) ---
    setup_uv

    # --- vLLM (Linux + NVIDIA GPU のみ) ---
    if [[ "$OS" == "linux" && "$GPU_TYPE" == "nvidia" ]]; then
        setup_vllm
    else
        info "vLLM: スキップ (Linux + NVIDIA GPU 環境のみ対象)"
    fi

    # --- MLX (macOS Apple Silicon のみ) ---
    if [[ "$OS" == "macos" && "$ARCH" == "arm64" ]]; then
        setup_mlx
    else
        info "MLX: スキップ (macOS Apple Silicon 環境のみ対象)"
    fi

    # --- Docker ---
    setup_docker_check
}

setup_uv() {
    if command -v uv &>/dev/null; then
        ok "uv は既にインストール済み"
        return
    fi

    info "uv をインストール中..."
    if curl -LsSf https://astral.sh/uv/install.sh | sh 2>/dev/null; then
        ok "uv インストール完了"
    else
        warn "uv のインストールに失敗しました (本線に影響なし)"
    fi
}

setup_vllm() {
    info "vLLM のセットアップを試行中..."

    if ! command -v python3 &>/dev/null; then
        warn "Python3 が見つかりません。vLLM スキップ"
        return
    fi

    if python3 -c "import vllm" 2>/dev/null; then
        ok "vLLM は既にインストール済み"
        return
    fi

    if pip3 install vllm 2>/dev/null || pip install vllm 2>/dev/null; then
        ok "vLLM インストール完了"
    else
        warn "vLLM のインストールに失敗しました (本線に影響なし)"
    fi
}

setup_mlx() {
    info "MLX のセットアップを試行中..."

    if ! command -v python3 &>/dev/null; then
        warn "Python3 が見つかりません。MLX スキップ"
        return
    fi

    if python3 -c "import mlx" 2>/dev/null; then
        ok "MLX は既にインストール済み"
        return
    fi

    if pip3 install mlx mlx-lm 2>/dev/null || pip install mlx mlx-lm 2>/dev/null; then
        ok "MLX インストール完了"
    else
        warn "MLX のインストールに失敗しました (本線に影響なし)"
    fi
}

setup_docker_check() {
    if ! command -v docker &>/dev/null; then
        info "Docker が見つかりません。インストールします..."
        install_docker
    fi

    if ! command -v docker &>/dev/null; then
        warn "Docker のインストールに失敗しました (本線に影響なし)"
        return
    fi

    # Docker デーモン起動確認
    if ! docker info &>/dev/null 2>&1; then
        info "Docker デーモンを起動します..."
        if [[ "$OS" == "macos" ]]; then
            open -a Docker 2>/dev/null || true
            # Docker Desktop の起動を最大60秒待つ
            local dw=0
            while ! docker info &>/dev/null 2>&1; do
                sleep 2
                dw=$((dw + 2))
                if [[ $dw -ge 60 ]]; then
                    warn "Docker デーモンの起動がタイムアウトしました"
                    return
                fi
            done
        elif [[ "$OS" == "linux" ]]; then
            sudo systemctl start docker 2>/dev/null || sudo service docker start 2>/dev/null || true
            sleep 2
        fi
    fi

    if docker info &>/dev/null 2>&1; then
        ok "Docker デーモンは稼働中"
    else
        warn "Docker はインストール済みですがデーモンが停止中です"
        return
    fi

    # --- NVIDIA Container Runtime / nvidia-docker テスト ---
    if [[ "$OS" != "linux" || "$GPU_TYPE" != "nvidia" ]]; then
        info "nvidia-docker テスト: スキップ (Linux + NVIDIA GPU 環境のみ対象)"
        return
    fi

    test_nvidia_docker
}

test_nvidia_docker() {
    info "nvidia-docker の動作テストを実行中..."

    # 1. nvidia-container-toolkit / nvidia-docker2 がインストールされているか
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
        warn "nvidia-container-toolkit が見つかりません"
        warn "インストール手順: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html"
        return
    fi
    ok "nvidia-container-toolkit 検出"

    # 2. --gpus フラグで nvidia-smi が実行できるか (実動作テスト)
    info "コンテナ内で nvidia-smi を実行してGPUアクセスを検証中..."
    local test_output
    if test_output=$(docker run --rm --gpus all nvidia/cuda:12.6.3-base-ubuntu24.04 nvidia-smi 2>&1); then
        ok "nvidia-docker 動作確認: コンテナからGPUにアクセスできます"
        NVIDIA_DOCKER_OK=true

        # GPU 名とドライババージョンを表示
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
        warn "nvidia-docker 動作テスト失敗: コンテナからGPUにアクセスできません"
        warn "エラー詳細:"
        echo "$test_output" | tail -5 | while IFS= read -r line; do
            warn "  $line"
        done
        warn ""
        warn "トラブルシューティング:"
        warn "  1. nvidia-container-toolkit を再インストール:"
        warn "     sudo apt-get install -y nvidia-container-toolkit"
        warn "     sudo nvidia-ctk runtime configure --runtime=docker"
        warn "     sudo systemctl restart docker"
        warn "  2. Docker デーモンを再起動: sudo systemctl restart docker"
        warn "  3. NVIDIA ドライバが正常か確認: nvidia-smi"
    fi
}

# ============================================================
# Phase 6: Mubo Agent (自己書き換えAI) デプロイ & 起動
# ============================================================
generate_agent_identity() {
    local agent_dir="$1"
    local config_file="${agent_dir}/config.json"

    # 既に config.json があればスキップ
    if [[ -f "$config_file" ]]; then
        ok "エージェント設定は既に存在します"
        return
    fi

    info "Ollama にエージェントの名前とカラースキームを考えさせています..."

    # マシン情報を収集
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
    # macOS: ダークモード判定
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

以下のJSON形式のみで回答してください。他のテキストは一切含めないでください:
{
  \"agent_name\": \"マシンの雰囲気に合った短い名前(2-4文字の日本語 or 英語)\",
  \"agent_name_en\": \"英語での名前\",
  \"theme\": \"dark または light (OSの外観モードやマシンの雰囲気から判断)\",
  \"colors\": {
    \"bg\": \"背景色(hex)\",
    \"bg_secondary\": \"セカンダリ背景色(hex)\",
    \"header_bg\": \"ヘッダー背景(CSS gradient可)\",
    \"text\": \"テキスト色(hex)\",
    \"text_secondary\": \"セカンダリテキスト色(hex)\",
    \"accent\": \"アクセント色(hex)\",
    \"accent_secondary\": \"アクセントのセカンダリ色(hex)\",
    \"user_msg_bg\": \"ユーザーメッセージ背景(hex)\",
    \"user_msg_border\": \"ユーザーメッセージ枠線(hex)\",
    \"assistant_msg_bg\": \"アシスタントメッセージ背景(hex)\",
    \"assistant_msg_border\": \"アシスタントメッセージ枠線(hex)\",
    \"input_bg\": \"入力欄背景(hex)\",
    \"input_border\": \"入力欄枠線(hex)\",
    \"button_bg\": \"送信ボタン背景(CSS gradient可)\",
    \"system_msg_bg\": \"システムメッセージ背景(hex)\",
    \"system_msg_border\": \"システムメッセージ枠線(hex)\",
    \"system_msg_text\": \"システムメッセージ文字色(hex)\"
  },
  \"personality\": \"このエージェントの性格を一文で(日本語)\"
}"

    local response
    response=$(curl -s --max-time 60 "${OLLAMA_BASE:-http://localhost:11434}/api/generate" \
        -d "$(printf '{"model":"%s","prompt":"%s","stream":false,"options":{"temperature":0.8}}' \
            "${BASE_MODEL}" \
            "$(echo "$prompt" | sed 's/"/\\"/g' | tr '\n' ' ')")" \
        2>/dev/null)

    if [[ -z "$response" ]]; then
        warn "Ollama からの応答がありません。デフォルト設定を使用します"
        _write_default_config "$config_file"
        return
    fi

    # Ollama の response フィールドから JSON を抽出
    local llm_output
    llm_output=$(echo "$response" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    text = data.get('response', '')
    # JSON部分を抽出
    start = text.find('{')
    end = text.rfind('}') + 1
    if start >= 0 and end > start:
        # パース確認
        config = json.loads(text[start:end])
        print(json.dumps(config, ensure_ascii=False, indent=2))
    else:
        sys.exit(1)
except:
    sys.exit(1)
" 2>/dev/null)

    if [[ $? -eq 0 && -n "$llm_output" ]]; then
        echo "$llm_output" > "$config_file"
        local agent_name
        agent_name=$(python3 -c "import json; d=json.load(open('${config_file}')); print(d.get('agent_name','Mubo'))" 2>/dev/null)
        ok "エージェント名: ${agent_name}"
        info "カラースキーム生成完了"
    else
        warn "LLM出力のパースに失敗しました。デフォルト設定を使用します"
        _write_default_config "$config_file"
    fi
}

_write_default_config() {
    cat > "$1" <<'DEFAULTCFG'
{
  "agent_name": "無貌",
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
  "personality": "炎のように情熱的で、知識の光を灯す守護者"
}
DEFAULTCFG
}

setup_agent() {
    step "Phase 6: Mubo Agent デプロイ"

    local agent_dir
    agent_dir="$(cd "$(dirname "$0")" && pwd)/agent"

    if [[ ! -f "${agent_dir}/app.py" ]]; then
        err "agent/app.py が見つかりません"
        return
    fi

    # エージェントの名前とカラースキームを Ollama で生成
    generate_agent_identity "$agent_dir"

    # uv が使えるか確認
    # インストール直後はPATHに反映されていない場合がある
    if ! command -v uv &>/dev/null; then
        if [[ -f "$HOME/.local/bin/uv" ]]; then
            export PATH="$HOME/.local/bin:$PATH"
        elif [[ -f "$HOME/.cargo/bin/uv" ]]; then
            export PATH="$HOME/.cargo/bin:$PATH"
        fi
    fi

    if ! command -v uv &>/dev/null; then
        warn "uv が見つかりません。エージェントの起動をスキップします"
        return
    fi

    info "依存パッケージをインストール中..."
    cd "$agent_dir"
    uv sync 2>/dev/null || uv pip install -r <(python3 -c "
import tomllib, pathlib
d = tomllib.loads(pathlib.Path('pyproject.toml').read_text())
for dep in d['project']['dependencies']:
    print(dep)
") 2>/dev/null || {
        warn "依存パッケージのインストールに失敗しました"
        cd - > /dev/null
        return
    }
    cd - > /dev/null

    # 初期バックアップ用ディレクトリ作成
    mkdir -p "${agent_dir}/history"

    # Agent をバックグラウンドで起動
    local port="${MUBO_PORT:-8392}"
    info "Mubo Agent を起動中 (port: ${port})..."

    MUBO_MODEL="${DERIVED_MODEL}" MUBO_PORT="${port}" \
        uv run --project "${agent_dir}" python "${agent_dir}/app.py" &
    local agent_pid=$!

    # 起動確認
    local aw=0
    while ! curl -s -o /dev/null http://localhost:${port}/ 2>/dev/null; do
        sleep 1
        aw=$((aw + 1))
        if [[ $aw -ge 15 ]]; then
            warn "Mubo Agent の起動がタイムアウトしました"
            warn "手動起動: cd agent && uv run python app.py"
            return
        fi
        # プロセスが死んでないか確認
        if ! kill -0 $agent_pid 2>/dev/null; then
            warn "Mubo Agent の起動に失敗しました"
            warn "手動起動: cd agent && MUBO_MODEL=${DERIVED_MODEL} uv run python app.py"
            return
        fi
    done

    ok "Mubo Agent 起動完了: http://localhost:${port}"
}

# ============================================================
# 最終レポート
# ============================================================
print_summary() {
    step "セットアップ完了"

    echo ""
    printf "${BOLD}┌─────────────────────────────────────────┐${NC}\n"
    printf "${BOLD}│         Mubo セットアップ完了            │${NC}\n"
    printf "${BOLD}└─────────────────────────────────────────┘${NC}\n"
    echo ""
    printf "  ${CYAN}環境:${NC}    %s / %s / RAM %dGB / GPU: %s\n" "$OS" "$ARCH" "$RAM_GB" "$GPU_TYPE"
    printf "  ${CYAN}Ollama:${NC}  http://localhost:11434\n"
    printf "  ${CYAN}モデル:${NC}  %s (ctx %dK)\n" "$DERIVED_MODEL" "$(( CTX_LENGTH / 1024 ))"
    if [[ "$OS" == "linux" && "$GPU_TYPE" == "nvidia" ]]; then
        if [[ "$NVIDIA_DOCKER_OK" == true ]]; then
            printf "  ${CYAN}nvidia-docker:${NC} ${GREEN}OK${NC}\n"
        else
            printf "  ${CYAN}nvidia-docker:${NC} ${RED}未動作 / 未検出${NC}\n"
        fi
    fi
    local port="${MUBO_PORT:-8392}"
    if curl -s -o /dev/null http://localhost:${port}/ 2>/dev/null; then
        printf "  ${CYAN}Agent:${NC}   ${GREEN}http://localhost:${port}${NC}\n"
    fi
    echo ""
    printf "  ${GREEN}使い方:${NC}\n"
    printf "    ブラウザで http://localhost:${port} を開く (Mubo Agent)\n"
    printf "    ollama run %s (CLI)\n" "$DERIVED_MODEL"
    echo ""
    printf "  ${GREEN}Agent 手動起動:${NC}\n"
    printf "    cd agent && MUBO_MODEL=%s uv run python app.py\n" "$DERIVED_MODEL"
    echo ""
    printf "  ${GREEN}API 利用:${NC}\n"
    printf "    curl http://localhost:11434/api/chat -d '{\n"
    printf "      \"model\": \"%s\",\n" "$DERIVED_MODEL"
    printf "      \"messages\": [{\"role\": \"user\", \"content\": \"Hello\"}]\n"
    printf "    }'\n"
    echo ""
}

# ============================================================
# メイン実行
# ============================================================
install_git() {
    if command -v git &>/dev/null; then
        return
    fi
    info "git が見つかりません。インストールします..."
    case "$(uname -s)" in
        Darwin)
            if command -v brew &>/dev/null; then
                brew install git
            else
                info "Xcode Command Line Tools 経由で git をインストール中..."
                xcode-select --install 2>/dev/null || true
                # xcode-select は対話的なので、完了を待つ
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
                err "git を自動インストールできません。手動でインストールしてください"
                exit 1
            fi
            ;;
    esac
    if command -v git &>/dev/null; then
        ok "git インストール完了"
    else
        err "git のインストールに失敗しました"
        exit 1
    fi
}

check_for_updates() {
    # gitリポジトリ内でなければスキップ
    if ! git rev-parse --is-inside-work-tree &>/dev/null 2>&1; then
        return
    fi

    local repo_dir
    repo_dir="$(git rev-parse --show-toplevel 2>/dev/null)"
    if [[ -z "$repo_dir" ]]; then
        return
    fi

    info "アップデートを確認中..."

    # リモートの最新情報を取得（通信エラーは無視）
    if ! git -C "$repo_dir" fetch origin --quiet 2>/dev/null; then
        warn "リモートへの接続に失敗しました。オフラインで続行します"
        return
    fi

    local local_head remote_head
    local_head=$(git -C "$repo_dir" rev-parse HEAD 2>/dev/null)
    remote_head=$(git -C "$repo_dir" rev-parse origin/main 2>/dev/null || \
                  git -C "$repo_dir" rev-parse origin/master 2>/dev/null || echo "")

    if [[ -z "$remote_head" ]]; then
        warn "リモートブランチが見つかりません。スキップします"
        return
    fi

    if [[ "$local_head" == "$remote_head" ]]; then
        ok "最新バージョンです"
        return
    fi

    # 差分のコミット数を表示
    local behind
    behind=$(git -C "$repo_dir" rev-list --count HEAD..origin/main 2>/dev/null || echo "?")
    info "新しいバージョンがあります (${behind} commits behind)"
    info "アップデート中..."

    # ユーザーのローカル変更を保護
    local has_changes=false
    if ! git -C "$repo_dir" diff --quiet 2>/dev/null || \
       ! git -C "$repo_dir" diff --cached --quiet 2>/dev/null; then
        has_changes=true
        info "ローカル変更を一時退避します..."
        git -C "$repo_dir" stash push -m "mubo-auto-update-$(date +%Y%m%d_%H%M%S)" --quiet 2>/dev/null || true
    fi

    # プル
    if git -C "$repo_dir" pull --ff-only origin main --quiet 2>/dev/null; then
        ok "アップデート完了"
        # 退避した変更を復元
        if [[ "$has_changes" == true ]]; then
            if git -C "$repo_dir" stash pop --quiet 2>/dev/null; then
                ok "ローカル変更を復元しました"
            else
                warn "ローカル変更の復元でコンフリクトが発生しました"
                warn "手動で解決してください: git stash pop"
            fi
        fi
        # setup.sh 自体が更新された可能性があるので再実行
        info "更新されたスクリプトで再起動します..."
        exec bash "$repo_dir/setup.sh" "$@"
    else
        warn "fast-forward マージができませんでした"
        warn "手動で更新してください: git pull"
        # stash を戻す
        if [[ "$has_changes" == true ]]; then
            git -C "$repo_dir" stash pop --quiet 2>/dev/null || true
        fi
    fi
}

ensure_repo() {
    # curl | bash で実行された場合、リポジトリをcloneして再実行
    if [[ ! -f "$(dirname "$0")/agent/app.py" ]] && [[ ! -f "./agent/app.py" ]]; then
        install_git
        info "リポジトリが見つかりません。cloneします..."
        local tmpdir
        tmpdir=$(mktemp -d)
        git clone https://github.com/shi3z/mubo.git "$tmpdir/mubo"
        cd "$tmpdir/mubo"
        exec bash ./setup.sh "$@"
    fi
}

setup_machine_branch() {
    # gitリポジトリ内でなければスキップ
    if ! git rev-parse --is-inside-work-tree &>/dev/null 2>&1; then
        return
    fi

    local repo_dir
    repo_dir="$(git rev-parse --show-toplevel 2>/dev/null)"
    if [[ -z "$repo_dir" ]]; then
        return
    fi

    # マシン固有のブランチ名を生成
    local hostname
    hostname="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo 'unknown')"
    # ブランチ名に使えない文字を除去
    hostname="$(echo "$hostname" | tr -c 'a-zA-Z0-9_-' '-')"
    local branch_name="machine/${hostname}"

    local current_branch
    current_branch="$(git -C "$repo_dir" branch --show-current 2>/dev/null)"

    # 既にマシンブランチにいればOK
    if [[ "$current_branch" == "$branch_name" ]]; then
        ok "マシンブランチ: ${branch_name}"
        return
    fi

    # マシンブランチが存在するか
    if git -C "$repo_dir" show-ref --verify --quiet "refs/heads/${branch_name}" 2>/dev/null; then
        info "既存のマシンブランチに切り替え: ${branch_name}"
        git -C "$repo_dir" checkout "$branch_name" --quiet 2>/dev/null
        # mainの更新をマージ
        git -C "$repo_dir" merge main --no-edit --quiet 2>/dev/null || true
    else
        info "マシンブランチを作成: ${branch_name}"
        git -C "$repo_dir" checkout -b "$branch_name" --quiet 2>/dev/null

        # gitユーザー設定がなければ仮設定 (コミットに必要)
        if ! git -C "$repo_dir" config user.name &>/dev/null; then
            git -C "$repo_dir" config user.name "mubo-agent"
        fi
        if ! git -C "$repo_dir" config user.email &>/dev/null; then
            git -C "$repo_dir" config user.email "mubo@localhost"
        fi

        # 初期コミット (ベースラインとしてタグを打つ)
        git -C "$repo_dir" add -A 2>/dev/null || true
        git -C "$repo_dir" commit --allow-empty -m "mubo: initial state for ${hostname}" --quiet 2>/dev/null || true
        git -C "$repo_dir" tag -f "mubo-initial-${hostname}" --quiet 2>/dev/null || true
    fi

    ok "マシンブランチ: ${branch_name}"
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
    echo "  無貌 — Local LLM Bootstrapper"
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
