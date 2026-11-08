# AI Agent 全栈工程师训练营的作业交付
## Navigation
### [ch1：第一章 llm gateway 的作业练习](ch1/llm-gateway/)
实现一个自建 **LLM 网关（llm-gateway）**，基于 **Python + FastAPI**，对外暴露 **OpenAI 兼容的 HTTP / SSE 接口**，统一收口多个 LLM Provider（如 Kimi、DeepSeek、OpenAI、Qwen）。网关贯穿完整调用链路：请求标准化、提示词渲染（Jinja2）、按成本 / 延迟 / thinking 能力动态路由选择模型、重试 / 降级 / 熔断容错、鉴权与 Token Bucket 限流、JSON Schema 结构化输出校验与自动修复，以及基于 SQLite 的用量 / 成本 / Trace 指标与"恰好一次"计费存储。

### [ch2：第二章转账功能的作业练习](ch2/tool_governance/)
实现一个 **工具治理演示框架（tool_governance）**，模拟 AI Agent 调用工具时的完整治理链路：**参数校验 → 权限判断 → 业务预检 → 人工审批 → 超时处理 → 结果脱敏 → 审计追踪**。项目在既有框架上新增 **transfer（跨账户转账）** 工具，覆盖 Pydantic 防注入校验、RBAC / 白名单 / plan 模式权限状态机、一次性参数绑定审批（SHA-256 摘要 + TTL）、非幂等写超时兜底、账户号脱敏与决策 / 执行两阶段审计。

### [ch3：第三章 guarded coding harness 的作业练习](ch3/guarded_coding_harness/)
实现一个 **受控编码执行框架（guarded_coding_harness）**，基于 **Python + smolagents 的 `ToolCallingAgent`**，在不改写内核源码的前提下仅覆写 `process_tool_calls` 一处拦截缝，为自主 Coding Agent 增加一层可恢复的治理层。框架贯穿完整调用链路：工具执行前统一门禁（参数校验 / 风险分级 / 重复动作 / 预算 / 写审批）、沙箱边界（路径三区与真实落点校验、命令白名单、超时与取消、真实终止进程组、环境变量与输出裁剪）、写入与测试运行的意图登记与 `EXECUTED / NOT_EXECUTED / UNCERTAIN` 三态对账、审批的签名 + 工区指纹 + TTL 绑定、基于 Checkpoint 的跨进程恢复（暂停可续、取消终态、阻断标记不可洗白）、只认当前指纹下真实测试证据的完成契约，以及 append-only 哈希链审计与按 `run_id` / `tool_call_id` 的决策链溯源；配套脚本化 Fake Model 覆盖项目要求的全部验收场景，`pytest -q` 全绿且不依赖真实模型。