# LLM Gateway

OpenAI 兼容的自建 LLM 网关：智能路由、容错（retry/fallback/circuit breaker）、分层限流、SSE 流式、结构化输出校验与自动修复、用量/追踪/计费（恰好一次）。

规格依据：[docs/SPEC.md](../docs/SPEC.md)

## 快速开始

以下命令均为 **Windows PowerShell** 语法（若使用 Bash，请将行尾反引号 `` ` `` 替换为 `\`）。

```powershell
# 1. 环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1      # Windows 激活虚拟环境
pip install -r requirements-dev.txt

# 2. 配置（填入各 Provider Key；密钥不硬编码，均从环境变量读取）
Copy-Item .env.example .env       # 再编辑 .env 填入真实 Key
$env:DEEPSEEK_API_KEY = "sk-your-key"   # 或临时设置单个变量

# 3. 启动
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
# 或 python -m app.main

# 4. 冒烟
curl.exe http://127.0.0.1:8000/healthz
curl.exe http://127.0.0.1:8000/v1/models -H "Authorization: Bearer sk-gateway-dev"
```

## 调用示例

> PowerShell 注意事项：多行命令用反引号 `` ` `` 续行；JSON Body 先写入临时文件再通过 `--data-binary` 发送（避免引号转义问题）。

```powershell
# 普通对话（非流式）
$body = '{"model":"deepseek-chat","messages":[{"role":"user","content":"1+1=?"}],"stream":false}' |
    Out-File "$env:TEMP\req.json" -Encoding ascii -NoNewline

curl.exe -X POST "http://127.0.0.1:8000/v1/chat/completions" `
  -H "Authorization: Bearer sk-gateway-dev" `
  -H "Content-Type: application/json" `
  --data-binary "@$env:TEMP\req.json"
```

结构化输出（JSON Schema 校验 + 自动修复）：

```powershell
$body = '{"model":"deepseek-chat","messages":[{"role":"user","content":"解析用户意图"}],
         "response_format":{"type":"json_schema","json_schema":{"name":"intent","schema":{"type":"object","properties":{"intent":{"type":"string"}},"required":["intent"]}}}}' |
    Out-File "$env:TEMP\req.json" -Encoding ascii -NoNewline

curl.exe -X POST "http://127.0.0.1:8000/v1/chat/completions" `
  -H "Authorization: Bearer sk-gateway-dev" `
  -H "Content-Type: application/json" `
  --data-binary "@$env:TEMP\req.json"
```

流式输出（Server-Sent Events）：

> 说明：Windows PowerShell 中 `curl` 是 `Invoke-WebRequest` 的别名，必须使用 `curl.exe`
> 才能支持 `-H`/`--data-binary` 等参数；流式场景还需加 `-N`（`--no-buffer`）关闭缓冲，
> 才能看到逐行输出的效果。幂等写法：先把 JSON 写入临时文件再发送，避免引号转义问题。

```powershell
$json = '{"model":"deepseek-chat","messages":[{"role":"user","content":"数到5"}],"stream":true}'
Set-Content -Path "$env:TEMP\req.json" -Value $json -Encoding UTF8 -NoNewline

curl.exe -s -N -X POST "http://127.0.0.1:8000/v1/chat/completions" `
  -H "Authorization: Bearer sk-gateway-dev" `
  -H "Content-Type: application/json" `
  --data-binary "@$env:TEMP\req.json"
```

响应为多条 `data: {...}` 事件，末行 `data: [DONE]` 表示结束。也可直接使用 Swagger UI
交互式测试：浏览器打开 `http://127.0.0.1:8000/docs`。

## 测试

`tests/` 内的测试均为 **E2E（全 mock）+ 单测**：通过 `conftest.py` 启动一个模拟的
LLM 上游服务（`tests/mock_upstream.py`），并为每个用例创建指向该 mock 的网关实例。
**无需真实 Provider API Key，不访问外部网络。**

> 注意：请务必使用虚拟环境中的解释器 `.\.venv\Scripts\python.exe`，不要用系统
> `python`（系统解释器未安装 pytest，会报 `No module named pytest`）。

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

### 常见问题：上游请求全部返回 502

若所有发往 mock 上游的用例（`test_chat_completion_nonstream` 等）都报
`Upstream error 502`，通常是 **Windows 系统代理**在作祟：httpx 默认 `trust_env=True`
会把对本地 `127.0.0.1` 端口的上游请求转发给系统代理，代理返回 502。

修复方式：网关上行的 HTTP 客户端禁用系统代理（`SharedHttpClientPool` 中已处理，
见 `app/core/http_client_pool.py`）：

```python
httpx.AsyncClient(timeout=timeout, limits=limits, trust_env=False)
```

## CLI 运维

```powershell
.\.venv\Scripts\python.exe -m cli.gateway_cli summary
.\.venv\Scripts\python.exe -m cli.gateway_cli daily --days 7
.\.venv\Scripts\python.exe -m cli.gateway_cli traces --status error
.\.venv\Scripts\python.exe -m cli.gateway_cli circuit
```

## 目录结构

```
app/
  api/chat.py          # /v1/chat/completions 管道
  core/                # 鉴权/限流/路由/熔断/取消/连接池/渲染/校验/配置
  adapters/            # openai-compat / anthropic 适配器 + 注册表
  storage/             # MetricsStore（SQLite WAL + 幂等计费）
  main.py              # FastAPI 入口 + 生命周期 + 配置热更新
cli/gateway_cli.py     # 运维查询 CLI
config/gateway.yaml    # 配置（支持热更新）
docker/                # Dockerfile（H3 待评审）
tests/                 # E2E（全 mock）+ 单测
```
