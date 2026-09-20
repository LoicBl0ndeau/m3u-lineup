FROM python:3.12-alpine

WORKDIR /app

RUN apk add --no-cache su-exec

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates ./templates
COPY static ./static
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

RUN addgroup -S lineup && adduser -S lineup -G lineup

ENV DB_PATH=/data/lineup.sqlite3
VOLUME /data

EXPOSE 9999
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "app.py"]