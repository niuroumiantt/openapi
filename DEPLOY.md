# 把网关部署到 api.semifly.ai

> 如果服务器由 `niuroumiantt/infra` 统一编排(Lightsail 合并主机),不要按本文手动部署:
> 网关作为 `openapi` 服务已写进 infra 的 compose 与 Caddyfile,push 即部署。本文适用于单独一台机器。

目标：客户用 `https://api.semifly.ai/v1` 和一把 `sk-local-…` 的 Key 调模型。

需要：一台有公网 IP 的 Linux 服务器（你现有的网站服务器就行），域名 DNS 的控制权。

## 1. DNS

在你域名的 DNS 里加一条 A 记录：

| 类型 | 主机 | 值 |
|---|---|---|
| A | `api` | 服务器的公网 IP |

生效通常几分钟。`ping api.semifly.ai` 能解析到那个 IP 就行。

## 2. 服务器上装 Docker（已有则跳过）

```bash
curl -fsSL https://get.docker.com | sh
```

## 3. 拉代码，填配置

```bash
git clone https://github.com/niuroumiantt/openapi.git /opt/openapi
cd /opt/openapi
cp upstreams.example.json upstreams.json
cp .env.example .env
```

编辑 `.env`：把 `GATEWAY_ADMIN_TOKEN` 改成一个长随机串（`openssl rand -hex 24` 可以生成一个），填上你买的 API 的 Key。

编辑 `upstreams.json`：删掉你没有的上游。第一阶段只卖买来的 API，就把 `local` 那组和 `semifly-27b` 删掉。

## 4. 启动

```bash
docker compose up -d --build
docker compose logs -f gateway     # 看到 "管理页面" 一行就好了，Ctrl+C 退出日志
```

Caddy 第一次启动会自动申请 HTTPS 证书，需要 80 和 443 端口没被别的程序占用。
如果服务器上已经有 nginx 或宝塔在用这两个端口，见下面"已有 nginx / 宝塔"。

## 5. 验证

```bash
curl https://api.semifly.ai/v1/key -H "Authorization: Bearer nope"
# 期望: {"detail":"invalid key"}   说明域名、证书、网关全通了
```

浏览器打开 `https://api.semifly.ai`，输入管理口令，发一把 Key，然后：

```bash
curl https://api.semifly.ai/v1/chat/completions \
  -H "Authorization: Bearer sk-local-你的Key" -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"你好"}]}'
```

## 已有 nginx / 宝塔

不用 Caddy。`docker-compose.yml` 里把 `caddy` 那一段整个删掉，只启动 gateway。
然后在宝塔里新建网站 `api.semifly.ai`，开反向代理到 `http://127.0.0.1:8800`，
再申请 SSL。反代要打开 WebSocket / 长连接支持，并关闭响应缓冲，否则流式输出会卡住。
nginx 配置关键行：

```
proxy_pass http://127.0.0.1:8800;
proxy_buffering off;
proxy_read_timeout 600s;
```

## 以后接自己的 GPU

在 AWS 上跑起 Ollama 后，在 `upstreams.json` 的 `upstreams` 里加一组，`base_url` 填那台机器的内网地址加 `/v1`，
在 `models` 里加一个公开名字指向它，然后 `docker compose restart gateway`。客户那边不用改任何东西。

## 更新代码

```bash
cd /opt/openapi && git pull && docker compose up -d --build
```

Key 数据库在 Docker 卷 `gateway-data` 里，更新代码不会丢。
