# LLM Gateway

OpenAI 兼容的自建 LLM 网关：统一接入多家供应商，提供智能路由、重试/降级/熔断、分层限流、SSE 流式、结构化输出校验与自动修复、模板管理，以及用量/追踪/计费（幂等）能力。

规格依据：[docs/SPEC.md](../docs/SPEC.md)

> 本文档中的示例命令均经过实际运行验证。PowerShell 与 Bash 命令请勿混用：续行符、引号和 JSON 转义规则不同。

## 功能地图

| 能力 | 实现位置 | 对外入口 |
|---|---|---|
| OpenAI 兼容对话 / 模型列表 | `app/api/chat.py` | `POST /v1/chat/completions`、`GET /v1/models` |
| 智能路由与候选链 | `app/core/router.py` | 请求省略 `model` 时自动选路 |
| 重试 / fallback / 熔断 | `app/core/retry_fallback.py`、`app/core/circuit_breaker.py` | 内部管道 |
| 分层限流 | `app/core/rate_limiter.py` | `429 rate_limit_exceeded` |
| Bearer 鉴权 | `app/core/auth.py` | 所有 `/v1/*` 管理类端点 |
| 结构化输出校验与修复 | `app/core/validator.py` | `response_format` |
| Prompt 注入检测 | `app/core/validator.py` | 消息内容命中规则即拒绝 |
| 提示词模板 | `app/core/prompt_renderer.py`、`app/api/templates.py` | `/v1/templates`、`/ui/templates`、CLI |
| 用量 / 追踪 / 计费 | `app/storage/metrics_store.py` | SQLite + CLI + `/v1/admin` |
| 指标与健康检查 | `app/main.py`、`app/core/metrics_collector.py` | `/healthz`、`/readyz`、`/metrics` |
| 配置加载与热更新 | `app/core/config_manager.py` | `config/gateway.yaml` + 环境变量 |
| 运维 CLI | `cli/gateway_cli.py` | `python -m cli.gateway_cli ...` |

### 请求管道

```text
鉴权 → 分层限流 → 请求校验 → Trace 固化 → Prompt 注入检测
→ 模板渲染（可选）→ 路由 → 重试 / fallback → Provider 适配器
→ 结构化输出校验 / 修复 → 幂等计费 → 响应（普通 / SSE）
```

## 快速开始

### Windows（PowerShell）

```powershell
# 1. 环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1      # Windows 激活虚拟环境
pip install -r requirements-dev.txt

# 2. 配置（填入各 Provider Key；密钥不硬编码，均从环境变量读取）
#    复制 .env.example 为 .env 并填入真实 Key（.env 已被 .gitignore 忽略，勿提交）
$env:DEEPSEEK_API_KEY = "sk-your-key"   # 或临时设置单个变量

# 3. 启动（务必用虚拟环境解释器；系统 python 未装项目依赖，
#    会报 ModuleNotFoundError: No module named 'dotenv'）
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 4. 冒烟
curl.exe http://127.0.0.1:8000/healthz
curl.exe http://127.0.0.1:8000/readyz
curl.exe http://127.0.0.1:8000/metrics
curl.exe http://127.0.0.1:8000/v1/models -H "Authorization: Bearer sk-gateway-dev"
```

### Linux / macOS（Bash）

```bash
# 1. 环境
python -m venv .venv
source .venv/Scripts/activate         # Linux/macOS 激活虚拟环境
pip install -r requirements-dev.txt

# 2. 配置（填入各 Provider Key；密钥不硬编码，均从环境变量读取）
#    复制 .env.example 为 .env 并填入真实 Key（.env 已被 .gitignore 忽略，勿提交）
export DEEPSEEK_API_KEY="sk-your-key"   # 或临时设置单个变量

# 3. 启动（务必用虚拟环境解释器；系统 python 未装项目依赖，
#    会报 ModuleNotFoundError: No module named 'dotenv'）
.venv/Scripts/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 4. 冒烟
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz
curl http://127.0.0.1:8000/metrics
curl http://127.0.0.1:8000/v1/models -H "Authorization: Bearer sk-gateway-dev"
```

### 鉴权与 Provider Key

网关准入使用统一 Bearer Key，来源优先级为 **环境变量 > `gateway.yaml`**：

- `gateway.yaml` 的 `api_keys`（默认含 `sk-gateway-dev`、`sk-gateway-test`）；
- 设置 `GATEWAY_API_KEYS`（逗号分隔）后，YAML 中的 key 被忽略。

上游 Key 均从环境变量读取，不写入配置文件：

| Provider | 环境变量 | 备注 |
|---|---|---|
| DeepSeek | `DEEPSEEK_API_KEY` | |
| OpenAI | `OPENAI_API_KEY` | |
| Kimi | `KIMI_API_KEY` | |
| Qwen | `QWEN_API_KEY` | |
| Anthropic | `ANTHROPIC_API_KEY` | |
| Ollama | — | 本地服务，不配置 `api_key_env` |

未配置 Key 的 Provider 会在路由阶段被剔除，不会发出非法请求，也不会计入熔断失败。

## 配置说明

### 配置文件结构

配置位于 `config/gateway.yaml`，支持保存后**原子热更新**，无需重启：

| 配置段 | 作用 |
|---|---|
| `server` | 监听地址、worker、日志、反向代理信任边界 |
| `storage` | SQLite 路径、trace TTL、观察表保留天数 |
| `rate_limit` | 令牌桶速率与各层上限 |
| `circuit_breaker` | 失败阈值、恢复时间、半开探针数 |
| `http_pool` | 连接池大小与上游超时 |
| `api_keys` | 网关准入 Key |
| `providers` | 供应商地址、Key 环境变量、价格、能力、模型别名 |
| `available_models` | 对外模型名、所属供应商、路由权重、可选上下文窗口 |

### 环境变量覆盖

| 变量 | 覆盖项 |
|---|---|
| `GATEWAY_CONFIG_FILE` | 配置文件路径 |
| `GATEWAY_API_KEYS` | 网关准入 Key 列表（逗号分隔） |
| `GATEWAY_DB_PATH` | SQLite 路径 |
| `GATEWAY_HOST` / `GATEWAY_PORT` / `GATEWAY_WORKERS` / `GATEWAY_LOG_LEVEL` | 服务监听与日志 |
| `GATEWAY_TRUST_PROXY_HEADERS` / `GATEWAY_TRUSTED_PROXIES` | X-Forwarded-For 信任策略 |
| `GATEWAY_RATE_REFILL` / `GATEWAY_RATE_BUCKET` | 令牌桶补充速率 / 容量 |
| `GATEWAY_RATE_MODEL` / `GATEWAY_RATE_ENDPOINT` / `GATEWAY_RATE_PROJECT` / `GATEWAY_RATE_USER` | 分层限流上限 |
| `GATEWAY_CB_THRESHOLD` / `GATEWAY_CB_RECOVERY` / `GATEWAY_CB_HALF_OPEN` | 熔断参数 |
| `GATEWAY_POOL_CONNS` / `GATEWAY_POOL_KEEPALIVE` | 连接池容量 |
| `GATEWAY_POOL_CONNECT_TO` / `GATEWAY_POOL_READ_TO` | 连接 / 读取超时 |
| `GATEWAY_UNBILLED_RETENTION_DAYS` | 观察表保留天数 |

### 供应商能力与默认价格

| Provider | `structured_output` | prompt 单价（$/M） | completion 单价（$/M） | thinking 能力 |
|---|---|---|---|---|
| deepseek | `json_object` | 0.5 | 2.0 | 0.8 |
| openai | `json_schema` | 2.5 | 10.0 | 0.9 |
| kimi | `json_object` | 1.0 | 3.5 | 0.85 |
| qwen | `json_object` | 0.8 | 3.0 | 0.75 |
| anthropic | `none` | 3.0 | 15.0 | 0.95 |
| ollama | `json_object` | 0.0 | 0.0 | 0.4 |

`structured_output` 决定网关如何向上游传递 `response_format`：`json_schema` 原样透传；`json_object` 降级为 JSON 对象模式并补充提示词；`none` 不转发原生约束，由适配器用提示方式兜底。本地 validator 始终是最终校验权威。

### 上游超时：只有 `http_pool.read_timeout` 生效

`providers.<name>.timeout` 目前**只被解析、未接线**（`ProviderConfig.timeout` 无任何消费方），改它不会影响实际超时。所有上游调用共用一个连接池超时：

```yaml
http_pool:
  connect_timeout: 5.0
  read_timeout: 60.0     # 真正生效的上游超时
```

env 覆盖：`GATEWAY_POOL_CONNECT_TO` / `GATEWAY_POOL_READ_TO`。修改后由配置监听热重载，`http_pool.update_params()` 会重建客户端，**无需重启**。

为避免"改了却没生效"，配置加载时若显式写了 `providers.<name>.timeout` 且与 `http_pool.read_timeout` 不一致，会打印 warning：

```text
WARNING Provider 'deepseek' 的 timeout=0.001s 不会生效：上游超时统一由
        http_pool.read_timeout=60.000s 控制（env GATEWAY_POOL_READ_TO）
```

该提示**只告警、不改变任何行为**（`ProviderConfig.timeout` 仍按配置保存）。

### 计费与定价（prompt / completion 分价）

供应商支持按 prompt / completion 分别定价（两者单价通常相差数倍）：

```yaml
providers:
  deepseek:
    cost_prompt_per_million_tokens: 0.5
    cost_completion_per_million_tokens: 2.0
```

- 配置了分价时，**计费、预算预检、路由加权价**均使用分价；只写 `cost_per_million_tokens`（单一均价）时，两价回退为该值，兼容旧配置。
- 单次成本 = `prompt_tokens/1e6 × prompt单价 + completion_tokens/1e6 × completion单价`，落库到 `usage_metrics.cost_usd`，并累计进 `daily_aggregates`。
- 三个价格字段全部缺失时会打印 warning 并按 `$1/M` 计费（避免静默按 0 计费）。
- 上游未返回 usage 的请求按估算 token 记录，并标记 `estimated=1`、`cost_usd=0`，因此日报成本可能偏低；精确账单以返回 usage 的请求为准。

`daily`（CLI 与 `/v1/admin/daily`）已按 `prompt_tokens` / `completion_tokens` 分列累计。

## API 参考

### 端点总览

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/v1/models` | Bearer | 列出对外可用模型 |
| POST | `/v1/chat/completions` | Bearer | 对话补全（普通 / 流式 / 结构化输出） |
| GET/POST/DELETE | `/v1/templates...` | Bearer | 模板管理，详见下文 |
| GET | `/v1/admin/summary` | Bearer | 累计用量概览 |
| GET | `/v1/admin/traces` | Bearer | 最近追踪记录 |
| GET | `/v1/admin/daily` | Bearer | 按天聚合用量与成本 |
| GET | `/healthz` | 无 | 存活探针 |
| GET | `/readyz` | 无 | 就绪探针 |
| GET | `/metrics` | 无 | Prometheus 文本指标 |
| GET | `/ui/templates` | 无（页面本身） | 模板管理单页，数据操作仍需 Bearer |

### 请求字段（`POST /v1/chat/completions`）

| 字段 | 类型 | 说明 |
|---|---|---|
| `model` | string，可选 | 省略时自动选路；显式指定则只走该模型 |
| `messages` | array | 必填，1~64 条；`role` 支持 system/user/assistant/tool |
| `stream` | bool | 是否 SSE 流式 |
| `temperature` | float，0~2 | 采样温度 |
| `max_tokens` | int，1~32768 | 最大生成 token 数 |
| `response_format` | object | `json_object` 或 `json_schema` |
| `top_p` | float，0~1 | 核采样 |
| `stop` | string / string[] | 最多 4 条，不可为空串 |
| `presence_penalty` | float，-2~2 | 话题新鲜度惩罚 |
| `frequency_penalty` | float，-2~2 | 词频惩罚 |
| `seed` | int | 尽力而为的可复现种子 |
| `logprobs` | bool | 返回 token 对数概率，仅非流式生效 |
| `top_logprobs` | int，0~20 | 需同时 `logprobs: true` |
| `user` | string | 用户标识，默认 `anonymous` |
| `project` | string | 项目标识，默认 `default`；参与限流与预算累计 |
| `budget_usd` | float | 项目预算；`0` 表示不限制 |
| `thinking` | bool | 要求更高思考能力，路由时提高能力权重 |
| `trace_id` | string，可选 | 调用方指定追踪 ID |
| `template_id` | string，可选 | 引用提示词模板 |
| `template_version` | int，可选 | 模板版本，缺省最新 |
| `template_vars` | object | 模板变量；设置 `template_id` 时必填 |

> `model` 为可选字段。省略时网关在**全部已配置 Key 的候选**里按「成本 + 思考能力」评分自动选路，**可能命中任意供应商**（如 qwen / kimi / anthropic），并非固定走 deepseek。响应体的 `model` 字段会回显实际命中的模型。需要固定供应商/模型时，请显式传 `model`，取值范围见 `config/gateway.yaml` 的 `available_models`（如 `deepseek-chat`、`qwen-max`）。下方示例均显式指定 `model`，便于复现。

### 普通对话（非流式）

**Windows（PowerShell）：** 多行命令用反引号 `` ` `` 续行；JSON Body 先写入临时文件再通过 `--data-binary` 发送（避免引号转义问题）。

```powershell
$body = '{"model":"deepseek-chat","messages":[{"role":"user","content":"1+1=?"}],"stream":false}' |
    Out-File "$env:TEMP\req.json" -Encoding ascii -NoNewline

curl.exe -X POST "http://127.0.0.1:8000/v1/chat/completions" `
  -H "Authorization: Bearer sk-gateway-dev" `
  -H "Content-Type: application/json" `
  --data-binary "@$env:TEMP\req.json"
```

**Linux / macOS / Git Bash：** 单引号包裹 JSON、`\` 续行，无需临时文件。`-sS` 静默进度但保留错误输出（连接失败时可看到 `Failed to connect`），`-i` 打印响应状态码与头（便于区分 401 invalid_auth / 200 OK 等）。网关准入 Key 必须是 `.env` 中 `GATEWAY_API_KEYS` 内的值，如 `sk-gateway-dev`（`sk-test` 等未在名单中的 Key 会返回 `401 invalid_auth`）。如需让测试 Key（如 `sk-test`）也可用，可在 `.env` 中追加后重启服务：`GATEWAY_API_KEYS=sk-gateway-dev,sk-gateway-test,sk-test`；生产环境不建议保留此类弱 Key (`sk-test`)。

```bash
curl -sS -i http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-gateway-dev" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"hi"}],"max_tokens":64}'
```

不带 `model` 的自动选路对照（省略即交给网关决定）：

```bash
curl -sS http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-gateway-dev" \
  -d '{"messages":[{"role":"user","content":"hi"}],"max_tokens":64}'
```

响应体 `"model"` 即实际命中的模型名；需要固定时加回 `"model":"deepseek-chat"`。

### 采样参数（top_p / stop / penalties / seed）

除 `temperature` / `max_tokens` / `response_format` 外，请求体还支持以下**可选**采样参数。均可不传；**不传时上游请求体与改造前完全一致**（严格向后兼容）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `top_p` | float，0~1 | 核采样阈值 |
| `stop` | string 或 string[] | 停止序列，最多 4 条、不可为空串 |
| `presence_penalty` | float，-2~2 | 话题新鲜度惩罚 |
| `frequency_penalty` | float，-2~2 | 词频惩罚 |
| `seed` | integer | 尽力而为的可复现采样种子 |
| `logprobs` | boolean | 返回 token 的对数概率（**仅非流式生效**） |
| `top_logprobs` | integer，0~20 | 每个位置返回 top-N 候选；要求同时 `logprobs: true` |

```bash
curl -sS http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-gateway-dev" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"hi"}],
        "top_p":0.9,"stop":["END"],"presence_penalty":0.1,"frequency_penalty":0.2,"seed":42}'
```

> **供应商差异**：OpenAI 兼容供应商原样透传全部字段；Anthropic 仅支持 `top_p` 与 `stop`（映射为 `stop_sequences`，最多 4 条），`presence_penalty` / `frequency_penalty` / `seed` / `logprobs` / `top_logprobs` 会被显式忽略（仅记录 debug 日志，不返回错误）。
>
> `top_logprobs` 上限 20，且必须与 `logprobs: true` 同时使用，否则返回 `400 invalid_request`。未请求 logprobs 时，响应中仍保留 `choices[0].logprobs: null`（对齐 OpenAI 原生行为）。流式请求暂不支持 logprobs，传入会被忽略。
>
> 其余未声明字段（如 `tools`、`n`）仍按「忽略」处理而非报错，避免破坏既有客户端；因此这类参数**不会生效**。

### logprobs：用途与边界

`logprobs` 返回模型生成每个 token 时**采样前的条件概率**（对数形式）：`p = exp(logprob)`，越接近 0 越确定。它描述"模型有多确信"，**不描述"答案是否正确"**。

| 用途 | 做法 |
|---|---|
| 置信度门控 | `exp(logprob)` 低于阈值 → 转人工 / 追问澄清 / 降级到 fallback 模型 |
| 幻觉定位 | 找出低概率 token（通常落在数字、专有名词、日期处），按位置复核而非整段打回 |
| 提示词歧义检测 | 看 `top_logprobs` 前二候选差距；`< 1.0 nat`（概率差 < 2.7 倍）说明模型在摇摆 |
| 版本回归对比 | 同 prompt、同模型跨版本比较几何平均 logprob；**跨模型不可比**（tokenizer 切分不同） |

关键边界：

- **高概率 ≠ 正确**：模型可以"自信地错"，不能替代 `response_format` 校验。
- **条件概率**：整段 `sum(logprob)` 随长度变负，比较长短回答时要用几何平均 `exp(-sum/len)`。
- **top-N 截断**：`top_logprobs` 只返回前 N 名，未列出的候选概率总和可能仍不小。
- **协议限制**：仅非流式生效；Anthropic 显式忽略并返回 `logprobs: null`；部分 OpenAI 兼容供应商即便请求也不返回该字段，网关**原样透传、不伪造**。
- **结构差异**：`logprobs.content` 数组长度 ≠ 字符串长度，首 token 常带前导空格。
- **安全**：等于暴露模型输出分布（可用于蒸馏/提取），多租户对外时应纳入 Key 级能力白名单。

```bash
curl -sS http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-gateway-dev" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"Say hi"}],
        "logprobs":true,"top_logprobs":3}'
```

响应中的关键片段：

```json
"choices": [{
  "message": {"role": "assistant", "content": "Hi!"},
  "logprobs": {"content": [
    {"token": "Hi", "logprob": -0.002, "top_logprobs": [
      {"token": "Hi", "logprob": -0.002},
      {"token": "Hello", "logprob": -6.2}
    ]}
  ]}
}]
```

消费侧判定（`logprobs` 为 `null` 表示上游未返回，不能当 0 处理）：

```python
import math

lp = resp["choices"][0]["logprobs"]
if lp:
    items = lp["content"]
    min_p = min(math.exp(t["logprob"]) for t in items)      # 最犹豫的 token
    ppl = math.exp(-sum(t["logprob"] for t in items) / len(items))  # 跨长度可比的困惑度
    if min_p < 0.5 or ppl < 0.6:
        mark_for_review()          # 低置信 → 人工复核 / 转 fallback
```

| 指标 | 建议阈值 | 含义 |
|---|---|---|
| `min(exp(logprob))` | `< 0.5` 需复核 | 存在模型明显犹豫的 token |
| 几何平均概率 | `< 0.6` 低置信 | 整段输出不稳定 |
| 前二候选差距 | `< 1.0 nat` 有歧义 | 提示词可能含糊，而非模型能力问题 |

### 结构化输出（JSON Schema 校验 + 自动修复）

**Windows（PowerShell）：**

```powershell
$body = '{"model":"deepseek-chat","messages":[{"role":"user","content":"解析用户意图"}],
         "response_format":{"type":"json_schema","json_schema":{"name":"intent","schema":{"type":"object","properties":{"intent":{"type":"string"}},"required":["intent"]}}}}' |
    Out-File "$env:TEMP\req.json" -Encoding ascii -NoNewline

curl.exe -X POST "http://127.0.0.1:8000/v1/chat/completions" `
  -H "Authorization: Bearer sk-gateway-dev" `
  -H "Content-Type: application/json" `
  --data-binary "@$env:TEMP\req.json"
```

**Linux / macOS（Bash）：** body 通过 heredoc 走 stdin（`--data-binary @-`），规避 Windows Git Bash 把命令行参数中的中文按 GBK 编码、导致服务端 JSON 解析失败。

```bash
curl -sS -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-gateway-dev" \
  -H "Content-Type: application/json" \
  --data-binary @- <<'JSON'
{"model":"deepseek-chat",
 "messages":[{"role":"user","content":"解析用户意图"}],
 "response_format":{"type":"json_schema","json_schema":{"name":"intent",
   "schema":{"type":"object","properties":{"intent":{"type":"string"}},"required":["intent"]}}}}
JSON
```

非流式路径在返回前完成校验，并尝试自动修复（如提取首个平衡的 `{...}` / `[...]` 块）。仍不通过则按可重试错误进入重试 / fallback；全部失败返回 `502 structured_output_failed`。

### 流式输出（Server-Sent Events）

**Windows（PowerShell）：** Windows PowerShell 中 `curl` 是 `Invoke-WebRequest` 的别名，必须使用 `curl.exe` 才能支持 `-H`/`--data-binary` 等参数；流式场景还需加 `-N`（`--no-buffer`）关闭缓冲，才能看到逐行输出的效果。幂等写法：先把 JSON 写入临时文件再发送，避免引号转义问题。

```powershell
$json = '{"model":"deepseek-chat","messages":[{"role":"user","content":"数到5"}],"stream":true}'

# PowerShell 5.1 的 -Encoding UTF8 会写入 BOM（非法 JSON 前导字节）；
# 用 .NET 写成无 BOM 的 UTF-8，PS 5.1 / PS 7 行为一致
[System.IO.File]::WriteAllText("$env:TEMP\req.json", $json, (New-Object System.Text.UTF8Encoding($false)))

curl.exe -s -N -X POST "http://127.0.0.1:8000/v1/chat/completions" `
  -H "Authorization: Bearer sk-gateway-dev" `
  -H "Content-Type: application/json" `
  --data-binary "@$env:TEMP\req.json"
```

**Linux / macOS / Git Bash：** `-N` 用于关闭缓冲，逐行输出 SSE 事件。body 走 stdin，避免中文参数被 shell 按 GBK 转码：

```bash
curl -s -N -X POST "http://127.0.0.1:8000/v1/chat/completions" \
  -H "Authorization: Bearer sk-gateway-dev" \
  -H "Content-Type: application/json" \
  --data-binary @- <<'JSON'
{"model":"deepseek-chat","messages":[{"role":"user","content":"数到5"}],"stream":true}
JSON
```

> **Windows Git Bash 必读**：不要写成 `-d '...中文...'`。中文 Windows 下该参数会按 GBK/CP936 传给 curl，服务端收到非法 UTF-8，在进入业务逻辑前就返回 `400 {"detail":"There was an error parsing the body"}`（与流式输出无关）。请使用上面的 stdin 写法，或先把 JSON 存为 UTF-8 文件再 `--data-binary @req.json`。

响应为多条 `data: {...}` 事件，末行 `data: [DONE]` 表示结束。也可直接使用 Swagger UI 交互式测试：浏览器打开 `http://127.0.0.1:8000/docs`。

### 流式 + 结构化输出（validation 事件契约）

当请求同时满足 `stream:true` 且携带 `response_format`（结构化输出）时，网关会在正常 chunk 事件之后、`[DONE]` 之前追加**一条** `validation` 事件，供调用方判断整段流式输出是否通过 schema 校验。

| 字段 | 值 | 说明 |
|---|---|---|
| `type` | `"validation"` | 事件类型标识 |
| `status` | `"ok"` \| `"error"` | 累积内容是否通过 schema 校验 |
| `error` | `null` \| string | 失败时为 validator 的错误描述 |
| `choices` | `[]` | 恒为空数组（非续写内容） |
| `id` / `object` / `created` / `model` | 与其它 chunk 一致 | 便于按唯一 id 关联同一次请求 |

事件序列（结构化输出流式）：

```text
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","model":"deepseek-chat","choices":[{"index":0,"delta":{"content":"{\"intent\":"},"finish_reason":null}]}
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","model":"deepseek-chat","choices":[{"index":0,"delta":{"content":"\"buy\"}"},"finish_reason":"stop"}]}
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","model":"deepseek-chat","type":"validation","status":"ok","error":null,"choices":[]}
data: [DONE]
```

> 普通对话流（未携带 `response_format`）**不会**发送 `validation` 事件，事件序列与 OpenAI 官方一致。

**语义与取舍：** 流式下 delta 已逐块透传给客户端，因此校验只能**事后进行、无法自动修复**；`status:"error"` 表示整段拼接内容不合 schema，调用方应丢弃本次结果或按业务重试。非流式路径则在返回前校验并尝试自动修复，失败会重试/fallback——两者行为不同，需分别处理。

**示例（Bash / Git Bash）：** body 通过 heredoc 走 stdin（`--data-binary @-`），避免 Windows 控制台把参数中的中文转成 GBK 导致服务端 JSON 解析失败。

```bash
curl -sS -N http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-gateway-dev" \
  --data-binary @- <<'JSON'
{"model":"deepseek-chat",
 "messages":[{"role":"user","content":"解析用户意图，返回 JSON"}],
 "stream":true,
 "response_format":{"type":"json_schema","json_schema":{"name":"intent",
   "schema":{"type":"object","properties":{"intent":{"type":"string"}},"required":["intent"]}}}}
JSON
```

### 模板管理 HTTP 端点（`/v1/templates`）

模板管理原本仅 CLI 可用（`python -m cli.gateway_cli templates ...`），现已暴露为 REST 端点，复用网关统一 Bearer 鉴权（默认 key 见 `config/gateway.yaml` 的 `api_keys`；若设置了 `GATEWAY_API_KEYS` 环境变量，则以环境变量为准，YAML 中的 key 被忽略）。

语义约定：

- 每次 `POST` upsert 自动自增 `version` 并保留历史版本；
- **模板不存在 → 404 `not_found`**（GET/DELETE/render/chat 引用均一致）；
- **输入类错误 → 400**：缺模板变量 `prompt_missing_vars`、模板语法错误/参数校验失败 `invalid_request`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/templates` | 列出所有模板（每个 template_id 的最新版本） |
| GET | `/v1/templates/{id}` | 查询模板，`?version=N` 取指定版本，缺省最新 |
| GET | `/v1/templates/{id}/versions` | 列出全部历史版本（升序） |
| POST | `/v1/templates` | 新增版本（自增 version、保留历史） |
| POST | `/v1/templates/{id}/render` | 用变量预览渲染结果（不调用上游） |
| DELETE | `/v1/templates/{id}` | 删除；`?version=N` 删单版本，缺省删全部（响应含 `deleted` 数量） |

**网页管理界面：** 网关内置了一个零依赖的单文件管理页（增删改查 + 版本查看 + 渲染预览），由网关同源提供，浏览器打开即用、无跨域问题：

```text
http://127.0.0.1:8000/ui/templates        # 友好短路径
http://127.0.0.1:8000/ui/templates.html   # 等价（静态挂载）
```

页面顶部填入网关 API Key（须为 `.env` 中 `GATEWAY_API_KEYS` 内的值，如 `sk-gateway-dev`；`sk-test` 等未在名单中的 Key 会返回 `401 invalid_auth`）后点「保存并测试」，后续所有操作自动携带 `Authorization: Bearer ...` 头。Key 仅保存在浏览器 `localStorage`，数据操作仍受 `/v1/templates` 统一鉴权保护。HTML 本身可匿名访问，页面源码见 `app/static/templates.html`。

![模板管理页面示例](../docs/tpl_greet_1.png)

**Windows（PowerShell）：** body 通过 here-string 走 stdin（`--data-binary "@-"`），与下面的 Bash heredoc 一一对应。PS 5.1 的管道默认按 ASCII 编码，中文会变成 `?`，故先统一管道编码为 UTF-8（不带 BOM）：

- `--data-binary "@-"` 的引号不能省：`@` 在 PowerShell 中是保留字符，裸写 `@-` 会报语法错误；
- `$OutputEncoding` 只影响"字符串 → 原生程序 stdin"，不影响控制台显示；
- 用 `New-Object System.Text.UTF8Encoding($false)` 保证不带 BOM；
- 若忘了这一步，中文 body 会得到 `400 {"detail":"There was an error parsing the body"}`。

验证通过时，渲染预览应返回 `HTTP 200` 与 `{"template_id":"tpl-greet","rendered":"你是销售助手，请回答：报价策略"}`。

```powershell
# 0) 统一管道编码为 UTF-8（PS 5.1 默认 ASCII，中文会变成 ?）
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)

# 1) 创建/更新模板（返回新版本号；再次调用同一 id 会自增 version）
@'
{"template_id":"tpl-greet","name":"greet","content":"你是{{role}}助手，请回答：{{question}}"}
'@ | curl.exe -X POST "http://127.0.0.1:8000/v1/templates" `
  -H "Authorization: Bearer sk-gateway-dev" `
  -H "Content-Type: application/json" `
  --data-binary "@-"

# 2) 列出所有模板
curl.exe http://127.0.0.1:8000/v1/templates -H "Authorization: Bearer sk-gateway-dev"

# 3) 查询模板（缺省最新；?version=1 取历史版本）
curl.exe http://127.0.0.1:8000/v1/templates/tpl-greet -H "Authorization: Bearer sk-gateway-dev"
curl.exe "http://127.0.0.1:8000/v1/templates/tpl-greet?version=1" -H "Authorization: Bearer sk-gateway-dev"

# 4) 列出历史版本
curl.exe http://127.0.0.1:8000/v1/templates/tpl-greet/versions -H "Authorization: Bearer sk-gateway-dev"

# 5) 渲染预览（不调用上游，适合调试模板）
@'
{"variables":{"role":"销售","question":"报价策略"}}
'@ | curl.exe -X POST "http://127.0.0.1:8000/v1/templates/tpl-greet/render" `
  -H "Authorization: Bearer sk-gateway-dev" `
  -H "Content-Type: application/json" `
  --data-binary "@-"

# 5b) 排查接口本身（纯 ASCII，不依赖管道编码）
@'
{"variables":{"role":"sales","question":"pricing"}}
'@ | curl.exe -X POST "http://127.0.0.1:8000/v1/templates/tpl-greet/render" `
  -H "Authorization: Bearer sk-gateway-dev" `
  -H "Content-Type: application/json" `
  --data-binary "@-"

# 6) 删除（?version=1 删指定版本；缺省删全部）
curl.exe -X DELETE "http://127.0.0.1:8000/v1/templates/tpl-greet?version=1" `
  -H "Authorization: Bearer sk-gateway-dev"
curl.exe -X DELETE http://127.0.0.1:8000/v1/templates/tpl-greet -H "Authorization: Bearer sk-gateway-dev"
```

**Linux / macOS（Bash）：** body 通过 heredoc 走 stdin，与上面的 PowerShell here-string 一一对应；heredoc 直接按 UTF-8 写入 stdin，**无需**像 PowerShell 那样额外设置管道编码。若在 **Windows Git Bash** 中直接把中文写进 `-d '...'`，命令行参数会被按 GBK 编码传入，服务端收到非 UTF-8 字节 → `400 {"detail":"There was an error parsing the body"}`。

```bash
# 1) 创建/更新模板（返回新版本号；再次调用同一 id 会自增 version）
curl -X POST "http://127.0.0.1:8000/v1/templates" \
  -H "Authorization: Bearer sk-gateway-dev" \
  -H "Content-Type: application/json" \
  --data-binary @- <<'JSON'
{"template_id":"tpl-greet","name":"greet","content":"你是{{role}}助手，请回答：{{question}}"}
JSON
```

```bash
# 2) 列出所有模板
curl http://127.0.0.1:8000/v1/templates -H "Authorization: Bearer sk-gateway-dev"

# 3) 查询模板（缺省最新；?version=1 取历史版本）
curl http://127.0.0.1:8000/v1/templates/tpl-greet -H "Authorization: Bearer sk-gateway-dev"
curl "http://127.0.0.1:8000/v1/templates/tpl-greet?version=1" -H "Authorization: Bearer sk-gateway-dev"

# 4) 列出历史版本
curl http://127.0.0.1:8000/v1/templates/tpl-greet/versions -H "Authorization: Bearer sk-gateway-dev"

# 5) 渲染预览（不调用上游，适合调试模板）
curl -X POST "http://127.0.0.1:8000/v1/templates/tpl-greet/render" \
  -H "Authorization: Bearer sk-gateway-dev" \
  -H "Content-Type: application/json" \
  --data-binary @- <<'JSON'
{"variables":{"role":"销售","question":"报价策略"}}
JSON
```

```bash
# 5b) 排查接口本身（纯 ASCII，不依赖命令行参数编码）
curl -X POST "http://127.0.0.1:8000/v1/templates/tpl-greet/render" \
  -H "Authorization: Bearer sk-gateway-dev" \
  -H "Content-Type: application/json" \
  --data-binary @- <<'JSON'
{"variables":{"role":"sales","question":"pricing"}}
JSON
```

```bash
# 6) 删除（?version=1 删指定版本；缺省删全部）
curl -X DELETE "http://127.0.0.1:8000/v1/templates/tpl-greet?version=1" \
  -H "Authorization: Bearer sk-gateway-dev"
curl -X DELETE http://127.0.0.1:8000/v1/templates/tpl-greet -H "Authorization: Bearer sk-gateway-dev"
```

> body 统一用 `--data-binary @- <<'JSON'` 走 stdin（纯 ASCII 同理），避免 `-d '...'` 的引号/转义在跨 shell 粘贴时出错。**不要把 PowerShell 命令直接粘进 Git Bash**：续行符是 `\` 而不是反引号，JSON 内也不要写 `\"` 转义；反引号在 Bash 中是命令替换，会报 `-H: command not found`，curl 最终因缺少 body 得到 422 `{"type":"missing","loc":["body"],"msg":"Field required"}`。

聊天中引用模板：在 `POST /v1/chat/completions` 请求体中通过扩展字段 `template_id` / `template_version`（缺省最新）/ `template_vars` 注入，渲染结果作为 system 消息参与对话：

```json
{"model":"deepseek-chat",
 "messages":[{"role":"user","content":"hello"}],
 "template_id":"tpl-greet",
 "template_vars":{"role":"销售","question":"报价策略"}}
```

与 CLI 的分工：**HTTP 端点适合在线管理**（需要网关运行中，走统一 Bearer 鉴权）；**CLI 适合离线运维**（无需启动网关，直接操作 SQLite）。CLI 用法见「CLI 运维 → 模板管理（CLI）」。

## 核心机制

### 智能路由

- 候选来自 `available_models`，按「成本 + 思考能力」评分：成本归一化后与能力缺口加权，再除以 `routing_weight`，分数越低越优先。
- 请求带 `thinking: true` 时提高能力权重（成本让位）。
- 硬过滤依次为：显式 `model` 匹配、缺失 Key、熔断 OPEN、上下文窗口（软过滤，全部超限时回退全量）、预算（按 `max_tokens` 预估最大成本）。
- 请求未指定 `model` 时，路由确定首选模型后会补检 per-model 限流层。
- 所有候选都被过滤：无可用模型返回 `503 model_unavailable`，预算不足返回 `402 budget_exhausted`。

### 重试与降级

- 可重试：`408`、`425`、`429`、`5xx`、超时与常见网络错误；默认最多 3 次，指数退避（0.5s 起、上限 8s、20% 抖动）。
- 认证失败（上游 401/403）不重试，对外统一映射为 `502`，不泄漏上游鉴权细节。
- 缺少 Key 视为配置问题：跳过该候选、不重试、不计入熔断。
- 单候选重试耗尽后切换下一候选；流式请求一旦某候选成功即停止，避免多个供应商的内容拼接。
- 流式首 token 后断流会记录 `partial_failed`，并计入熔断失败；客户端断连记录 `cancelled`，不计费。

### 熔断

按 Provider 维护状态机 `CLOSED → OPEN → HALF_OPEN → CLOSED/OPEN`，状态持久化在 SQLite，跨 worker 一致。默认连续失败 5 次打开，30 秒后进入半开并最多放行 3 个探针请求。路由前与每次上游调用前都会检查。

### 分层限流

令牌桶按四层检查，**最严格者生效**：`per-model > per-endpoint > per-project > per-user`。请求被拒返回 `429 rate_limit_exceeded`（含 `retry_after`），且不消耗任何层的令牌；桶缓存有 LRU 上限，防止随机 key 造成内存增长。

### 结构化输出与 Prompt 注入防护

- 按 Provider 能力降级 `response_format`：`json_schema` 原样透传；`json_object` 追加 JSON 提示词并注入完整 schema；`none` 由适配器用提示约束兜底。
- 本地 validator 是唯一校验权威：先严格解析，失败后尝试提取平衡的 `{...}` / `[...]` 块自动修复；仍失败则重试 / fallback。
- 上游对 `response_format` 返回 400 时，去掉该参数并补充 JSON 提示词重试一次。
- schema 校验器按 LRU 缓存（上限 256），防止恶意唯一 schema 撑爆内存。
- 消息中命中角色伪装、指令覆盖等启发式规则时返回 `400 prompt_injection_detected`；该机制是启发式防护，不是绝对安全边界。

### 计费与追踪

成功请求按实际 usage 与分价写入 `usage_metrics`，并同步累计 `daily_aggregates`；`billing_id` 不含 attempt 序号，客户端用同一 `billing_id` 重试不会重复计费。trace 记录请求快照、状态、provider/model、耗时、TTFT、尝试次数与成本；流式记录 TTFT 与半程状态。

## 错误码

| HTTP | `code` | 场景 |
|---|---|---|
| 400 | `invalid_request` | 请求体校验失败、采样参数非法 |
| 400 | `prompt_missing_vars` | 模板变量缺失 |
| 400 | `prompt_injection_detected` | 命中注入启发式规则 |
| 401 | `invalid_auth` | 网关 Key 不在准入名单 |
| 402 | `budget_exhausted` | 预算不足，无候选可承接 |
| 404 | `not_found` | 模板不存在 |
| 429 | `rate_limit_exceeded` | 任一层限流触发 |
| 502 | `upstream_error` | 上游错误且重试/降级均失败 |
| 502 | `structured_output_failed` | 上游 200 但结构校验失败 |
| 503 | `model_unavailable` | 无可用候选（Key/熔断/显式模型不可用） |
| 504 | `upstream_timeout` | 上游超时 |
| 500 | `internal_error` | 未预期异常 |

## 可观测性与健康检查

网关提供三个运维端点，均**无需鉴权、低成本**（`/metrics` 不进入 OpenAPI schema）：

| 端点 | 语义 | 返回 |
|---|---|---|
| `GET /healthz` | **存活**探针：只证明进程在运行，不访问任何依赖 | `200` + `config_version` / `providers` |
| `GET /readyz` | **就绪**探针：校验配置已加载、SQLite 可读写、连接池已初始化 | `200` / `503` + 逐项 `checks` |
| `GET /metrics` | Prometheus 文本格式指标 | `text/plain; version=0.0.4` |

两者分工：容器 `HEALTHCHECK` 用 `/healthz`（避免 SQLite 抖动误杀容器）；负载均衡 / 外部探针用 `/readyz`。Provider 缺 Key 只在 `providers.missing_key` 中降级提示，**不判定为 503**（属配置问题而非进程故障）。

```bash
curl -sS http://localhost:8000/readyz
# {"status":"ok","checks":{"config":{"ok":true,"version":1},"storage":{"ok":true},
#  "http_pool":{"ok":true},"providers":{"ok":false,"missing_key":["openai"]}}}

curl -sS http://localhost:8000/metrics
```

暴露的指标：

| 指标 | 类型 | 含义 |
|---|---|---|
| `llm_gateway_requests_total{endpoint,status}` | counter | 请求量；`status` 为 `success` 或错误码（如 `rate_limit_exceeded`） |
| `llm_gateway_request_duration_seconds_sum/_count{endpoint}` | summary | 处理耗时；流式请求记录「直到响应开始」的耗时 |
| `llm_gateway_rate_limit_rejected_total` | counter | 被限流拒绝的请求数 |
| `llm_gateway_upstream_attempts_total` | counter | 上游调用尝试次数（含重试与降级） |
| `llm_gateway_config_version` | gauge | 当前生效的配置版本号 |
| `llm_gateway_uptime_seconds` | gauge | 进程运行时长 |
| `llm_gateway_build_info{version}` | gauge | 构建版本（恒为 1） |

> 指标为**每进程视角**：多 worker 部署时各 worker 独立计数，`/metrics` 每次只命中单个 worker；跨进程的精确历史数据仍以 SQLite（`usage_metrics` / `daily_aggregates`）与 CLI 为准。

### 指标只读查询端点（`/v1/admin`）

与 CLI 查询同一份 SQLite 数据，但可被脚本/看板直接调用。**需要 Bearer 鉴权**，纯只读，查询规模有硬上限，不提供任何写操作。

| 端点 | 说明 | 关键参数 |
|---|---|---|
| `GET /v1/admin/summary` | 累计概览（请求数 / token / 成本 / 平均时延 / 错误数） | — |
| `GET /v1/admin/traces` | 最近追踪记录（不含 prompt/response 原文） | `limit`(1~200，默认 20)、`status` |
| `GET /v1/admin/daily` | 按天聚合的用量与成本 | `days`(1~90，默认 7)、`provider` |

```bash
curl -sS http://localhost:8000/v1/admin/summary -H "Authorization: Bearer sk-gateway-dev"
curl -sS "http://localhost:8000/v1/admin/traces?limit=10&status=error" -H "Authorization: Bearer sk-gateway-dev"
curl -sS "http://localhost:8000/v1/admin/daily?days=7" -H "Authorization: Bearer sk-gateway-dev"
```

超出上限的参数（如 `limit=99999`）会直接返回 `400 invalid_request`，不做静默截断。

### 客户端 IP 与反向代理（X-Forwarded-For）

trace 中的 `client_ip` **默认取 TCP 对端地址，不采信 `X-Forwarded-For`**，避免调用方伪造审计来源。相关配置在 `gateway.yaml` 的 `server` 段（或用 `GATEWAY_TRUST_PROXY_HEADERS` / `GATEWAY_TRUSTED_PROXIES` 覆盖）：

```yaml
server:
  trust_proxy_headers: false   # 默认关闭
  trusted_proxies:             # 仅当直连对端命中此列表时才解析 XFF
    - "127.0.0.1"
    - "::1"
```

部署在 nginx / 负载均衡之后时**必须**开启并登记代理地址，否则所有请求的 `client_ip` 都会变成代理自身地址（如 `127.0.0.1`）。开启后从右向左跳过可信代理，取第一个非可信地址作为客户端 IP（左侧条目可被客户端任意伪造，因此不会被采用）；非法地址会被忽略并回落对端地址。

### 成本口径：只统计成功请求（观察表）

**预算判定与日报成本只统计成功请求。** 失败请求的上游花费可能已经产生（超时、200 但结构校验失败），但不进入 `usage_metrics`，因此 `query_spent_usd` / `daily_aggregates` 会**系统性偏低**。为在不改变计费行为的前提下量化这一偏差，网关额外维护一张观察表。

| 数据源 | 统计范围 | 是否参与预算 |
|---|---|---|
| `usage_metrics` / `daily_aggregates` | 仅成功请求（`estimated` 行成本为 0） | 是 |
| `unbilled_attempt_costs` | 失败但仍可能计费的尝试（估算值） | 否 |

观察表规则：

- `reason` 取失败的错误码。`upstream_timeout`、`structured_output_failed`、`upstream_error`（4xx 与 429 除外）会记录；400/401/403/404/422/429、缺少 Key、路由阶段即被拒的 `model_unavailable` / `budget_exhausted` / `rate_limit_exceeded` 不记录。
- 成本口径为 `prompt_tokens/1e6 × prompt单价`，completion 记为 0，`estimated` 恒为 1；该值是**下界估算**，不用于对账。
- 主键为 `billing_id:provider:model`（不含 attempt 序号），同候选重试只记一条；一次请求在多个候选上先后失败会产生多行。
- 保留期见 `storage.unbilled_retention_days`（env `GATEWAY_UNBILLED_RETENTION_DAYS`），默认 30 天，`<=0` 表示不清理；启动时清理一次，之后每 6 小时一次，只删该表。

表在启动时随 `executescript` 自动创建（老库无需迁移）。查询示例：

```powershell
@'
import asyncio, json
from app.storage.metrics_store import MetricsStore

async def main():
    s = MetricsStore("data/metrics.db")
    await s.initialize()
    print(json.dumps(await s.summarize_unbilled_attempt_costs(days=7), ensure_ascii=False, indent=2))
    await s.close()

asyncio.run(main())
'@ | .\.venv\Scripts\python.exe -
```

**确认采集在工作**：`error_log` 中 `upstream_timeout` / `structured_output_failed` / `upstream_error` 有量时，观察表也应有数据；若为 0 行，说明采集未生效。交叉验证（列名是 `error_code`，不是 `code`）：

```sql
SELECT error_code, COUNT(*) FROM error_log GROUP BY error_code ORDER BY 2 DESC;
```

主动制造一条样本（端到端验证链路，验证完务必改回）：

1. 把 `http_pool.read_timeout` 临时改为 `0.001`（热生效，无需重启）；
2. 发请求，并用 `project` 字段打标记：

```powershell
curl.exe -sS -X POST http://127.0.0.1:8000/v1/chat/completions `
  -H "Authorization: Bearer sk-gateway-dev" `
  -H "Content-Type: application/json" `
  -d '{\"model\":\"deepseek-chat\",\"project\":\"synthetic\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}'
```

3. 期望响应 `upstream_timeout`，且观察表新增一行 `project_id='synthetic'`；
4. **把 `read_timeout` 改回 `60.0`**。

合成数据统一用 `project_id='synthetic'` 标记，统计时排除。

或直接 SQL（漏算率 = 漏算成本 / (已记成本 + 漏算成本)，排除合成样本与超出保留期的数据）：

```sql
-- 漏算率 = 漏算成本 / (已记成本 + 漏算成本)；排除合成样本与超出保留期的数据
SELECT
  (SELECT COALESCE(SUM(cost_usd),0) FROM usage_metrics
     WHERE created_at >= CAST(strftime('%s','now') AS INTEGER) - 14*86400)      AS billed,
  (SELECT COALESCE(SUM(cost_usd),0) FROM unbilled_attempt_costs
     WHERE created_at >= CAST(strftime('%s','now') AS INTEGER) - 14*86400
       AND COALESCE(project_id,'') <> 'synthetic')                             AS unbilled,
  (SELECT COUNT(*) FROM unbilled_attempt_costs
     WHERE created_at >= CAST(strftime('%s','now') AS INTEGER) - 14*86400
       AND COALESCE(project_id,'') <> 'synthetic')                             AS attempts;
```

> 统计窗口不要超过 `unbilled_retention_days`，否则老数据已被清理，比值会失真。判读建议：观察值是**下界**，实际漏算通常为 2~4 倍；校正后漏算率 < 1% 可忽略，1%~5% 视 `budget_usd` 是否对外承诺决定，> 5% 建议把失败成本纳入预算（届时需先让结构化校验失败携带真实 usage，而非估算）。

## CLI 运维

### 用量 / 追踪 / 熔断查询

**Windows（PowerShell）：**

```powershell
.\.venv\Scripts\python.exe -m cli.gateway_cli summary
.\.venv\Scripts\python.exe -m cli.gateway_cli daily --days 7
.\.venv\Scripts\python.exe -m cli.gateway_cli traces --status error
.\.venv\Scripts\python.exe -m cli.gateway_cli circuit
```

**Linux / macOS（Bash）：**

```bash
.venv/Scripts/python -m cli.gateway_cli summary
.venv/Scripts/python -m cli.gateway_cli daily --days 7
.venv/Scripts/python -m cli.gateway_cli traces --status error
.venv/Scripts/python -m cli.gateway_cli circuit
```

CLI 还支持 `--db` 指定 SQLite 路径（默认 `data/metrics.db`）；`traces` 支持 `--limit` 与 `--status success|error|cancelled`，`daily` 支持 `--days` 与 `--provider`。

### 模板管理（CLI）

模板管理也可全部走 CLI（与「调用示例 → 模板管理 HTTP 端点」操作同一 SQLite 存储，无需启动网关服务、无需鉴权头，适合脚本化/离线运维）。子命令 `list / get / upsert / delete`，行为与 HTTP 端点一致：upsert 自增 version 保留历史，`--version` 缺省取最新或删除全部。

**Windows（PowerShell）：**

```powershell
# 列出所有模板（每个 template_id 的最新版本）
.\.venv\Scripts\python.exe -m cli.gateway_cli templates list

# 查询模板（--version 缺省取最新版本）
.\.venv\Scripts\python.exe -m cli.gateway_cli templates get tpl-greet
.\.venv\Scripts\python.exe -m cli.gateway_cli templates get tpl-greet --version 1

# 新增版本（自增 version、保留历史）
.\.venv\Scripts\python.exe -m cli.gateway_cli templates upsert --id tpl-greet --name greet --content "你是{{role}}助手"

# 删除（--version 删除指定版本；缺省删除全部版本）
.\.venv\Scripts\python.exe -m cli.gateway_cli templates delete tpl-greet --version 1
.\.venv\Scripts\python.exe -m cli.gateway_cli templates delete tpl-greet
```

**Linux / macOS（Bash）：**

```bash
.venv/Scripts/python -m cli.gateway_cli templates list
.venv/Scripts/python -m cli.gateway_cli templates get tpl-greet --version 1
.venv/Scripts/python -m cli.gateway_cli templates upsert --id tpl-greet --name greet --content "你是{{role}}助手"
.venv/Scripts/python -m cli.gateway_cli templates delete tpl-greet
```

对应 HTTP 端点见「模板管理 HTTP 端点」。

## 测试

`tests/` 内的测试均为 **E2E（全 mock）+ 单测**：通过 `conftest.py` 启动一个模拟的 LLM 上游服务（`tests/mock_upstream.py`），并为每个用例创建指向该 mock 的网关实例。**无需真实 Provider API Key，不访问外部网络。**

> 注意：请务必使用虚拟环境中的解释器（Windows 为 `.\.venv\Scripts\python.exe`，Linux/macOS 为 `.venv/bin/python`），不要用系统 `python`（系统解释器未安装 pytest，会报 `No module named pytest`）。

运行全部测试：

```powershell
.\.venv\Scripts\python.exe -m pytest tests -v
```

只运行某个文件 / 某个用例：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_api.py -v
.\.venv\Scripts\python.exe -m pytest tests/test_rate_limiter.py::test_token_bucket_capacity -v
```

带覆盖率：

```powershell
.\.venv\Scripts\python.exe -m pytest tests -v --cov=app --cov-report=term
```

## 常见问题

### 上游请求全部返回 502

若所有发往 mock 上游的用例（`test_chat_completion_nonstream` 等）都报 `Upstream error 502`，通常是 **Windows 系统代理**在作祟：httpx 默认 `trust_env=True` 会把对本地 `127.0.0.1` 端口的上游请求转发给系统代理，代理返回 502。

修复方式：网关上行的 HTTP 客户端禁用系统代理（`SharedHttpClientPool` 中已处理，见 `app/core/http_client_pool.py`）：

```python
httpx.AsyncClient(timeout=timeout, limits=limits, trust_env=False)
```

### `{"detail":"There was an error parsing the body"}`

该 400 由 FastAPI 解析 body 阶段抛出，说明服务端收到的**不是合法 UTF-8 JSON**，与鉴权、schema 无关（因此不会返回 `invalid_auth` / `invalid_request`）。最常见原因：在中文 Windows 的 Git Bash / PowerShell 里用 `curl -d '...中文...'`，命令行参数被按 ANSI 代码页（GBK/CP936）传给 curl，body 变成非法 UTF-8。

规避方式（任选其一）：

1. body 走 stdin：`curl ... --data-binary @- <<'JSON' ... JSON`（推荐，见上方示例）；
2. 先把 JSON 写成**无 BOM 的 UTF-8** 文件，再 `--data-binary @req.json`（PS 5.1 的 `-Encoding UTF8` 会带 BOM，推荐用 .NET `WriteAllText`）；
3. 控制台切 UTF-8 代码页（`chcp.com 65001`），或示例内容改用 ASCII。

### 401 invalid_auth

请求头中的网关 Key 不在 `GATEWAY_API_KEYS` / `gateway.yaml::api_keys` 名单内。确认 Key 与运行中的配置一致；修改环境变量后需重启服务。

### 流式结构化输出校验失败能自动修复吗？

不能。流式 delta 已逐块发给客户端，网关只能在 `[DONE]` 前追加 `validation` 事件；需要在非流式路径使用自动修复，或在业务侧丢弃/重试。

## 设计说明：协议选型（Chat Completions vs Responses API）

本项目上游统一走 **OpenAI Chat Completions 协议**（`POST /v1/chat/completions`），而非 OpenAI 的 **Responses API**（`POST /v1/responses`）。原因：

- **多供应商统一**：网关覆盖 OpenAI、DeepSeek、Kimi、Qwen、Ollama。Chat Completions 是事实上的兼容基线，上述供应商均实现该协议；Responses API 目前仅 OpenAI 官方支持，采用它会破坏「一个适配器通吃全部供应商」的架构对称性（路由/重试/熔断/计费都要分叉）。
- **成熟稳定**：工具调用、结构化输出（`response_format`）、SSE 流式在该协议上均有完善且一致的支持，候选切换时各供应商行为对齐。
- **生态工具链**：主流 SDK 与第三方网关均以 Chat Completions 为兼容目标。

代价是放弃了 Responses API 的服务端状态续接、内置工具（web search / file search / code interpreter）、推理摘要等新特性——这些能力需网关自行编排。若未来需要，可在 openai-compat 适配器内扩展：透传 `tools` / `tool_choice`、在 `StreamEvent` 中增加工具调用事件、为 Provider 标记能力并在路由层过滤，以及用「模型生成 tool_calls → 网关本地执行 → 结果注入下一轮」近似内置工具。如此新增能力只收敛在适配器与路由的少数几个点。

## 验收记录：真实双供应商模型 Fallback

演示环境配置：deepseek 的 `api_base_url` 临时指向不可达地址（用于强制触发重试→fallback），kimi 未配置 `KIMI_API_KEY`，qwen 配置真实 Key。请求未指定 `model`，触发完整候选链。

请求（网关鉴权 key 取 `config/gateway.yaml` 中 `api_keys` 的任一值，本地默认 `sk-gateway-dev`）：

```powershell
# PowerShell
@'
{"messages":[{"role":"user","content":"1+1=?"}],"stream":false}
'@ | curl.exe -s -X POST "http://127.0.0.1:8000/v1/chat/completions" `
  -H "Authorization: Bearer sk-gateway-dev" `
  -H "Content-Type: application/json" `
  --data-binary "@-"
```

```bash
# Git Bash
curl -s -X POST "http://127.0.0.1:8000/v1/chat/completions" \
  -H "Authorization: Bearer sk-gateway-dev" \
  -H "Content-Type: application/json" \
  --data-binary @- <<'JSON'
{"messages":[{"role":"user","content":"1+1=?"}],"stream":false}
JSON
```

响应（摘录，供应商 Key 不出现于任何输出）：

```json
{
  "id": "chatcmpl-84180ab635874d3e962fcf34",
  "object": "chat.completion",
  "model": "qwen-max",
  "choices": [{"index": 0, "message": {"role": "assistant", "content": "1+1=2"},
               "finish_reason": "stop"}],
  "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}
}
```

实际链路与旁证（`cli.gateway_cli` 查询）：

- **deepseek**：上游不可达，按重试策略消耗尝试后失败（`upstream_error`，已记录为 error trace）。
- **kimi**：未配置 Key，被路由阶段按 `has_api_key()` 剔除（不产生非法请求头、不重试、不计熔断失败）。
- **qwen**：200 OK 命中，trace 记录 `provider=qwen / model=qwen-max`，响应 id 为 24 位无连字符格式（`chatcmpl-<24 hex>`）。

```text
$ python -m cli.gateway_cli traces --status error
{ ... "status": "error", "error_code": "upstream_error", ... }   # deepseek 失败链路
$ python -m cli.gateway_cli traces --status success
{ ... "provider": "qwen", "model": "qwen-max", "status": "success", ... }
$ python -m cli.gateway_cli summary
requests           29
prompt_tokens      531
completion_tokens  510
total_tokens       1041
total_cost         $0.0011
avg_latency_ms     2710.6
avg_ttft_ms        677.3
providers          2
errors             16
```

该记录同时验证：候选链评分/重试/熔断过滤、trace 状态列与 payload 一致性、CLI 状态过滤正确。

## 目录结构

```text
app/
  api/chat.py           # /v1/chat/completions、/v1/models 管道
  api/templates.py      # /v1/templates 模板管理 HTTP 端点
  api/admin.py          # /v1/admin 指标只读查询端点（summary/traces/daily）
  static/templates.html # /ui/templates 单文件模板管理页
  core/                 # 鉴权/限流/路由/熔断/取消/连接池/渲染/校验/配置/指标
  adapters/             # openai-compat / anthropic 适配器 + 注册表
  storage/              # MetricsStore（SQLite WAL + 幂等计费）
  main.py               # FastAPI 入口 + 生命周期 + 配置热更新 + /healthz /readyz /metrics
cli/gateway_cli.py      # 运维查询 CLI
config/gateway.yaml     # 配置（支持热更新）
docker/                 # Dockerfile（H3 待评审）
tests/                  # E2E（全 mock）+ 单测
```
