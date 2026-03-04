# SPEC.md — One-shot Local LLM Bootstrapper (Ollama + gpt-oss:20b + ctx拡張 + 任意extras)

## 0. 概要

本プロジェクトは、リポジトリをダウンロードして **`./setup.sh` を実行するだけ**で、ローカルLLM実行環境を自動的に整えるブートストラッパである。

実行すると以下を順に行う：

1. **環境調査**（OS/CPU/RAM/GPU、macOSならユニファイドメモリとNPUの有無など）
2. **Ollama の導入**（未導入の場合）および疎通確認
3. `ollama pull gpt-oss:20b` によるモデル取得
4. `gpt-oss:20b` をベースに **コンテキスト長を 64K 以上（可能なら 128K）**へ拡張した派生モデルを生成・ロード
5. 条件が合えば追加環境（vLLM / MLX / uv / docker など）を構築（任意）

---

## 1. 目的

- ローカルLLMを “誰でも” “迷わず” 起動できる状態にする
- OS/ハードウェア差を吸収し、**最小限の分岐**で最大限の自動化を行う
- 追加の推論バックエンド（vLLM/MLX 等）は **動くときだけ**導入し、失敗しても本線（Ollama）は成立させる

---

## 2. スコープ

### 2.1 対象プラットフォーム（MVP）
- **macOS**（Apple Silicon 優先、Intel は best-effort）
- **Linux**（x86_64 優先、aarch64 は best-effort）

### 2.2 非対象（MVP）
- Windows ネイティブ対応（将来：WSL対応を検討）
- 全GPU/全ドライバ環境の完全最適化（まずは安全なヒューリスティック）
- 企業環境の厳格なプロキシ設定や証明書周りの完全対応（案内は行う）

---

## 3. ユーザー体験（UX）

### 3.1 最小実行手順
```bash
git clone <repo>
cd <repo>
./setup.sh
