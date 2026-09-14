# 上传协议（自建接收端）

本项目**不含任何内置上传端点或内置凭据**。上传默认关闭，只有你同时配置
`TRAJ_PLATFORM_URL` 与 `TRAJ_UPLOAD_TOKEN` 才会启用。

这意味着要用上传功能，你得自己跑一个接收端。这篇文档是实现它所需的全部协议 ——
本文是对 `uploader.py` 现有行为的如实描述，不是理想设计。

> ⚠️ **先读一遍你将要接收什么。** 上传的是**完整对话内容**：提示词、模型回复、
> 源码、命令输出、绝对路径。除三个请求头外**没有内容级脱敏**（见
> [SECURITY.md](../SECURITY.md)）。接收端因此持有和源码同等敏感的数据，
> 请按这个级别做访问控制与留存策略。
>
> ⚠️ **用 HTTPS。** 客户端不做证书固定，但明文 HTTP 会让 token 与全部对话内容
> 在链路上明文传输。

---

## 配置

| 环境变量 | 必填 | 说明 |
| --- | --- | --- |
| `TRAJ_PLATFORM_URL` | 是 | 接收端基地址，如 `https://traj.example.com`。**末尾斜杠会被去掉** |
| `TRAJ_UPLOAD_TOKEN` | 是 | 共享凭据，作为 `X-Upload-Token` 头发送 |
| `TRAJ_USER_ID` | 否 | 随每个文件上传的 form 字段，默认空串 |
| `TRAJ_DEVICE_ID` | 否 | 同上，默认空串 |
| `TRAJ_CLEANUP_AFTER_UPLOAD` | 否 | 默认 `true`；上传确认后删除本地数据文件（保留 `.uploaded` 标记）。设为 `false` 保留 |

两个必填项任一为空即禁用上传，不发任何请求。

---

## 你需要实现两个端点

基地址记为 `$BASE`（即 `TRAJ_PLATFORM_URL` 去掉末尾斜杠）。

### 1. `GET $BASE/api/v1/health`

健康检查。客户端每 **60 秒**探测一次，超时 5 秒。

- 返回 **200** = 健康。其他状态码或连接失败 = 不可达。
- 判定为不可达时，客户端**不再尝试直传**，直接把文件转入重试队列，
  等恢复后补传。所以这个端点挂了不会丢数据，但会让上传全部延迟。
- 响应体内容不做校验，返回空 body 也可以。

### 2. `POST $BASE/api/v1/upload/session-file`

单文件上传，`multipart/form-data`。**一个会话会调用 3 次**（见下方文件类型）。

**请求头**

| 头 | 说明 |
| --- | --- |
| `X-Upload-Token` | 你配置的 `TRAJ_UPLOAD_TOKEN`。**请校验它**，不匹配返回 401 |
| `X-Content-SHA256` | 压缩后文件的 SHA256 十六进制小写 |

**multipart 字段**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `file` | 文件 | **gzip 压缩后**的内容，`Content-Type: application/gzip`，文件名形如 `session.traj.gz` |
| `session_id` | 文本 | 会话 ID。已在客户端校验为 `^[a-zA-Z0-9_-]+$`，但**接收端必须自己再校验一次**再用于拼路径 |
| `file_type` | 文本 | `traj` / `raw` / `events` 三者之一 |
| `tool_source` | 文本 | 来源工具，如 `claude-code` / `codex` |
| `compressed` | 文本 | 恒为 `"true"` |
| `user_id` | 文本 | 可能是空串 |
| `device_id` | 文本 | 可能是空串 |

**一个会话上传的三个文件**

| `file_type` | 原始文件名 | 内容 |
| --- | --- | --- |
| `traj` | `session.traj` | SWE-agent 兼容轨迹（JSON） |
| `raw` | `raw.jsonl` | 原始 API 请求/响应（JSONL） |
| `events` | `events.jsonl` | Hook 事件时间线（JSONL） |

**你必须返回的响应**

| 状态码 | 客户端行为 |
| --- | --- |
| **200** | 成功。若 body 是 JSON 且含 `sha256` 字段，客户端会与本地值**二次比对，不一致则判定失败并重试** —— 所以要么回传正确的 sha256，要么干脆不带这个字段 |
| **409** | 视为成功（幂等去重）。已存在的文件返回 409 即可，客户端不会重试 |
| 其他 | 失败，进入重试。响应体前 200 字符会记入错误信息 |

最小可用的 200 响应：

```json
{"sha256": "<你算出的压缩文件 sha256>"}
```

不想算就返回 `{}`。

---

## 客户端的重试行为（实现接收端时需要知道）

- **单请求超时 120 秒**。大 `raw.jsonl` 压缩后仍可能偏大，别让接收端在这之内不响应。
- **首次上传立即重试 5 次**，之后转入持久化队列。
- **队列指数退避**，base 2、上限 32 秒，累计最多 **50 次**重试（覆盖约 24 小时）。
- **队列每 5 分钟扫描一次**补传，队列文件在 `~/.claude-trace/.upload_queue.jsonl`，
  进程重启后会恢复。
- **三个文件全部确认成功**后才写 `.uploaded` 标记并（默认）清理本地文件。
  任一失败则本地文件保留。

也就是说：**接收端返回非 2xx/409 不会丢数据**，但会持续重试约一天。
如果你想让客户端停止重试某个文件，返回 409。

---

## 最小实现示例

仅供打通链路参考。**它没有做鉴权之外的任何加固，不要直接用于生产。**

```python
#!/usr/bin/env python3
"""最小上传接收端。生产环境请自行加上 HTTPS、限流、来源 IP 限制、留存策略。"""
import hashlib
import os
import re
from pathlib import Path

from aiohttp import web

TOKEN = os.environ["UPLOAD_TOKEN"]          # 与客户端的 TRAJ_UPLOAD_TOKEN 一致
STORE = Path(os.environ.get("STORE", "./received")).resolve()
SAFE_ID = re.compile(r"^[a-zA-Z0-9_-]+$")
SAFE_TYPE = {"traj", "raw", "events"}


async def health(request):
    return web.Response(status=200)


async def upload(request):
    if request.headers.get("X-Upload-Token") != TOKEN:
        return web.json_response({"error": "unauthorized"}, status=401)

    reader = await request.multipart()
    fields, blob, filename = {}, None, None
    while (part := await reader.next()) is not None:
        if part.name == "file":
            filename = part.filename or "unnamed"
            blob = await part.read()
        else:
            fields[part.name] = (await part.read()).decode()

    session_id = fields.get("session_id", "")
    file_type = fields.get("file_type", "")
    # 自己再校验一次：这两个值会进路径
    if not SAFE_ID.match(session_id) or file_type not in SAFE_TYPE:
        return web.json_response({"error": "bad session_id or file_type"}, status=400)
    if blob is None:
        return web.json_response({"error": "no file"}, status=400)

    digest = hashlib.sha256(blob).hexdigest()
    if (claimed := request.headers.get("X-Content-SHA256")) and claimed != digest:
        return web.json_response({"error": "sha256 mismatch"}, status=400)

    dest = STORE / session_id / Path(filename).name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return web.json_response({"sha256": digest}, status=409)   # 幂等
    dest.write_bytes(blob)
    return web.json_response({"sha256": digest}, status=200)


app = web.Application(client_max_size=512 * 1024 * 1024)
app.router.add_get("/api/v1/health", health)
app.router.add_post("/api/v1/upload/session-file", upload)

if __name__ == "__main__":
    # 只绑回环。要对外服务请放在 TLS 反代之后。
    web.run_app(app, host="127.0.0.1", port=8080)
```

跑起来后在客户端侧配置：

```bash
export TRAJ_PLATFORM_URL=http://127.0.0.1:8080
export TRAJ_UPLOAD_TOKEN=<与接收端 UPLOAD_TOKEN 相同>
./install-daemon.sh restart
grep -E '可靠上传已启用|上传未配置' /tmp/claude-trace-proxy.log | tail -3
```

---

## 已知的协议局限

如实列出，避免你在接收端侧白花时间：

- **共享静态 token，无轮换机制**。所有客户端用同一个值，泄露后只能全量换。
- **无客户端身份认证**。`user_id` / `device_id` 是自称的 form 字段，可任意伪造，
  不要当作可信身份用于计费或审计。
- **无签名**。`X-Content-SHA256` 只防传输损坏，不防篡改 —— 持有 token 的人可以
  上传任意内容并附上匹配的 hash。
- **无内容级脱敏**。接收端拿到的是原样对话，密钥清洗（如果需要）得由接收端自己做。
