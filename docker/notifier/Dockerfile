FROM python:3.12-alpine
# tzdata for US Central date handling; runs as an unprivileged user that owns /data
RUN apk add --no-cache tzdata ca-certificates \
 && adduser -D -H -u 10001 notifier \
 && mkdir /data && chown notifier /data
USER notifier
ENV PYTHONUNBUFFERED=1
