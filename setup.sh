#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Suzaku — One-shot Local LLM Bootstrapper
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

install_ollama() {
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
    modelfile=$(mktemp /tmp/suzaku-modelfile.XXXXXX)

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
        info "Docker: 未インストール (必須ではありません)"
        info "  インストール: https://docs.docker.com/get-docker/"
        return
    fi

    if ! docker info &>/dev/null 2>&1; then
        warn "Docker はインストール済みですがデーモンが停止中です"
        return
    fi
    ok "Docker デーモンは稼働中"

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
# 最終レポート
# ============================================================
print_summary() {
    step "セットアップ完了"

    echo ""
    printf "${BOLD}┌─────────────────────────────────────────┐${NC}\n"
    printf "${BOLD}│         Suzaku セットアップ完了          │${NC}\n"
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
    echo ""
    printf "  ${GREEN}使い方:${NC}\n"
    printf "    ollama run %s\n" "$DERIVED_MODEL"
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
main() {
    echo ""
    printf "${BOLD}${CYAN}"
    echo "  ____                   _          "
    echo " / ___|  _   _  ______ | | ___   _ "
    echo " \\___ \\ | | | ||_  /  \\| |/ / | | |"
    echo "  ___) || |_| | / /| () |   <| |_| |"
    echo " |____/  \\__,_|/___\\__/|_|\\_\\\\__,_|"
    printf "${NC}\n"
    echo "  Local LLM Bootstrapper"
    echo ""

    detect_environment
    setup_ollama
    pull_model
    create_extended_model
    setup_extras
    print_summary
}

main "$@"
