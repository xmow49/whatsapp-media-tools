FROM python:3.12-slim

ENV TZ=Europe/Paris
ENV PYTHONUNBUFFERED=1


RUN apt-get update && apt-get install -y \
    cron \
    exiftool \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY restore-exif.py .
COPY requirements.txt .

RUN pip install -r requirements.txt

COPY entrypoint.sh entrypoint.sh
RUN chmod +x entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]
