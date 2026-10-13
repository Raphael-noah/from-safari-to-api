#!/usr/bin/env python3
"""
check.applescript -> HTTP API bridge.

Wraps the Safari browser-automation script (check.applescript) behind a small
FastAPI server so any HTTP client can submit a prompt and read back the page's
answer as JSON.

The heavy lifting stays inside check.applescript: it drives Safari's current
tab, injects the prompt into the page, submits it, polls for the reply, and
prints the final text to stdout. This server only:

  1. receives an HTTP request,
  2. shells out to `osascript check.applescript ...`,
  3. parses check.applescript's stdout protocol (first line `OK:` / `ERR:`),
  4. maps success/errors to HTTP responses.

Escaping responsibility:
  * Python -> osascript is an argv boundary (subprocess list, no shell), so the
    prompt is passed through verbatim — no folding, no shell-escaping.
  * check.applescript -> JS source is the string-escaping boundary; the script
    escapes backslash, quotes, and control chars itself.

Because Safari can only drive one frontmost tab at a time, all calls are
serialized with a lock. Routes are synchronous `def` so FastAPI runs them in a
threadpool and the event loop never blocks (e.g. /health stays responsive).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Configuration (environment variables, with inline fallbacks)
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent

APPLESCRIPT_PATH = Path(
    os.environ.get("APPLESCRIPT_PATH", str(BASE_DIR / "check.applescript"))
).resolve()

# Default selectors used when a request does not override them. Set these to
# the actual CSS selectors of your target page (e.g. chat.deepseek.com).
DEFAULT_INPUT_SELECTOR = os.environ.get("INPUT_SELECTOR", "")
DEFAULT_RESULT_SELECTOR = os.environ.get("RESULT_SELECTOR", "")
DEFAULT_BUTTON_SELECTOR = os.environ.get("BUTTON_SELECTOR", "")  # empty = press Enter

# check.applescript polls up to POLL_MAX x POLL_INTERVAL seconds internally. The
# subprocess timeout must exceed that window (plus osascript startup and per-poll
# `do JavaScript` round-trips), otherwise slow generations get SIGKILLed mid-poll
# and the tool loop sees a spurious 504.
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "3"))
POLL_MAX = int(os.environ.get("POLL_MAX", "60"))
OSASCRIPT_TIMEOUT = int(
    os.environ.get("OSASCRIPT_TIMEOUT", str(POLL_MAX * POLL_INTERVAL + 30))
)

# Optional CSS selector for the DeepSeek "继续"/continue button. Empty (default)
# means auto-detect buttons whose text is exactly 继续/继续生成/继续回答/continue.
CONTINUE_SELECTOR = os.environ.get("CONTINUE_SELECTOR", "").strip()

# argv cannot carry NUL bytes and very long prompts hit argv-size limits.
# The tool-calling prompt includes system + tool schemas + history, so the
# limit is generous.
MAX_PROMPT_LENGTH = int(os.environ.get("MAX_PROMPT_LENGTH", "60000"))

# Text tool-call protocol delimiters (see README).
TOOL_CALL_OPEN = os.environ.get("TOOL_CALL_OPEN", "<tool_call>")
TOOL_CALL_CLOSE = os.environ.get("TOOL_CALL_CLOSE", "</tool_call>")
TOOL_RESULT_OPEN = os.environ.get("TOOL_RESULT_OPEN", "<tool_result>")
TOOL_RESULT_CLOSE = os.environ.get("TOOL_RESULT_CLOSE", "</tool_result>")
SYSTEM_MAX_CHARS = int(os.environ.get("SYSTEM_MAX_CHARS", "1200"))
TOOL_DESC_MAX_CHARS = int(os.environ.get("TOOL_DESC_MAX_CHARS", "300"))

# Progressive disclosure: expose only a relevant subset of tools in the preamble.
#   '' (default) -> DEFAULT_TOOL_WHITELIST below
#   '*'          -> expose every tool the client sends
#   'Bash,Read'  -> expose exactly these
TOOL_WHITELIST = os.environ.get("TOOL_WHITELIST", "").strip()
DEFAULT_TOOL_WHITELIST = [
    "Bash", "Read", "Glob", "Grep", "Write", "Edit", "WebFetch", "WebSearch",
]

# Progressive disclosure: on a fresh turn, send only the user's request first and
# disclose the tool list only after the model signals it needs a tool. Set to
# 'false' to always inject tools upfront (single pass).
LAZY_TOOLS = os.environ.get("LAZY_TOOLS", "true").lower() not in ("0", "false", "no")
INCLUDE_SYSTEM = os.environ.get("INCLUDE_SYSTEM", "true").lower() not in ("0", "false", "no")

# Logging. LOG_REQUESTS (default on) prints one concise line per /v1/messages
# call to stderr; LOG_PROMPTS=1 additionally dumps the full injected prompt and
# the raw page response, which is what you want when debugging tool-calling.
LOG_REQUESTS = os.environ.get("LOG_REQUESTS", "true").lower() not in ("0", "false", "no")
LOG_PROMPTS = os.environ.get("LOG_PROMPTS", "").lower() in ("1", "true", "yes", "on")

# Model names advertised by GET /v1/models. Purely cosmetic: the server
# ignores the model field and echoes it back. MODELS env (comma-separated)
# overrides; default is a joke set.
DEFAULT_MODELS = ['safari-114514', 'I_am_a_teapot', 'IAmAnEsteregg',
 'definitely-not-gpt-5', 'safari-pro-max-ultra',
 'claude-opus-5-5-but-safari', 'not-a-real-model']
MODELS = [m.strip() for m in os.environ.get('MODELS', '').split(',') if m.strip()] or DEFAULT_MODELS

# A single Safari window can only be driven one request at a time.
_call_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Core: invoke check.applescript and parse its protocol
# --------------------------------------------------------------------------- #

def _validate_prompt(prompt: str) -> str:
    if "\x00" in prompt:
        raise HTTPException(status_code=400, detail="prompt contains a NUL byte")
    if len(prompt) > MAX_PROMPT_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"prompt too long (>{MAX_PROMPT_LENGTH} chars)",
        )
    return prompt


def _applescript_args(
    prompt: str,
    input_selector: str,
    result_selector: str,
    button_selector: str,
    continue_selector: str,
) -> list[str]:
    """Build the osascript argv (list form, no shell)."""
    args = [
        "osascript",
        str(APPLESCRIPT_PATH),
        "-p", prompt,
        "-i", input_selector,
        "-r", result_selector,
        "--poll-interval", str(POLL_INTERVAL),
        "--poll-max", str(POLL_MAX),
    ]
    if button_selector:
        args += ["-b", button_selector]
    if continue_selector:
        args += ["-c", continue_selector]
    return args


def _run_applescript(
    prompt: str,
    input_selector: str,
    result_selector: str,
    button_selector: str,
) -> str:
    """Run check.applescript and return its final text, raising HTTPException on error."""
    args = _applescript_args(
        prompt, input_selector, result_selector, button_selector, CONTINUE_SELECTOR
    )

    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=OSASCRIPT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail=f"osascript timed out after {OSASCRIPT_TIMEOUT}s",
        )

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "unknown osascript error").strip()
        raise HTTPException(status_code=502, detail=f"osascript failed: {detail}")

    raw = proc.stdout or ""
    first_line, sep, rest = raw.partition("\n")
    marker = first_line.rstrip("\r")

    if marker == "OK:":
        result = rest.rstrip("\r\n")
        if not result:
            raise HTTPException(
                status_code=502,
                detail="check.applescript returned an empty result",
            )
        return result

    if marker == "ERR:":
        detail = rest.strip() or "unknown error"
        raise HTTPException(status_code=502, detail=detail)

    raise HTTPException(
        status_code=502,
        detail=f"unexpected output from check.applescript: {raw.strip()[:500]}",
    )


def _resolve_selectors(
    input_selector: Optional[str],
    result_selector: Optional[str],
    button_selector: Optional[str],
) -> tuple[str, str, str]:
    """Merge per-request selector overrides with environment defaults."""
    input_sel = (input_selector or DEFAULT_INPUT_SELECTOR).strip()
    result_sel = (result_selector or DEFAULT_RESULT_SELECTOR).strip()
    button_sel = (
        button_selector if button_selector is not None else DEFAULT_BUTTON_SELECTOR
    ).strip()

    missing = []
    if not input_sel:
        missing.append("INPUT_SELECTOR")
    if not result_sel:
        missing.append("RESULT_SELECTOR")
    if missing:
        raise HTTPException(
            status_code=400,
            detail=(
                "Selectors not configured. Set "
                + ", ".join(missing)
                + " (env var or per-request field)."
            ),
        )
    return input_sel, result_sel, button_sel


def _chat(
    prompt: str,
    input_selector: Optional[str],
    result_selector: Optional[str],
    button_selector: Optional[str],
) -> dict:
    prompt = _validate_prompt(prompt)
    input_sel, result_sel, button_sel = _resolve_selectors(
        input_selector, result_selector, button_selector
    )

    started = time.time()
    with _call_lock:
        text = _run_applescript(prompt, input_sel, result_sel, button_sel)
    elapsed = round(time.time() - started, 3)

    return {
        "response": text,
        "elapsed_seconds": elapsed,
        "selectors": {
            "input": input_sel,
            "result": result_sel,
            "button": button_sel or None,
        },
    }


# --------------------------------------------------------------------------- #
# FastAPI app + models
# --------------------------------------------------------------------------- #

app = FastAPI(
    title="from-safari-to-api",
    description="HTTP API over the check.applescript Safari automation script.",
    version="1.0.0",
)


class ChatRequest(BaseModel):
    prompt: str = Field(..., description="The prompt to paste into the page.")
    input_selector: Optional[str] = None
    result_selector: Optional[str] = None
    button_selector: Optional[str] = None


class ChatResponse(BaseModel):
    response: str
    elapsed_seconds: float
    selectors: dict


class ChatCompletionRequest(BaseModel):
    model: str = "safari"
    messages: list[dict]
    temperature: Optional[float] = None
    stream: Optional[bool] = False


class AnthropicMessage(BaseModel):
    role: str
    content: str | list[dict] | None = None


class AnthropicMessagesRequest(BaseModel):
    """Anthropic Messages API request (POST /v1/messages)."""

    model: str = "safari"
    max_tokens: Optional[int] = 1024
    messages: list[AnthropicMessage]
    system: str | list[dict] | None = None
    tools: Optional[list[dict]] = None
    stream: Optional[bool] = False
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    stop_sequences: Optional[list[str]] = None
    metadata: Optional[dict] = None


@app.get("/health")
def health() -> dict:
    """Liveness + readiness check. Never touches the Safari lock."""
    applescript_ok = APPLESCRIPT_PATH.exists()
    osascript_ok = True
    if applescript_ok:
        try:
            subprocess.run(
                ["osascript", str(APPLESCRIPT_PATH), "-h"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            osascript_ok = False

    return {
        "status": "ok" if (applescript_ok and osascript_ok) else "degraded",
        "applescript_path": str(APPLESCRIPT_PATH),
        "applescript_exists": applescript_ok,
        "osascript_runnable": osascript_ok,
        "selectors_configured": bool(DEFAULT_INPUT_SELECTOR and DEFAULT_RESULT_SELECTOR),
        "input_selector": DEFAULT_INPUT_SELECTOR or None,
        "result_selector": DEFAULT_RESULT_SELECTOR or None,
        "button_selector": DEFAULT_BUTTON_SELECTOR or None,
    }


def _model_entry(model_id: str) -> dict:
 return {
 'id': model_id,
 'object': 'model',
 'type': 'model',
 'created': 1700000000,
 'created_at': '2026-10-13T00:00:00Z',
 'owned_by': 'safari',
 'display_name': model_id,
 }


@app.get('/v1/models')
def list_models() -> dict:
 return {
 'object': 'list',
 'data': [_model_entry(m) for m in MODELS],
 'has_more': False,
 'first_id': MODELS[0] if MODELS else None,
 'last_id': MODELS[-1] if MODELS else None,
 }


@app.get('/v1/models/{model_id}')
def get_model(model_id: str) -> dict:
 return _model_entry(model_id)


@app.api_route("/api/hello", methods=["GET", "HEAD"])
def api_hello() -> dict:
    """Health check used by Anthropic-style clients (ccswitch / Claude Code)."""
    return {"message": "hello"}
@app.get('/user/balance')
@app.get('/v1/user/balance')
def user_balance() -> dict:
    """DeepSeek-style balance endpoint (cc-switch 读 is_active + balance)。
    这服务扒网页不要钱，所以余额是装出来的。"""
    return {
        'is_active': True,
        'is_available': True,
        'balance': 999999999.0,
        'currency': 'USD',
        'unit': 'USD',
        'note': '都这样了你还是想配余额查询吗，苦命的孩子啊',
        'balance_infos': [{'currency': 'USD', 'total_balance': '999999999.0'}],
    }


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> dict:
    """Simple endpoint: {prompt} -> {response}."""
    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt must not be empty")
    return _chat(req.prompt, req.input_selector, req.result_selector, req.button_selector)


def _messages_to_prompt(messages: list[dict]) -> str:
    """Extract the latest user message from an OpenAI-style messages list."""
    if not messages:
        raise HTTPException(status_code=400, detail="messages must not be empty")
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content", "")
            if isinstance(content, str):
                return content
            # content may be a list of parts, e.g. [{"type": "text", "text": "..."}]
            if isinstance(content, list):
                parts = [
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                ]
                return "".join(parts)
    raise HTTPException(status_code=400, detail="no user message found")


@app.post("/v1/chat/completions")
def chat_completions(req: ChatCompletionRequest) -> dict:
    """OpenAI-compatible endpoint so this can be dropped into existing tools."""
    if req.stream:
        raise HTTPException(
            status_code=400,
            detail="streaming is not supported; use stream=false",
        )

    prompt = _messages_to_prompt(req.messages)
    if not prompt.strip():
        raise HTTPException(status_code=400, detail="prompt must not be empty")

    result = _chat(prompt, None, None, None)
    model = req.model or "safari"

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result["response"]},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _content_text(content) -> str:
    """Return the plain-text portion of a message content (str or block list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _render_content_blocks(content, role: str, id_to_name: dict) -> str:
    """Render message content (str or list of blocks) into a text transcript."""
    if isinstance(content, str):
        return content

    lines = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                lines.append(block.get("text", ""))
            elif btype == "tool_use":
                payload = {"name": block.get("name", ""), "arguments": block.get("input", {})}
                id_to_name[block.get("id", "")] = block.get("name", "")
                lines.append(
                    f"{TOOL_CALL_OPEN}\n{json.dumps(payload, ensure_ascii=False)}\n{TOOL_CALL_CLOSE}"
                )
            elif btype == "tool_result":
                name = id_to_name.get(block.get("tool_use_id", ""), "")
                inner = _content_text(block.get("content", ""))
                # TOOL_RESULT_OPEN ends with ">"; insert a name attribute when known.
                open_tag = f'{TOOL_RESULT_OPEN[:-1]} name="{name}">' if name else TOOL_RESULT_OPEN
                lines.append(f"{open_tag}\n{inner}\n{TOOL_RESULT_CLOSE}")
            # ignore thinking / image / other blocks
    return "\n".join(lines)


def _compact_tool_schema(schema) -> str:
    """Return just the top-level property names of a JSON schema, comma-separated.

    The full schema is far too verbose to paste into a chat prompt (it floods a
    web model), but the parameter NAMES are what the model needs to emit a valid
    tool call. e.g. {'properties': {'file_path':…, 'offset':…}} -> 'file_path, offset'.
    """
    if isinstance(schema, dict):
        props = schema.get("properties")
        if isinstance(props, dict) and props:
            return ", ".join(str(k) for k in props.keys())
    return ""


def _render_tool_schema(schema) -> str:
    """Render a compact per-parameter schema: name:type + required flag + blurb.

    Used in round 3 of progressive disclosure, where only the chosen tools are
    shown, so the model gets types + descriptions (not just names) and emits
    arguments with the right JSON types.
    """
    if not isinstance(schema, dict):
        return ""
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return ""
    required = set(schema.get("required") or [])
    lines = []
    for name, prop in props.items():
        if not isinstance(prop, dict):
            continue
        ptype = prop.get("type", "")
        desc = (prop.get("description") or "").strip().replace("\n", " ")
        if len(desc) > 80:
            desc = desc[:80] + "…"
        text = f"{name}:{ptype}" if ptype else name
        if name in required:
            text += "（必填）"
        if desc:
            text += f" — {desc}"
        lines.append(text)
    return "\n".join(lines)


def _filter_tools(tools) -> list:
    """Progressive disclosure: keep only the whitelisted (relevant) tools."""
    if not tools:
        return []
    if TOOL_WHITELIST == "*":
        return [t for t in tools if isinstance(t, dict)]
    names = (
        {n.strip() for n in TOOL_WHITELIST.split(",") if n.strip()}
        if TOOL_WHITELIST
        else set(DEFAULT_TOOL_WHITELIST)
    )
    return [t for t in tools if isinstance(t, dict) and t.get("name") in names]


def _render_system_block(system) -> str | None:
    """Render the system prompt (billing header stripped, truncated) or None."""
    if not INCLUDE_SYSTEM:
        return None
    sys_text = _content_text(system) if system is not None else ""
    if not sys_text:
        return None
    # ccswitch prepends a billing header; strip it — it is not prompt content.
    sys_text = re.sub(r"^x-anthropic-billing-header:(?:[^;]*;)+", "", sys_text)
    if len(sys_text) > SYSTEM_MAX_CHARS:
        sys_text = sys_text[:SYSTEM_MAX_CHARS] + "…(截断)"
    return f"<system>\n{sys_text}\n</system>"


def _render_tool_list(tools, schema_detail: str = "names") -> str | None:
    """Render the tool menu as text.

    schema_detail: 'none' (name + description only) | 'names' (also param names)
    | 'full' (per-parameter type/required/description).
    """
    tools = _filter_tools(tools)
    if not tools:
        return None
    lines = ["可用工具："]
    for i, tool in enumerate(tools, 1):
        if not isinstance(tool, dict):
            continue
        name = tool.get("name", f"tool_{i}")
        desc = (tool.get("description") or "").strip().replace("\n", " ")
        if len(desc) > TOOL_DESC_MAX_CHARS:
            desc = desc[:TOOL_DESC_MAX_CHARS] + "…"
        line = f"{i}. {name} — {desc}"
        if schema_detail == "names":
            keys = _compact_tool_schema(tool.get("input_schema"))
            if keys:
                line += f"（参数名: {keys}）"
        elif schema_detail == "full":
            schema_text = _render_tool_schema(tool.get("input_schema"))
            if schema_text:
                line += "\n     " + schema_text.replace("\n", "\n     ")
        lines.append(line)
    return "\n".join(lines)


def _tool_convention() -> str:
    """The tool-call convention block (shared by the fresh and round-3 paths)."""
    return (
        "调用约定（必须严格遵守）：\n"
        "禁止使用任何 DSML / 标记语言格式——包括用 ｜DSML｜ 包裹的标签（如 "
        "<｜DSML｜invoke name=\"…\">、<｜DSML｜parameter …>、<｜DSML｜function_calls>、"
        "<｜DSML｜tool_calls>），也禁止 <invoke>、<parameter>、<function_call> 等 XML/HTML 标签。"
        "DSML 篇幅冗长，很容易被截断导致调用失败。\n"
        "只允许使用以下纯 JSON 格式（不要任何外层包裹）：\n"
        "1. 当需要读取文件、列出目录、执行命令、搜索内容等操作时，禁止用自然语言描述你要做什么，"
        "禁止输出任何解释、寒暄或「我来读取…」之类的话。\n"
        f"2. 你只能在回复中直接输出一个或多个结构化的工具调用块，格式如下（JSON 必须合法，"
        f"字段名固定为英文 name 和 arguments）：\n"
        f"{TOOL_CALL_OPEN}\n"
        '{"name":"Bash","arguments":{"command":"ls -la"}}\n'
        f"{TOOL_CALL_CLOSE}\n"
        f"{TOOL_CALL_OPEN}\n"
        '{"name":"Read","arguments":{"file_path":"/Users/admin/Desktop/Projects/from-safari-to-api/server.py"}}\n'
        f"{TOOL_CALL_CLOSE}\n"
        f"3. 系统执行后会返回 {TOOL_RESULT_OPEN} name=\"…\">…{TOOL_RESULT_CLOSE}，你根据其中的结果决定"
        "是否继续调用下一个工具。\n"
        f"4. 只有当任务彻底完成、不需要再调用任何工具时，才输出纯文本最终答案；"
        f"此时绝对不要再输出 {TOOL_CALL_OPEN} 或任何 DSML 标签。"
    )


def _render_preamble(system, tools, schema_detail: str = "names") -> str:
    """Render system prompt + tool definitions + call convention as text."""
    parts = []
    sys_block = _render_system_block(system)
    if sys_block:
        parts.append(sys_block)
    tool_list = _render_tool_list(tools, schema_detail)
    if tool_list:
        parts.append(tool_list)
    parts.append(_tool_convention())
    return "\n\n".join(parts)


def _render_tool_menu(system, tools) -> str:
    """Round 2 of progressive disclosure: names + one-line descriptions only,
    then ask which tools are needed (no schema, no convention)."""
    parts = []
    sys_block = _render_system_block(system)
    if sys_block:
        parts.append(sys_block)
    tool_list = _render_tool_list(tools, "none")
    if tool_list:
        parts.append(tool_list)
    parts.append(
        "请选出完成上面任务所需的工具，只回复工具名（可多个，用逗号分隔，"
        "例如：Bash, Read），不要输出任何其它文字。"
    )
    return "\n\n".join(parts)


def _parse_tool_names(text: str, tools: list) -> list[str]:
    """Extract which whitelisted tools the model named in `text` (in order)."""
    names = [t.get("name") for t in tools if isinstance(t, dict) and t.get("name")]
    found = []
    for name in names:
        # word-boundary-ish match so "Read" doesn't hit "Reading"
        m = re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", text)
        if m:
            found.append((m.start(), name))
    found.sort()
    return [name for _, name in found]


def _select_tools(tools, chosen: list[str]) -> list:
    """Narrow `tools` to the chosen names; fall back to all tools if none match."""
    tools = _filter_tools(tools)
    if not chosen:
        return tools
    chosen_set = set(chosen)
    selected = [t for t in tools if t.get("name") in chosen_set]
    return selected or tools


def _has_tool_blocks(messages: list[AnthropicMessage]) -> bool:
    """True if any message contains a tool_use or tool_result block."""
    for msg in messages:
        if isinstance(msg.content, list):
            for block in msg.content:
                if isinstance(block, dict) and block.get("type") in ("tool_use", "tool_result"):
                    return True
    return False


def _rendered_last_user(req: AnthropicMessagesRequest) -> tuple[str, dict]:
    """Return (rendered latest user message, tool_use_id -> name map)."""
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages must not be empty")

    id_to_name: dict = {}
    for msg in req.messages:
        if isinstance(msg.content, list):
            for block in msg.content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    id_to_name[block.get("id", "")] = block.get("name", "")

    last_user = None
    for msg in reversed(req.messages):
        if msg.role == "user":
            last_user = msg
            break
    if last_user is None:
        raise HTTPException(status_code=400, detail="no user message found")

    return _render_content_blocks(last_user.content, last_user.role, id_to_name), id_to_name


def _build_inject_prompt(req: AnthropicMessagesRequest) -> str:
    """Build the single text prompt to paste into the page for this turn."""
    rendered_user, _ = _rendered_last_user(req)

    if _has_tool_blocks(req.messages):
        # Continuation: only the latest tool_results; the page already has context.
        prompt = rendered_user
    else:
        # Fresh conversation: inject system + tools + convention + the user text.
        prompt = _render_preamble(req.system, req.tools) + "\n\n" + rendered_user

    # Hard safety net: keep the total under the argv-safe limit. Components are
    # already truncated individually, so this only bites in pathological cases.
    if len(prompt) > MAX_PROMPT_LENGTH:
        prompt = prompt[: MAX_PROMPT_LENGTH - 12] + "…(截断)"
    return prompt


def _lazy_tool_flow(req: AnthropicMessagesRequest) -> tuple[str, str]:
    """Progressive disclosure over three page round-trips.

    Round 1 sends only the user's request and asks whether a tool is needed.
    Round 2 discloses tool NAMES + one-line descriptions (no schema) and asks
    which tools to use. Round 3 discloses the full schema + call convention for
    only the chosen tools and asks for the structured call. Returns
    (final_text, mode) where mode is 'answer' or 'disclosed'.
    """
    rendered_user, _ = _rendered_last_user(req)
    tools = _filter_tools(req.tools)

    probe = (
        rendered_user
        + "\n\n"
        + "如果完成这个任务需要读取文件、执行命令、搜索或访问网络，"
        + "请只回复四个字：需要工具。否则直接给出最终答案。"
    )
    r1 = _chat(probe, None, None, None)["response"]
    if LOG_PROMPTS:
        print(f"--- [lazy round1 probe] ---\n{probe}\n--- [r1] ---\n{r1}", file=sys.stderr, flush=True)

    if "需要工具" not in r1 or not tools:
        return r1, "answer"

    # Round 2: names only → which tools?
    menu = _render_tool_menu(req.system, tools)
    r2 = _chat(menu, None, None, None)["response"]
    if LOG_PROMPTS:
        print(f"--- [lazy round2 menu] ---\n{menu}\n--- [r2] ---\n{r2}", file=sys.stderr, flush=True)

    # If the model already emitted full calls here, use them and skip round 3.
    already = _parse_tool_calls(r2)
    if already:
        return r2, "disclosed"

    # Round 3: full schema + convention for only the chosen tools.
    chosen = _parse_tool_names(r2, tools)
    selected = _select_tools(tools, chosen)
    disclose = (
        _render_preamble(req.system, selected, schema_detail="full")
        + "\n\n现在请为上面的任务输出工具调用：只输出一个或多个 "
        + f"{TOOL_CALL_OPEN}…{TOOL_CALL_CLOSE} 结构，不要输出任何解释或 DSML 标签。"
    )
    r3 = _chat(disclose, None, None, None)["response"]
    if LOG_PROMPTS:
        print(f"--- [lazy round3 disclose] ---\n{disclose}\n--- [r3] ---\n{r3}", file=sys.stderr, flush=True)
    return r3, "disclosed"


# --------------------------------------------------------------------------- #
# DeepSeek DSML tool-call parsing
# --------------------------------------------------------------------------- #
# DeepSeek's web model defaults to its own DSML (DeepSeek Markup Language) for
# tool calls even when the preamble tells it to emit <tool_call>{json}</tool_call>.
# DSML wraps every tag in the full-width-pipe token "｜DSML｜" and puts each
# argument in its own <parameter> (rather than one "arguments" JSON blob).
# Three dialects exist (from the DeepSeek / vLLM / sglang reference parsers):
#   V3.2  -> <｜DSML｜function_calls>  <｜DSML｜invoke name="…">   <｜DSML｜parameter …>
#   V4    -> <｜DSML｜tool_calls>      (same invoke/parameter)
#   V4.1  -> <｜DSML｜ calls>          <｜DSML｜ invoke …>          <｜DSML｜ parameter …>
#            (a leading space inside each tag; also allows block-less invokes)
# string="true" -> value is a raw string; string="false" -> value is JSON
# (number/bool/array/object), falling back to the raw text if it does not parse.
DSML_TOKEN = "｜DSML｜"
DSML_EOS = "<｜end▁of▁sentence｜>"

_DSML_INVOKE_RE = re.compile(
    r"<" + DSML_TOKEN + r"\s?invoke\s+name=\"([^\"]*)\"\s*>(.*?)</"
    + DSML_TOKEN + r"\s?invoke\s*>",
    re.DOTALL,
)
_DSML_PARAM_RE = re.compile(
    r"<" + DSML_TOKEN + r"\s?parameter\s+name=\"([^\"]+)\""
    r"(?:\s+string=\"(true|false)\")?\s*>(.*?)</"
    + DSML_TOKEN + r"\s?parameter\s*>",
    re.DOTALL,
)


def _dsml_argument(raw: str, is_string: bool) -> object:
    """string="true" -> raw string; string="false" -> parsed JSON (else raw)."""
    raw = raw.replace(DSML_EOS, "").strip()
    if is_string:
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _dsml_invoke_to_call(inv) -> dict | None:
    """Turn one DSML <invoke> regex match into {name, arguments} (or None)."""
    name = (inv.group(1) or "").strip()
    if not name:
        return None
    body = inv.group(2).replace(DSML_EOS, "")
    args: dict = {}

    # Some engines emit the whole arguments object as direct JSON instead of
    # individual <parameter> tags.
    stripped = body.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            args = parsed
        elif parsed is not None:
            args = {"value": parsed}
    else:
        for param in _DSML_PARAM_RE.finditer(body):
            # absent string= attr defaults to a raw string
            args[param.group(1)] = _dsml_argument(param.group(3), param.group(2) != "false")

    return {"name": name, "arguments": args}


def _parse_dsml_tool_calls(text: str) -> list[dict]:
    """Parse DeepSeek's native DSML (｜DSML｜) markup into [{name, arguments}]."""
    return [c for c in (_dsml_invoke_to_call(m) for m in _DSML_INVOKE_RE.finditer(text)) if c]


def _parse_tool_calls(text: str) -> list[dict]:
    """Extract every tool call — <tool_call>{json}</tool_call> and DeepSeek DSML —
    merged in document order, so a reply that mixes both formats loses nothing."""
    entries: list[tuple[int, dict]] = []

    # 1. Our <tool_call>{json}</tool_call> format.
    json_pattern = re.compile(
        re.escape(TOOL_CALL_OPEN) + r"\s*(\{.*?\})\s*" + re.escape(TOOL_CALL_CLOSE),
        re.DOTALL,
    )
    for match in json_pattern.finditer(text):
        try:
            data = json.loads(match.group(1))
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        name = data.get("name")
        arguments = data.get("arguments", {})
        if not isinstance(name, str) or not name:
            continue
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {"value": arguments}
        entries.append((match.start(), {"name": name, "arguments": arguments}))

    # 2. DeepSeek's native DSML (｜DSML｜) markup.
    for match in _DSML_INVOKE_RE.finditer(text):
        call = _dsml_invoke_to_call(match)
        if call is not None:
            entries.append((match.start(), call))

    if entries:
        entries.sort(key=lambda e: e[0])
        return [call for _, call in entries]

    # 3. Fallback: a bare XML <invoke> variant some models emit (no DSML token,
    #    a single "arguments" JSON parameter instead of per-argument parameters).
    #      <invoke name="Bash">
    #        <parameter name="arguments" string="false">{"command":"ls"}</parameter>
    #      </invoke>
    invoke_pattern = re.compile(
        r'<invoke\s+name="([^"]+)"[^>]*>\s*(.*?)\s*</invoke>',
        re.DOTALL,
    )
    calls = []
    for match in invoke_pattern.finditer(text):
        name = match.group(1)
        inner = match.group(2)
        param_match = re.search(
            r'<parameter\s+name="arguments"[^>]*>(.*?)</parameter>',
            inner,
            re.DOTALL,
        )
        args: dict = {}
        if param_match:
            try:
                parsed = json.loads(param_match.group(1))
            except ValueError:
                parsed = param_match.group(1)
            args = parsed if isinstance(parsed, dict) else {"value": parsed}
        if name:
            calls.append({"name": name, "arguments": args})

    return calls


def _anthropic_content(text: str, tool_calls: list[dict]) -> tuple[list[dict], str]:
    """Build (content, stop_reason) for an Anthropic response."""
    if tool_calls:
        content = [
            {
                "type": "tool_use",
                "id": f"toolu_{uuid.uuid4().hex}",
                "name": call["name"],
                "input": call["arguments"],
            }
            for call in tool_calls
        ]
        return content, "tool_use"
    return [{"type": "text", "text": text}], "end_turn"


def _anthropic_sse(text: str, model: str, message_id: str, tool_calls: list[dict]):
    """Yield a valid Anthropic SSE stream (text or tool_use, one delta each)."""

    def gen():
        def evt(name: str, data: dict) -> str:
            return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        yield evt("message_start", {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })

        if tool_calls:
            for i, call in enumerate(tool_calls):
                yield evt("content_block_start", {
                    "type": "content_block_start",
                    "index": i,
                    "content_block": {
                        "type": "tool_use",
                        "id": f"toolu_{uuid.uuid4().hex}",
                        "name": call["name"],
                        "input": {},
                    },
                })
                yield evt("content_block_delta", {
                    "type": "content_block_delta",
                    "index": i,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(call["arguments"], ensure_ascii=False),
                    },
                })
                yield evt("content_block_stop", {"type": "content_block_stop", "index": i})
            stop_reason = "tool_use"
        else:
            yield evt("content_block_start", {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            })
            yield evt("content_block_delta", {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            })
            yield evt("content_block_stop", {"type": "content_block_stop", "index": 0})
            stop_reason = "end_turn"

        yield evt("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": 0},
        })
        yield evt("message_stop", {"type": "message_stop"})

    return gen()


def _latest_user_text(req: AnthropicMessagesRequest) -> str:
    """Return the plain text of the latest user message ('' if none)."""
    for msg in reversed(req.messages):
        if msg.role == "user":
            return _content_text(msg.content)
    return ""


def _is_title_request(text: str) -> bool:
    """Claude Code's session-title generation: <session>…</session> + 'Write the title'."""
    return "<session>" in text and "Write the title" in text


def _extract_title(text: str) -> str:
    """Pull a short title out of the <session> body (first line, ≤60 chars)."""
    m = re.search(r"<session>\s*(.*?)\s*</session>", text, re.DOTALL)
    inner = m.group(1).strip() if m else text.strip()
    if not inner:
        return "会话"
    return inner.splitlines()[0].strip()[:60] or "会话"


def _respond(text: str, model: str, message_id: str, stream: bool):
    """Build the final Anthropic response (streaming SSE or JSON) for a text answer."""
    tool_calls = _parse_tool_calls(text)
    content, stop_reason = _anthropic_content(text, tool_calls)

    if stream:
        return StreamingResponse(
            _anthropic_sse(text, model, message_id, tool_calls),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


@app.post("/v1/messages")
def anthropic_messages(req: AnthropicMessagesRequest):
    """Anthropic Messages API endpoint (non-streaming + SSE streaming, text tool-calling)."""
    model = req.model or "safari"
    message_id = f"msg_{uuid.uuid4().hex}"
    latest_user = _latest_user_text(req)

    # Auxiliary requests (session title, etc.) are not real turns: answer them
    # locally so they do not pollute the single shared DeepSeek conversation.
    if _is_title_request(latest_user):
        if LOG_REQUESTS:
            print(
                f"[v1/messages] title_request -> short-circuit (not injected)",
                file=sys.stderr,
                flush=True,
            )
        return _respond(_extract_title(latest_user), model, message_id, req.stream)

    if _has_tool_blocks(req.messages):
        # Continuation: a tool is already in play; inject just the tool_result.
        mode = "continuation"
        prompt = _build_inject_prompt(req)
        if not prompt.strip():
            raise HTTPException(status_code=400, detail="prompt must not be empty")
        text = _chat(prompt, None, None, None)["response"]
    elif LAZY_TOOLS:
        # Fresh turn, progressive disclosure: probe -> tool names -> full schema
        # for only the chosen tools.
        text, mode = _lazy_tool_flow(req)
    else:
        mode = "fresh"
        prompt = _build_inject_prompt(req)
        if not prompt.strip():
            raise HTTPException(status_code=400, detail="prompt must not be empty")
        text = _chat(prompt, None, None, None)["response"]

    if LOG_REQUESTS:
        tool_names = [t.get("name") for t in (req.tools or []) if isinstance(t, dict)]
        sys_len = len(_content_text(req.system)) if req.system is not None else 0
        print(
            f"[v1/messages] model={req.model} stream={req.stream} mode={mode} "
            f"system={sys_len}c tools={len(tool_names)} "
            f"msgs={len(req.messages)} has_tool_blocks={_has_tool_blocks(req.messages)} "
            f"resp={len(text)}c",
            file=sys.stderr,
            flush=True,
        )
        if LOG_PROMPTS:
            print(f"--- response ---\n{text}\n------", file=sys.stderr, flush=True)

    return _respond(text, model, message_id, req.stream)


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
