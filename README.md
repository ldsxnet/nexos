# Nexos.ai OpenAI 兼容代理（FastAPI 版）

将 nexos.ai 的接口封装为 OpenAI 兼容 API，现已重构为 Python + FastAPI。

## 功能

- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/chat/create`
- `POST /v1/chat/switch`
- `GET /v1/chat/current`
- `GET /v1/files/{chat_id}/{file_id}/download`

重点改造：

1. cookie 从 `nexos_accounts.json` 按账号读取。
2. model mapping 从 `nexos_accounts.json` 按账号读取（每个账号独立）。
3. 请求未传 `chat_id` 时，自动创建 chat 并写入 `current-chat.json`。

---

## 安装

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

> Linux/macOS 激活命令：`source .venv/bin/activate`

---

## 配置

复制配置模板：

```bash
copy .env.example .env
```

`.env` 示例见 `.env.example`，常用项：

- `HOST` / `PORT`
- `NEXOS_BASE_URL`
- `NEXOS_ACCOUNTS_FILE`（默认 `./nexos_accounts.json`）
- `CURRENT_CHAT_FILE`（默认 `./current-chat.json`）
- `DISABLE_HISTORY`
- `NEXOS_TIMEOUT`

---

## 运行

```bash
python app.py
```

或：

```bash
uvicorn app:app --host 127.0.0.1 --port 23001
```

---

## 账号选择规则

如果不指定账号，默认使用 `nexos_accounts.json` 第 1 个账号。

可通过以下方式指定账号（二选一）：

1. Header: `X-Nexos-Account-Email: xxx@example.com`
2. Header: `X-Nexos-Account-Index: 1`（从 0 开始）

Body 也支持：

- `account_email`
- `account_index`

---

## Chat ID 规则

优先级：

1. Header `X-Nexos-Chat-Id`
2. Body `chat_id`
3. 自动创建一个新的 chat id（本次改造新增）

自动创建后会写入 `current-chat.json`（按账号保存）。

---

## 示例

### 1) 获取模型

```bash
curl http://localhost:23001/v1/models
```

### 2) 非流式聊天（不传 chat_id，自动创建）

> 下方 `bash` 示例适用于 Linux/macOS/Git Bash。Windows `cmd.exe` 请使用后面的 `cmd` 示例（`cmd` 不支持单引号 JSON 与 `\` 换行续行）。

```bash
curl http://localhost:23001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-opus-4-6",
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

Windows `cmd.exe`（单行，推荐直接复制）：

```cmd
curl http://localhost:23001/v1/chat/completions -H "Content-Type: application/json" -d "{\"model\":\"claude-opus-4-6\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}"
```

Windows PowerShell：

```powershell
curl http://localhost:23001/v1/chat/completions -H "Content-Type: application/json" -d '{"model":"claude-opus-4-6","messages":[{"role":"user","content":"你好"}]}'
```

### 3) 流式聊天（SSE）

```bash
curl -N http://localhost:23001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-opus-4-6",
    "stream": true,
    "messages": [{"role": "user", "content": "请用三句话介绍 FastAPI"}]
  }'
```

> 说明：`-N`（`--no-buffer`）可关闭 curl 输出缓冲，实时看到 `data: ...` 的 SSE 分片。

### 4) 指定账号（按邮箱）

```bash
curl http://localhost:23001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Nexos-Account-Email: tmpsrasfr29n@wachimi.dpdns.org" \
  -d '{
    "model": "gpt-5",
    "messages": [{"role": "user", "content": "hello"}]
  }'
```

### 5) 手动创建对话

```bash
curl -X POST http://localhost:23001/v1/chat/create
```

### 6) 切换当前对话

```bash
curl -X POST http://localhost:23001/v1/chat/switch \
  -H "Content-Type: application/json" \
  -d '{"chatId":"your-chat-id"}'
```

---

## 说明

- 每个账号使用自己 `model_mapping`，同名模型在不同账号下会映射到不同 handler id。
- 模型名支持点号/横线混用（如 `gpt-5.1` 与 `gpt-5-1`）。
- `gemini` 模型 `max_tokens` 会自动限制到 `65536`。
- 图片下载会按代理地址返回，避免客户端直连 nexos 域名。
