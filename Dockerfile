FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY src/server/requirements.txt /app/src/server/requirements.txt
RUN pip install --no-cache-dir -r /app/src/server/requirements.txt

COPY src/server /app/src/server
COPY src/client /app/src/client

RUN useradd --system --uid 10001 --no-create-home appuser
USER 10001

WORKDIR /app/src/server
EXPOSE 5000

# One worker keeps Prometheus metrics coherent (they are per-process); threads give concurrency.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "--access-logfile", "-", "app:app"]
