FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY Pipfile Pipfile.lock* ./
RUN pip install --no-cache-dir pipenv \
    && pipenv install --system --deploy

COPY app/main.py ./main.py
COPY app/db.py ./db.py
COPY app/search.py ./search.py
COPY app/templates ./templates
COPY app/static ./static
COPY scripts/embed/migrations ./migrations

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
