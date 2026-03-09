# Mubo（無貌）

One-shot local LLM bootstrapper. Sets up everything you need to run a local LLM — automatically.

## Quick Install

```bash
curl -fsSL https://raw.githubusercontent.com/shi3z/mubo/main/setup.sh | bash
```

Or clone and run:

```bash
git clone https://github.com/shi3z/mubo.git
cd mubo
./setup.sh
```

After setup, open http://localhost:8392 in your browser to use the AI agent.

## Features

- **Fully automatic setup** — Detects OS, CPU, RAM, GPU and configures everything
- **Smart hardware detection** — NVIDIA / AMD / Apple Silicon GPU, unified memory, NPU
- **Optimal context length** — Automatically set based on available memory (16K–128K)
- **Safe optional installs** — vLLM, MLX, Docker, uv are installed only when possible, never breaking the core
- **Self-modifying AI agent** — Web-based agent that can rewrite its own source code, with full version history and rollback

## Supported Platforms

| OS | Architecture | Support |
|---|---|---|
| macOS | Apple Silicon | Primary |
| macOS | Intel | Best-effort |
| Linux | x86_64 | Primary |
| Linux | aarch64 | Best-effort |

## Setup Phases

1. **Environment detection** — OS, CPU architecture, RAM, GPU type
2. **Ollama install** — Install Ollama and verify server health
3. **Base model download** — Pull gpt-oss:20b
4. **Extended model creation** — Create a derived model with optimal context length
5. **Extras (optional)** — vLLM, MLX, uv, Docker
6. **Mubo Agent deploy** — Launch the self-modifying AI agent with web UI

## Requirements

- bash
- Internet connection
- Sufficient storage for model weights

## License

MIT
