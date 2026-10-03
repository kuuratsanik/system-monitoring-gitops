FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Hash-pinned lock (generated from requirements.txt); see the header of that file.
COPY src/server/requirements.lock /app/src/server/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /app/src/server/requirements.lock

COPY src/server /app/src/server
COPY src/client /app/src/client

RUN useradd --system --uid 10001 --no-create-home appuser
USER 10001

WORKDIR /app/src/server
EXPOSE 5000

# One worker keeps Prometheus metrics coherent (they are per-process); threads give concurrency.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "--access-logfile", "-", "app:app"]
