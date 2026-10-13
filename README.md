# from-safari-to-api

把 Safari 的网页对话界面（如 chat.deepseek.com）包装成 API（毕竟大多数情况下网站是免费的）。也可接入 Claude Code / ccswitch 等 agent 工具（支持Anthropic Messages API 与 OpenAI Chat Completions 两套协议）。

## 架构

项目由两个组件构成，各自独立：

````
HTTP 客户端
 │ POST /chat · /v1/chat/completions · /v1/messages
 ▼
server.py FastAPI  门面：收请求 → 调 osascript → 解析输出 → 映射成 HTTP 响应
 │ osascript check.applescript -p … -i … -r …
 ▼
check.applescript  Safari 自动化引擎：向当前标签页注入 prompt → 提交 → 轮询抓取回复
 │ do JavaScript / KeyboardEvent
 ▼
Safari 当前窗口的当前标签页
````


- check.applescript 是自动化引擎。它只操作 Safari「当前窗口的当前标签页」，把 prompt 写进页面输入框、触发提交、轮询结果节点，最后把最终文本打印到 stdout。
- server.py 是 HTTP 门面。它不接触浏览器，只负责收请求、以 argv 方式调用脚本、解析脚本的输出协议、把结果翻译成 HTTP 响应。

注：之所以那玩意叫check.applescript是因为这玩意本来是为了验证我的想法用的，后来一直没改名

两者通过一条明确的接口通信（见「输出协议」一节），可以分别修改而不互相破坏。

## 环境要求

- 仅限 macOS。整个项目建立在 Safari + AppleScript + osascript 之上，Linux / Windows 跑不了。
- Safari：脚本驱动的是 Safari 当前窗口的当前标签页，Chrome / Firefox 都不行。
- Python 3.13+，外加 fastapi、uvicorn（见 requirements.txt）。
- 目标网页要在 Safari 里调整到可以输入的位置，我们没有高级到帮你登录并查找网页。

## 组件：check.applescript

### 命令行接口


osascript check.applescript -p <prompt> -i <输入框选择器> -r <结果选择器> [-b <发送按钮选择器>] [-c <继续按钮选择器>] [--poll-interval <秒>] [--poll-max <次数>]


| 参数 | 必填 | 说明 |
| --- | --- | --- |
| -p / --prompt | 是 | 要注入的提示词 |
| -i / --input | 是 | 输入框 CSS 选择器 |
| -r / --result | 是 | 结果节点 CSS 选择器 |
| -b / --button | 否 | 发送按钮选择器；留空则对输入框派发回车键事件 |
| -c / --continue-selector | 否 | 「继续」按钮选择器；留空则按按钮文本自动识别 |
| --poll-interval | 否 | 每次轮询间隔秒数，默认 3 |
| --poll-max | 否 | 最大轮询次数，默认 60 |
| -h / --help | 否 | 打印用法 |

### 输出协议

脚本在 stdout 上输出两行式协议，server.py 按首行解析：

````
OK:
<结果文本>
````

````
ERR:
<错误信息>
````


成功时首行为 `OK:`，其后为抓取到的正文；失败时首行为 `ERR:`，其后为原因（缺少参数、注入失败、按钮/输入框未找到、轮询超时等）。首行以 \n 分隔，正文保留原始换行。

### 工作流程

1. 注入：把 prompt 转义为 JS 单引号字符串，用 nativeSetter 写入输入框并派发 input 事件，然后按回车或点击发送按钮提交。
2. 轮询：每隔 --poll-interval 秒读取结果节点文本。当文本连续 3 次轮询保持不变，判定生成完成。
3. 续写：因为某些原因网页生成自己停止时（我不知道为什么），脚本会自动点击「继续」按钮取回剩余内容，并把该次轮询标记为「未稳定」以继续等待。
   (这玩意不好用，建议自己用手点击)
4. 返回：最多轮询 --poll-max 次仍未稳定则返回 `ERR: 超时`；否则返回 `OK: + 最终文本`。

### 转义边界

脚本负责 prompt → JS 源码 这一步的字符串转义：把 prompt 拼进 JS 单引号字面量前，依次转义反斜杠、双引号、单引号，再把换行/回车/制表符替换为 \n/\r/\t。控制字符必须在反斜杠翻倍之后处理，否则新引入的反斜杠会被二次翻倍。这是项目里唯一需要转义的边界——上游（server.py）传入的 prompt 已是原样文本。

## 组件：server.py

FastAPI 服务，默认监听 http://127.0.0.1:8000，提供以下端点。所有 Safari 调用由互斥锁串行化（同一时刻只有一个请求能驱动浏览器），路由声明为同步 def 以便运行在线程池中，事件循环不会被阻塞。

### 前置条件（一次性设置）

1. Safari 允许 Apple 事件中的 JavaScript：Safari → 开发菜单 → 勾选「允许 Apple 事件中的 JavaScript」。若没有「开发」菜单，先在 Safari 偏好设置 → 高级 → 勾选「在菜单栏中显示“开发”菜单」。
2. 调用时 Safari 的前台标签页要停留在目标网页（如 chat.deepseek.com）。脚本只操作「当前窗口的当前标签页」，不会自动切换标签页。
3. 首次运行，macOS 会弹「python3/osascript 想要控制 Safari」的自动化授权，点允许。

### 安装与运行

先装依赖（建议在虚拟环境里）：

````bash
pip install -r requirements.txt # fastapi, uvicorn
````

然后配置选择器并启动。选择器要对着目标网页的 DOM 自己填(我相信你们会用元素选择器)，不填会 400：

````bash
export INPUT_SELECTOR='textarea' # DeepSeek 输入框的实际 CSS 选择器
export RESULT_SELECTOR='.ds-markdown' # 结果节点的实际 CSS 选择器
export BUTTON_SELECTOR='' # 留空 = 回车提交；填了就用按钮点击

python3 server.py或干脆直接./run.sh
````



## 配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| INPUT_SELECTOR | 空 | 输入框 CSS 选择器（必填） |
| RESULT_SELECTOR | 空 | 结果节点 CSS 选择器（必填） |
| BUTTON_SELECTOR | 空 | 发送按钮选择器；留空按回车提交 |
| OSASCRIPT_TIMEOUT | 自动派生 ≈210 | 单次调用的超时秒数；默认 = POLL_MAX × POLL_INTERVAL + 30，须大于脚本轮询窗口，否则慢生成被强杀返回 504 |
| POLL_INTERVAL | 3 | 脚本每次轮询结果的间隔秒数 |
| POLL_MAX | 60 | 脚本最大轮询次数（轮询窗口 = POLL_INTERVAL × POLL_MAX ≈ 180s） |
| CONTINUE_SELECTOR | 空 | DeepSeek「继续」按钮的 CSS 选择器；留空=按按钮文本自动识别（继续 / 继续生成 / 继续回答 / continue） |
| MAX_PROMPT_LENGTH | 60000 | prompt 最大字符数（argv 长度限制的防御；工具协议会拼入 system+工具 schema+历史，故放宽） |
| APPLESCRIPT_PATH | ./check.applescript | 脚本路径 |
| HOST / PORT | 127.0.0.1 / 8000 | 监听地址 |
| TOOL_CALL_OPEN / TOOL_CALL_CLOSE | <tool_call> / </tool_call> | 文本工具调用的开/闭分隔符 |
| TOOL_RESULT_OPEN / TOOL_RESULT_CLOSE | <tool_result> / </tool_result> | 文本工具结果的开/闭分隔符 |
| SYSTEM_MAX_CHARS | 1200 | system 提示词渲染进 prompt 时的最大字符数（超出截断） |
| TOOL_DESC_MAX_CHARS | 300 | 单个工具描述的最大字符数（多行压成一行，超出截断） |
| INCLUDE_SYSTEM | true | 是否把 system 渲染进新会话 prompt |
| TOOL_WHITELIST | 空 | 只披露哪些工具：空=默认核心集，*=全部，Bash,Read=指定 |
| LAZY_TOOLS | true | 三轮渐进式披露：探针 → 工具名 → 只披露所选工具的完整 schema |
| LOG_REQUESTS | true | 每次 /v1/messages 调用打印一行摘要到 stderr |
| LOG_PROMPTS | 空 | 额外 dump 完整注入 prompt 与原始页面回复（调试工具调用时用） |

## 端点

### GET /health

检查脚本是否存在、osascript 是否可运行、选择器是否已配置。不占用 Safari 锁，长请求期间仍可响应。

### POST /chat（简单接口）


````bash
curl -s http://127.0.0.1:8000/chat \
 -H 'Content-Type: application/json' \
 -d '{"prompt": "你好，用一句话介绍你自己"}'
````


````json
{
 "response": "……页面回复正文……",
 "elapsed_seconds": 12.34,
 "selectors": {"input": "textarea", "result": ".ds-markdown", "button": null}
}
````


可按请求覆盖选择器（不覆盖则用环境变量默认值）：


````json
{"prompt": "…", "input_selector": "…", "result_selector": "…", "button_selector": "…"}
````


### POST /v1/chat/completions（OpenAI 兼容）

便于接入现有 agent / 工具（作为本地模型）。返回标准 OpenAI 结构：


````bash
curl -s http://127.0.0.1:8000/v1/chat/completions \
 -H 'Content-Type: application/json' \
 -d '{"model": "deepseek-safari", "messages": [{"role": "user", "content": "你好"}], "stream": false}'
 ````



````json
{
 "id": "chatcmpl-…",
 "object": "chat.completion",
 "created": 1700000000,
 "model": "deepseek-safari",
 "choices": [{"index": 0, "message": {"role": "assistant", "content": "…"}, "finish_reason": "stop"}],
 "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
}
````


取 messages 中最后一条 role == "user" 的 content（支持字符串或 [{type:"text",text}] 列表）；stream: true 会返回 400（脚本只返回最终文本，无法流式）。

### GET /api/hello（Anthropic 客户端健康检查）

ccswitch / Claude Code 等 Anthropic 风格客户端会用 HEAD /api/hello 探测服务是否在线，返回 200 {"message":"hello"}。该路由必须声明 methods=["GET","HEAD"]——FastAPI/Starlette 的 GET 路由不会自动应答 HEAD。

### POST /v1/messages（Anthropic Messages API）

供 ccswitch / Claude Code 直接接入。支持 stream: false（JSON）与 stream: true（SSE）两种：


````bash
curl -s -X POST 'http://127.0.0.1:8000/v1/messages' \
 -H 'Content-Type: application/json' \
 -d '{"model":"claude-opus-5-5","max_tokens":1024,"messages":[{"role":"user","content":"你好"}]}'
````


````json
{
 "id": "msg_…",
 "type": "message",
 "role": "assistant",
 "model": "claude-opus-5-5",
 "content": [{"type": "text", "text": "…"}],
 "stop_reason": "end_turn",
 "stop_sequence": null,
 "usage": {"input_tokens": 0, "output_tokens": 0}
}
````


## 工具调用（文本工具协议）

Claude Code 会通过 /v1/messages 传入 system、tools（工具定义）和 messages（含 tool_use/tool_result）。由于网页端无法走原生 tool 协议，本服务用文本协议模拟工具回环：

1. 新会话（messages 里没有任何 tool_use/tool_result），默认走三轮渐进式披露（LAZY_TOOLS）：
 - 第一轮只把用户请求贴入网页（无 system/工具），问模型「需要工具吗？需要就回『需要工具』」。
 - 若模型回「需要工具」，第二轮只贴 system + 工具名 + 一句话描述（不含 schema、不含约定），问模型「需要哪些工具，只回工具名」。
 - 第三轮只对模型选中的工具贴入完整 schema（参数名/类型/必填/描述）+ <tool_call> 约定，让模型输出结构化调用。若第二轮模型直接输出了完整调用，则跳过第三轮。
 - 若模型能直接作答，则跳过后面所有轮，直接返回答案。
 - 设 LAZY_TOOLS=false 则退回到「一次性把 system+工具+约定都贴入」的单轮模式。
2. 续轮（历史里已出现 tool_use/tool_result）：只把「最后一条 user 消息」（即本轮 tool_result）贴入网页，让 DeepSeek 在页面已有的对话上下文上继续。
3. 网页回复里的工具调用被解析并翻译成 Anthropic 的 tool_use 内容块返回（stop_reason="tool_use"）；否则整段当普通文本返回（stop_reason="end_turn"）。畸形 JSON 自动降级为文本。
4. 回复里可以混用两种格式：<tool_call>{json}</tool_call> 和 DeepSeek 原生的 DSML（｜DSML｜ 全角竖线包裹的标签，每个参数一个 <parameter name="…" string="true|false">）。服务把两种格式按出现顺序合并，所以「前半 <tool_call>、后半 DSML」的回复里，后半段的 DSML 调用不会被丢弃。DSML 支持三种方言——V3.2 的 <｜DSML｜function_calls>、V4 的 <｜DSML｜tool_calls>、V4.1 的 <｜DSML｜ calls>（标签名带前导空格）——以及直接 JSON 参数体。string="true" 按字符串、string="false" 按 JSON（数字/布尔/数组/对象）取值，解析失败回退为原始文本。
5. 我不知道上面这条什么时候好用什么时候不好用（测试的时候真的就是有时好有时不好用），如果出现直接出现原文本的情况，可以把提示词重新发一遍或告诉DeepSeek这一情况。亲测有效。

调用约定（渲染进 prompt，模型应遵循）：

````json
<tool_call>
{"name":"工具名","arguments":{…}}
</tool_call>
````

tool_result 渲染为 `<tool_result name="工具名">…内容…</tool_result>`（name 由历史里 tool_use_id → name 映射得出）。

## 接入 ccswitch / Claude Code

1. 启动服务（已配置好选择器）：
 
 ````bash
 export INPUT_SELECTOR='…' RESULT_SELECTOR='…'
 ./run.sh
 ````
 
2. 在 ccswitch 里新增一个 Anthropic 协议 的 provider/端点，base_url 填 http://127.0.0.1:8000（客户端会自动请求 /v1/messages 和 /api/hello）。不要填成 http://127.0.0.1:8000/v1。
3. 模型名随便填（比如 safari-free-114514 ）：本服务不校验 model，只把请求里的 model 原样回显。如果报「selected model … may not exist」是因为 /v1/messages 返回了 404，大概率没问题。

## 分层转义责任（重要，防止把脚本改崩）

- Python → osascript：**这是 argv 边界**。用 subprocess 的 list 形式调用、不走 shell，prompt 原样透传（不折叠换行、不做 shell 转义、不二次转义）。Python 只做防御性校验：拒绝含 NUL 的 prompt、限制最大长度。
- check.applescript → JS 源码：**这才是字符串转义边界**，由脚本负责。脚本把 prompt 拼进单引号 JS 字符串，因此必须转义 \、'、"，以及 LF/CR/TAB 等控制字符。注意顺序：先翻倍反斜杠，再转义控制字符，否则新加的 \n 会被二次翻倍。

**不听改脚本改坏了别怪我。**

## 已知限制

- 只能驱动 Safari 当前窗口的当前标签页；并发请求由锁串行化，会排队等待。
- 结果抓取依赖「结果文本连续 N 次轮询不变」判定完成，最多 POLL_MAX 次 × POLL_INTERVAL 秒（默认 60×3s≈180s）；超时返回 `ERR: 超时…`。长回复命中官网长度上限时，脚本会自动点击「继续」按钮取回完整回复（可用 CONTINUE_SELECTOR 覆盖按钮选择器）。
- 没有真正的流式增量：脚本只返回最终文本；Anthropic 端点的 stream: true 只是把整段文本当作单个 delta 一次性返回（SSE 结构合法，供流式客户端兼容）。
- 工具协议是文本层面的 fallback，依赖模型老老实实输出 <tool_call> 块：模型可能不遵守约定、输出畸形的 JSON（会被降级为普通文本）、或在一次回复里既给工具又给最终答案（本服务取解析出的工具调用，忽略其它文本）。约定现在点名禁止 DSML（｜DSML｜ 标签），但 DeepSeek 仍可能坚持输出 DSML，可能是因为这破玩意儿的System prompt或者训练太顽固了。一般来说重复几次即可。
- 工具回环靠 DeepSeek 网页自身的对话累积维持状态，服务端不存历史。因此：每个 Claude Code 会话开始前，先在 DeepSeek 页手动点「新对话」进入输入界面（可能需要先发一个占位符），否则旧对话会堆叠、干扰模型。
- 同一会话里若模型在「无工具调用」的一轮后继续多轮纯文本，会因重复注入 preamble 而累积噪音；建议每段独立任务都开新的 DeepSeek 对话。
