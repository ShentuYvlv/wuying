FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src
ENV ADB_PATH=/usr/bin/adb

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends adb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["python", "run.py", "api"]
