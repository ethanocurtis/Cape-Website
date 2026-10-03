FROM python:3.12-alpine
# tzdata so UPDATE_TIME is read in local time; git for optional GIT_PULL of curated data
RUN apk add --no-cache tzdata git ca-certificates
WORKDIR /repo
ENV PYTHONUNBUFFERED=1
