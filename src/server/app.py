"""Flask backend for the system-monitoring app."""
import ipaddress
import math
import os
import time

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry
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
RATE_LIMIT_KEY_PREFIX = "rl:visits:"
# Retry-After for the fail-closed 503 "rate limit unavailable"; not tied to the window.
RATE_LIMIT_ERROR_RETRY_AFTER_SECONDS = 2
ALLOWED_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
)
MAX_IP_HEADER_LEN = 45  # longest textual IPv6 (IPv4-mapped) address
# Outage errors flip redis_up; every other error is a data/command error.
_OUTAGE_ERRORS = (redis.ConnectionError, redis.TimeoutError)
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
}
DEFAULT_CLIENT_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "client")
)


# Atomic charge: INCR, attach the window TTL when missing, and roll back with
# DECR when over the limit. Returns {count, pttl}: count is -1 when over limit,
# pttl is the remaining window in ms (negative when the key has no expiry).
_CHARGE_RL_LUA = """
local n = redis.call('INCR', KEYS[1])
if redis.call('TTL', KEYS[1]) < 0 then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
if n > tonumber(ARGV[1]) then
  redis.call('DECR', KEYS[1])
  return {-1, redis.call('PTTL', KEYS[1])}
end
return {n, -1}
"""


def _env_int(name, default):
    """Parse a positive int env var; empty/invalid → default."""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_float(name, default):
    """Parse a positive float env var; empty/invalid → default."""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _canonical_ip(value):
    """Canonical IP string (IPv4-mapped IPv6 -> IPv4, scope id dropped); ValueError if invalid."""
    addr = ipaddress.ip_address(value)
    if addr.version == 6:
        if addr.scope_id:
            addr = ipaddress.IPv6Address(addr.packed)
        addr = addr.ipv4_mapped or addr
    return str(addr)


def make_redis_client():
    return redis.Redis(
        host=os.environ.get("REDIS_HOST", "redis"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        socket_timeout=1,
        socket_connect_timeout=1,
        # redis-py defaults to 10 backoff retries, far beyond the readiness timeout.
        retry=Retry(NoBackoff(), 0),
        password=os.environ.get("REDIS_PASSWORD") or None,
        decode_responses=True,
    )


def create_app(redis_client=None, client_dir=None):
    app = Flask(__name__, static_folder=None)
    client_dir = os.path.abspath(client_dir or os.environ.get("CLIENT_DIR", DEFAULT_CLIENT_DIR))
    rdb = redis_client if redis_client is not None else make_redis_client()
    started = time.time()
    version = os.environ.get("APP_VERSION", "dev")
    visits_rate_limit = _env_int("VISITS_RATE_LIMIT", 30)
    visits_rate_window = _env_float("VISITS_RATE_WINDOW_SECONDS", 60.0)
    rate_window_expire = max(1, math.ceil(visits_rate_window))
    client_ip_header = os.environ.get("VISITS_CLIENT_IP_HEADER", "").strip()

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
    visits_rl_errors = Counter(
        "visits_rate_limit_errors_total",
        "Rate-limit charge failures (non-connection errors); distinct from redis_up",
        registry=registry,
    )
    visits_store_errors = Counter(
        "visits_store_errors_total",
        "Visit counter errors that are not connection outages; distinct from redis_up",
        registry=registry,
    )
    ip_header_invalid = Counter(
        "visits_client_ip_header_invalid_total",
        "Client IP header present but invalid; fell back to remote_addr",
        registry=registry,
    )
    redis_up.set(0)
    # Exposed for tests that must observe gauges without /metrics' check_redis refresh.
    app.extensions["prometheus_registry"] = registry

    def check_redis():
        try:
            rdb.ping()
            redis_up.set(1)
            return True
        except Exception as exc:
            app.logger.warning("redis ping failed: %r", exc)
            redis_up.set(0)
            return False

    def client_ip():
        """Rate-limit key: VISITS_CLIENT_IP_HEADER value when set and a valid IP.

        Only enable behind a proxy that always overwrites the header (nginx sets
        X-Real-IP); otherwise clients could spoof it. Falls back to remote_addr.
        """
        if client_ip_header:
            value = request.headers.get(client_ip_header, "").strip()
            if value:
                try:
                    if len(value) > MAX_IP_HEADER_LEN:
                        raise ValueError("too long")
                    return _canonical_ip(value)
                except ValueError:
                    ip_header_invalid.inc()
        remote = request.remote_addr
        if remote:
            try:
                return _canonical_ip(remote)
            except ValueError:
                return remote
        return "unknown"

    def charge_rate_limit(key):
        """Charge one unit. Returns (admitted, pttl_ms).

        pttl_ms is only meaningful when not admitted. ConnectionError returns
        (True, -1): fall through, the visits INCR will 503. Any other error
        (including TimeoutError) propagates and the caller fails closed.
        """
        try:
            n, pttl = rdb.eval(_CHARGE_RL_LUA, 1, key, visits_rate_limit, rate_window_expire)
        except redis.ConnectionError:
            return True, -1
        return int(n) != -1, int(pttl)

    @app.before_request
    def _start():
        g.t0 = time.perf_counter()

    @app.after_request
    def _record(resp):
        # Use the route template (not the raw path) to bound label cardinality.
        endpoint = request.url_rule.rule if request.url_rule else "unmatched"
        method = request.method if request.method in ALLOWED_METHODS else "OTHER"
        req_count.labels(method, endpoint, str(resp.status_code)).inc()
        if hasattr(g, "t0"):
            req_latency.labels(method, endpoint).observe(time.perf_counter() - g.t0)
        for name, value in SECURITY_HEADERS.items():
            resp.headers[name] = value
        return resp

    @app.get("/live")
    def live():
        # k8s liveness should use this — never depends on Redis.
        return jsonify(status="ok")

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
            except _OUTAGE_ERRORS:
                redis_up.set(0)
                connected = False
            except Exception:
                # Data error (e.g. WRONGTYPE): Redis answered, so stay connected.
                visits = None
        return jsonify(
            service="app",
            version=version,
            uptime_seconds=round(time.time() - started, 3),
            redis={"connected": connected},
            visits=visits,
        )

    @app.post("/api/visits")
    def add_visit():
        key = f"{RATE_LIMIT_KEY_PREFIX}{client_ip()}"
        try:
            admitted, pttl = charge_rate_limit(key)
        except Exception:
            # Fail closed; not a Redis outage, so redis_up is left alone.
            visits_rl_errors.inc()
            return (
                jsonify(error="rate limit unavailable"),
                503,
                {"Retry-After": str(RATE_LIMIT_ERROR_RETRY_AFTER_SECONDS)},
            )
        if not admitted:
            retry_after = (
                max(1, math.ceil(pttl / 1000)) if pttl > 0 else rate_window_expire
            )
            return (
                jsonify(error="rate limit exceeded"),
                429,
                {"Retry-After": str(retry_after)},
            )
        try:
            n = int(rdb.incr(VISITS_KEY))
        except Exception as exc:
            # Unlike a TimeoutError on EVAL (outcome unknown, so fail closed without
            # blaming redis_up), INCR timing out is a plain outage signal.
            app.logger.warning("visits INCR failed: %r", exc)
            try:
                rdb.decr(key)  # give back the charge
            except Exception:
                pass
            if isinstance(exc, _OUTAGE_ERRORS):
                redis_up.set(0)
                return jsonify(error="redis unavailable"), 503
            visits_store_errors.inc()
            return jsonify(error="visits store error"), 503
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
