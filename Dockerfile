FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && groupadd -g 10002 gateway && useradd -u 10002 -g 10002 -M -s /usr/sbin/nologin gateway \
 && mkdir -p /data && chown 10002:10002 /data
COPY gateway ./gateway
COPY upstreams.example.json .
ENV GATEWAY_HOST=0.0.0.0 GATEWAY_PORT=8800 GATEWAY_DB=/data/keys.sqlite3 GATEWAY_CONFIG=/data/upstreams.json
USER 10002:10002
VOLUME /data
EXPOSE 8800
CMD ["python", "-m", "gateway.app"]
