FROM python:3.12-alpine

ENV DB_PATH=/data/kostendeler.db \
    PORT=8099 \
    PYTHONDONTWRITEBYTECODE=1

COPY app /app

CMD ["python3", "-u", "/app/server.py"]
