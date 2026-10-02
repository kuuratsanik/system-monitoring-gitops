import fakeredis
import pytest
import redis

from app import create_app


class DownRedis:
    """Stand-in client whose every call fails like an unreachable server."""

    def __getattr__(self, name):
        def fail(*a, **k):
            raise redis.ConnectionError("down")
        return fail


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
