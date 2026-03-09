"""
Mubo Agent — Self-rewriting agentic AI
Features a plugin system to dynamically add tools through conversation.
Version management via git, with rollback to any previous state.
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
REPO_DIR = APP_FILE.parent.parent  # git repository root
PLUGINS_DIR = APP_FILE.parent / "plugins"
CONFIG_FILE = APP_FILE.parent / "config.json"

PLUGINS_DIR.mkdir(exist_ok=True)

# --- Web Search ---
def _web_search(query: str, max_results: int = 5) -> str:
    """Search the web using DuckDuckGo and return results."""
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return "No results found."
        output = []
        for r in results:
            title = r.get("title", "")
            href = r.get("href", "")
            body = r.get("body", "")
            output.append(f"**{title}**\n{href}\n{body}")
        return "\n\n".join(output)
    except ImportError:
        return "Error: duckduckgo-search package not installed. Run: uv add duckduckgo-search"
    except Exception as e:
        return f"Search error: {e}"


# --- File Operations ---
HOME_DIR = Path.home()


def _file_read(path: str) -> str:
    """Read a file and return its contents."""
    try:
        p = Path(path)
        if not p.is_absolute():
            p = HOME_DIR / p
        if not p.exists():
            return f"Error: file not found: {p}"
        if not p.is_file():
            return f"Error: not a file: {p}"
        content = p.read_text(encoding="utf-8", errors="replace")
        if len(content) > 10000:
            content = content[:10000] + "\n... (truncated, 10000 chars shown)"
        return content
    except Exception as e:
        return f"Error: {e}"


def _file_write(path: str, content: str) -> str:
    """Write content to a file."""
    try:
        p = Path(path)
        if not p.is_absolute():
            p = HOME_DIR / p
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Written {len(content)} chars to {p}"
    except Exception as e:
        return f"Error: {e}"


def _list_files(path: str = ".", pattern: str = "*") -> str:
    """List files in a directory."""
    try:
        p = Path(path)
        if not p.is_absolute():
            p = HOME_DIR / p
        if not p.exists():
            return f"Error: directory not found: {p}"
        if not p.is_dir():
            return f"Error: not a directory: {p}"
        entries = sorted(p.glob(pattern))
        if not entries:
            return "(no files matching)"
        lines = []
        for e in entries[:100]:
            prefix = "d " if e.is_dir() else "f "
            size = e.stat().st_size if e.is_file() else 0
            lines.append(f"{prefix}{e.relative_to(p)}  ({size} bytes)" if e.is_file() else f"{prefix}{e.relative_to(p)}/")
        result = "\n".join(lines)
        if len(entries) > 100:
            result += f"\n... ({len(entries) - 100} more entries)"
        return result
    except Exception as e:
        return f"Error: {e}"


# --- Python Runner ---
def _python_run(code: str, timeout: int = 30) -> str:
    """Execute Python code in a subprocess and return stdout/stderr."""
    try:
        result = subprocess.run(
            ["python3", "-c", code],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(APP_FILE.parent),
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += ("\n" if output else "") + result.stderr
        if not output:
            output = "(no output)"
        # Truncate very long output
        if len(output) > 5000:
            output = output[:5000] + "\n... (truncated)"
        return output
    except subprocess.TimeoutExpired:
        return f"Error: execution timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


# --- Config ---
DEFAULT_STRINGS = {
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
    "initial_not_found": "Initial commit not found",
}

DEFAULT_CONFIG = {
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
        "system_msg_text": "#f7c948",
    },
    "personality": "A passionate guardian who ignites the light of knowledge",
    "strings": DEFAULT_STRINGS,
}


def _load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg = json.load(f)
            merged = {**DEFAULT_CONFIG, **cfg}
            merged["colors"] = {**DEFAULT_CONFIG["colors"], **cfg.get("colors", {})}
            merged["strings"] = {**DEFAULT_STRINGS, **cfg.get("strings", {})}
            return merged
        except (json.JSONDecodeError, KeyError):
            pass
    return DEFAULT_CONFIG


CONFIG = _load_config()
S = CONFIG["strings"]  # shorthand for strings


# --- Git Operations ---
def _git(*args, check=True) -> str:
    """Run a git command and return its output."""
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
    """Stage changes and commit."""
    _git("add", "-A", check=False)
    _git("commit", "-m", message, "--allow-empty", check=False)


def _git_log(max_count: int = 50) -> list[dict]:
    """Get commit history."""
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
    """Find the machine-specific initial tag."""
    output = _git("tag", "-l", "mubo-initial-*")
    if output:
        return output.splitlines()[0].strip()
    return ""


def _git_revert_to_commit(commit_hash: str) -> str:
    """Revert app.py to the specified commit."""
    _git_commit(f"mubo: auto-save before revert to {commit_hash[:8]}")
    # Retrieve agent/app.py from that commit
    result = _git("show", f"{commit_hash}:agent/app.py", check=False)
    if not result:
        return f"{S['error_prefix']}: cannot retrieve app.py from {commit_hash[:8]}"
    APP_FILE.write_text(result, encoding="utf-8")
    _git_commit(f"mubo: revert app.py to {commit_hash[:8]}")
    return f"{commit_hash[:8]} — {S['restored_msg']}"


def _git_revert_to_previous() -> str:
    log = _git_log(max_count=2)
    if len(log) < 2:
        return S["no_previous"]
    return _git_revert_to_commit(log[1]["hash"])


def _git_revert_to_initial() -> str:
    """Revert to the initial state."""
    tag = _git_get_initial_tag()
    if tag:
        commit = _git("rev-list", "-1", tag)
        if commit:
            return _git_revert_to_commit(commit)
    # If no tag found, revert to the first commit
    first = _git("rev-list", "--max-parents=0", "HEAD")
    if first:
        return _git_revert_to_commit(first.splitlines()[0])
    return S["initial_not_found"]


# --- Plugin System ---
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
        return f"{S['error_prefix']}: plugin '{name}' not found"
    plugin = plugins[name]
    if not plugin.get("enabled", True):
        return f"{S['error_prefix']}: plugin '{name}' is disabled"
    code = plugin.get("code", "")
    if not code:
        return f"{S['error_prefix']}: plugin '{name}' has no code"
    try:
        local_ns = {}
        exec(code, {"__builtins__": __builtins__}, local_ns)
        if "run" not in local_ns:
            return f"{S['error_prefix']}: plugin missing run(args) function"
        result = local_ns["run"](args)
        return str(result)
    except Exception as e:
        return f"{S['error_prefix']}: plugin execution failed: {e}\n{traceback.format_exc()}"


# --- System Prompt ---
def _build_system_prompt() -> str:
    plugins = _load_plugins()
    enabled = {k: v for k, v in plugins.items() if v.get("enabled", True)}

    plugin_list = ""
    if enabled:
        plugin_list = "\n### Available Plugins\n"
        for name, p in enabled.items():
            plugin_list += f"- **{name}**: {p.get('description', 'No description')}\n"
        plugin_list += (
            "\nTo use a plugin:\n"
            "```tool_call\n"
            '{"tool": "use_plugin", "plugin": "plugin_name", "args": {"key": "value"}}\n'
            "```\n"
        )

    return (
        f'You are "{CONFIG["agent_name"]}" ({CONFIG["agent_name_en"]}), a self-evolving AI assistant.\n'
        f'Personality: {CONFIG.get("personality", "An intelligent and helpful assistant")}\n'
        "You respond to user conversations and can improve your own capabilities.\n"
        "\n"
        "## Important Rules\n"
        "**When using a tool, you MUST declare which tool you will use before calling it.**\n"
        "Example: \"I will use the web_search tool to look this up.\"\n"
        "Example: \"I will use the rewrite_self tool to modify the code.\"\n"
        "Example: \"I will use the create_plugin tool to add a new plugin.\"\n"
        "Do NOT use tools without declaring them first.\n"
        "\n"
        "**When the user asks about current events, news, real-time information, or anything you are unsure about, "
        "you MUST use the web_search tool. Do NOT make up information.**\n"
        "\n"
        "**When the user asks you to DO something (write a file, run code, read a file, search, etc.), "
        "you MUST actually execute the action using the appropriate tool. Do NOT just show the code or explain how to do it. "
        "Actually do it by calling the tool.**\n"
        "- To write a file → use file_write\n"
        "- To read a file → use file_read\n"
        "- To run code/calculate → use python_run\n"
        "- To list files → use list_files\n"
        "- To search the web → use web_search\n"
        "\n"
        "## Built-in Tools\n"
        "To use a tool, include the following JSON format in your response:\n"
        "\n"
        "### web_search — Search the web for current information\n"
        "Use this tool whenever you need up-to-date information, news, facts, etc.\n"
        "```tool_call\n"
        '{"tool": "web_search", "query": "search query here", "max_results": 5}\n'
        "```\n"
        "\n"
        "### python_run — Execute Python code\n"
        "Run Python code and return the output. Use this for calculations, data processing, etc.\n"
        "```tool_call\n"
        '{"tool": "python_run", "code": "print(2 + 2)", "timeout": 30}\n'
        "```\n"
        "\n"
        "### file_read — Read a file\n"
        "```tool_call\n"
        '{"tool": "file_read", "path": "relative/or/absolute/path.txt"}\n'
        "```\n"
        "- Relative paths are resolved from the agent directory\n"
        "\n"
        "### file_write — Write to a file\n"
        "```tool_call\n"
        '{"tool": "file_write", "path": "path.txt", "content": "file contents here"}\n'
        "```\n"
        "\n"
        "### list_files — List files in a directory\n"
        "```tool_call\n"
        '{"tool": "list_files", "path": ".", "pattern": "*.py"}\n'
        "```\n"
        "\n"
        "### rewrite_self — Rewrite your own source code\n"
        "```tool_call\n"
        '{"tool": "rewrite_self", "new_code": "...full new app.py content..."}\n'
        "```\n"
        "\n"
        "### create_plugin — Create a new plugin\n"
        "When the user requests new functionality, add it as a plugin.\n"
        "```tool_call\n"
        '{"tool": "create_plugin", "name": "alphanumeric_name", "description": "description", '
        '"code": "def run(args):\\n    return str(result)"}\n'
        "```\n"
        "- code must define a `def run(args):` function\n"
        "- args is a dict passed when the plugin is called\n"
        "- The return value of run() is displayed as the result\n"
        "\n"
        "### use_plugin — Execute a plugin\n"
        "```tool_call\n"
        '{"tool": "use_plugin", "plugin": "plugin_name", "args": {"key": "value"}}\n'
        "```\n"
        + plugin_list
        + "\n"
        "## Notes\n"
        "- Be careful not to break existing functionality when using rewrite_self\n"
        "- Only use tools when the user explicitly requests it, or when you need real-time information\n"
        "- All changes are managed with git and can be reverted at any time\n"
    )


# --- HTML Generation ---
def _build_html():
    c = CONFIG["colors"]
    s = CONFIG["strings"]
    name = CONFIG["agent_name"]
    name_en = CONFIG["agent_name_en"]
    return f"""\
<!DOCTYPE html>
<html>
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
    display: flex;
    align-items: center;
    gap: 8px;
}}
.message.tool-use .tool-icon {{
    display: inline-block;
    width: 14px;
    height: 14px;
    border: 2px solid {c['accent']};
    border-top-color: transparent;
    border-radius: 50%;
    animation: tool-spin 0.8s linear infinite;
    flex-shrink: 0;
}}
.message.tool-use.done .tool-icon {{
    animation: none;
    border: none;
    width: auto;
    height: auto;
}}
.message.tool-use.done .tool-icon::after {{
    content: "✓";
}}
.message.tool-result {{
    align-self: flex-start;
    background: {c['bg_secondary']};
    border: 1px solid {c['accent']}20;
    font-size: 0.8em;
    color: {c['text_secondary']};
    padding: 10px 14px;
    border-radius: 8px;
    max-height: 200px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-word;
}}
@keyframes tool-spin {{
    to {{ transform: rotate(360deg); }}
}}
.thinking-block {{
    background: {c['bg_secondary']};
    border: 1px solid {c['accent']}15;
    border-radius: 8px;
    padding: 10px 14px;
    margin-bottom: 8px;
    font-size: 0.8em;
    color: {c['text_secondary']};
    max-height: 150px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-word;
    cursor: pointer;
    transition: max-height 0.3s;
}}
.thinking-block.collapsed {{
    max-height: 32px;
    overflow: hidden;
}}
.thinking-label {{
    font-size: 0.75em;
    color: {c['accent']};
    margin-bottom: 4px;
    display: flex;
    align-items: center;
    gap: 6px;
}}
.thinking-label .spinner {{
    display: inline-block;
    width: 10px; height: 10px;
    border: 2px solid {c['accent']};
    border-top-color: transparent;
    border-radius: 50%;
    animation: tool-spin 0.8s linear infinite;
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
        <button onclick="showPlugins()">{s['plugins']}</button>
        <button onclick="showHistory()">{s['history']}</button>
        <button onclick="revertPrev()">{s['undo']}</button>
        <button class="danger" onclick="revertInitial()">{s['reset']}</button>
    </div>
</div>
<div id="chat"></div>
<div id="model-info">Model: <span id="model-name">loading...</span></div>
<div id="input-area">
    <textarea id="msg" placeholder="{s['placeholder']}" rows="1"
        onkeydown="handleKeyDown(event)"></textarea>
    <button id="send-btn" onclick="send()">{s['send']}</button>
</div>

<!-- History Modal -->
<div class="modal-overlay" id="history-modal">
    <div class="modal">
        <h2>{s['history_title']}</h2>
        <div class="list-area" id="history-list"></div>
        <button class="close-btn" onclick="closeModal('history-modal')">{s['close']}</button>
    </div>
</div>

<!-- Plugins Modal -->
<div class="modal-overlay" id="plugins-modal">
    <div class="modal">
        <h2>{s['plugins_title']}</h2>
        <div class="list-area" id="plugins-list"></div>
        <button class="close-btn" onclick="closeModal('plugins-modal')">{s['close']}</button>
    </div>
</div>

<script>
const S = {json.dumps(s, ensure_ascii=False)};
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

    let assistantDiv = addMessage("assistant", "");
    let fullText = "";
    let thinkingDiv = null;
    let thinkingText = "";

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
                    if (j.new_assistant) {{
                        assistantDiv = addMessage("assistant", "");
                        fullText = "";
                    }}
                    if (j.thinking_start) {{
                        // Create thinking block before the assistant div
                        const wrapper = document.createElement("div");
                        wrapper.style.alignSelf = "flex-start";
                        wrapper.style.maxWidth = "85%";
                        const label = document.createElement("div");
                        label.className = "thinking-label";
                        label.innerHTML = '<span class="spinner"></span>Thinking...';
                        label.id = "thinking-label";
                        wrapper.appendChild(label);
                        thinkingDiv = document.createElement("div");
                        thinkingDiv.className = "thinking-block";
                        thinkingDiv.onclick = function() {{ this.classList.toggle("collapsed"); }};
                        wrapper.appendChild(thinkingDiv);
                        chat.insertBefore(wrapper, assistantDiv);
                        thinkingText = "";
                    }}
                    if (j.thinking && thinkingDiv) {{
                        thinkingText += j.thinking;
                        thinkingDiv.textContent = thinkingText;
                        chat.scrollTop = chat.scrollHeight;
                    }}
                    if (j.thinking_end) {{
                        if (thinkingDiv) thinkingDiv.classList.add("collapsed");
                        const lbl = document.getElementById("thinking-label");
                        if (lbl) lbl.innerHTML = "Thinking (click to expand)";
                        thinkingDiv = null;
                    }}
                    if (j.content) {{
                        fullText += j.content;
                        scheduleRender(assistantDiv, fullText);
                    }}
                    if (j.tool_call) {{
                        const toolDiv = document.createElement("div");
                        toolDiv.className = "message tool-use";
                        toolDiv.innerHTML = '<span class="tool-icon"></span>' + j.tool_call;
                        toolDiv.id = "tool-active";
                        chat.appendChild(toolDiv);
                        chat.scrollTop = chat.scrollHeight;
                    }}
                    if (j.tool_result) {{
                        const active = document.getElementById("tool-active");
                        if (active) {{
                            active.classList.add("done");
                            active.removeAttribute("id");
                        }}
                        const resultDiv = document.createElement("div");
                        resultDiv.className = "message tool-result";
                        resultDiv.textContent = j.tool_result;
                        chat.appendChild(resultDiv);
                        chat.scrollTop = chat.scrollHeight;
                    }}
                    if (j.tool_error) {{
                        const active = document.getElementById("tool-active");
                        if (active) {{
                            active.classList.add("done");
                            active.removeAttribute("id");
                        }}
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
        list.innerHTML = '<div class="empty-msg">' + S.no_history + '</div>';
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
                <button onclick="revertToCommit('${{c.hash}}')">${{S.revert_btn}}</button>
            </div>`;
        list.appendChild(item);
    }}
    document.getElementById("history-modal").classList.add("active");
}}

async function revertInitial() {{
    if (!confirm(S.confirm_reset)) return;
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
    if (!confirm(S.confirm_revert + " (" + hash.slice(0,8) + ")")) return;
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
        list.innerHTML = '<div class="empty-msg">' + S.no_plugins + '</div>';
    }}
    for (const p of d.plugins) {{
        const item = document.createElement("div");
        item.className = "item-row";
        const checked = p.enabled ? "checked" : "";
        item.innerHTML = `
            <div class="item-info">
                <div class="item-name">${{p.name}}</div>
                <div class="item-desc">${{p.description || S.no_description}}</div>
            </div>
            <div class="item-actions">
                <label class="toggle">
                    <input type="checkbox" ${{checked}} onchange="togglePlugin('${{p.name}}', this.checked)">
                    <span class="slider"></span>
                </label>
                <button class="del" onclick="deletePlugin('${{p.name}}')">${{S.delete_btn}}</button>
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
    if (!confirm(S.confirm_delete_plugin + " (" + name + ")")) return;
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
        return JSONResponse({"message": "Invalid commit hash"}, status_code=400)
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
            results.append({"error": f"JSON parse error: {e}"})
            continue

        tool = tc.get("tool", "")
        if tool == "web_search":
            query = tc.get("query", "")
            max_results = tc.get("max_results", 5)
            if not query:
                results.append({"error": "web_search: query is required"})
            else:
                search_result = _web_search(query, max_results=max_results)
                results.append({"call": f"web_search: {query}", "result": search_result})

        elif tool == "python_run":
            code = tc.get("code", "")
            timeout = tc.get("timeout", 30)
            if not code:
                results.append({"error": "python_run: code is required"})
            else:
                run_result = _python_run(code, timeout=timeout)
                results.append({"call": "python_run", "result": run_result})

        elif tool == "file_read":
            fpath = tc.get("path", "") or tc.get("file_path", "") or tc.get("filename", "")
            if not fpath:
                results.append({"error": "file_read: path is required"})
            else:
                content = _file_read(fpath)
                results.append({"call": f"file_read: {fpath}", "result": content})

        elif tool == "file_write":
            fpath = tc.get("path", "") or tc.get("file_path", "") or tc.get("filename", "")
            content = tc.get("content", "") or tc.get("text", "")
            if not fpath:
                results.append({"error": "file_write: path is required"})
            else:
                write_result = _file_write(fpath, content)
                results.append({"call": f"file_write: {fpath}", "result": write_result})

        elif tool == "list_files":
            fpath = tc.get("path", "") or tc.get("dir", "") or tc.get("directory", "") or "."
            pattern = tc.get("pattern", "") or tc.get("glob", "") or "*"
            list_result = _list_files(fpath, pattern)
            results.append({"call": f"list_files: {fpath}", "result": list_result})

        elif tool == "rewrite_self":
            new_code = tc.get("new_code", "")
            if new_code:
                _git_commit("mubo: auto-save before rewrite_self")
                APP_FILE.write_text(new_code, encoding="utf-8")
                _git_commit("mubo: rewrite_self by agent")
                results.append({"call": "rewrite_self", "result": S["code_rewritten"], "restart": True})
            else:
                results.append({"error": "rewrite_self: new_code is empty"})

        elif tool == "create_plugin":
            pname = tc.get("name", "")
            pdesc = tc.get("description", "")
            pcode = tc.get("code", "")
            if not pname or not pcode:
                results.append({"error": "create_plugin: name and code are required"})
            else:
                plugin = {"name": pname, "description": pdesc, "code": pcode, "enabled": True}
                _save_plugin(plugin)
                results.append({"call": f"create_plugin: {pname}", "result": f"{S['plugin_created']}: {pname}"})

        elif tool == "use_plugin":
            pname = tc.get("plugin", "")
            pargs = tc.get("args", {})
            if not pname:
                results.append({"error": "use_plugin: plugin name is required"})
            else:
                result = _run_plugin(pname, pargs)
                results.append({"call": f"use_plugin: {pname}", "result": result})

        else:
            results.append({"error": f"Unknown tool: {tool}"})

    return results


@app.post("/api/chat")
async def chat_endpoint(request: Request):
    body = await request.json()
    user_messages = body.get("messages", [])
    system_prompt = _build_system_prompt()
    messages = [{"role": "system", "content": system_prompt}] + user_messages

    async def stream():
        full_response = ""
        full_thinking = ""
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream(
                    "POST",
                    f"{OLLAMA_BASE}/api/chat",
                    json={"model": MODEL, "messages": messages, "stream": True, "options": {"num_predict": -1}},
                ) as resp:
                    is_thinking = False
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            msg = data.get("message", {})
                            thinking = msg.get("thinking", "")
                            content = msg.get("content", "")

                            # Handle thinking tokens
                            if thinking:
                                full_thinking += thinking
                                if not is_thinking:
                                    is_thinking = True
                                    yield f"data: {json.dumps({'thinking_start': True})}\n\n"
                                yield f"data: {json.dumps({'thinking': thinking})}\n\n"

                            # Handle content tokens
                            if content:
                                if is_thinking:
                                    is_thinking = False
                                    yield f"data: {json.dumps({'thinking_end': True})}\n\n"
                                full_response += content
                                yield f"data: {json.dumps({'content': content})}\n\n"
                        except json.JSONDecodeError:
                            continue

            # If model only produced thinking but no content, extract intent and act
            if not full_response.strip() and full_thinking.strip():
                if is_thinking:
                    yield f"data: {json.dumps({'thinking_end': True})}\n\n"
                # Use a smaller, focused prompt to extract tool calls from thinking
                extract_messages = [
                    {"role": "system", "content": (
                        "You are a tool-call extractor. The user's AI assistant thought about a task but failed to output tool calls. "
                        "Based on the thinking content, output the appropriate ```tool_call``` blocks. "
                        "Available tools: web_search, python_run, file_read, file_write, list_files. "
                        "Output ONLY the tool_call blocks, no other text. Example:\n"
                        "```tool_call\n{\"tool\": \"python_run\", \"code\": \"print('hello')\"}\n```"
                    )},
                    {"role": "user", "content": f"Original request: {user_messages[-1].get('content', '') if user_messages else ''}\n\nAssistant's thinking:\n{full_thinking[:2000]}"},
                ]
                async with httpx.AsyncClient(timeout=300.0) as client_retry:
                    async with client_retry.stream(
                        "POST",
                        f"{OLLAMA_BASE}/api/chat",
                        json={"model": MODEL, "messages": extract_messages, "stream": True, "options": {"num_predict": -1}},
                    ) as resp_retry:
                        async for line_r in resp_retry.aiter_lines():
                            if not line_r:
                                continue
                            try:
                                data_r = json.loads(line_r)
                                msg_r = data_r.get("message", {})
                                content_r = msg_r.get("content", "")
                                if content_r:
                                    full_response += content_r
                                    yield f"data: {json.dumps({'content': content_r})}\n\n"
                            except json.JSONDecodeError:
                                continue

            if "```tool_call" in full_response:
                results = _process_tool_calls(full_response)
                need_restart = False
                tool_outputs = []  # Collect results that need LLM follow-up
                for r in results:
                    if "call" in r:
                        yield f"data: {json.dumps({'tool_call': r['call']})}\n\n"
                        yield f"data: {json.dumps({'tool_result': r['result']})}\n\n"
                        # Tools whose results need LLM follow-up
                        call_name = r["call"]
                        followup_tools = ("web_search:", "python_run", "file_read:", "list_files:")
                        if any(call_name.startswith(t) or call_name == t for t in followup_tools):
                            tool_outputs.append({"tool": call_name, "output": r["result"]})
                        if r.get("restart"):
                            need_restart = True
                    if "error" in r:
                        yield f"data: {json.dumps({'tool_error': r['error']})}\n\n"

                # Feed tool outputs back to the LLM for a synthesized response
                if tool_outputs:
                    tool_context = "\n\n---\n\n".join(
                        f"[{t['tool']}]\n{t['output']}" for t in tool_outputs
                    )
                    followup_messages = messages + [
                        {"role": "assistant", "content": full_response},
                        {"role": "user", "content": f"Here are the tool execution results. Based on these results, provide an accurate and helpful answer to the user's original question.\n\n{tool_context}"},
                    ]
                    # Signal frontend to start a new assistant message
                    yield f"data: {json.dumps({'new_assistant': True})}\n\n"
                    async with httpx.AsyncClient(timeout=300.0) as client2:
                        async with client2.stream(
                            "POST",
                            f"{OLLAMA_BASE}/api/chat",
                            json={"model": MODEL, "messages": followup_messages, "stream": True, "options": {"num_predict": -1}},
                        ) as resp2:
                            async for line2 in resp2.aiter_lines():
                                if not line2:
                                    continue
                                try:
                                    data2 = json.loads(line2)
                                    content2 = data2.get("message", {}).get("content", "")
                                    if content2:
                                        yield f"data: {json.dumps({'content': content2})}\n\n"
                                except json.JSONDecodeError:
                                    continue

                if need_restart:
                    _restart_server()

            yield "data: [DONE]\n\n"
        except httpx.ConnectError:
            yield f"data: {json.dumps({'content': S['error_ollama']})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            err_prefix = S["error_prefix"]
            yield f"data: {json.dumps({'content': f'{err_prefix}: {str(e)}'})}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("MUBO_PORT", "8392"))
    print(f"\n  Mubo Agent starting on http://localhost:{port}\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
