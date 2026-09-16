# openapi

一个"轻"的本地模型接入层。目标：让 OA（或任何 OpenAI 兼容工具）用同一种写法调用本地模型和云端 API，
换模型只改地址和 Key。

第一个模块是 **gateway**：给本地 Ollama 模型发放"限时"或"限量"的临时 API Key。

```
你的 OA / Python 脚本 / 任何 OpenAI 兼容工具
        │  Authorization: Bearer sk-local-xxx
        ▼
网关 127.0.0.1:8800   ← 检查 Key：过期了吗？额度还有吗？记账
        │
        ▼
Ollama 127.0.0.1:11434 → 你的 27b 模型
```

## 启动

```bash
git clone https://github.com/niuroumiantt/openapi.git
cd openapi
pip install -r requirements.txt
GATEWAY_MODEL=qwen3.8:27b-mxfp8 python -m gateway.app
```

浏览器打开 `http://127.0.0.1:8800`。右上角绿点表示 Ollama 已连接。

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama 地址 |
| `GATEWAY_MODEL` | 空 | 设了就强制所有请求用这个模型；空则由调用方指定 |
| `GATEWAY_HOST` / `GATEWAY_PORT` | `127.0.0.1` / `8800` | 网关监听地址。要给局域网其他设备用就改成 `0.0.0.0` |
| `GATEWAY_DB` | `keys.sqlite3` | Key 数据库文件 |

## 发 Key

管理页上两组按钮：按时间（5 分钟、30 分钟、2 小时、1 天）和按用量（10 万、100 万、1000 万 token）。
Key 只显示一次，数据库里只存哈希。额度归零或过期后 Key 自动失效，也可以手动点"作废"。

也可以用 curl（管理接口只接受本机请求）：

```bash
curl -X POST http://127.0.0.1:8800/admin/keys -H 'content-type: application/json' \
     -d '{"kind":"time","minutes":5}'
curl -X POST http://127.0.0.1:8800/admin/keys -H 'content-type: application/json' \
     -d '{"kind":"tokens","tokens":1000000}'
```

## 用 Key

和调用 OpenAI 完全一样，只换 `base_url` 和 `api_key`：

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8800/v1", api_key="sk-local-替换成你的Key")
resp = client.chat.completions.create(
    model="qwen3.8:27b-mxfp8",
    messages=[{"role": "user", "content": "用一句话总结这份周报"}],
)
print(resp.choices[0].message.content)
```

支持 `/v1/chat/completions`、`/v1/completions`、`/v1/embeddings`、`/v1/models`，流式和非流式都支持。
限量计数用的是 Ollama 返回的 `usage.total_tokens`（含输入、输出和思考过程）。
最后一次请求可能略微超出额度，之后 Key 立即失效。

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest
```

测试用一个假的 Ollama（`tests/fake_ollama.py`）跑通发 Key、扣减、用完失效、过期、作废、错误 Key 拒绝。

## 结构

```
gateway/app.py           网关本体：鉴权、代理、计量（~200 行）
gateway/db.py            SQLite 存 Key（只存哈希）
gateway/static/index.html 管理页面
tests/                   测试
```

## 为什么用 Python 而不是 Rust

Zed 快，是因为它是一个 **编辑器**：每一次按键都要在 16 毫秒内重绘整个界面，它自己写了 GPU 渲染层（GPUI），
这种活只有 Rust/C++ 干得了。而这个项目的每个请求都要等模型算几秒到几十秒，网关本身几乎不占时间。
换成 Rust 省下的是毫秒，模型花的是秒，用户感觉不到区别。

Python 真正的挑战不在性能，而在"不堆屎山"：依赖别乱加、单文件能解决就不拆包、每个功能都有测试。
Rust 的收益要等到做桌面端（像 Zed 那样的原生界面）时才会出现，到时再评估。
