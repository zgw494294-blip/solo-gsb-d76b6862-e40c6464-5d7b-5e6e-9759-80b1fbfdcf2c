FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    INBOX_DB_PATH=/data/inbox.db

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 收件库 SQLite 所在目录（Compose 挂载持久卷）；nobody 可写
RUN mkdir -p /data && chown nobody /data

EXPOSE 8080

USER nobody

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
