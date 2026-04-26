FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# libgl1 + libglib2 for opencv-python-headless transitive deps;
# ultralytics uses pillow which works on the slim image without extras.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY worker/yolo_detect/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY worker/yolo_detect/main.py ./main.py

# Ultralytics writes a settings.yaml on first import; pin it inside /tmp
# so the read-only Fargate root filesystem never gets in the way.
ENV YOLO_CONFIG_DIR=/tmp/Ultralytics \
    MPLCONFIGDIR=/tmp/matplotlib \
    YOLO_IMGSZ=640 \
    YOLO_CONF=0.10 \
    YOLO_IOU=0.5 \
    YOLO_APPROX_EPS_PX=1.5 \
    YOLO_WAIT_FOR_FRAMES_SEC=900 \
    YOLO_WAIT_POLL_SEC=20

CMD ["python", "main.py"]
