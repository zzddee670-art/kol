# 零依赖，不需要 pip install，所以镜像很小、构建很快。
FROM python:3.12-slim

WORKDIR /app
COPY . /app

ENV KOL_HOST=0.0.0.0 \
    PORT=8787 \
    KOL_NO_BROWSER=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8787

CMD ["python", "server/app.py"]
