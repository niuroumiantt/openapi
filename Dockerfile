FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && groupadd -g 10002 gateway && useradd -u 10002 -g 10002 -M -s /usr/sbin/nologin gateway \
 && mkdir -p /data && chown 10002:10002 /data
COPY gateway ./gateway
COPY upstreams.example.json .
# 生产签出用 umask 077(infra 的 setup_server.sh 与 autopull.sh 都是进程级 077),
# 而 COPY 原样保留源文件的权限位 —— 于是 /app 下是 0600 root:root。本镜像以
# uid 10002 运行,读不了自己的 __init__.py,`python -m gateway.app` 在 import
# 阶段就抛 PermissionError,容器无限重启,入口看到的是 502。
# 这个坑 OA 的镜像踩过并在它的 Dockerfile 里写明了修法,这里是同一条。
RUN chmod -R a+rX /app
ENV GATEWAY_HOST=0.0.0.0 GATEWAY_PORT=8800 GATEWAY_DB=/data/keys.sqlite3 GATEWAY_CONFIG=/data/upstreams.json
USER 10002:10002
VOLUME /data
EXPOSE 8800
CMD ["python", "-m", "gateway.app"]
