# LLM Gateway 规格计划书（SPEC）

| 项目 | 内容 |
| --- | --- |
| 文档名称 | LLM Gateway 规格计划书 |
| 版本 | v1.0 |
| 状态 | 待评审（Draft / Ready for Review） |
| 日期 | 2026-09-11 |
| 适用范围 | 自建 LLM 网关（Self-hosted LLM Gateway） |
| 目标读者 | 技术团队、架构评审、实施工程师 |

> 本文档由架构讨论、需求确认及 Top 5 紧急修复项综合整理而成，作为后续开发与验收的唯一规格依据。文中所有模块名均以逻辑模块形式描述，不绑定具体文件路径。

---

## 1. Problem Statement（问题陈述）

企业内部存在多个 LLM Provider（Kimi、DeepSeek、OpenAI、Qwen、Anthropic 等）与 ≥10 个下游客户端（Agent SDK、内部服务、业务应用）。当前各客户端直接调用各供应商原始 API，存在以下问题：

1. **供应商锁定与切换成本高**：客户端需要适配每家供应商不同的鉴权、请求/响应格式与流式协议。
2. **成本不可控**：模型选择无统一策略，无法按"成本/延迟最低 + 解决能力（thinking）"动态路由，LLM 调用成本敏感。
3. **可靠性不足**：无统一的重试、fallback、熔断、超时与取消机制，无法达成 99.5%~99.9% 可用性目标。
4. **可观测性缺失**：QPS、延迟、Token 用量、TTFT、成本、错误率等指标无统一采集口径。
5. **输出质量不稳定**：结构化输出（JSON Schema）缺乏统一校验与自动修复机制。
6. **安全与限流零散**：鉴权、Prompt 注入防护、per-model/per-endpoint/per-project/per-user 限流均缺失。

目标：构建一个自建的、OpenAI 兼容的 LLM 网关，统一收口所有上游 LLM 调用，提供标准化请求处理、智能路由、容错、限流、观测与结构化输出保障。

---

## 2. Solution（解决方案概述）

构建一个基于 **Python + FastAPI** 的自建 LLM 网关，对外暴露 **OpenAI 兼容的 HTTP / SSE 接口**，对内通过 **Model Adapter 模式**统一接入多个 LLM Provider。网关在请求全链路中完成：

- 请求标准化（OpenAI 兼容格式 ↔ 各供应商格式互转）
- 固化提示词渲染（Jinja2）、输出约束（JSON Schema + 原生 Structured Output）、额度预算（Budget）、链路可追踪信息（Trace）
- 按成本/延迟/解决能力动态挑选可用模型与供应商
- 在超时窗口内执行调用，并具备 retry / fallback / failover / cancel 能力
- 校验返回结果，无效 JSON 自动修复后以 SSE 流式事件下发
- 统计用量并完成链路追踪，保证"计费与追踪恰好一次"
- 返回正常结果或结构化错误信息

---

## 3. 系统架构概述（System Architecture Overview）

### 3.1 总体分层

```
Client / Agent SDK（≥10 个下游客户端）
   │  OpenAI Compatible HTTP / SSE
   ▼
┌──────────────────────────────────────────────────────────────┐
│                    FastAPI 应用层（Gateway）                    │
│  ┌─────────────┐ ┌─────────────┐ ┌──────────────────────┐    │
│  │ AuthCheck   │ │ RateLimiter │ │ Pydantic Validation  │    │
│  │ (Bearer API │ │ (Token      │ │ (入参/出参校验)        │    │
│  │  Key)       │ │  Bucket)    │ │                      │    │
│  └──────┬──────┘ └──────┬──────┘ └──────────┬───────────┘    │
│         └───────────────┼───────────────────┘                │
│                         ▼                                    │
│                   Request Pipeline                          │
└─────────────────────────┬────────────────────────────────────┘
                          │
        ┌─────────────────┼─────────────────┐
        ▼                 ▼                 ▼
┌──────────────┐   ┌────────────┐   ┌──────────────────┐
│PromptRenderer│   │   Router   │   │    Validator     │
│(Jinja2+SQLite)│  │(Priority / │   │ (JSON Schema)    │
│              │   │  Weighted  │   │                  │
│              │   │  Round-    │   │                  │
│              │   │  Robin)    │   │                  │
└──────┬───────┘   └─────┬──────┘   └────────┬─────────┘
       │                 │                   │
       └─────────────────┼───────────────────┘
                         ▼
        ┌────────────────────────────────┐
        │  Retry + Fallback + CircuitBreaker │
        └───────────────┬────────────────┘
                        │
      ┌─────────────────┼──────────────────┐
      ▼                 ▼                  ▼
┌──────────┐    ┌──────────┐        ┌──────────┐
│ OpenAI   │    │ DeepSeek │        │ 其他 Adapter│
│ Adapter  │    │ Adapter  │        │ (Kimi/Qwen/ │
│          │    │          │        │ Anthropic/  │
│          │    │          │        │ Local/Ollama)│
└────┬─────┘    └────┬─────┘        └────┬─────┘
     └───────────────┼───────────────────┘
                     ▼
        ┌───────────────────────────────┐
        │  SharedHttpClientPool（连接池）  │
        └───────────────┬───────────────┘
                        ▼
        ┌───────────────────────────────┐
        │   Structured Output Validator  │
        │   （无效 JSON 自动修复）          │
        └───────────────┬───────────────┘
                        ▼
        ┌───────────────────────────────┐
        │  Usage / Metrics Store (SQLite)│
        │  Token / Cost / Latency / TTFT │
        │  Trace / Billing / Circuit     │
        └───────────────────────────────┘
```

### 3.2 横向支撑组件

| 组件 | 职责 |
| --- | --- |
| CancellationHandler | 客户端断连检测与上游流式连接联动清理 |
| ConfigManager | 配置热更新（文件系统 Watcher + 原子替换） |
| TraceContext | 全链路 Trace ID 生成与传递 |
| API Key 管理 | 密钥动态配置与轮换（多 Key 池） |

### 3.3 关键设计原则

1. **OpenAI 兼容面**：对外接口与 OpenAI Chat Completions 兼容（含 SSE），下游零改造接入。
2. **Adapter 即插即用**：新增供应商仅需实现统一 Adapter 接口，网关核心不感知供应商差异。
3. **State 单一来源**：熔断器状态、计费、指标统一持久化到 SQLite（WAL），跨 worker 一致。
4. **资源收敛**：全网关共享 HTTP 连接池；密钥变更只重建对应供应商的客户端。
5. **故障隔离**：熔断器按 provider 隔离，单点故障不影响全局。
6. **"恰好一次"语义**：计费与用量统计通过幂等写入保证不重复、不丢失。

---

## 4. 核心功能模块说明（Core Functional Modules）

### 4.1 AuthCheck（认证鉴权）

- **方式**：Bearer API Key 为主（`Authorization: Bearer <api_key>`）。
- **密钥来源**：配置中心 / 环境变量 / 密钥库，支持**动态配置与轮换**（H4：当前仅提供 Kimi、DeepSeek 两个测试密钥，其余预留动态下发通道）。
- **行为**：未携带 / 非法 / 过期 Key 返回 `401`；无权限访问特定模型返回 `403`。
- **性能**：密钥校验命中缓存（TTL 缓存），避免每次请求访问数据库。

### 4.2 RateLimiter（限流）

- **算法**：Token Bucket（令牌桶）。
- **粒度优先级（降序）**：`per-model` > `per-endpoint` > `per-project` > `per-user`。
- **当前范围**：token 配额与突发量暂不考虑。
- **行为**：超限返回 `429`，携带 `Retry-After` 头。

### 4.3 Pydantic Validation（入参/出参校验）

- 请求体、响应体统一经过 Pydantic v2 模型校验。
- 出参使用 **JSON Schema** 约束 + 供应商**原生 Structured Output** 双重保障，降低格式偏离概率。
- 非法数据在网关边界即被拦截，保证"不合格的数据不进入管道"。

### 4.4 Request Pipeline（请求管道）

串联鉴权 → 限流 → 标准化 → 渲染 → 路由 → 调用 → 校验 → 统计 → 响应 的完整处理流程，是请求的生命周期编排器。

### 4.5 PromptRenderer（提示词渲染）

- **引擎**：Jinja2 模板 + SQLite 模板存储。
- **能力**：模板版本管理、变量填充、缺失变量检测（缺失时返回结构化错误，不调用上游）。
- **固化**：提示词、输出约束（Schema）、额度预算（Budget）、链路追踪信息在同一请求中一并固化。

### 4.6 Router（模型路由）

- **主策略**：优先选择**成本 + 延迟最低**的可用模型；在能力要求高时（thinking 评判标准）提升"问题解决能力"权重，支持 AI 动态选择。
- **辅助策略**：Priority（优先级）/ Weighted Round-Robin（加权轮询）。
- **约束条件**：预算检查（Budget Exhausted 则拦截）、熔断器状态（OPEN 的 provider 不入选）、模型别名解析。
- **Fallback / Failover**：主选失败时按降级链自动切换备选模型/供应商。

### 4.7 Retry + Fallback + CircuitBreaker（重试与容错）

- **重试**：仅对可重试错误（429、5xx、超时）生效；幂等重试并遵守退避策略；认证失败、非法请求不重试。
- **Fallback/Failover**：失败后按路由降级链切换备选供应商。
- **熔断器**：按 provider 维护状态机 `CLOSED → OPEN → HALF_OPEN`，状态持久化到 SQLite（跨 worker 一致）。
  - `failure_threshold`（默认 5）：失败次数阈值
  - `recovery_timeout`（默认 30s）：OPEN → HALF_OPEN 的恢复窗口
  - `half_open_max_calls`（默认 3）：半开态探针上限
- **超时**：连接 / 读取超时按 provider 独立配置（默认连接 5s、读取 60s）。

### 4.8 Adapter 层（Model Adapter）

- 统一接口：`chat_complete()` / `stream_chat()`，内部完成格式互转（OpenAI 兼容格式 ↔ 供应商原生格式）。
- 首批适配：**OpenAI、DeepSeek、Kimi、Qwen**，扩展 **Anthropic**（H1）、**Local / Ollama**。
- 供应商差异（鉴权头、请求字段、SSE 事件格式、停止原因）全部收敛在 Adapter 内部。

### 4.9 CancellationHandler（取消处理）

- 监听客户端断连（`request.is_disconnected()`）。
- 断连时通过**共享 CancelSignal** 同时取消上游任务并关闭上游流式响应上下文，释放连接资源（修复"取消后上游连接未关闭"问题）。

### 4.10 Structured Output Validator（结构化输出校验与修复）

- 按 JSON Schema 校验上游输出。
- 无效 JSON 进入**自动修复**流程（截取/补全/重试解析），仍失败则触发重试或返回结构化错误。

### 4.11 Usage / Metrics Store（用量与指标存储）

- **存储**：SQLite（异步 + WAL + `INSERT OR IGNORE` 幂等）。
- **指标**：QPS、Latency、Token 用量、TTFT、成本、重试次数、fallback 次数、错误率、错误信息。
- **追踪**：Trace 记录（trace_id、时间戳、请求快照、Schema 快照、预算、用户/项目、端点、客户端 IP、运行时指标）。
- **计费**：按 `billing_id` 唯一约束保证"恰好一次"计费。
- **保留**：日志/指标保留周期以年为单位。
- **查询**：通过 CLI 工具提供指标查询与报表能力（M5）。

### 4.12 ConfigManager（配置管理）

- 配置来源优先级：环境变量 > `.env` > YAML 配置文件 > 默认值。
- **热更新**：文件系统 Watcher 监听配置变化（M4），解析成功后**原子替换**配置指针；解析失败保留旧配置，服务不中断。
- 配置内容：Provider 列表（base_url、成本、thinking 能力、容量、超时、模型别名）、模型列表、限流阈值、熔断参数等。

### 4.13 SharedHttpClientPool（共享连接池）

- 每个 provider base_url 对应**一个** `httpx.AsyncClient`，全网关共享（修复"每 Adapter 自建客户端、Key 变更需重建整个客户端"问题）。
- 连接数上限默认 100，keep-alive 默认 20。

---

## 5. 接口定义（API Definitions）

### 5.1 对外接口（OpenAI 兼容）

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/v1/chat/completions` | POST | 非流式对话补全，返回 JSON |
| `/v1/chat/completions`（`stream: true`） | POST | 流式对话补全，返回 `text/event-stream`（SSE） |
| `/v1/models` | GET | 可用模型列表（供客户端发现能力） |
| `/v1/batches`（预留） | POST | 批量调用（扩展预留） |

### 5.2 请求体（`/v1/chat/completions`）

```json
{
  "model": "gpt-5 | deepseek-chat | kimi-k2 | qwen-max | ...",
  "messages": [{"role": "system|user|assistant", "content": "..."}],
  "stream": true,
  "temperature": 0.7,
  "max_tokens": 2048,
  "response_format": {"type": "json_schema", "json_schema": {"name": "...", "schema": {...}}},
  "user": "user_id",
  "project": "project_id"
}
```

- `response_format` 缺省时无强约束；提供时按 JSON Schema 校验/修复。
- 网关扩展字段：`budget_usd`（额度预算）、`trace_id`（可选，缺省自动生成）。

### 5.3 响应体（非流式）

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "created": 1690000000,
  "model": "deepseek-chat",
  "choices": [{"index": 0, "message": {"role": "assistant", "content": "..."}, "finish_reason": "stop"}],
  "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300}
}
```

### 5.4 SSE 流式事件

标准 OpenAI 格式：`data: {"choices":[{"delta":{...}}]}` … 最终 `data: [DONE]`。网关透传 delta，末尾附加 `usage` 统计事件。

### 5.5 错误响应格式

```json
{
  "error": {
    "code": "rate_limit_exceeded",
    "message": "Rate limit exceeded for model 'deepseek-chat'. Retry after 2s.",
    "type": "rate_limit",
    "retry_after": 2,
    "trace_id": "uuid"
  }
}
```

标准错误码：`invalid_auth`（401）、`forbidden`（403）、`not_found`（404）、`rate_limit_exceeded`（429）、`invalid_request`（400）、`upstream_error`（502）、`upstream_timeout`（504）、`budget_exhausted`（402）、`model_unavailable`（503）、`prompt_missing_vars`（400）。

### 5.6 内部接口（Module 间）

| 模块间调用 | 说明 |
| --- | --- |
| Pipeline → AuthCheck | 鉴权结果 |
| Pipeline → RateLimiter | 消耗令牌，返回是否放行 |
| Pipeline → PromptRenderer | 渲染后的 messages + 缺失变量告警 |
| Pipeline → Router | 选中的 provider / model + fallback 链 |
| Router → CircuitBreaker | 查询/更新 provider 熔断状态 |
| Pipeline → Adapter | 调用上游，返回流或最终结果 |
| Pipeline → Validator | Schema 校验与修复 |
| Pipeline → MetricsStore | 写入指标、trace、计费（幂等） |

---

## 6. 数据流设计（Data Flow Design）

### 6.1 非流式请求主链路

```
客户端 POST /v1/chat/completions
  → 1. AuthCheck：校验 Bearer Key（缓存命中优先）→ 401/403 拦截
  → 2. RateLimiter：per-model → per-endpoint → per-project → per-user 逐级消耗令牌 → 429 拦截
  → 3. Pydantic Validation：请求体校验 → 400 拦截
  → 4. 生成 Trace：trace_id、时间戳、请求快照、Schema 快照、budget 快照固化
  → 5. PromptRenderer：模板渲染，缺失变量检测 → 400 拦截
  → 6. Router：按 成本/延迟/thinking 权重选主选模型；检查熔断状态与预算 → 不可用则走 fallback 链
  → 7. 调用上游（Adapter + SharedHttpClientPool），含超时控制
  → 8. 失败 → Retry（可重试错误）→ 仍失败 → Fallback/Failover 下一候选 → 记录熔断失败计数
  → 9. 成功 → Structured Output Validator 校验/修复 JSON
  → 10. MetricsStore 幂等写入：usage（token/cost/latency/TTFT）+ trace + billing
  → 11. 返回标准化 JSON 响应
```

### 6.2 流式请求主链路

```
同上 1~7（stream: true）
  → 8. 建立 SSE 响应；CancellationHandler 注册 CancelSignal 监听断连
  → 9. 逐块透传上游 delta（SSE 事件）
  → 10. 首 token 到达时间记录 TTFT
  → 11. 结束（[DONE]）或断连/异常：
        · 正常结束 → 累计 token/成本 → 幂等写 metrics + billing
        · 首 token 后断流（上游异常）→ 触发 fallback 或返回错误事件
        · 客户端取消 → CancelSignal 触发，关闭上游流上下文，清理资源
  → 12. 返回完整 SSE 或结构化错误
```

### 6.3 计费与追踪"恰好一次"

- 计费以 `billing_id`（由 trace_id + request 指纹派生）为唯一键，`INSERT OR IGNORE` 幂等写入。
- 即使重试/fallback 多次，同一请求只计费一次；失败请求不计费但记录错误指标。
- Trace 与 usage 分离写入，互不阻塞。

---

## 7. 性能指标（Performance Metrics / SLA）

| 指标 | 目标值 | 说明 |
| --- | --- | --- |
| 可用性（SLA） | 99.5% ~ 99.9% | 月度可用性 |
| 网关侧延迟预算 | 100ms ~ 500ms | 网关内部处理（不含上游 LLM 推理时间） |
| QPS | ~100 | 稳态吞吐 |
| 并发量 | ~200 | 并发连接 |
| 月请求量 | ≥ 35,000 | 月度总请求 |
| 支持 Provider 数 | ≥ 4（Kimi、DeepSeek、OpenAI、Qwen，扩展 Anthropic、Ollama） | 可插拔 |
| 支持下游客户端 | ≥ 10 | 通过 OpenAI 兼容接口接入 |
| 追踪指标 | QPS、Latency、Token、TTFT、成本、重试、fallback、错误率、错误信息 | 全量记录 |
| 日志保留 | 以年为单位 | SQLite + 归档 |
| 单请求开销 | ≤ 50ms（P99） | 鉴权/限流/路由/校验合计，命中缓存前提 |

---

## 8. 安全要求（Security Requirements）

### 8.1 认证与授权
- API Key 认证（Bearer），密钥动态配置与轮换，支持多 Key 池。
- Key 校验带 TTL 缓存与防爆破限制。

### 8.2 Prompt 注入防护（P0）
- 系统提示词与用户输入隔离，渲染时对用户输入做定界/转义。
- 对 `response_format` 约束场景强制输出 JSON，降低注入影响面。
- 检测已知注入模式（角色伪装、指令覆盖等启发式规则），命中则拦截并返回错误。

### 8.3 边界防护
- 请求体大小上限、`max_tokens` 上限、消息条数上限。
- 上游响应大小/时长上限，超限强制截断或断开。

### 8.4 敏感信息
- 当前**不落盘** prompt / response 原文（日志与指标只记录元数据）。
- 暂不实施脱敏/审计、内容过滤与合规约束（见 Out of Scope）。

### 8.5 上游安全
- 上游调用使用网关侧密钥，不向前端透传。
- 对上游地址做白名单/配置化管理，禁止任意 URL 请求（SSRF 防护）。

---

## 9. 错误处理与容错矩阵

| 故障场景 | 网关行为 |
| --- | --- |
| 429（上游限流） | 可重试：退避重试；仍失败 → fallback 下一候选；记录指标 |
| 超时（connect/read） | 可重试：按 provider 超时配置执行；连续超时计入熔断 |
| 认证失败（上游 401/403） | 不可重试：立即返回 `502 upstream_error`，触发密钥轮换告警 |
| 非法 JSON 输出 | Structured Output Validator 自动修复；修复失败 → 重试 → fallback |
| 首 token 后断流 | 视为流中断：尝试 fallback；无法恢复则返回结构化错误事件 |
| 客户端取消 | CancelSignal 联动关闭上游流；资源清理；不计费 |
| Prompt 缺变量 | 400 `prompt_missing_vars`，不调用上游 |
| 预算耗尽 | 402 `budget_exhausted`，不调用上游 |
| Provider 熔断 OPEN | 路由阶段跳过该 provider，直接选备选 |

---

## 10. 用户故事（User Stories）

1. 作为下游客户端开发者，我希望通过 OpenAI 兼容接口调用任意已接入的模型，以便零改造接入网关。
2. 作为应用方，我希望请求自动路由到成本+延迟最低的可用模型，以便降低 LLM 调用成本。
3. 作为高要求任务方，我希望在复杂推理场景下自动切换到 thinking 能力更强的模型，以便保证解决能力。
4. 作为网关管理员，我希望在某个 Provider 故障时自动 fallback 到备选供应商，以便维持 99.5%+ 可用性。
5. 作为运维负责人，我希望网关对 429/超时/断流自动重试与降级，以便减少人工介入。
6. 作为客户端，我希望取消请求时网关及时释放上游资源，以便避免带宽与连接浪费。
7. 作为业务方，我希望输出被 JSON Schema 约束并在非法时自动修复，以便下游程序稳定解析。
8. 作为成本负责人，我希望每次请求都统计 token 与费用且恰好一次，以便成本审计准确。
9. 作为管理员，我希望按 model > endpoint > project > user 分层限流，以便保护关键模型不被滥用。
10. 作为开发者，我希望看到 QPS/延迟/TTFT/错误率/重试/fallback 指标，以便定位问题。
11. 作为开发者，我希望通过 CLI 查询 SQLite 中的用量与 trace，以便离线分析。
12. 作为运维人员，我希望配置热更新且失败时保持旧配置，以便不停机调整模型与密钥。
13. 作为安全负责人，我希望网关拦截 Prompt 注入，以便保护系统提示词。
14. 作为平台方，我希望新增一个 LLM 供应商只实现一个 Adapter，以便低成本扩展。
15. 作为客户端，我希望在预算耗尽时收到明确错误而非无响应，以便前端友好提示。
16. 作为客户端，我希望 Prompt 模板缺失变量时得到明确 400 错误，以便快速修正调用。

---

## 11. 实现决策（Implementation Decisions）

1. **技术栈**：Python + FastAPI + Uvicorn（多 worker）+ httpx（异步客户端）+ Pydantic v2 + Jinja2 + SQLite（aiosqlite）+ cachetools。
2. **对外协议**：OpenAI Chat Completions 兼容（HTTP/SSE），降低下游接入成本。
3. **Adapter 模式**：每个供应商一个 Adapter，统一 `chat_complete/stream_chat` 接口，格式互转收敛在 Adapter 内（新增供应商不修改核心代码）。
4. **限流**：Token Bucket，粒度 per-model > per-endpoint > per-project > per-user；暂不含 token 配额与突发量。
5. **路由**：主策略 成本+延迟最低，叠加 thinking 能力权重；支持 Priority / 加权轮询；预算与熔断状态作为硬约束。
6. **熔断器**：状态持久化 SQLite（跨 worker 单一事实来源），TTL 缓存加速热路径；参数可配置。
7. **存储**：SQLite（WAL 模式、busy_timeout、异步连接池化、按表加锁），承载 metrics / trace / billing / circuit / prompt 模板 / 密钥缓存。
8. **计费幂等**：`billing_id` 唯一约束 + `INSERT OR IGNORE`，保证"恰好一次"。
9. **Trace 模型**：Pydantic v2 BaseModel 冻结快照 + 运行时指标可变容器，降低 GC 压力与复制开销。
10. **取消**：共享 CancelSignal，断连检测与上游任务联动取消并关闭流上下文。
11. **HTTP 连接池**：每 provider base_url 一个共享 AsyncClient，Key 变更仅重建对应客户端。
12. **配置热更新**：文件系统 Watcher + 解析成功后再原子替换指针；失败保留旧配置并告警。
13. **依赖锁定**：全部直接/间接依赖指定明确版本范围（H2），保证开发与生产一致。
14. **密钥管理**：Kimi / DeepSeek 密钥内置供测试，其余供应商密钥通过动态配置下发（H4）。
15. **Docker**：提供 Gateway 镜像构建（H3，详见部署说明）；具体是否启用由后续评审决定。
16. **监控预留**：不集成 Prometheus/Grafana，但指标存储层预留导出接口与配置位（M1）。
17. **追踪系统**：不集成 Jaeger/Tempo，追踪逻辑仅限网关内 Trace 记录（M3）。
18. **CLI 工具**：提供指标查询、trace 查询、报表导出等命令（M5）。

---

## 12. 测试决策（Testing Decisions）

1. **测试原则**：只测外部行为（接口契约、错误码、流事件序列、幂等性），不测内部实现细节。
2. **E2E 测试套件（M2）**：对全部 Provider 交互进行全面 **mock**（不依赖真实第三方服务），覆盖：
   - 各 Provider 请求/响应格式互转正确性
   - SSE 流事件顺序与 `[DONE]` 终止
   - 429 / 超时 / 认证失败 / 非法 JSON / 首 token 断流 / 客户端取消 全故障矩阵
   - Prompt 缺变量、预算耗尽拦截
   - fallback / failover 链切换
   - 计费幂等（重试后仍只计费一次）
3. **单元测试**：RateLimiter（令牌桶边界）、Router（权重与成本排序）、CircuitBreaker（状态机迁移）、Validator（修复算法）、ConfigManager（热更新原子性）。
4. **集成测试**：真实 SQLite 下验证并发写入（WAL）、跨 worker 熔断一致性、热更新生效。
5. **性能冒烟**：本地压测 QPS≈100、并发≈200 下的延迟预算达标性。
6. **测试前置**：mock 服务模拟各供应商真实协议行为，作为 E2E 的固定测试基座。

---

## 13. 部署说明（Deployment）

### 13.1 单机部署（本地/内网）

- 进程模型：Uvicorn 多 worker（默认 4），worker 间共享 SQLite（WAL 支持并发读）。
- 前置 Nginx（可选）：SSL 终止 + 负载均衡 + 连接缓冲。
- 数据目录：SQLite 数据文件（metrics/trace/billing），定期归档，保留周期以年为单位。

### 13.2 Docker（H3，方案待评审确认）

| 组件 | 说明 |
| --- | --- |
| gateway 镜像 | 基于 Python 官方镜像构建，安装锁定版本依赖，暴露 8000 端口 |
| Dockerfile 位置 | 随 Docker 方案评审时确定 |
| Docker Compose | 编排 gateway + Nginx（可选）+ 数据卷挂载（SQLite 持久化） |
| 配置注入 | 通过环境变量 / 挂载配置文件实现，支持热更新 |

> 当前评审点：是否启用 Docker 及 Compose 参数，待 H3 文档评审后定稿。

### 13.3 配置与密钥

- `.env` / 环境变量：端口、worker 数、日志级别、DB 路径。
- YAML：Provider 定义（base_url、成本、thinking、容量、超时、模型别名）、模型列表、限流与熔断参数。
- 热更新：文件变更 → Watcher 触发 → 原子替换，无需重启。

### 13.4 日志与运维（OPC 模式）

- 无专职运维团队 / 无 on-call：要求告警自包含、错误自解释、状态自愈（熔断/fallback）。
- 日志输出到 stdout + 定期归档到 SQLite 指标表；保留以年为单位。

---

## 14. 扩展性考虑（Extensibility）

1. **Provider 扩展**：新增供应商仅新增 Adapter + 配置项，核心管道零改动。
2. **下游扩展**：OpenAI 兼容接口天然支持任意 ≥10 客户端，未来可补充 SDK 示例。
3. **监控扩展**：MetricsStore 预留导出接口，未来接 Prometheus/Grafana 只需实现拉取端点（M1 预留位）。
4. **分布式追踪**：当前不集成 Jaeger/Tempo，但 Trace 记录字段已含标准 trace_id，未来可映射 W3C traceparent。
5. **限流扩展**：预留 token 配额与突发量参数位，后续可平滑开启。
6. **批量接口**：预留 `/v1/batches`，支持未来离线批量任务。
7. **多区域**：当前不做多区域/跨区域容灾（明确 Out of Scope），架构不为此设障碍。
8. **缓存策略**：暂不引入缓存（Out of Scope），预留接口位。

---

## 15. Out of Scope（不在当前范围）

- 脱敏 / 审计、prompt/response 原文记录、内容过滤、合规设定
- 单区域 / 多区域部署、跨区域容灾
- 缓存策略
- Prometheus/Grafana 监控系统集成（仅预留接口）
- Jaeger/Tempo 分布式追踪集成
- Token 配额与突发量限流
- L1~L5 各项改进策略（延后讨论）
- 基础设施即代码 / 云托管（当前无基础设施）

---

## 16. 后续说明（Further Notes）

1. **待办映射**：本文档对应需求优先级 H1~H4、M1~M5；L1~L5 明确延后。
2. **风险提示**：
   - 单机 SQLite 在 200 并发下为潜在瓶颈，已通过 WAL + 异步 + 幂等写入缓解；若 QPS 上探需重新评估。
   - 99.9% 可用性依赖上游质量，网关侧仅能通过熔断/fallback 兜底，需在 SLA 中明确上游责任边界。
   - Prompt 注入防护当前为启发式规则，不可视为绝对安全边界。
3. **验收标准**：满足第 7 节性能指标、第 9 节故障矩阵、第 12 节测试覆盖要求，且 Top 5 修复项（Trace 模型 GC 压力、SQLite 并发与计费幂等、跨 worker 熔断一致、取消后上游连接清理、HTTP 连接池与密钥轮换、配置热更新原子性）均落地验证。
4. **下一步实施顺序建议**：核心管道（Pipeline + Router + Adapter）→ 容错（Retry/Fallback/CircuitBreaker）→ 观测（MetricsStore/Trace）→ 安全与限流 → E2E 测试 → Docker/CLI。

---

> 文档结束。评审通过后以此规格作为开发基线，任何变更需更新本 SPEC 并标注版本。
