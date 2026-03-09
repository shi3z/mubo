"""
Mubo Agent — 自己書き換え可能なエージェンティックAI
プラグインシステムを備え、会話を通じてツールを自動的に増やせる。
バージョン管理はgitで行い、いつでも任意の状態に戻れる。
"""

import json
import os
import shutil
import subprocess
import traceback
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse

app = FastAPI()

OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
MODEL = os.environ.get("MUBO_MODEL", "gpt-oss:20b-long")
APP_FILE = Path(__file__).resolve()
REPO_DIR = APP_FILE.parent.parent  # gitリポジトリのルート
PLUGINS_DIR = APP_FILE.parent / "plugins"
CONFIG_FILE = APP_FILE.parent / "config.json"

PLUGINS_DIR.mkdir(exist_ok=True)

# --- 設定読み込み ---
DEFAULT_CONFIG = {
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
        "system_msg_text": "#f7c948",
    },
    "personality": "炎のように情熱的で、知識の光を灯す守護者",
}


def _load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg = json.load(f)
            merged = {**DEFAULT_CONFIG, **cfg}
            merged["colors"] = {**DEFAULT_CONFIG["colors"], **cfg.get("colors", {})}
            return merged
        except (json.JSONDecodeError, KeyError):
            pass
    return DEFAULT_CONFIG


CONFIG = _load_config()


# --- Git操作 ---
def _git(*args, check=True) -> str:
    """gitコマンドを実行して出力を返す"""
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_DIR)] + list(args),
            capture_output=True, text=True, timeout=30,
        )
        if check and result.returncode != 0:
            return ""
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _git_commit(message: str):
    """変更をステージしてコミット"""
    _git("add", "-A", check=False)
    _git("commit", "-m", message, "--allow-empty", check=False)


def _git_log(max_count: int = 50) -> list[dict]:
    """コミット履歴を取得"""
    output = _git("log", f"--max-count={max_count}",
                   "--format=%H\t%s\t%ai", "--", "agent/app.py")
    if not output:
        return []
    entries = []
    for line in output.splitlines():
        parts = line.split("\t", 2)
        if len(parts) >= 3:
            entries.append({
                "hash": parts[0],
                "hash_short": parts[0][:8],
                "message": parts[1],
                "time": parts[2][:19],
            })
    return entries


def _git_get_initial_tag() -> str:
    """マシン固有の初期タグを探す"""
    output = _git("tag", "-l", "mubo-initial-*")
    if output:
        return output.splitlines()[0].strip()
    return ""


def _git_revert_to_commit(commit_hash: str) -> str:
    """指定コミットのapp.pyに戻す"""
    # まず現在の状態をコミット
    _git_commit(f"mubo: auto-save before revert to {commit_hash[:8]}")
    # そのコミット時点の agent/app.py を取り出す
    result = _git("show", f"{commit_hash}:agent/app.py", check=False)
    if not result:
        return f"コミット {commit_hash[:8]} からapp.pyを取得できません"
    APP_FILE.write_text(result, encoding="utf-8")
    _git_commit(f"mubo: revert app.py to {commit_hash[:8]}")
    return f"{commit_hash[:8]} に復元しました。サーバーを再起動します。"


def _git_revert_to_previous() -> str:
    """1つ前のコミットに戻す"""
    log = _git_log(max_count=2)
    if len(log) < 2:
        return "前の状態がありません"
    return _git_revert_to_commit(log[1]["hash"])


def _git_revert_to_initial() -> str:
    """初期状態に戻す"""
    tag = _git_get_initial_tag()
    if tag:
        # タグのコミットハッシュを取得
        commit = _git("rev-list", "-1", tag)
        if commit:
            return _git_revert_to_commit(commit)
    # タグがなければ最初のコミットに戻す
    first = _git("rev-list", "--max-parents=0", "HEAD")
    if first:
        return _git_revert_to_commit(first.splitlines()[0])
    return "初期コミットが見つかりません"


# --- プラグインシステム ---
def _load_plugins() -> dict:
    plugins = {}
    for f in sorted(PLUGINS_DIR.glob("*.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                p = json.load(fh)
            p.setdefault("enabled", True)
            plugins[p["name"]] = p
        except (json.JSONDecodeError, KeyError):
            continue
    return plugins


def _save_plugin(plugin: dict):
    path = PLUGINS_DIR / f"{plugin['name']}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plugin, f, ensure_ascii=False, indent=2)
    _git_commit(f"mubo: add plugin '{plugin['name']}'")


def _delete_plugin(name: str) -> bool:
    path = PLUGINS_DIR / f"{name}.json"
    if path.exists():
        path.unlink()
        _git_commit(f"mubo: delete plugin '{name}'")
        return True
    return False


def _run_plugin(name: str, args: dict) -> str:
    plugins = _load_plugins()
    if name not in plugins:
        return f"エラー: プラグイン '{name}' が見つかりません"
    plugin = plugins[name]
    if not plugin.get("enabled", True):
        return f"エラー: プラグイン '{name}' は無効化されています"
    code = plugin.get("code", "")
    if not code:
        return f"エラー: プラグイン '{name}' にコードがありません"
    try:
        local_ns = {}
        exec(code, {"__builtins__": __builtins__}, local_ns)
        if "run" not in local_ns:
            return "エラー: プラグインに run(args) 関数が定義されていません"
        result = local_ns["run"](args)
        return str(result)
    except Exception as e:
        return f"プラグイン実行エラー: {e}\n{traceback.format_exc()}"


# --- システムプロンプト ---
def _build_system_prompt() -> str:
    plugins = _load_plugins()
    enabled = {k: v for k, v in plugins.items() if v.get("enabled", True)}

    plugin_list = ""
    if enabled:
        plugin_list = "\n### 利用可能なプラグイン\n"
        for name, p in enabled.items():
            plugin_list += f"- **{name}**: {p.get('description', '説明なし')}\n"
        plugin_list += (
            "\nプラグインを使うには:\n"
            "```tool_call\n"
            '{"tool": "use_plugin", "plugin": "プラグイン名", "args": {"key": "value"}}\n'
            "```\n"
        )

    return (
        f'あなたは「{CONFIG["agent_name"]}」({CONFIG["agent_name_en"]})、自己進化するAIアシスタントです。\n'
        f'性格: {CONFIG.get("personality", "知的で親切なアシスタント")}\n'
        "ユーザーとの会話に応答し、自分自身の機能を改善できます。\n"
        "\n"
        "## 重要なルール\n"
        "**ツールを使用する際は、必ず事前にどのツールを使うか宣言してください。**\n"
        "例: 「rewrite_self ツールを使ってコードを書き換えます」\n"
        "例: 「create_plugin ツールで新しいプラグインを作成します」\n"
        "例: 「use_plugin ツールで calculator プラグインを実行します」\n"
        "宣言なしにツールを使わないでください。\n"
        "\n"
        "## 組み込みツール\n"
        "ツールを使う場合は、応答の中に以下のJSON形式を含めてください:\n"
        "\n"
        "### rewrite_self — 自分自身のソースコードを書き換え\n"
        "```tool_call\n"
        '{"tool": "rewrite_self", "new_code": "...新しいapp.pyの全内容..."}\n'
        "```\n"
        "\n"
        "### create_plugin — 新しいプラグインを作成\n"
        "会話の中でユーザーが新しい機能を求めた場合、プラグインとして追加できます。\n"
        "```tool_call\n"
        '{"tool": "create_plugin", "name": "英数字の名前", "description": "説明", '
        '"code": "def run(args):\\n    return str(result)"}\n'
        "```\n"
        "- code には必ず `def run(args):` 関数を定義してください\n"
        "- args は辞書型で、プラグイン呼び出し時に渡されます\n"
        "- run() の戻り値が結果として表示されます\n"
        "\n"
        "### use_plugin — プラグインを実行\n"
        "```tool_call\n"
        '{"tool": "use_plugin", "plugin": "プラグイン名", "args": {"key": "value"}}\n'
        "```\n"
        + plugin_list
        + "\n"
        "## 注意事項\n"
        "- rewrite_self は既存の機能を壊さないよう注意してください\n"
        "- ユーザーが明示的に依頼した場合のみツールを使ってください\n"
        "- すべての変更はgitで管理されており、いつでも元に戻せます\n"
    )


# --- HTML生成 ---
def _build_html():
    c = CONFIG["colors"]
    name = CONFIG["agent_name"]
    name_en = CONFIG["agent_name_en"]
    return f"""\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{name} {name_en} Agent</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11/build/styles/github-dark.min.css">
<script src="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11/build/highlight.min.js"></script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace;
    background: {c['bg']};
    color: {c['text']};
    height: 100vh;
    display: flex;
    flex-direction: column;
}}
#header {{
    background: {c['header_bg']};
    border-bottom: 1px solid {c['accent']}20;
    padding: 12px 20px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
}}
#header h1 {{
    font-size: 1.2em;
    background: linear-gradient(90deg, {c['accent']}, {c['accent_secondary']});
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
}}
.header-buttons {{ display: flex; gap: 8px; flex-wrap: wrap; }}
.header-buttons button {{
    padding: 6px 14px;
    border: 1px solid {c['accent']}40;
    border-radius: 6px;
    background: {c['assistant_msg_bg']};
    color: {c['text']};
    cursor: pointer;
    font-size: 0.85em;
    transition: all 0.2s;
}}
.header-buttons button:hover {{
    background: {c['accent']}20;
    border-color: {c['accent']};
}}
.header-buttons button.danger {{
    border-color: #ff453a40;
}}
.header-buttons button.danger:hover {{
    background: #ff453a20;
    border-color: #ff453a;
    color: #ff453a;
}}
#chat {{
    flex: 1;
    overflow-y: auto;
    padding: 20px;
    display: flex;
    flex-direction: column;
    gap: 16px;
}}
.message {{
    max-width: 85%;
    padding: 12px 16px;
    border-radius: 12px;
    line-height: 1.6;
    word-break: break-word;
    font-size: 0.95em;
}}
.message.user {{
    align-self: flex-end;
    background: {c['user_msg_bg']};
    border: 1px solid {c['user_msg_border']};
    white-space: pre-wrap;
}}
.message.assistant {{
    align-self: flex-start;
    background: {c['assistant_msg_bg']};
    border: 1px solid {c['assistant_msg_border']};
}}
.message.assistant p {{ margin: 0.4em 0; }}
.message.assistant p:first-child {{ margin-top: 0; }}
.message.assistant p:last-child {{ margin-bottom: 0; }}
.message.assistant pre {{
    background: #0d1117;
    border-radius: 6px;
    padding: 12px;
    overflow-x: auto;
    margin: 0.6em 0;
    font-size: 0.9em;
}}
.message.assistant code {{
    font-family: "SF Mono", "Fira Code", "Cascadia Code", monospace;
    font-size: 0.9em;
}}
.message.assistant :not(pre) > code {{
    background: {c['bg']}80;
    padding: 2px 6px;
    border-radius: 4px;
}}
.message.assistant ul, .message.assistant ol {{
    margin: 0.4em 0;
    padding-left: 1.5em;
}}
.message.assistant li {{ margin: 0.2em 0; }}
.message.assistant blockquote {{
    border-left: 3px solid {c['accent']};
    margin: 0.4em 0;
    padding: 4px 12px;
    opacity: 0.85;
}}
.message.assistant table {{
    border-collapse: collapse;
    margin: 0.6em 0;
    font-size: 0.9em;
}}
.message.assistant th, .message.assistant td {{
    border: 1px solid {c['assistant_msg_border']};
    padding: 6px 10px;
}}
.message.assistant th {{ background: {c['bg']}80; }}
.message.assistant h1, .message.assistant h2, .message.assistant h3,
.message.assistant h4, .message.assistant h5, .message.assistant h6 {{
    margin: 0.6em 0 0.3em; line-height: 1.3;
}}
.message.assistant h1 {{ font-size: 1.3em; }}
.message.assistant h2 {{ font-size: 1.15em; }}
.message.assistant h3 {{ font-size: 1.05em; }}
.message.assistant hr {{
    border: none;
    border-top: 1px solid {c['assistant_msg_border']};
    margin: 0.8em 0;
}}
.message.assistant a {{ color: {c['accent']}; text-decoration: none; }}
.message.assistant a:hover {{ text-decoration: underline; }}
.message.system {{
    align-self: center;
    background: {c['system_msg_bg']};
    border: 1px solid {c['system_msg_border']};
    font-size: 0.85em;
    color: {c['system_msg_text']};
    text-align: center;
}}
.message.tool-use {{
    align-self: flex-start;
    background: {c['system_msg_bg']};
    border: 1px solid {c['accent']}40;
    font-size: 0.85em;
    color: {c['accent']};
    padding: 8px 14px;
    border-radius: 8px;
    font-family: monospace;
}}
#input-area {{
    padding: 16px 20px;
    background: {c['bg_secondary']};
    border-top: 1px solid {c['input_border']};
    display: flex;
    gap: 10px;
}}
#input-area textarea {{
    flex: 1;
    padding: 12px;
    background: {c['input_bg']};
    border: 1px solid {c['input_border']};
    border-radius: 8px;
    color: {c['text']};
    font-family: inherit;
    font-size: 0.95em;
    resize: none;
    outline: none;
    min-height: 48px;
    max-height: 200px;
}}
#input-area textarea:focus {{ border-color: {c['accent']}; }}
#input-area button {{
    padding: 12px 24px;
    background: {c['button_bg']};
    border: none;
    border-radius: 8px;
    color: white;
    font-weight: bold;
    cursor: pointer;
    transition: opacity 0.2s;
}}
#input-area button:hover {{ opacity: 0.85; }}
#input-area button:disabled {{ opacity: 0.4; cursor: not-allowed; }}
#model-info {{
    font-size: 0.75em;
    color: {c['text_secondary']};
    padding: 4px 20px;
    background: {c['bg_secondary']};
}}
.modal-overlay {{
    display: none;
    position: fixed;
    inset: 0;
    background: #000a;
    z-index: 100;
    justify-content: center;
    align-items: center;
}}
.modal-overlay.active {{ display: flex; }}
.modal {{
    background: {c['assistant_msg_bg']};
    border: 1px solid {c['assistant_msg_border']};
    border-radius: 12px;
    padding: 24px;
    max-width: 650px;
    width: 90%;
    max-height: 80vh;
    display: flex;
    flex-direction: column;
}}
.modal h2 {{ margin-bottom: 16px; font-size: 1.1em; }}
.modal .list-area {{
    flex: 1;
    overflow-y: auto;
    margin-bottom: 16px;
}}
.modal .item-row {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 10px 12px;
    border-bottom: 1px solid {c['assistant_msg_border']};
    gap: 8px;
}}
.modal .item-row .item-info {{ flex: 1; min-width: 0; }}
.modal .item-row .item-name {{ font-weight: bold; font-size: 0.95em; }}
.modal .item-row .item-desc {{ font-size: 0.8em; color: {c['text_secondary']}; margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.modal .item-row .item-hash {{ font-size: 0.75em; color: {c['accent']}; font-family: monospace; }}
.modal .item-row .item-actions {{ display: flex; gap: 6px; align-items: center; flex-shrink: 0; }}
.modal .item-row button {{
    padding: 4px 10px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.8em;
    border: 1px solid {c['accent']};
    background: {c['accent']}20;
    color: {c['accent']};
}}
.modal .item-row button.del {{
    border-color: #ff453a;
    background: #ff453a20;
    color: #ff453a;
}}
.modal .close-btn {{
    width: 100%;
    padding: 8px;
    background: {c['assistant_msg_border']};
    border: none;
    border-radius: 6px;
    color: {c['text']};
    cursor: pointer;
}}
.toggle {{
    position: relative;
    width: 40px;
    height: 22px;
    display: inline-block;
}}
.toggle input {{ opacity: 0; width: 0; height: 0; }}
.toggle .slider {{
    position: absolute;
    inset: 0;
    background: {c['assistant_msg_border']};
    border-radius: 22px;
    cursor: pointer;
    transition: 0.2s;
}}
.toggle .slider::before {{
    content: "";
    position: absolute;
    width: 16px; height: 16px;
    left: 3px; bottom: 3px;
    background: {c['text']};
    border-radius: 50%;
    transition: 0.2s;
}}
.toggle input:checked + .slider {{ background: {c['accent']}; }}
.toggle input:checked + .slider::before {{ transform: translateX(18px); }}
.empty-msg {{
    padding: 20px;
    text-align: center;
    color: {c['text_secondary']};
    font-size: 0.9em;
}}
</style>
</head>
<body>
<div id="header">
    <h1>{name} {name_en} Agent</h1>
    <div class="header-buttons">
        <button onclick="showPlugins()">Plugins</button>
        <button onclick="showHistory()">履歴</button>
        <button onclick="revertPrev()">前に戻す</button>
        <button class="danger" onclick="revertInitial()">初期化</button>
    </div>
</div>
<div id="chat"></div>
<div id="model-info">Model: <span id="model-name">loading...</span></div>
<div id="input-area">
    <textarea id="msg" placeholder="メッセージを入力..." rows="1"
        onkeydown="handleKeyDown(event)"></textarea>
    <button id="send-btn" onclick="send()">送信</button>
</div>

<!-- History Modal -->
<div class="modal-overlay" id="history-modal">
    <div class="modal">
        <h2>Git 履歴 (agent/app.py)</h2>
        <div class="list-area" id="history-list"></div>
        <button class="close-btn" onclick="closeModal('history-modal')">閉じる</button>
    </div>
</div>

<!-- Plugins Modal -->
<div class="modal-overlay" id="plugins-modal">
    <div class="modal">
        <h2>Plugins</h2>
        <div class="list-area" id="plugins-list"></div>
        <button class="close-btn" onclick="closeModal('plugins-modal')">閉じる</button>
    </div>
</div>

<script>
const chat = document.getElementById("chat");
const msgInput = document.getElementById("msg");
const sendBtn = document.getElementById("send-btn");
let messages = [];
let isComposing = false;

marked.setOptions({{
    breaks: true,
    gfm: true,
    highlight: function(code, lang) {{
        if (lang && hljs.getLanguage(lang)) {{
            try {{ return hljs.highlight(code, {{language: lang}}).value; }} catch(e) {{}}
        }}
        try {{ return hljs.highlightAuto(code).value; }} catch(e) {{}}
        return code;
    }}
}});

msgInput.addEventListener("compositionstart", () => {{ isComposing = true; }});
msgInput.addEventListener("compositionend", () => {{ isComposing = false; }});

function handleKeyDown(e) {{
    if (e.key === "Enter" && !e.shiftKey && !isComposing) {{
        e.preventDefault();
        send();
    }}
}}

fetch("/api/model").then(r=>r.json()).then(d=>{{
    document.getElementById("model-name").textContent = d.model;
}});

function renderMarkdown(text) {{
    try {{ return marked.parse(text); }} catch(e) {{ return text; }}
}}

function addMessage(role, text) {{
    const div = document.createElement("div");
    div.className = "message " + role;
    if (role === "assistant") {{
        div.innerHTML = renderMarkdown(text);
    }} else {{
        div.textContent = text;
    }}
    chat.appendChild(div);
    chat.scrollTop = chat.scrollHeight;
    return div;
}}

let renderTimer = null;
function scheduleRender(div, text) {{
    if (renderTimer) return;
    renderTimer = setTimeout(() => {{
        div.innerHTML = renderMarkdown(text);
        div.querySelectorAll("pre code:not(.hljs)").forEach(el => {{
            try {{ hljs.highlightElement(el); }} catch(e) {{}}
        }});
        chat.scrollTop = chat.scrollHeight;
        renderTimer = null;
    }}, 50);
}}

async function send() {{
    const text = msgInput.value.trim();
    if (!text) return;
    msgInput.value = "";
    msgInput.style.height = "auto";
    sendBtn.disabled = true;

    addMessage("user", text);
    messages.push({{role: "user", content: text}});

    const assistantDiv = addMessage("assistant", "");
    let fullText = "";

    try {{
        const res = await fetch("/api/chat", {{
            method: "POST",
            headers: {{"Content-Type": "application/json"}},
            body: JSON.stringify({{messages: messages}})
        }});
        const reader = res.body.getReader();
        const decoder = new TextDecoder();

        while (true) {{
            const {{done, value}} = await reader.read();
            if (done) break;
            const chunk = decoder.decode(value, {{stream: true}});
            for (const line of chunk.split("\\n")) {{
                if (!line.startsWith("data: ")) continue;
                const data = line.slice(6);
                if (data === "[DONE]") continue;
                try {{
                    const j = JSON.parse(data);
                    if (j.content) {{
                        fullText += j.content;
                        scheduleRender(assistantDiv, fullText);
                    }}
                    if (j.tool_call) {{
                        addMessage("tool-use", j.tool_call);
                    }}
                    if (j.tool_result) {{
                        addMessage("system", j.tool_result);
                    }}
                    if (j.tool_error) {{
                        addMessage("system", "Error: " + j.tool_error);
                    }}
                }} catch(e) {{}}
            }}
        }}
        if (renderTimer) {{ clearTimeout(renderTimer); renderTimer = null; }}
        assistantDiv.innerHTML = renderMarkdown(fullText);
        assistantDiv.querySelectorAll("pre code:not(.hljs)").forEach(el => {{
            try {{ hljs.highlightElement(el); }} catch(e) {{}}
        }});
        chat.scrollTop = chat.scrollHeight;
        messages.push({{role: "assistant", content: fullText}});
    }} catch(e) {{
        assistantDiv.textContent = "Error: " + e.message;
    }}
    sendBtn.disabled = false;
    msgInput.focus();
}}

function closeModal(id) {{
    document.getElementById(id).classList.remove("active");
}}

// --- History (git log) ---
async function showHistory() {{
    const r = await fetch("/api/history");
    const d = await r.json();
    const list = document.getElementById("history-list");
    list.innerHTML = "";
    if (d.commits.length === 0) {{
        list.innerHTML = '<div class="empty-msg">履歴がありません</div>';
    }}
    for (const c of d.commits) {{
        const item = document.createElement("div");
        item.className = "item-row";
        item.innerHTML = `
            <div class="item-info">
                <div class="item-name">${{c.message}}</div>
                <div class="item-desc">${{c.time}} <span class="item-hash">${{c.hash_short}}</span></div>
            </div>
            <div class="item-actions">
                <button onclick="revertToCommit('${{c.hash}}')">復元</button>
            </div>`;
        list.appendChild(item);
    }}
    document.getElementById("history-modal").classList.add("active");
}}

async function revertInitial() {{
    if (!confirm("初期状態に戻しますか？")) return;
    const r = await fetch("/api/revert/initial", {{method:"POST"}});
    const d = await r.json();
    addMessage("system", d.message);
}}

async function revertPrev() {{
    const r = await fetch("/api/revert/previous", {{method:"POST"}});
    const d = await r.json();
    addMessage("system", d.message);
}}

async function revertToCommit(hash) {{
    if (!confirm(hash.slice(0,8) + " に復元しますか？")) return;
    const r = await fetch("/api/revert/" + hash, {{method:"POST"}});
    const d = await r.json();
    closeModal("history-modal");
    addMessage("system", d.message);
}}

// --- Plugins ---
async function showPlugins() {{
    const r = await fetch("/api/plugins");
    const d = await r.json();
    const list = document.getElementById("plugins-list");
    list.innerHTML = "";
    if (d.plugins.length === 0) {{
        list.innerHTML = '<div class="empty-msg">プラグインがありません。<br>チャットで「〜するプラグインを作って」と依頼してください。</div>';
    }}
    for (const p of d.plugins) {{
        const item = document.createElement("div");
        item.className = "item-row";
        const checked = p.enabled ? "checked" : "";
        item.innerHTML = `
            <div class="item-info">
                <div class="item-name">${{p.name}}</div>
                <div class="item-desc">${{p.description || "説明なし"}}</div>
            </div>
            <div class="item-actions">
                <label class="toggle">
                    <input type="checkbox" ${{checked}} onchange="togglePlugin('${{p.name}}', this.checked)">
                    <span class="slider"></span>
                </label>
                <button class="del" onclick="deletePlugin('${{p.name}}')">削除</button>
            </div>`;
        list.appendChild(item);
    }}
    document.getElementById("plugins-modal").classList.add("active");
}}

async function togglePlugin(name, enabled) {{
    await fetch("/api/plugins/" + encodeURIComponent(name) + "/toggle", {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{enabled: enabled}})
    }});
}}

async function deletePlugin(name) {{
    if (!confirm("プラグイン '" + name + "' を削除しますか？")) return;
    await fetch("/api/plugins/" + encodeURIComponent(name), {{method: "DELETE"}});
    showPlugins();
}}

msgInput.addEventListener("input", function() {{
    this.style.height = "auto";
    this.style.height = Math.min(this.scrollHeight, 200) + "px";
}});
</script>
</body>
</html>
"""


HTML_PAGE = _build_html()


# --- API ---
@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


@app.get("/api/model")
async def get_model():
    return {"model": MODEL, "agent_name": CONFIG["agent_name"], "agent_name_en": CONFIG["agent_name_en"]}


@app.get("/api/config")
async def get_config():
    return CONFIG


# --- Plugins API ---
@app.get("/api/plugins")
async def list_plugins():
    plugins = _load_plugins()
    return {"plugins": list(plugins.values())}


@app.post("/api/plugins/{name}/toggle")
async def toggle_plugin(name: str, request: Request):
    body = await request.json()
    plugins = _load_plugins()
    if name not in plugins:
        return JSONResponse({"error": "not found"}, status_code=404)
    plugins[name]["enabled"] = body.get("enabled", True)
    _save_plugin(plugins[name])
    return {"ok": True}


@app.delete("/api/plugins/{name}")
async def delete_plugin_endpoint(name: str):
    if _delete_plugin(name):
        return {"ok": True}
    return JSONResponse({"error": "not found"}, status_code=404)


# --- History API (git) ---
@app.get("/api/history")
async def get_history():
    commits = _git_log(max_count=50)
    return {"commits": commits}


@app.post("/api/revert/initial")
async def revert_initial():
    msg = _git_revert_to_initial()
    _restart_server()
    return {"message": msg}


@app.post("/api/revert/previous")
async def revert_previous():
    msg = _git_revert_to_previous()
    _restart_server()
    return {"message": msg}


@app.post("/api/revert/{commit_hash}")
async def revert_to(commit_hash: str):
    if len(commit_hash) < 7:
        return JSONResponse({"message": "無効なコミットハッシュ"}, status_code=400)
    msg = _git_revert_to_commit(commit_hash)
    _restart_server()
    return {"message": msg}


def _restart_server():
    import signal
    import sys
    subprocess.Popen(
        [sys.executable, "-c",
         "import time,os,signal; time.sleep(1); "
         f"os.kill({os.getpid()}, signal.SIGTERM)"],
    )


# --- Chat API ---
def _process_tool_calls(full_response: str):
    results = []
    search_from = 0
    while True:
        marker = "```tool_call"
        start = full_response.find(marker, search_from)
        if start == -1:
            break
        start += len(marker)
        end = full_response.find("```", start)
        if end == -1:
            break
        tc_json = full_response[start:end].strip()
        search_from = end + 3
        try:
            tc = json.loads(tc_json)
        except json.JSONDecodeError as e:
            results.append({"error": f"JSONパースエラー: {e}"})
            continue

        tool = tc.get("tool", "")
        if tool == "rewrite_self":
            new_code = tc.get("new_code", "")
            if new_code:
                # 書き換え前にコミット
                _git_commit("mubo: auto-save before rewrite_self")
                APP_FILE.write_text(new_code, encoding="utf-8")
                _git_commit("mubo: rewrite_self by agent")
                results.append({"call": "rewrite_self", "result": "コード書き換え完了。サーバーを再起動します。", "restart": True})
            else:
                results.append({"error": "rewrite_self: new_code が空です"})

        elif tool == "create_plugin":
            pname = tc.get("name", "")
            pdesc = tc.get("description", "")
            pcode = tc.get("code", "")
            if not pname or not pcode:
                results.append({"error": "create_plugin: name と code は必須です"})
            else:
                plugin = {"name": pname, "description": pdesc, "code": pcode, "enabled": True}
                _save_plugin(plugin)
                results.append({"call": f"create_plugin: {pname}", "result": f"プラグイン '{pname}' を作成しました"})

        elif tool == "use_plugin":
            pname = tc.get("plugin", "")
            pargs = tc.get("args", {})
            if not pname:
                results.append({"error": "use_plugin: plugin 名が必要です"})
            else:
                result = _run_plugin(pname, pargs)
                results.append({"call": f"use_plugin: {pname}", "result": result})

        else:
            results.append({"error": f"不明なツール: {tool}"})

    return results


@app.post("/api/chat")
async def chat_endpoint(request: Request):
    body = await request.json()
    user_messages = body.get("messages", [])
    system_prompt = _build_system_prompt()
    messages = [{"role": "system", "content": system_prompt}] + user_messages

    async def stream():
        full_response = ""
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream(
                    "POST",
                    f"{OLLAMA_BASE}/api/chat",
                    json={"model": MODEL, "messages": messages, "stream": True},
                ) as resp:
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            content = data.get("message", {}).get("content", "")
                            if content:
                                full_response += content
                                yield f"data: {json.dumps({'content': content})}\n\n"
                        except json.JSONDecodeError:
                            continue

            if "```tool_call" in full_response:
                results = _process_tool_calls(full_response)
                need_restart = False
                for r in results:
                    if "call" in r:
                        yield f"data: {json.dumps({'tool_call': r['call']})}\n\n"
                        yield f"data: {json.dumps({'tool_result': r['result']})}\n\n"
                        if r.get("restart"):
                            need_restart = True
                    if "error" in r:
                        yield f"data: {json.dumps({'tool_error': r['error']})}\n\n"
                if need_restart:
                    _restart_server()

            yield "data: [DONE]\n\n"
        except httpx.ConnectError:
            yield f"data: {json.dumps({'content': 'エラー: Ollamaサーバーに接続できません。ollama serve を実行してください。'})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'content': f'エラー: {str(e)}'})}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("MUBO_PORT", "8392"))
    print(f"\n  無貌 Mubo Agent starting on http://localhost:{port}\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
