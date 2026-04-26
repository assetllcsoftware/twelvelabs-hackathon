FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY worker/frame_embed/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY worker/frame_embed/main.py ./main.py

# Defaults — Lambda overrides S3_KEY per invocation, the rest are tunable
# via the task definition without rebuilding the image.
ENV FPS=1.0 \
    WIDTH=720 \
    QUALITY=4 \
    PARALLEL=8 \
    THUMB_PREFIX=embeddings/frames

CMD ["python", "main.py"]
