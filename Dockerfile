FROM python:3.12-alpine

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates ./templates
COPY static ./static

ENV DB_PATH=/data/lineup.sqlite3
VOLUME /data

EXPOSE 8080
CMD ["python3", "app.py"]
