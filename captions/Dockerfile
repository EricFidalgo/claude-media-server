# Python plus ffmpeg, which rewrites videos without their captions. The code is mounted in, like the realdebrid service.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
