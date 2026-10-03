import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import fakeredis
import pytest
import redis
from prometheus_client import generate_latest

from app import (
    RATE_LIMIT_ERROR_RETRY_AFTER_SECONDS,
    RATE_LIMIT_KEY_PREFIX,
    VISITS_KEY,
    create_app,
    make_redis_client,
)


def _assert_retry_after(resp, *, min_seconds=1, max_seconds=None):
    """Retry-After must be present and a plausible positive integer (seconds)."""
    raw = resp.headers.get("Retry-After")
    assert raw is not None, "expected Retry-After header"
    seconds = int(raw)
    assert seconds >= min_seconds
    if max_seconds is not None:
        assert seconds <= max_seconds
    return seconds


class DownRedis:
    """Stand-in client whose every call fails like an unreachable server."""

    def __getattr__(self, name):
        def fail(*a, **k):
            raise redis.ConnectionError("down")
        return fail


class VisitIncrFailRedis:
    """fakeredis that charges RL via EVAL but fails INCR on the visits key."""

    def __init__(self, inner=None):
        self._inner = inner or fakeredis.FakeRedis(decode_responses=True)
        self.visit_incr_calls = 0
        self.decr_calls = []

    def incr(self, name, *args, **kwargs):
        if name == VISITS_KEY:
            self.visit_incr_calls += 1
            raise redis.ConnectionError("visits incr failed")
        return self._inner.incr(name, *args, **kwargs)

    def decr(self, name, *args, **kwargs):
        self.decr_calls.append(name)
        return self._inner.decr(name, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class TimeoutEvalRedis:
    """EVAL times out; must fail closed without a second INCR."""

    def __init__(self, inner=None):
        self._inner = inner or fakeredis.FakeRedis(decode_responses=True)
        self.eval_calls = 0
        self.incr_calls = []

    def eval(self, *args, **kwargs):
        self.eval_calls += 1
        raise redis.TimeoutError("eval timed out")

    def incr(self, name, *args, **kwargs):
        self.incr_calls.append(name)
        return self._inner.incr(name, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class RlDownVisitsUpRedis:
    """RL path ConnectionError; visits INCR still works (pre-charge fallthrough)."""

    def __init__(self, inner=None):
        self._inner = inner or fakeredis.FakeRedis(decode_responses=True)

    def eval(self, *args, **kwargs):
        raise redis.ConnectionError("rl down")

    def incr(self, name, *args, **kwargs):
        if str(name).startswith(RATE_LIMIT_KEY_PREFIX):
            raise redis.ConnectionError("rl down")
        return self._inner.incr(name, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.fixture
def client_dir(tmp_path):
    (tmp_path / "index.html").write_text("<h1>hello</h1>")
    (tmp_path / "app.js").write_text("console.log(1)")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.css").write_text("a{}")
    # a secret outside the served dir, sibling-named to test prefix tricks
    (tmp_path.parent / "secret.txt").write_text("secret")
    return tmp_path


@pytest.fixture
def ok(client_dir):
    return create_app(fakeredis.FakeRedis(decode_responses=True), str(client_dir)).test_client()


@pytest.fixture
def down(client_dir):
    return create_app(DownRedis(), str(client_dir)).test_client()


def test_live_ok(ok, down):
    for c in (ok, down):
        r = c.get("/live")
        assert r.status_code == 200
        assert r.get_json() == {"status": "ok"}


def test_health_ok(ok):
    r = ok.get("/health")
    assert r.status_code == 200
    assert r.get_json() == {"status": "ok", "redis": "ok"}


def test_health_degraded(down):
    r = down.get("/health")
    assert r.status_code == 503
    assert r.get_json() == {"status": "degraded", "redis": "down"}


def test_status_ok(ok):
    ok.post("/api/visits")
    j = ok.get("/api/status").get_json()
    assert j["service"] == "app"
    assert isinstance(j["version"], str)
    assert isinstance(j["uptime_seconds"], (int, float))
    assert j["redis"] == {"connected": True}
    assert j["visits"] == 1


def test_status_zero_visits(ok):
    assert ok.get("/api/status").get_json()["visits"] == 0


def test_status_redis_down(down):
    r = down.get("/api/status")
    assert r.status_code == 200
    j = r.get_json()
    assert j["redis"] == {"connected": False}
    assert j["visits"] is None


def test_version_env(monkeypatch, client_dir):
    monkeypatch.setenv("APP_VERSION", "1.2.3")
    c = create_app(fakeredis.FakeRedis(), str(client_dir)).test_client()
    assert c.get("/api/status").get_json()["version"] == "1.2.3"


def test_visits_increment(ok):
    assert ok.post("/api/visits").get_json() == {"visits": 1}
    assert ok.post("/api/visits").get_json() == {"visits": 2}


def test_visits_redis_down(down):
    r = down.post("/api/visits")
    assert r.status_code == 503
    assert r.get_json() == {"error": "redis unavailable"}
    # Genuine redis-down path: no Retry-After inventing for this task.
    assert r.headers.get("Retry-After") is None


def test_visits_rate_limit_429(monkeypatch, client_dir):
    """Charge-first fixed window: N successes then 429; over-limit does not incr visits."""
    monkeypatch.setenv("VISITS_RATE_LIMIT", "3")
    monkeypatch.setenv("VISITS_RATE_WINDOW_SECONDS", "60")
    rdb = fakeredis.FakeRedis(decode_responses=True)
    c = create_app(rdb, str(client_dir)).test_client()
    for _ in range(3):
        assert c.post("/api/visits").status_code == 200
    r = c.post("/api/visits")
    assert r.status_code == 429
    assert r.get_json() == {"error": "rate limit exceeded"}
    ttl = int(rdb.ttl(list(rdb.scan_iter(f"{RATE_LIMIT_KEY_PREFIX}*"))[0]))
    retry_after = _assert_retry_after(r, min_seconds=1, max_seconds=60)
    # Prefer remaining RL key TTL when available.
    assert retry_after == ttl or abs(retry_after - ttl) <= 1
    assert int(rdb.get(VISITS_KEY) or 0) == 3
    # Over-limit charge is rolled back; RL key stays at the limit.
    rl_keys = list(rdb.scan_iter(f"{RATE_LIMIT_KEY_PREFIX}*"))
    assert len(rl_keys) == 1
    assert int(rdb.get(rl_keys[0])) == 3
    assert rdb.ttl(rl_keys[0]) > 0
    body = c.get("/metrics").get_data(as_text=True)
    assert "app_visits_total 3.0" in body


def test_visits_503_does_not_burn_rate_limit(monkeypatch, client_dir):
    """Redis-down on RL falls through; visit INCR 503s without burning quota."""
    monkeypatch.setenv("VISITS_RATE_LIMIT", "3")
    monkeypatch.setenv("VISITS_RATE_WINDOW_SECONDS", "60")
    down_c = create_app(DownRedis(), str(client_dir)).test_client()
    for _ in range(5):
        r = down_c.post("/api/visits")
        assert r.status_code == 503
        assert r.get_json() == {"error": "redis unavailable"}

    ok_c = create_app(
        fakeredis.FakeRedis(decode_responses=True), str(client_dir)
    ).test_client()
    for _ in range(3):
        assert ok_c.post("/api/visits").status_code == 200
    assert ok_c.post("/api/visits").status_code == 429


def test_visits_rl_rollback_when_visit_incr_fails(monkeypatch, client_dir):
    """Charge-first: visit INCR failure DECR's RL so quota is not burned."""
    monkeypatch.setenv("VISITS_RATE_LIMIT", "2")
    monkeypatch.setenv("VISITS_RATE_WINDOW_SECONDS", "60")
    rdb = VisitIncrFailRedis()
    c = create_app(rdb, str(client_dir)).test_client()
    r = c.post("/api/visits")
    assert r.status_code == 503
    assert r.get_json() == {"error": "redis unavailable"}
    assert rdb.visit_incr_calls == 1
    rl_keys = list(rdb._inner.scan_iter(f"{RATE_LIMIT_KEY_PREFIX}*"))
    assert len(rl_keys) == 1
    assert int(rdb._inner.get(rl_keys[0]) or 0) == 0


def test_visits_concurrent_does_not_overshoot_limit(monkeypatch, client_dir):
    """Parallel POSTs cannot race past VISITS_RATE_LIMIT (Lua charge-first)."""
    monkeypatch.setenv("VISITS_RATE_LIMIT", "5")
    monkeypatch.setenv("VISITS_RATE_WINDOW_SECONDS", "60")
    rdb = fakeredis.FakeRedis(decode_responses=True)
    app = create_app(rdb, str(client_dir))
    barrier = threading.Barrier(20)

    def one():
        with app.test_client() as c:
            barrier.wait(timeout=5)
            return c.post("/api/visits").status_code

    codes = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(one) for _ in range(20)]
        for f in as_completed(futures):
            codes.append(f.result())

    assert codes.count(200) == 5
    assert codes.count(429) == 15
    assert int(rdb.get(VISITS_KEY) or 0) == 5
    body = app.test_client().get("/metrics").get_data(as_text=True)
    assert "app_visits_total 5.0" in body


def test_visits_over_limit_sets_ttl_on_sticky_key(monkeypatch, client_dir):
    """Over-limit path attaches a TTL when the RL key has none (no sticky 429)."""
    monkeypatch.setenv("VISITS_RATE_LIMIT", "2")
    monkeypatch.setenv("VISITS_RATE_WINDOW_SECONDS", "60")
    rdb = fakeredis.FakeRedis(decode_responses=True)
    c = create_app(rdb, str(client_dir)).test_client()
    assert c.post("/api/visits").status_code == 200
    assert c.post("/api/visits").status_code == 200
    key = list(rdb.scan_iter(f"{RATE_LIMIT_KEY_PREFIX}*"))[0]
    assert rdb.persist(key) in (True, 1)
    assert int(rdb.ttl(key)) == -1
    r = c.post("/api/visits")
    assert r.status_code == 429
    assert int(rdb.get(VISITS_KEY) or 0) == 2
    assert int(rdb.ttl(key)) > 0
    _assert_retry_after(r, min_seconds=1, max_seconds=60)


def test_visits_rl_error_does_not_clear_redis_up(monkeypatch, client_dir):
    """RL ERROR increments visits_rate_limit_errors_total; redis_up stays up."""
    monkeypatch.setenv("VISITS_RATE_LIMIT", "5")
    monkeypatch.setenv("VISITS_RATE_WINDOW_SECONDS", "60")
    rdb = TimeoutEvalRedis()
    app = create_app(rdb, str(client_dir))
    c = app.test_client()
    assert c.get("/health").status_code == 200
    r = c.post("/api/visits")
    assert r.status_code == 503
    assert r.get_json() == {"error": "rate limit unavailable"}
    assert _assert_retry_after(r) == RATE_LIMIT_ERROR_RETRY_AFTER_SECONDS
    # Read registry directly — /metrics calls check_redis() and would refresh redis_up.
    body = generate_latest(app.extensions["prometheus_registry"]).decode()
    assert "redis_up 1.0" in body
    assert "visits_rate_limit_errors_total 1.0" in body


def test_visits_rl_connection_error_fallthrough_still_records(monkeypatch, client_dir):
    """Pre-charge RL ConnectionError falls through; visits INCR can still succeed."""
    monkeypatch.setenv("VISITS_RATE_LIMIT", "2")
    monkeypatch.setenv("VISITS_RATE_WINDOW_SECONDS", "60")
    rdb = RlDownVisitsUpRedis()
    c = create_app(rdb, str(client_dir)).test_client()
    assert c.post("/api/visits").status_code == 200
    assert c.post("/api/visits").get_json() == {"visits": 2}
    # No RL accounting when pre-charge path is down — unlimited until Redis recovers.
    assert c.post("/api/visits").status_code == 200
    assert int(rdb._inner.get(VISITS_KEY) or 0) == 3


def test_visits_eval_timeout_fail_closed_no_double_charge(monkeypatch, client_dir):
    """EVAL TimeoutError must not Python-fallback INCR (no double-charge / fail-open)."""
    monkeypatch.setenv("VISITS_RATE_LIMIT", "5")
    monkeypatch.setenv("VISITS_RATE_WINDOW_SECONDS", "60")
    rdb = TimeoutEvalRedis()
    c = create_app(rdb, str(client_dir)).test_client()
    r = c.post("/api/visits")
    assert r.status_code == 503
    assert r.get_json() == {"error": "rate limit unavailable"}
    assert _assert_retry_after(r) == RATE_LIMIT_ERROR_RETRY_AFTER_SECONDS
    assert rdb.eval_calls == 1
    assert rdb.incr_calls == []
    assert int(rdb._inner.get(VISITS_KEY) or 0) == 0


def _ip_app(monkeypatch, client_dir, header="X-Real-IP", limit="2"):
    if header is None:
        monkeypatch.delenv("VISITS_CLIENT_IP_HEADER", raising=False)
    else:
        monkeypatch.setenv("VISITS_CLIENT_IP_HEADER", header)
    monkeypatch.setenv("VISITS_RATE_LIMIT", limit)
    monkeypatch.setenv("VISITS_RATE_WINDOW_SECONDS", "60")
    return create_app(fakeredis.FakeRedis(decode_responses=True), str(client_dir)).test_client()


def test_visits_client_ip_header_separate_buckets(monkeypatch, client_dir):
    c = _ip_app(monkeypatch, client_dir)
    for _ in range(2):
        assert c.post("/api/visits", headers={"X-Real-IP": "1.1.1.1"}).status_code == 200
    assert c.post("/api/visits", headers={"X-Real-IP": "1.1.1.1"}).status_code == 429
    assert c.post("/api/visits", headers={"X-Real-IP": "2.2.2.2"}).status_code == 200


def test_visits_client_ip_header_missing_falls_back_to_remote_addr(monkeypatch, client_dir):
    c = _ip_app(monkeypatch, client_dir)
    assert c.post("/api/visits").status_code == 200
    assert c.post("/api/visits", headers={"X-Real-IP": ""}).status_code == 200
    assert c.post("/api/visits").status_code == 429


def test_visits_client_ip_header_ignored_when_unset(monkeypatch, client_dir):
    c = _ip_app(monkeypatch, client_dir, header=None)
    assert c.post("/api/visits", headers={"X-Real-IP": "1.1.1.1"}).status_code == 200
    assert c.post("/api/visits", headers={"X-Real-IP": "2.2.2.2"}).status_code == 200
    assert c.post("/api/visits", headers={"X-Real-IP": "3.3.3.3"}).status_code == 429


def test_visits_get_not_allowed(ok):
    assert ok.get("/api/visits").status_code == 405


def test_metrics(ok):
    ok.post("/api/visits")
    ok.get("/health")
    r = ok.get("/metrics")
    assert r.status_code == 200
    assert r.content_type.startswith("text/plain")
    body = r.get_data(as_text=True)
    assert 'http_requests_total{endpoint="/health",method="GET",status="200"} 1.0' in body
    assert "http_request_duration_seconds_bucket" in body
    assert "redis_up 1.0" in body
    assert "app_visits_total 1.0" in body


def test_metrics_redis_down(down):
    r = down.get("/metrics")
    assert r.status_code == 200
    assert "redis_up 0.0" in r.get_data(as_text=True)


def test_metrics_unmatched_path_bounded(ok):
    ok.get("/nope/123")
    assert 'endpoint="unmatched"' in ok.get("/metrics").get_data(as_text=True)


def test_index(ok):
    r = ok.get("/")
    assert r.status_code == 200
    assert b"<h1>hello</h1>" in r.data
    r.close()


def test_static(ok):
    r = ok.get("/static/app.js")
    assert r.status_code == 200 and b"console.log" in r.data
    r.close()
    r = ok.get("/static/sub/x.css")
    assert r.status_code == 200
    r.close()


def test_static_missing(ok):
    assert ok.get("/static/missing.js").status_code == 404


@pytest.mark.parametrize("path", [
    "/static/../secret.txt",
    "/static/%2e%2e/secret.txt",
    "/static/..%2fsecret.txt",
    "/static/sub/../../secret.txt",
    "/static//etc/passwd",
    "/static/%2fetc/passwd",
])
def test_static_traversal(ok, path):
    r = ok.get(path, follow_redirects=True)
    assert r.status_code in (400, 404)
    assert b"secret" not in r.data and b"root:" not in r.data


# ---- audit fixes ----


def test_redis_client_has_no_retries():
    assert make_redis_client().get_retry()._retries == 0


def test_redis_client_password_from_env(monkeypatch):
    monkeypatch.setenv("REDIS_PASSWORD", "s3cret")
    kw = make_redis_client().connection_pool.connection_kwargs
    assert kw["password"] == "s3cret"
    monkeypatch.setenv("REDIS_PASSWORD", "")
    assert make_redis_client().connection_pool.connection_kwargs["password"] is None
    monkeypatch.delenv("REDIS_PASSWORD")
    assert make_redis_client().connection_pool.connection_kwargs["password"] is None


def test_metrics_method_label_bounded(ok):
    for m in ("FOO", "BAR", "PROPFIND"):
        ok.open("/live", method=m)
    ok.get("/live")
    body = ok.get("/metrics").get_data(as_text=True)
    lines = [l for l in body.splitlines()
             if l.startswith(("http_requests_total{", "http_request_duration_seconds"))]
    methods = {l.split('method="')[1].split('"')[0] for l in lines}
    assert methods == {"GET", "OTHER"}
    for m in ("FOO", "BAR", "PROPFIND"):
        assert f'method="{m}"' not in body


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1.2.3.4", "1.2.3.4"),
        ("  1.2.3.4  ", "1.2.3.4"),
        ("2001:DB8:0:0:0:0:0:1", "2001:db8::1"),
        ("::1", "::1"),
        ("not-an-ip", "REMOTE"),
        ("1.2.3.4, 5.6.7.8", "REMOTE"),
        ("1.2.3.4:80", "REMOTE"),
        ("a" * 100, "REMOTE"),
        ("1" * 46, "REMOTE"),
        ("0" * 40 + "::1", "REMOTE"),
    ],
)
def test_client_ip_header_validation(monkeypatch, client_dir, value, expected):
    monkeypatch.setenv("VISITS_CLIENT_IP_HEADER", "X-Real-IP")
    rdb = fakeredis.FakeRedis(decode_responses=True)
    c = create_app(rdb, str(client_dir)).test_client()
    assert c.post("/api/visits", headers={"X-Real-IP": value}).status_code == 200
    keys = list(rdb.scan_iter(f"{RATE_LIMIT_KEY_PREFIX}*"))
    assert len(keys) == 1
    got = keys[0].decode() if isinstance(keys[0], bytes) else keys[0]
    got = got[len(RATE_LIMIT_KEY_PREFIX):]
    assert got == ("127.0.0.1" if expected == "REMOTE" else expected)


def _wrongtype_app(client_dir):
    rdb = fakeredis.FakeRedis(decode_responses=True)
    rdb.lpush(VISITS_KEY, "x")  # INCR/GET on a list -> WRONGTYPE ResponseError
    return rdb, create_app(rdb, str(client_dir))


def test_visits_wrongtype_is_store_error_not_outage(monkeypatch, client_dir):
    monkeypatch.setenv("VISITS_RATE_LIMIT", "5")
    rdb, app = _wrongtype_app(client_dir)
    c = app.test_client()
    assert c.get("/health").status_code == 200  # redis_up -> 1
    r = c.post("/api/visits")
    assert r.status_code == 503
    assert r.get_json() == {"error": "visits store error"}
    body = generate_latest(app.extensions["prometheus_registry"]).decode()
    assert "redis_up 1.0" in body
    assert "visits_store_errors_total 1.0" in body
    # rate-limit charge rolled back
    rl = list(rdb.scan_iter(f"{RATE_LIMIT_KEY_PREFIX}*"))
    assert len(rl) == 1 and int(rdb.get(rl[0])) == 0


def test_visits_connection_error_does_not_count_store_error(client_dir):
    app = create_app(VisitIncrFailRedis(), str(client_dir))
    assert app.test_client().post("/api/visits").get_json() == {"error": "redis unavailable"}
    body = generate_latest(app.extensions["prometheus_registry"]).decode()
    assert "visits_store_errors_total 0.0" in body
    assert "redis_up 0.0" in body


def test_visits_timeout_error_is_outage(client_dir):
    class TimeoutIncr(VisitIncrFailRedis):
        def incr(self, name, *a, **k):
            if name == VISITS_KEY:
                raise redis.TimeoutError("slow")
            return self._inner.incr(name, *a, **k)

    app = create_app(TimeoutIncr(), str(client_dir))
    r = app.test_client().post("/api/visits")
    assert r.status_code == 503
    assert r.get_json() == {"error": "redis unavailable"}
    body = generate_latest(app.extensions["prometheus_registry"]).decode()
    assert "redis_up 0.0" in body


def test_status_wrongtype_keeps_connected(client_dir):
    rdb, app = _wrongtype_app(client_dir)
    j = app.test_client().get("/api/status").get_json()
    assert j["redis"] == {"connected": True}
    assert j["visits"] is None
    body = generate_latest(app.extensions["prometheus_registry"]).decode()
    assert "redis_up 1.0" in body


def test_status_get_timeout_marks_down(client_dir):
    class TimeoutGet:
        def __init__(self):
            self._inner = fakeredis.FakeRedis(decode_responses=True)

        def get(self, *a, **k):
            raise redis.TimeoutError("slow")

        def __getattr__(self, n):
            return getattr(self._inner, n)

    app = create_app(TimeoutGet(), str(client_dir))
    j = app.test_client().get("/api/status").get_json()
    assert j["redis"] == {"connected": False}
    assert j["visits"] is None


def test_retry_after_uses_pttl_near_expiry(monkeypatch, client_dir):
    monkeypatch.setenv("VISITS_RATE_LIMIT", "1")
    monkeypatch.setenv("VISITS_RATE_WINDOW_SECONDS", "60")
    rdb = fakeredis.FakeRedis(decode_responses=True)
    c = create_app(rdb, str(client_dir)).test_client()
    assert c.post("/api/visits").status_code == 200
    key = list(rdb.scan_iter(f"{RATE_LIMIT_KEY_PREFIX}*"))[0]
    rdb.pexpire(key, 150)  # sub-second remainder must round up to 1, not 0
    r = c.post("/api/visits")
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "1"
    rdb.pexpire(key, 2500)
    assert c.post("/api/visits").headers["Retry-After"] == "3"


def test_retry_after_no_expiry_uses_window(monkeypatch, client_dir):
    monkeypatch.setenv("VISITS_RATE_LIMIT", "1")
    monkeypatch.setenv("VISITS_RATE_WINDOW_SECONDS", "42")

    class NoExpiry:
        def eval(self, *a, **k):
            return [-1, -1]  # over limit, key without expiry

    r = create_app(NoExpiry(), str(client_dir)).test_client().post("/api/visits")
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "42"


def test_retry_after_single_round_trip(monkeypatch, client_dir):
    monkeypatch.setenv("VISITS_RATE_LIMIT", "1")
    inner = fakeredis.FakeRedis(decode_responses=True)

    class NoTtl:
        def __getattr__(self, n):
            if n in ("ttl", "pttl"):
                raise AssertionError("no second round trip expected")
            return getattr(inner, n)

    c = create_app(NoTtl(), str(client_dir)).test_client()
    c.post("/api/visits")
    assert c.post("/api/visits").status_code == 429


@pytest.mark.parametrize("path", ["/", "/api/status", "/live", "/nope"])
def test_security_headers(ok, path):
    r = ok.get(path)
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert r.headers["Content-Security-Policy"] == (
        "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    )


def test_auth_error_is_connection_error_subclass():
    # Auth failures therefore map to the outage path (redis_up 0 / 503 unavailable).
    assert issubclass(redis.AuthenticationError, redis.ConnectionError)


def _bucket(monkeypatch, client_dir, header_value=None, **kw):
    monkeypatch.setenv("VISITS_CLIENT_IP_HEADER", "X-Real-IP")
    rdb = fakeredis.FakeRedis(decode_responses=True)
    app = create_app(rdb, str(client_dir))
    headers = {"X-Real-IP": header_value} if header_value is not None else {}
    assert app.test_client().post("/api/visits", headers=headers, **kw).status_code == 200
    key = list(rdb.scan_iter(f"{RATE_LIMIT_KEY_PREFIX}*"))[0]
    key = key.decode() if isinstance(key, bytes) else key
    return key[len(RATE_LIMIT_KEY_PREFIX):], app


def test_client_ip_45_char_boundary_and_mapped(monkeypatch, client_dir):
    v = "0000:0000:0000:0000:0000:ffff:255.255.255.255"
    assert len(v) == 45
    assert _bucket(monkeypatch, client_dir, v)[0] == "255.255.255.255"
    assert _bucket(monkeypatch, client_dir, "::ffff:1.2.3.4")[0] == "1.2.3.4"


def test_client_ip_scope_id_stripped(monkeypatch, client_dir):
    assert _bucket(monkeypatch, client_dir, "fe80::1%eth0")[0] == "fe80::1"


def test_remote_addr_normalised(monkeypatch, client_dir):
    got, _ = _bucket(monkeypatch, client_dir, environ_base={"REMOTE_ADDR": "::ffff:9.9.9.9"})
    assert got == "9.9.9.9"


def test_client_ip_invalid_counter(monkeypatch, client_dir):
    monkeypatch.setenv("VISITS_CLIENT_IP_HEADER", "X-Real-IP")
    app = create_app(fakeredis.FakeRedis(decode_responses=True), str(client_dir))
    c = app.test_client()
    c.post("/api/visits", headers={"X-Real-IP": "junk"})
    c.post("/api/visits", headers={"X-Real-IP": "a" * 100})
    c.post("/api/visits", headers={"X-Real-IP": "1.2.3.4"})  # valid
    c.post("/api/visits")  # absent
    body = generate_latest(app.extensions["prometheus_registry"]).decode()
    assert "visits_client_ip_header_invalid_total 2.0" in body
