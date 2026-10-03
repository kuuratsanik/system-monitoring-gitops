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


class PythonFallbackRedis:
    """Force Python RL path (no EVAL); optional expire/incr failure hooks."""

    def __init__(self, inner=None, expire_error=None):
        self._inner = inner or fakeredis.FakeRedis(decode_responses=True)
        self.expire_error = expire_error
        self.eval_calls = 0
        self.rl_incr_calls = 0
        self.visit_incr_calls = 0

    def eval(self, *args, **kwargs):
        self.eval_calls += 1
        raise redis.ResponseError("unknown command 'EVAL'")

    def incr(self, name, *args, **kwargs):
        if name == VISITS_KEY:
            self.visit_incr_calls += 1
        elif str(name).startswith(RATE_LIMIT_KEY_PREFIX):
            self.rl_incr_calls += 1
        return self._inner.incr(name, *args, **kwargs)

    def expire(self, name, *args, **kwargs):
        if self.expire_error is not None:
            raise self.expire_error
        return self._inner.expire(name, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class TimeoutEvalRedis:
    """EVAL times out; must not fall through to a second Python RL INCR."""

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
    # Rollback is Lua EXISTS+DECR (or Python fallback); assert accounting, not call shape.
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
    """Over-limit path EXPIRE's when RL key has no TTL (sticky 429 scenario)."""
    monkeypatch.setenv("VISITS_RATE_LIMIT", "2")
    monkeypatch.setenv("VISITS_RATE_WINDOW_SECONDS", "60")
    rdb = fakeredis.FakeRedis(decode_responses=True)
    c = create_app(rdb, str(client_dir)).test_client()
    assert c.post("/api/visits").status_code == 200
    assert c.post("/api/visits").status_code == 200
    rl_keys = list(rdb.scan_iter(f"{RATE_LIMIT_KEY_PREFIX}*"))
    assert len(rl_keys) == 1
    key = rl_keys[0]
    assert rdb.persist(key) in (True, 1)
    assert int(rdb.ttl(key)) == -1
    r = c.post("/api/visits")
    assert r.status_code == 429
    assert int(rdb.get(VISITS_KEY) or 0) == 2
    assert int(rdb.ttl(key)) > 0
    _assert_retry_after(r, min_seconds=1, max_seconds=60)


def test_visits_python_expire_fail_after_incr_does_not_admit(monkeypatch, client_dir):
    """Forced Python RL: expire failure after INCR rolls back and 503s (no admit)."""
    monkeypatch.setenv("VISITS_RATE_LIMIT", "5")
    monkeypatch.setenv("VISITS_RATE_WINDOW_SECONDS", "60")
    rdb = PythonFallbackRedis(expire_error=redis.RedisError("expire failed"))
    c = create_app(rdb, str(client_dir)).test_client()
    r = c.post("/api/visits")
    assert r.status_code == 503
    assert r.get_json() == {"error": "rate limit unavailable"}
    assert _assert_retry_after(r) == RATE_LIMIT_ERROR_RETRY_AFTER_SECONDS
    assert rdb.eval_calls >= 1
    assert rdb.rl_incr_calls == 1
    assert rdb.visit_incr_calls == 0
    assert int(rdb._inner.get(VISITS_KEY) or 0) == 0
    rl_keys = list(rdb._inner.scan_iter(f"{RATE_LIMIT_KEY_PREFIX}*"))
    assert len(rl_keys) == 1
    assert int(rdb._inner.get(rl_keys[0]) or 0) == 0


def test_visits_python_over_limit_expire_fail_is_error_not_sticky(monkeypatch, client_dir):
    """Python over-limit + expire fail → ERROR/503, not endless sticky OVER_LIMIT."""
    monkeypatch.setenv("VISITS_RATE_LIMIT", "2")
    monkeypatch.setenv("VISITS_RATE_WINDOW_SECONDS", "60")
    rdb = PythonFallbackRedis()
    c = create_app(rdb, str(client_dir)).test_client()
    assert c.post("/api/visits").status_code == 200
    assert c.post("/api/visits").status_code == 200
    rl_keys = list(rdb._inner.scan_iter(f"{RATE_LIMIT_KEY_PREFIX}*"))
    assert len(rl_keys) == 1
    key = rl_keys[0]
    assert rdb._inner.persist(key) in (True, 1)
    assert int(rdb._inner.ttl(key)) == -1
    rdb.expire_error = redis.RedisError("expire failed")
    r = c.post("/api/visits")
    assert r.status_code == 503
    assert r.get_json() == {"error": "rate limit unavailable"}
    assert _assert_retry_after(r) == RATE_LIMIT_ERROR_RETRY_AFTER_SECONDS
    assert rdb.visit_incr_calls == 2  # only the two successful visits
    assert int(rdb._inner.get(VISITS_KEY) or 0) == 2
    # Must not advertise OVER_LIMIT while key can stick without TTL.
    assert int(rdb._inner.ttl(key)) == -1
    # Repeated hits stay ERROR (503), not sticky 429.
    r2 = c.post("/api/visits")
    assert r2.status_code == 503
    assert _assert_retry_after(r2) == RATE_LIMIT_ERROR_RETRY_AFTER_SECONDS


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


def test_visits_rollback_missing_rl_key_no_orphan(monkeypatch, client_dir):
    """Rollback when RL key is already gone must not recreate a ttl=-1 orphan."""
    monkeypatch.setenv("VISITS_RATE_LIMIT", "2")
    monkeypatch.setenv("VISITS_RATE_WINDOW_SECONDS", "60")

    class VisitFailDeletesRl(VisitIncrFailRedis):
        def incr(self, name, *args, **kwargs):
            if name == VISITS_KEY:
                for k in list(self._inner.scan_iter(f"{RATE_LIMIT_KEY_PREFIX}*")):
                    self._inner.delete(k)
                self.visit_incr_calls += 1
                raise redis.ConnectionError("visits incr failed")
            return self._inner.incr(name, *args, **kwargs)

    rdb = VisitFailDeletesRl()
    c = create_app(rdb, str(client_dir)).test_client()
    r = c.post("/api/visits")
    assert r.status_code == 503
    assert r.get_json() == {"error": "redis unavailable"}
    rl_keys = list(rdb._inner.scan_iter(f"{RATE_LIMIT_KEY_PREFIX}*"))
    assert rl_keys == []
    # No orphan with value -1 / ttl=-1 from blind DECR on a missing key.
    for k in rdb._inner.keys(f"{RATE_LIMIT_KEY_PREFIX}*"):
        assert int(rdb._inner.get(k) or 0) >= 0
        assert int(rdb._inner.ttl(k)) != -1 or rdb._inner.get(k) is None


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


def test_visits_xff_spoof_shares_remote_addr(monkeypatch, client_dir):
    """Default: X-Forwarded-For is ignored; all requests share remote_addr bucket."""
    monkeypatch.delenv("VISITS_TRUST_XFF", raising=False)
    monkeypatch.setenv("VISITS_RATE_LIMIT", "2")
    monkeypatch.setenv("VISITS_RATE_WINDOW_SECONDS", "60")
    c = create_app(fakeredis.FakeRedis(decode_responses=True), str(client_dir)).test_client()
    assert c.post("/api/visits", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert c.post("/api/visits", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 200
    assert c.post("/api/visits", headers={"X-Forwarded-For": "3.3.3.3"}).status_code == 429


def test_visits_trust_xff_uses_rightmost_hop(monkeypatch, client_dir):
    """VISITS_TRUST_XFF: rate-limit key is the rightmost XFF hop."""
    monkeypatch.setenv("VISITS_TRUST_XFF", "1")
    monkeypatch.setenv("VISITS_RATE_LIMIT", "2")
    monkeypatch.setenv("VISITS_RATE_WINDOW_SECONDS", "60")
    c = create_app(fakeredis.FakeRedis(decode_responses=True), str(client_dir)).test_client()
    assert c.post(
        "/api/visits", headers={"X-Forwarded-For": "spoofed, 10.0.0.1"}
    ).status_code == 200
    assert c.post(
        "/api/visits", headers={"X-Forwarded-For": "other, 10.0.0.2"}
    ).status_code == 200
    # second hit for rightmost 10.0.0.1
    assert c.post(
        "/api/visits", headers={"X-Forwarded-For": "again, 10.0.0.1"}
    ).status_code == 200
    assert c.post(
        "/api/visits", headers={"X-Forwarded-For": "still, 10.0.0.1"}
    ).status_code == 429


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
