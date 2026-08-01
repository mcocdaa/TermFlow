# API 示例

以下命令假设 B 在本机，变量由安全的 shell 环境或 secret manager 注入。示例不会执行
`echo "$TERMFLOW_ADMIN_TOKEN"`。

```bash
export TERMFLOW_URL='http://127.0.0.1:8765'
export TERMFLOW_ADMIN_TOKEN='<admin-token>'
```

创建注册码（响应中的 token 只应交给目标 A 一次）：

```bash
curl -fsS -X POST "$TERMFLOW_URL/api/v1/enrollment-tokens" \
  -H "Authorization: Bearer $TERMFLOW_ADMIN_TOKEN"
```

浏览器等同源 C 先交换 HttpOnly 会话 Cookie。示例 cookie jar 只用于演示，不应提交：

```bash
curl -fsS -c /tmp/termflow-cookie -X POST "$TERMFLOW_URL/api/v1/session" \
  -H 'Content-Type: application/json' \
  --data "{\"admin_token\":\"$TERMFLOW_ADMIN_TOKEN\"}"
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

from websockets.asyncio.client import connect


async def main() -> None:
    instance_id = os.environ["INSTANCE_ID"]
    url = f"ws://127.0.0.1:8765/api/v1/events?instance_id={instance_id}"
    headers = {"Authorization": f"Bearer {os.environ['TERMFLOW_ADMIN_TOKEN']}"}
    async with connect(url, additional_headers=headers, ping_interval=None) as websocket:
        while True:
            message = json.loads(await websocket.recv())
            if message["type"] == "pane.output":
                raw = base64.b64decode(message["payload"]["data_base64"], validate=True)
                print(f"received {len(raw)} bytes")


asyncio.run(main())
```

完整 tmux 控制台使用 `/api/v1/terms/{instance_id}/terminal`：二进制帧是原始终端输入/输出，
JSON 文本帧是 ready、size、bindings、action、error、closed 等控制事件。非浏览器客户端
可继续在握手中发送 Bearer Header；浏览器 Web C 使用同源 HttpOnly Cookie 和 Origin
校验。该通道的 A 权威 rows/cols 是只读的，客户端不得发送 resize。
