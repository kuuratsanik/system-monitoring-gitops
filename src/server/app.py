"""Flask backend for the system-monitoring app."""
import os
import time

import redis
from flask import Flask, Response, g, jsonify, request, send_from_directory
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

VISITS_KEY = "visits"
DEFAULT_CLIENT_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "client")
)


def make_redis_client():
    return redis.Redis(
        host=os.environ.get("REDIS_HOST", "redis"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        socket_timeout=1,
        socket_connect_timeout=1,
        decode_responses=True,
    )


def create_app(redis_client=None, client_dir=None):
    app = Flask(__name__, static_folder=None)
    client_dir = os.path.abspath(client_dir or os.environ.get("CLIENT_DIR", DEFAULT_CLIENT_DIR))
    rdb = redis_client if redis_client is not None else make_redis_client()
    started = time.time()
    version = os.environ.get("APP_VERSION", "dev")

    # Per-app registry so create_app() can be called repeatedly (tests).
    registry = CollectorRegistry()
    req_count = Counter(
        "http_requests_total", "HTTP requests", ["method", "endpoint", "status"],
        registry=registry,
    )
    req_latency = Histogram(
        "http_request_duration_seconds", "HTTP request latency in seconds",
        ["method", "endpoint"], registry=registry,
    )
    redis_up = Gauge("redis_up", "1 if Redis is reachable, else 0", registry=registry)
    visits_total = Counter("app_visits_total", "Visits recorded via POST /api/visits",
                           registry=registry)
    redis_up.set(0)

    def check_redis():
        try:
            rdb.ping()
            redis_up.set(1)
            return True
        except Exception:
            redis_up.set(0)
            return False

    @app.before_request
    def _start():
        g.t0 = time.perf_counter()

    @app.after_request
    def _record(resp):
        # Use the route template (not the raw path) to bound label cardinality.
        endpoint = request.url_rule.rule if request.url_rule else "unmatched"
        req_count.labels(request.method, endpoint, str(resp.status_code)).inc()
        if hasattr(g, "t0"):
            req_latency.labels(request.method, endpoint).observe(time.perf_counter() - g.t0)
        return resp

    @app.get("/health")
    def health():
        if check_redis():
            return jsonify(status="ok", redis="ok")
        return jsonify(status="degraded", redis="down"), 503

    @app.get("/metrics")
    def metrics():
        check_redis()  # keep redis_up fresh at scrape time
        return Response(generate_latest(registry), content_type=CONTENT_TYPE_LATEST)

    @app.get("/api/status")
    def status():
        connected = check_redis()
        visits = None
        if connected:
            try:
                visits = int(rdb.get(VISITS_KEY) or 0)
            except Exception:
                redis_up.set(0)
                connected = False
        return jsonify(
            service="app",
            version=version,
            uptime_seconds=round(time.time() - started, 3),
            redis={"connected": connected},
            visits=visits,
        )

    @app.post("/api/visits")
    def add_visit():
        try:
            n = int(rdb.incr(VISITS_KEY))
        except Exception:
            redis_up.set(0)
            return jsonify(error="redis unavailable"), 503
        redis_up.set(1)
        visits_total.inc()
        return jsonify(visits=n)

    @app.get("/")
    def index():
        return send_from_directory(client_dir, "index.html")

    @app.get("/static/<path:filename>")
    def static_files(filename):
        # send_from_directory uses safe_join: traversal attempts yield 404.
        return send_from_directory(client_dir, filename)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
