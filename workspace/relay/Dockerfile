FROM python:3.11-slim

# 中继仅依赖 Python 标准库（http.server + sqlite3），无需 pip install
WORKDIR /app

COPY backend/ /app/backend/
COPY frontend/ /app/frontend/
COPY seed_demo.py /app/seed_demo.py

RUN mkdir -p /app/data
ENV RELAY_DB=/app/data/relay.db \
    RELAY_HOST=0.0.0.0 \
    RELAY_PORT=8090 \
    RELAY_CALLBACK_ADAPTER=stub \
    PYTHONUNBUFFERED=1

EXPOSE 8090
VOLUME ["/app/data"]

HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/relay/api/health',timeout=3).status==200 else 1)"

CMD ["python3", "/app/backend/server.py"]
