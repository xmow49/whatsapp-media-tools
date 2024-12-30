FROM python:3.12-slim

ENV TZ=Europe/Paris
ENV PYTHONUNBUFFERED=1


RUN apt-get update && apt-get install -y \
    cron \
    libimage-exiftool-perl \
    ffmpeg \
    exiv2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY entrypoint.sh entrypoint.sh

COPY restore-exif.py .
RUN chmod +x entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]
