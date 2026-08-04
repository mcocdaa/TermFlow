# API 示例

以下命令假设 B 在本机，变量由安全的 shell 环境或 secret manager 注入。公网部署时将 URL
替换为 `https://relay.example.com`；事件和终端 WebSocket 也必须相应使用 `wss://`。示例
不会执行 `echo "$TERMFLOW_ADMIN_TOKEN"`。这些资源请求假设 TOTP 未启用；启用后先用
`/api/v1/admin/cli-tokens` 和当前验证码换取带 scopes 的 CLI token，再替换下面的 Bearer 值。

```bash
export TERMFLOW_URL='http://127.0.0.1:8765'
export TERMFLOW_ADMIN_TOKEN='<admin-token>'
```

TOTP 开启后的脚本先换取一个 scoped CLI token（下面示例给出全部四个当前 scope）：

```bash
CLI_TOKEN="$(curl -fsS -X POST "$TERMFLOW_URL/api/v1/admin/cli-tokens" \
  -H 'Content-Type: application/json' \
  --data "{\"admin_token\":\"$TERMFLOW_ADMIN_TOKEN\",\"totp_code\":\"<current-6-digit-code>\",\"scopes\":[\"terminal.read\",\"terminal.write\",\"computers.read\",\"computers.write\"]}" \
  | python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')"
```

后续资源请求把 `TERMFLOW_ADMIN_TOKEN` 替换为 `$CLI_TOKEN`；CLI token 是短期凭据，不要提交
到 shell history、脚本或日志。

创建注册码（响应中的 token 只应交给目标 A 一次）：

```bash
curl -fsS -X POST "$TERMFLOW_URL/api/v1/enrollment-tokens" \
  -H "Authorization: Bearer $TERMFLOW_ADMIN_TOKEN"
```

浏览器等同源 C 先交换 HttpOnly 会话 Cookie。示例 cookie jar 只用于演示，不应提交：

```bash
curl -fsS -c /tmp/termflow-cookie -X POST "$TERMFLOW_URL/api/v1/admin/sessions" \
  -H "Origin: $TERMFLOW_URL" \
  -H 'Content-Type: application/json' \
  --data "{\"admin_token\":\"$TERMFLOW_ADMIN_TOKEN\"}"
curl -fsS -b /tmp/termflow-cookie "$TERMFLOW_URL/api/v1/dashboard"
```

如果已在 Web C 设置中启用双重认证，第一次请求会返回 `202` 和 `challenge_id`，此时不要把
Token 再写入 URL，而是用验证器当前 6 位代码完成挑战：

```bash
CHALLENGE_ID='<response-challenge-id>'
curl -fsS -c /tmp/termflow-cookie -X POST \
  "$TERMFLOW_URL/api/v1/admin/sessions/$CHALLENGE_ID/totp" \
  -H "Origin: $TERMFLOW_URL" \
  -H 'Content-Type: application/json' \
  --data '{"code":"<current-6-digit-code>"}'
curl -fsS -b /tmp/termflow-cookie "$TERMFLOW_URL/api/v1/dashboard"
```

列出 Instance 并读取在线拓扑：

```bash
curl -fsS "$TERMFLOW_URL/api/v1/instances" \
  -H "Authorization: Bearer $TERMFLOW_ADMIN_TOKEN"
curl -fsS "$TERMFLOW_URL/api/v1/instances/$INSTANCE_ID/topology" \
  -H "Authorization: Bearer $TERMFLOW_ADMIN_TOKEN"
```

向已有 Pane `%1` 发送普通文字并追加 Enter。URL 中 `%` 编码为 `%25`：

```bash
IDEMPOTENCY_KEY="$(python -c 'import uuid; print(uuid.uuid4())')"
curl -fsS -X POST \
  "$TERMFLOW_URL/api/v1/instances/$INSTANCE_ID/panes/%251/input" \
  -H "Authorization: Bearer $TERMFLOW_ADMIN_TOKEN" \
  -H "Idempotency-Key: $IDEMPOTENCY_KEY" \
  -H 'Content-Type: application/json' \
  --data '{"text":"printf hello-termflow","submit":true}'
```

Python 事件订阅（输出保持为 bytes，由终端渲染层自行处理）：

```python
import asyncio
import base64
import json
import os
from urllib.parse import urlsplit

from websockets.asyncio.client import connect


async def main() -> None:
    instance_id = os.environ["INSTANCE_ID"]
    base = urlsplit(os.environ["TERMFLOW_URL"])
    ws_scheme = "wss" if base.scheme == "https" else "ws"
    url = f"{ws_scheme}://{base.netloc}/api/v1/events?instance_id={instance_id}"
    headers = {"Authorization": f"Bearer {os.environ['TERMFLOW_ADMIN_TOKEN']}"}
    async with connect(url, additional_headers=headers, ping_interval=None) as websocket:
        while True:
            message = json.loads(await websocket.recv())
            if message["type"] == "pane.output":
                raw = base64.b64decode(message["payload"]["data_base64"], validate=True)
                print(f"received {len(raw)} bytes")


asyncio.run(main())
```

原生 App/EXE 的授权不是把管理员 Token 放进应用自己的持久化配置。客户端访问
`/.well-known/oauth-authorization-server` 后，通过 PKCE 和 DPoP 打开系统浏览器；用户在
Web C 的 `/authorize` 页面批准客户端，启用 TOTP 时输入验证码，浏览器再通过
`termflow://auth/callback` 返回一次性结果。这个回调和 token 交换由 Tauri 客户端完成，
不需要手工 curl 模拟。

完整 tmux 控制台使用 `/api/v1/terms/{instance_id}/terminal`：二进制帧是原始终端输入/输出，
JSON 文本帧是 ready、size、bindings、action、error、closed 等控制事件。非浏览器客户端
可继续在握手中发送 Bearer Header；浏览器 Web C 使用同源 HttpOnly Cookie 和 Origin
校验。该通道的 A 权威 rows/cols 是只读的，客户端不得发送 resize。
