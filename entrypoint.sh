#!/bin/bash

echo "${CRON_SCHEDULE} /usr/local/bin/python3 /app/restore-exif.py -r /media >> /proc/1/fd/1 2>&1" > /etc/cron.d/whatsapp-tools
chmod 0644 /etc/cron.d/whatsapp-tools
crontab /etc/cron.d/whatsapp-tools

echo "Starting cron: ${CRON_SCHEDULE}"
exec cron -f
