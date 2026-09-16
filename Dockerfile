FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY gateway ./gateway
ENV GATEWAY_HOST=0.0.0.0 GATEWAY_PORT=8800 GATEWAY_DB=/data/keys.sqlite3 GATEWAY_CONFIG=/app/upstreams.json
VOLUME /data
EXPOSE 8800
CMD ["python", "-m", "gateway.app"]
