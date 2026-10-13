# from-safari-to-api

Wrap web chat UIs (e.g., chat.deepseek.com) into an API (since most websites are free, after all). It can also be integrated with AI agent tools like Claude Code / ccswitch (supports both Anthropic Messages API and OpenAI Chat Completions).

## Architecture

The project consists of two independent components:

````
HTTP Client
 │ POST /chat · /v1/chat/completions · /v1/messages
 ▼
server.py FastAPI  Facade: receives request → calls osascript → parses output → maps to HTTP response
 │ osascript check.applescript -p … -i … -r …
 ▼
check.applescript  Safari automation engine: injects prompt into current tab → submits → polls for reply
 │ do JavaScript / KeyboardEvent
 ▼
Safari's current tab in the current window
````

- `check.applescript` is the automation engine. It only operates on Safari's "current tab of the current window". It writes the prompt into the page's input field, triggers submission, polls the result node, and finally prints the final text to stdout.
- `server.py` is the HTTP facade. It doesn't touch the browser. It only receives requests, calls the script via argv, parses the script's output protocol, and translates the result into an HTTP response.

Note: It's called `check.applescript` because it was originally a quick proof-of-concept, and I never bothered to rename it.

They communicate through a clear interface (see "Output Protocol" section), so they can be modified independently without breaking each other.

## Requirements

- macOS only. The entire project is built on Safari + AppleScript + osascript. Linux / Windows won't work.
- Safari: The script drives Safari's current window and current tab. Chrome / Firefox won't work.
- Python 3.13+, plus fastapi, uvicorn (see requirements.txt).
- The target webpage must be open and ready for input in Safari. We're not advanced enough to log you in or find the page for you.

## Component: check.applescript

### CLI Interface

````
osascript check.applescript -p <prompt> -i <input_selector> -r <result_selector> [-b <button_selector>] [-c <continue_selector>] [--poll-interval <seconds>] [--poll-max <times>]
````

| Param | Required | Description |
| --- | --- | --- |
| -p / --prompt | Yes | Prompt to inject |
| -i / --input | Yes | Input field CSS selector |
| -r / --result | Yes | Result node CSS selector |
| -b / --button | No | Submit button selector; if empty, dispatches an Enter key event |
| -c / --continue-selector | No | "Continue" button CSS selector; if empty, auto-detects by button text |
| --poll-interval | No | Seconds between poll requests, default 3 |
| --poll-max | No | Max poll attempts, default 60 |
| -h / --help | No | Prints usage |

### Output Protocol

The script outputs a two-line protocol on stdout, parsed by `server.py` based on the first line:

````
OK:
<Result text>
````

````
ERR:
<Error message>
````

On success, the first line is `OK:`, followed by the scraped text. On failure, the first line is `ERR:`, followed by the reason (missing parameters, injection failure, button/input not found, polling timeout, etc.). The first line is separated by \n, and the body preserves original line breaks.

### Workflow

1. Injection: Escapes the prompt as a JS single-quoted string, writes it into the input field using `nativeSetter`, dispatches an `input` event, then submits via Enter or button click.
2. Polling: Reads the result node text every `--poll-interval` seconds. If the text remains unchanged for 3 consecutive polls, generation is considered complete.
3. Continuation: When generation stops by itself for some reason (I have no idea why), the script automatically clicks the "Continue" button to retrieve the rest, marking that poll as "unstable" to keep waiting.
   *(This is janky, better to just click it manually.)*
4. Return: If still unstable after `--poll-max` attempts, returns `ERR: Timeout`; otherwise returns `OK: + final_text`.

### Escaping Boundaries

The script handles string escaping for prompt → JS source: before concatenating the prompt into a JS single-quoted literal, it sequentially escapes backslashes, double quotes, single quotes, then replaces newlines/carriage returns/tabs with `\n`/`\r`/`\t`. Control characters must be processed *after* backslashes are doubled, otherwise newly introduced backslashes get double-escaped. This is the only escaping boundary in the project — the prompt passed from upstream (`server.py`) is already raw text.

## Component: server.py

FastAPI service, listens on `http://127.0.0.1:8000` by default, provides the following endpoints. All Safari calls are serialized by a mutex lock (only one request can drive the browser at a time). Routes are declared as synchronous `def` to run in a threadpool, so the event loop is never blocked.

### Prerequisites (One-time setup)

1. Allow JavaScript from Apple Events in Safari: Safari → Develop menu → check "Allow JavaScript from Apple Events". If there's no "Develop" menu, first go to Safari Preferences → Advanced → check "Show Develop menu in menu bar".
2. When calling, Safari's frontmost tab must be on the target webpage (e.g., chat.deepseek.com). The script only operates on the "current tab of the current window" and won't switch tabs automatically.
3. On first run, macOS will prompt for automation permission: "python3/osascript wants to control Safari". Click Allow.

### Installation & Run

Install dependencies first (venv recommended):

````bash
pip install -r requirements.txt # fastapi, uvicorn
````

Then configure selectors and start. You need to fill in the selectors yourself by inspecting the target webpage's DOM (I'm sure you know how to use the element inspector). If left empty, it returns 400:

````bash
export INPUT_SELECTOR='textarea' # Actual input field CSS selector. I found this one for DeepSeek. Other sites need to be found manually.
export RESULT_SELECTOR='.ds-markdown' # Actual result node CSS selector. Same as above.
export BUTTON_SELECTOR='' # Empty = press Enter to submit; fill in to use button click.

python3 server.py # or just ./run.sh
````

## Configuration (Environment Variables)

| Variable | Default | Description |
| --- | --- | --- |
| INPUT_SELECTOR | Empty | Input field CSS selector (Required) |
| RESULT_SELECTOR | Empty | Result node CSS selector (Required) |
| BUTTON_SELECTOR | Empty | Submit button selector; empty = press Enter |
| OSASCRIPT_TIMEOUT | Auto-derived ~210 | Timeout in seconds for a single call; default = POLL_MAX × POLL_INTERVAL + 30. Must be greater than the script's polling window, otherwise slow generations get SIGKILLed and return 504 |
| POLL_INTERVAL | 3 | Seconds between poll requests |
| POLL_MAX | 60 | Max poll attempts (polling window = POLL_INTERVAL × POLL_MAX ≈ 180s) |
| CONTINUE_SELECTOR | Empty | CSS selector for DeepSeek's "Continue" button; empty = auto-detect by button text (继续 / 继续生成 / 继续回答 / continue) |
| MAX_PROMPT_LENGTH | 60000 | Max prompt length (defense against argv length limits; tool protocol concatenates system + tool schema + history, so it's generous) |
| APPLESCRIPT_PATH | ./check.applescript | Script path |
| HOST / PORT | 127.0.0.1 / 8000 | Listen address |
| TOOL_CALL_OPEN / TOOL_CALL_CLOSE | `<tool_call>` / `</tool_call>` | Text tool call delimiters |
| TOOL_RESULT_OPEN / TOOL_RESULT_CLOSE | `<tool_result>` / `</tool_result>` | Text tool result delimiters |
| SYSTEM_MAX_CHARS | 1200 | Max chars for system prompt when rendered into the prompt (truncated if exceeded) |
| TOOL_DESC_MAX_CHARS | 300 | Max chars per tool description (multi-line squashed to one line, truncated if exceeded) |
| INCLUDE_SYSTEM | true | Whether to render system into a new session's prompt |
| TOOL_WHITELIST | Empty | Which tools to expose: empty = default core set, `*` = all, `Bash,Read` = specific |
| LAZY_TOOLS | true | Three-turn progressive disclosure: probe → tool names → only disclose full schema for chosen tools |
| LOG_REQUESTS | true | Print a one-line summary to stderr for each `/v1/messages` call |
| LOG_PROMPTS | Empty | Additionally dump the full injected prompt and raw page response (for debugging tool calls) |
| MODELS | A bunch of joke names | Model names returned by `GET /v1/models`, comma-separated. Purely decorative (server doesn't validate model, just echoes it back) |

## Endpoints

### GET /health

Checks if the script exists, osascript is runnable, and selectors are configured. Does not take the Safari lock, remains responsive during long requests.

### GET /v1/models (Model Discovery)

Clients (agent tools, ccswitch, etc.) often use this to "discover available models". This service doesn't validate the model, only echoes it back, so this list is purely decorative — default is a bunch of joke names:

````
safari-114514, I_am_a_teapot, IAmAnEsteregg, definitely-not-gpt-5, safari-pro-max-ultra, claude-opus-5-5-but-safari, not-a-real-model
(I'm a genius)
````

````bash
curl -s http://127.0.0.1:8000/v1/models
````

````json
{
 "object": "list",
 "data": [{"id": "safari-114514", "object": "model", "type": "model", "owned_by": "safari", "display_name": "safari-114514"}],
 "has_more": false,
 "first_id": "safari-114514",
 "last_id": "not-a-real-model"
}
````

The response body contains both OpenAI (`object`/`owned_by`) and Anthropic (`type`/`display_name`) fields, so both types of clients can parse it. Use the `MODELS` env var (comma-separated) to customize.

`GET /v1/models/{model_id}` for single model query, accepts any id (names are decorative, no validation).

### GET /user/balance (Balance Query)

DeepSeek-style balance endpoint (alias `/v1/user/balance`). Clients like cc-switch read this to display "remaining quota". Scraping webpages costs nothing, so the balance is made up:

````bash
curl -s http://127.0.0.1:8000/user/balance
````

````json
{
 "is_active": true,
 "is_available": true,
 "balance": 999999999.0,
 "currency": "USD",
 "unit": "USD",
 "note": "Even after all this, you still want a balance check? You poor thing."
}
````

The fields satisfy both cc-switch's extractor (reads `is_active` / `balance`) and DeepSeek's `balance_infos` structure. It never runs out — because it never costs anything.

### POST /chat (Simple Interface)

````bash
curl -s http://127.0.0.1:8000/chat \
 -H 'Content-Type: application/json' \
 -d '{"prompt": "Hello, introduce yourself in one sentence."}'
````

````json
{
 "response": "...page response body...",
 "elapsed_seconds": 12.34,
 "selectors": {"input": "textarea", "result": ".ds-markdown", "button": null}
}
````

Selectors can be overridden per-request (otherwise uses env var defaults):

````json
{"prompt": "...", "input_selector": "...", "result_selector": "...", "button_selector": "..."}
````

### POST /v1/chat/completions (OpenAI Compatible)

For easy integration with existing agents/tools (as a local model). Returns standard OpenAI structure:

````bash
curl -s http://127.0.0.1:8000/v1/chat/completions \
 -H 'Content-Type: application/json' \
 -d '{"model": "deepseek-safari", "messages": [{"role": "user", "content": "Hello"}], "stream": false}'
````

````json
{
 "id": "chatcmpl-...",
 "object": "chat.completion",
 "created": 1700000000,
 "model": "deepseek-safari",
 "choices": [{"index": 0, "message": {"role": "assistant", "content": "..."}, "finish_reason": "stop"}],
 "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
}
````

Extracts the content of the last `role == "user"` message (supports string or `[{"type":"text","text":...}]` list); `stream: true` returns 400 (script only returns final text, no streaming possible).

### GET /api/hello (Anthropic Client Health Check)

Anthropic-style clients like ccswitch / Claude Code use `HEAD /api/hello` to probe if the service is online. Returns 200 `{"message":"hello"}`. This route MUST declare `methods=["GET","HEAD"]` — FastAPI/Starlette's GET route won't automatically respond to HEAD.

### POST /v1/messages (Anthropic Messages API)

For direct integration with ccswitch / Claude Code. Supports both `stream: false` (JSON) and `stream: true` (SSE):

````bash
curl -s -X POST 'http://127.0.0.1:8000/v1/messages' \
 -H 'Content-Type: application/json' \
 -d '{"model":"claude-opus-5-5","max_tokens":1024,"messages":[{"role":"user","content":"Hello"}]}'
````

````json
{
 "id": "msg_...",
 "type": "message",
 "role": "assistant",
 "model": "claude-opus-5-5",
 "content": [{"type": "text", "text": "..."}],
 "stop_reason": "end_turn",
 "stop_sequence": null,
 "usage": {"input_tokens": 0, "output_tokens": 0}
}
````

## Tool Calling (Text Tool Protocol)

Claude Code passes `system`, `tools` (tool definitions), and `messages` (containing `tool_use`/`tool_result`) via `/v1/messages`. Since the web UI can't use the native tool protocol, this service simulates the tool loop with a text protocol:

1. **New session** (no `tool_use`/`tool_result` in messages): Defaults to three-turn progressive disclosure (`LAZY_TOOLS`):
   - Turn 1: Only paste the user request (no system/tools), ask the model "Do you need a tool? Reply 'need tool' if yes."
   - If the model replies "need tool", Turn 2: Only paste system + tool names + one-line descriptions (no schema, no convention), ask "Which tools? Reply with just tool names."
   - Turn 3: Only paste the full schema (param names/types/required/description) for the chosen tools + `<tool_call>` convention, letting the model output structured calls. If Turn 2 already output full calls, skip Turn 3.
   - If the model can answer directly, skip all subsequent turns and return the answer.
   - Set `LAZY_TOOLS=false` to fall back to "paste everything at once" single-turn mode.
2. **Continuation** (history contains `tool_use`/`tool_result`): Only paste the "last user message" (i.e., this turn's `tool_result`) into the page, letting DeepSeek continue on the page's existing conversation context.
3. Tool calls in the page response are parsed and translated into Anthropic `tool_use` content blocks (`stop_reason="tool_use"`); otherwise the whole text is returned as `stop_reason="end_turn"`. Malformed JSON gracefully degrades to text.
4. Responses can mix two formats: `<tool_call>{json}</tool_call>` and DeepSeek's native DSML (｜DSML｜ wrapped in full-width pipes, one `<parameter name="..." string="true|false">` per argument). The service merges both formats in document order, so a "first half `<tool_call>`, second half DSML" reply won't drop the DSML calls. DSML supports three dialects — V3.2's `<｜DSML｜function_calls>`, V4's `<｜DSML｜tool_calls>`, V4.1's `<｜DSML｜ calls>` (leading space) — plus direct JSON bodies. `string="true"` reads as string, `string="false"` reads as JSON (number/bool/array/object), falling back to raw text on parse failure.
5. I have no idea when the above is reliable and when it's not (in testing, it really was hit-or-miss). If it outputs raw text, just resend the prompt or tell DeepSeek about the issue. Empirically effective.

Call convention (rendered into prompt, models should follow):

````json
<tool_call>
{"name":"tool_name","arguments":{...}}
</tool_call>
````

`tool_result` is rendered as `<tool_result name="tool_name">...content...</tool_result>` (the `name` comes from the `tool_use_id` → name mapping in history).

## Integrating with ccswitch / Claude Code

1. Start the service (selectors configured):

 ````bash
 export INPUT_SELECTOR='...' RESULT_SELECTOR='...'
 ./run.sh
 ````

2. Add a new Anthropic protocol provider/endpoint in ccswitch, base_url filled with `http://127.0.0.1:8000` (the client will automatically request `/v1/messages` and `/api/hello`). Do NOT fill it as `http://127.0.0.1:8000/v1`.
3. Model name is arbitrary (e.g., `safari-free-114514`): This service doesn't validate the model, only echoes it back. If it complains "selected model ... may not exist" because `/v1/messages` returned 404, it's probably fine.

## Layered Escaping Responsibility (Important, don't break the script)

- Python → osascript: **This is the argv boundary**. Use `subprocess` list form, no shell. The prompt is passed through verbatim (no folding, no shell escaping, no double escaping). Python only does defensive validation: rejects prompts containing NUL, limits max length.
- check.applescript → JS source: **This is the string escaping boundary**, handled by the script. The script concatenates the prompt into a single-quoted JS string, so it MUST escape `\`, `'`, `"`, and LF/CR/TAB control characters. Note the order: double the backslashes first, then escape control characters, otherwise newly added `\n` gets double-escaped.

**If you break it by modifying the script, don't blame me.**

## Known Limitations (Currently only tested on DeepSeek; theoretically works on other sites if you find the input box)

- Only drives Safari's current window and current tab; concurrent requests are serialized by a lock and will queue.
- Result scraping relies on "result text unchanged for N consecutive polls" to determine completion, up to `POLL_MAX` × `POLL_INTERVAL` seconds (default 60×3s≈180s); timeout returns `ERR: Timeout...`. If a long response hits the website's length limit, the script automatically clicks the "Continue" button to retrieve the rest (override with `CONTINUE_SELECTOR`).
- No true streaming increment: the script only returns final text; the Anthropic endpoint's `stream: true` just returns the whole text as a single delta (valid SSE structure, for streaming client compatibility).
- The tool protocol is a text-level fallback, relying on the model dutifully outputting `<tool_call>` blocks: the model may not follow the convention, output malformed JSON (degraded to plain text), or output both a tool call and a final answer in one reply (this service takes the parsed tool calls and ignores other text). The convention explicitly forbids DSML (｜DSML｜ tags), but DeepSeek may stubbornly output it anyway, probably because this damn thing's System prompt or training is too stubborn. Usually repeating a few times works.
- The tool loop relies on DeepSeek's page-side conversation accumulation to maintain state; the server doesn't store history. Therefore: before each Claude Code session, manually click "New Chat" on the DeepSeek page to enter the input interface (may need to send a placeholder first), otherwise old conversations stack up and interfere with the model.
- If the model continues multi-turn plain text after a "no tool call" turn in the same session, repeated preamble injection accumulates noise; it's recommended to start a new DeepSeek conversation for each independent task.
