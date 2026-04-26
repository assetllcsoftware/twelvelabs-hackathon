FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY worker/clip_pegasus/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY worker/clip_pegasus/main.py ./main.py

ENV DERIVED_CLIPS_PREFIX=derived/clips \
    PEGASUS_INFERENCE_ID=us.twelvelabs.pegasus-1-2-v1:0 \
    PEGASUS_PROMPT_ID=inspector \
    PEGASUS_TEMPERATURE=0.0

CMD ["python", "main.py"]
