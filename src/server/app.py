"""Flask backend for the system-monitoring app."""
import enum
import math
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
RATE_LIMIT_KEY_PREFIX = "rl:visits:"
# Short backoff when RL charge fails closed (ERROR → 503 rate limit unavailable).
# Clients should wait this many seconds before retrying; not tied to window TTL.
RATE_LIMIT_ERROR_RETRY_AFTER_SECONDS = 2
DEFAULT_CLIENT_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "client")
)


class RateLimitCharge(enum.Enum):
    """Result of charge_rate_limit (visits POST gate).

    CHARGED     — RL unit taken; proceed to visit INCR
    OVER_LIMIT  — over window limit (charge rolled back) → 429
    UNAVAILABLE — Redis connectivity failed *before* any charge → try visit
                  (visit INCR 503s when Redis is down)
    ERROR       — ambiguous / post-charge failure → fail closed 503
                  (never admit without accounting; no second INCR after EVAL timeout)
    """

    CHARGED = "charged"
    OVER_LIMIT = "over_limit"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


# Atomic charge: INCR → reject+DECR if over limit → EXPIRE when TTL missing.
# EXPIRE runs in-script on every charge path so a completed EVAL leaves a TTL
# even if the client later times out waiting for the reply (residual: if the
# script never ran, heal is best-effort only — see _heal_rl_ttl_best_effort).
# Returns charged count, or -1 when over limit (charge rolled back).
_CHARGE_RL_LUA = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local expire = tonumber(ARGV[2])
local n = redis.call('INCR', key)
if n > limit then
  redis.call('DECR', key)
  local ttl = redis.call('TTL', key)
  if ttl < 0 then
    redis.call('EXPIRE', key, expire)
  end
  return -1
end
local ttl = redis.call('TTL', key)
if ttl < 0 then
  redis.call('EXPIRE', key, expire)
end
return n
"""

# Rollback: DECR only when the key exists (avoids recreating a -1 orphan).
_ROLLBACK_RL_LUA = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  return redis.call('DECR', KEYS[1])
end
return 0
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


def _env_truthy(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


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
    visits_rate_limit = _env_int("VISITS_RATE_LIMIT", 30)
    visits_rate_window = _env_float("VISITS_RATE_WINDOW_SECONDS", 60.0)
    rate_window_expire = max(1, math.ceil(visits_rate_window))
    trust_xff = _env_truthy("VISITS_TRUST_XFF")

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
        "Rate-limit charge failures (timeout/ambiguous/post-charge); distinct from redis_up",
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
        except Exception:
            redis_up.set(0)
            return False

    def client_ip():
        """Rate-limit key: proxy hop (remote_addr), or rightmost XFF if trusted.

        Default: request.remote_addr only — do not trust client-supplied
        X-Forwarded-For (leftmost spoofing).

        VISITS_TRUST_XFF=1|true|yes: use the *rightmost* X-Forwarded-For hop
        (closest to us). The edge proxy must set/overwrite XFF; otherwise
        clients can still spoof.
        """
        if trust_xff:
            forwarded = request.headers.get("X-Forwarded-For", "")
            hops = [h.strip() for h in forwarded.split(",") if h.strip()]
            if hops:
                return hops[-1]
        return request.remote_addr or "unknown"

    def rate_limit_key(ip):
        return f"{RATE_LIMIT_KEY_PREFIX}{ip}"

    def _rl_ttl(key):
        try:
            return int(rdb.ttl(key))
        except Exception:
            return -1

    def _retry_after_over_limit(ip):
        """Seconds for Retry-After on 429: remaining RL key TTL, else window ceil."""
        ttl = _rl_ttl(rate_limit_key(ip))
        if ttl > 0:
            return ttl
        return rate_window_expire

    def _ensure_rl_expire(key):
        """Set window TTL when missing. Returns True when key has a non-negative TTL."""
        if _rl_ttl(key) >= 0:
            return True
        try:
            if not bool(rdb.expire(key, rate_window_expire)):
                return False
        except Exception:
            return False
        # Confirm TTL stuck; expire=True with ttl still <0 is treated as failure.
        return _rl_ttl(key) >= 0

    def _heal_rl_ttl_best_effort(key):
        """After TimeoutError: if key exists without TTL, attempt EXPIRE.

        Residual ambiguity: we cannot know whether EVAL/INCR applied. Healing
        only reduces phantom sticky keys when the write completed but the reply
        timed out; if Redis is still unhealthy the heal may also fail.
        """
        try:
            if rdb.exists(key) and _rl_ttl(key) < 0:
                rdb.expire(key, rate_window_expire)
        except Exception:
            pass

    def _best_effort_decr(key):
        """DECR only if the key exists — never recreate a -1 orphan with ttl=-1."""
        try:
            rdb.eval(_ROLLBACK_RL_LUA, 1, key)
            return
        except Exception:
            pass
        try:
            if not rdb.exists(key):
                return
            rdb.decr(key)
        except Exception:
            pass

    def _charge_rate_limit_python(key):
        """Near-atomic charge when EVAL/Lua is unavailable (e.g. fakeredis sans lupa).

        Pre-INCR ConnectionError propagates (caller → UNAVAILABLE).
        After INCR: never fail-open; rollback on error and return OVER_LIMIT/ERROR.
        Over-limit without a bound TTL is ERROR (503), not sticky OVER_LIMIT (429).
        """
        try:
            n = int(rdb.incr(key))
        except redis.ConnectionError:
            raise
        except redis.TimeoutError:
            # Ambiguous: INCR may or may not have applied — do not admit.
            _heal_rl_ttl_best_effort(key)
            return RateLimitCharge.ERROR
        except Exception:
            return RateLimitCharge.ERROR

        if n > visits_rate_limit:
            try:
                rdb.decr(key)
            except Exception:
                # Over-limit but cannot roll back accounting — fail closed.
                return RateLimitCharge.ERROR
            # Must attach TTL before rejecting; otherwise endless sticky 429.
            if not _ensure_rl_expire(key):
                return RateLimitCharge.ERROR
            return RateLimitCharge.OVER_LIMIT

        if not _ensure_rl_expire(key):
            # Charged but cannot bound the window — roll back and fail closed.
            _best_effort_decr(key)
            return RateLimitCharge.ERROR
        return RateLimitCharge.CHARGED

    def charge_rate_limit(ip):
        """Charge one RL unit (Lua INCR+limit+EXPIRE; Python fallback if no EVAL).

        Returns RateLimitCharge:
          CHARGED     — charged; caller may proceed to visit INCR
          OVER_LIMIT  — over limit (charge rolled back); caller should 429
          UNAVAILABLE — Redis connectivity error *before* any charge; fall through
                        to visit INCR (503 when Redis is down)
          ERROR       — timeout/ambiguous/post-charge failure; fail closed 503
        """
        key = rate_limit_key(ip)
        try:
            n = int(
                rdb.eval(
                    _CHARGE_RL_LUA,
                    1,
                    key,
                    visits_rate_limit,
                    rate_window_expire,
                )
            )
            return RateLimitCharge.CHARGED if n != -1 else RateLimitCharge.OVER_LIMIT
        except redis.ConnectionError:
            return RateLimitCharge.UNAVAILABLE
        except redis.TimeoutError:
            # EVAL may have applied — never fall through to a second Python INCR.
            # Best-effort TTL heal if the script completed but reply timed out.
            _heal_rl_ttl_best_effort(key)
            return RateLimitCharge.ERROR
        except Exception:
            # Unknown command / no Lua runtime: use Python path (still charge-first).
            pass
        try:
            return _charge_rate_limit_python(key)
        except redis.ConnectionError:
            return RateLimitCharge.UNAVAILABLE

    def rollback_rate_limit(ip):
        """Undo a successful charge when visit INCR fails."""
        _best_effort_decr(rate_limit_key(ip))

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
        # Charge-first: Lua/Python RL → OVER_LIMIT 429; ERROR 503; else INCR visits.
        # UNAVAILABLE (pre-charge only) falls through to visit INCR (503 if Redis down).
        # RL ERROR is fail-closed but does not flip redis_up (≠ Redis outage).
        ip = client_ip()
        charged = charge_rate_limit(ip)
        if charged is RateLimitCharge.OVER_LIMIT:
            # Prefer remaining rl:visits:<ip> TTL; else ceil(window).
            return (
                jsonify(error="rate limit exceeded"),
                429,
                {"Retry-After": str(_retry_after_over_limit(ip))},
            )
        if charged is RateLimitCharge.ERROR:
            visits_rl_errors.inc()
            # Fixed short backoff — RL path unavailable, not window expiry.
            return (
                jsonify(error="rate limit unavailable"),
                503,
                {"Retry-After": str(RATE_LIMIT_ERROR_RETRY_AFTER_SECONDS)},
            )
        try:
            n = int(rdb.incr(VISITS_KEY))
        except Exception:
            if charged is RateLimitCharge.CHARGED:
                rollback_rate_limit(ip)
            redis_up.set(0)
            # Genuine redis unavailable: no Retry-After policy for this path.
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
