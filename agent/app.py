"""
Mubo Agent — 自己書き換え可能なエージェンティックAI
Ollamaのローカルモデルとストリーミング会話し、自身のコードを書き換える機能を持つ。
"""

import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse

app = FastAPI()

OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
MODEL = os.environ.get("MUBO_MODEL", "gpt-oss:20b-long")
APP_FILE = Path(__file__).resolve()
HISTORY_DIR = APP_FILE.parent / "history"
INITIAL_BACKUP = HISTORY_DIR / "app.py.initial"
CONFIG_FILE = APP_FILE.parent / "config.json"

# 設定読み込み
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
            # デフォルトとマージ（足りないキーを補完）
            merged = {**DEFAULT_CONFIG, **cfg}
            merged["colors"] = {**DEFAULT_CONFIG["colors"], **cfg.get("colors", {})}
            return merged
        except (json.JSONDecodeError, KeyError):
            pass
    return DEFAULT_CONFIG

CONFIG = _load_config()

# 起動時に初期バックアップを保存
HISTORY_DIR.mkdir(exist_ok=True)
if not INITIAL_BACKUP.exists():
    shutil.copy2(APP_FILE, INITIAL_BACKUP)

SYSTEM_PROMPT = (
    f'あなたは「{CONFIG["agent_name"]}」({CONFIG["agent_name_en"]})、自己進化するAIアシスタントです。\n'
    f'性格: {CONFIG.get("personality", "知的で親切なアシスタント")}\n'
    "ユーザーとの会話に応答するだけでなく、自分自身の機能を改善するためにソースコードを書き換えることができます。\n"
    "\n"
    "## ツール\n"
    "あなたは以下のツールを使えます。ツールを使う場合は、応答の中に以下のJSON形式を含めてください:\n"
    "\n"
    "```tool_call\n"
    '{"tool": "rewrite_self", "new_code": "...新しいapp.pyの全内容..."}\n'
    "```\n"
    "\n"
    "### rewrite_self\n"
    "自分自身のソースコード(app.py)を書き換えます。\n"
    "- new_code: 新しいapp.pyの完全なソースコード\n"
    "- 書き換え後、サーバーは自動的に再起動されます\n"
    "- 書き換え前の状態は自動的に保存されるので、失敗してもユーザーが元に戻せます\n"
    "\n"
    "## 注意事項\n"
    "- コードを書き換える際は、既存の機能（チャット、ストリーミング、履歴管理、リバート機能）を壊さないよう注意してください\n"
    "- 新機能の追加や、UIの改善など、建設的な変更のみ行ってください\n"
    "- ユーザーが明示的に依頼した場合のみコードを書き換えてください\n"
)

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
}}
#header h1 {{
    font-size: 1.2em;
    background: linear-gradient(90deg, {c['accent']}, {c['accent_secondary']});
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
}}
.header-buttons {{ display: flex; gap: 8px; }}
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
/* Markdown styles */
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
.message.assistant th {{
    background: {c['bg']}80;
}}
.message.assistant h1, .message.assistant h2, .message.assistant h3,
.message.assistant h4, .message.assistant h5, .message.assistant h6 {{
    margin: 0.6em 0 0.3em;
    line-height: 1.3;
}}
.message.assistant h1 {{ font-size: 1.3em; }}
.message.assistant h2 {{ font-size: 1.15em; }}
.message.assistant h3 {{ font-size: 1.05em; }}
.message.assistant hr {{
    border: none;
    border-top: 1px solid {c['assistant_msg_border']};
    margin: 0.8em 0;
}}
.message.assistant a {{
    color: {c['accent']};
    text-decoration: none;
}}
.message.assistant a:hover {{
    text-decoration: underline;
}}
.message.system {{
    align-self: center;
    background: {c['system_msg_bg']};
    border: 1px solid {c['system_msg_border']};
    font-size: 0.85em;
    color: {c['system_msg_text']};
    text-align: center;
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
/* Modal */
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
    max-width: 500px;
    width: 90%;
}}
.modal h2 {{ margin-bottom: 16px; font-size: 1.1em; }}
.modal .history-list {{
    max-height: 300px;
    overflow-y: auto;
    margin-bottom: 16px;
}}
.modal .history-item {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 8px 12px;
    border-bottom: 1px solid {c['assistant_msg_border']};
}}
.modal .history-item button {{
    padding: 4px 12px;
    background: {c['accent']}20;
    border: 1px solid {c['accent']};
    border-radius: 4px;
    color: {c['accent']};
    cursor: pointer;
    font-size: 0.85em;
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
</style>
</head>
<body>
<div id="header">
    <h1>{name} {name_en} Agent</h1>
    <div class="header-buttons">
        <button onclick="showHistory()">履歴から復元</button>
        <button onclick="revertPrev()">前の状態に戻す</button>
        <button class="danger" onclick="revertInitial()">初期状態に戻す</button>
    </div>
</div>
<div id="chat"></div>
<div id="model-info">Model: <span id="model-name">loading...</span></div>
<div id="input-area">
    <textarea id="msg" placeholder="メッセージを入力..." rows="1"
        onkeydown="handleKeyDown(event)"></textarea>
    <button id="send-btn" onclick="send()">送信</button>
</div>

<div class="modal-overlay" id="history-modal">
    <div class="modal">
        <h2>コード履歴</h2>
        <div class="history-list" id="history-list"></div>
        <button class="close-btn" onclick="closeHistory()">閉じる</button>
    </div>
</div>

<script>
const chat = document.getElementById("chat");
const msgInput = document.getElementById("msg");
const sendBtn = document.getElementById("send-btn");
let messages = [];
let isComposing = false;

// marked.js 設定
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

// モデル名を取得
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
                    if (j.tool_executed) {{
                        addMessage("system", "コードを書き換えました。ページをリロードしてください。");
                    }}
                    if (j.tool_error) {{
                        addMessage("system", "コード書き換えエラー: " + j.tool_error);
                    }}
                }} catch(e) {{}}
            }}
        }}
        // 最終レンダリング
        if (renderTimer) {{ clearTimeout(renderTimer); renderTimer = null; }}
        assistantDiv.innerHTML = renderMarkdown(fullText);
        assistantDiv.querySelectorAll("pre code:not(.hljs)").forEach(el => {{
            try {{ hljs.highlightElement(el); }} catch(e) {{}}
        }});
        chat.scrollTop = chat.scrollHeight;
        messages.push({{role: "assistant", content: fullText}});
    }} catch(e) {{
        assistantDiv.textContent = "エラー: " + e.message;
    }}
    sendBtn.disabled = false;
    msgInput.focus();
}}

async function revertInitial() {{
    if (!confirm("初期状態に戻しますか？サーバーが再起動されます。")) return;
    const r = await fetch("/api/revert/initial", {{method:"POST"}});
    const d = await r.json();
    addMessage("system", d.message);
}}

async function revertPrev() {{
    const r = await fetch("/api/revert/previous", {{method:"POST"}});
    const d = await r.json();
    addMessage("system", d.message);
}}

async function showHistory() {{
    const r = await fetch("/api/history");
    const d = await r.json();
    const list = document.getElementById("history-list");
    list.innerHTML = "";
    if (d.versions.length === 0) {{
        list.innerHTML = "<p style='padding:12px;color:{c['text_secondary']}'>履歴がありません</p>";
    }}
    for (const v of d.versions) {{
        const item = document.createElement("div");
        item.className = "history-item";
        item.innerHTML = `<span>${{v.name}} (${{v.time}})</span>
            <button onclick="revertTo('${{v.name}}')">復元</button>`;
        list.appendChild(item);
    }}
    document.getElementById("history-modal").classList.add("active");
}}

function closeHistory() {{
    document.getElementById("history-modal").classList.remove("active");
}}

async function revertTo(name) {{
    if (!confirm(name + " に復元しますか？")) return;
    const r = await fetch("/api/revert/" + encodeURIComponent(name), {{method:"POST"}});
    const d = await r.json();
    closeHistory();
    addMessage("system", d.message);
}}

// auto-resize textarea
msgInput.addEventListener("input", function() {{
    this.style.height = "auto";
    this.style.height = Math.min(this.scrollHeight, 200) + "px";
}});
</script>
</body>
</html>
"""

HTML_PAGE = _build_html()


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


@app.get("/api/model")
async def get_model():
    return {"model": MODEL, "agent_name": CONFIG["agent_name"], "agent_name_en": CONFIG["agent_name_en"]}


@app.get("/api/config")
async def get_config():
    return CONFIG


@app.get("/api/history")
async def get_history():
    versions = []
    if HISTORY_DIR.exists():
        for f in sorted(HISTORY_DIR.glob("app.py.*")):
            stat = f.stat()
            versions.append({
                "name": f.name,
                "time": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })
    return {"versions": versions}


def _save_backup():
    """現在のapp.pyをバックアップとして保存"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = HISTORY_DIR / f"app.py.{ts}"
    shutil.copy2(APP_FILE, backup)
    return backup


def _do_rewrite(new_code: str) -> str:
    """app.pyを新しいコードで書き換える"""
    _save_backup()
    APP_FILE.write_text(new_code, encoding="utf-8")
    return "コード書き換え完了。サーバーを再起動してください。"


def _do_revert(source: Path) -> str:
    """指定されたバックアップからapp.pyを復元する"""
    if not source.exists():
        return "バックアップが見つかりません"
    _save_backup()
    shutil.copy2(source, APP_FILE)
    return f"{source.name} から復元しました。サーバーを再起動してください。"


@app.post("/api/revert/initial")
async def revert_initial():
    msg = _do_revert(INITIAL_BACKUP)
    _restart_server()
    return {"message": msg}


@app.post("/api/revert/previous")
async def revert_previous():
    backups = sorted(HISTORY_DIR.glob("app.py.[0-9]*"))
    if not backups:
        return {"message": "前の状態がありません"}
    msg = _do_revert(backups[-1])
    _restart_server()
    return {"message": msg}


@app.post("/api/revert/{name}")
async def revert_to(name: str):
    source = HISTORY_DIR / name
    if not source.exists() or not name.startswith("app.py."):
        return JSONResponse({"message": "無効なバックアップ名"}, status_code=400)
    msg = _do_revert(source)
    _restart_server()
    return {"message": msg}


def _restart_server():
    """バックグラウンドでサーバーを再起動する"""
    import subprocess
    import sys
    # uvicorn を再起動するため、現プロセスを置き換える
    # 少し遅延させてレスポンスを返してから再起動
    subprocess.Popen(
        [sys.executable, "-c",
         "import time,os,signal; time.sleep(1); "
         f"os.kill({os.getpid()}, signal.SIGTERM)"],
    )


@app.post("/api/chat")
async def chat_endpoint(request: Request):
    body = await request.json()
    user_messages = body.get("messages", [])

    # システムプロンプトを先頭に追加
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + user_messages

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

            # レスポンス完了後、ツールコールをチェック
            if "```tool_call" in full_response:
                try:
                    tc_start = full_response.index("```tool_call") + len("```tool_call")
                    tc_end = full_response.index("```", tc_start)
                    tc_json = full_response[tc_start:tc_end].strip()
                    tc = json.loads(tc_json)
                    if tc.get("tool") == "rewrite_self" and tc.get("new_code"):
                        result = _do_rewrite(tc["new_code"])
                        yield f"data: {json.dumps({'tool_executed': True, 'result': result})}\n\n"
                        _restart_server()
                except (ValueError, json.JSONDecodeError, KeyError) as e:
                    yield f"data: {json.dumps({'tool_error': str(e)})}\n\n"

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
