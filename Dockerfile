FROM python:3.12-alpine

# Tijdzone-data, zodat het maandrapport op Nederlandse tijd verstuurd wordt.
RUN apk add --no-cache tzdata

ENV DB_PATH=/data/kostendeler.db \
    PORT=8099 \
    PYTHONDONTWRITEBYTECODE=1

COPY app /app

CMD ["python3", "-u", "/app/server.py"]
