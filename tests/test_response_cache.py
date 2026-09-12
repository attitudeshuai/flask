from __future__ import annotations

import io
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from flask import Flask
from flask import g
from flask import redirect
from flask import request
from flask import send_file
from flask import session


def make_app(**config) -> Flask:
    app = Flask(__name__)
    app.config.update(config)
    return app


def enable(app: Flask, **overrides) -> Flask:
    app.config["RESPONSE_CACHE_ENABLED"] = True
    app.config.update(overrides)
    app.init_response_cache()
    return app


@pytest.fixture
def app():
    return enable(make_app(TESTING=True))


@pytest.fixture
def client(app):
    return app.test_client()


class TestDisabled:
    def test_disabled_by_default(self):
        app = make_app()
        assert app.response_cache is None

        with pytest.raises(RuntimeError):
            app.get_response_cache_stats()

        with pytest.raises(RuntimeError):
            app.clear_response_cache()

    def test_disabled_behavior_unchanged(self, app):
        # Fixture enables it; turn it back off.
        app.config["RESPONSE_CACHE_ENABLED"] = False
        app.init_response_cache()
        calls = 0

        @app.route("/x")
        def x():
            nonlocal calls
            calls += 1
            return f"n={calls}", {"X-Call": str(calls)}

        client = app.test_client()
        first = client.get("/x")
        second = client.get("/x")

        assert calls == 2
        assert first.get_data() == b"n=1"
        assert second.get_data() == b"n=2"
        assert first.headers["X-Call"] == "1"
        assert second.headers["X-Call"] == "2"


class TestBasicCaching:
    def test_hit_skips_view(self, app, client):
        calls = 0

        @app.route("/hello")
        def hello():
            nonlocal calls
            calls += 1
            return "hello", {"X-View": "yes"}

        first = client.get("/hello")
        second = client.get("/hello")

        assert calls == 1
        assert first.status_code == second.status_code == 200
        assert first.get_data() == second.get_data() == b"hello"
        assert second.headers["X-View"] == "yes"

        stats = app.get_response_cache_stats()
        assert stats == {
            "hits": 1,
            "misses": 1,
            "entries": 1,
            "max_entries": 512,
        }

    def test_query_string_is_part_of_key(self, app, client):
        calls = 0

        @app.route("/search")
        def search():
            nonlocal calls
            calls += 1
            return request.query_string.decode()

        client.get("/search?a=1")
        client.get("/search?a=1")
        client.get("/search?a=2")

        assert calls == 2

        # Raw query string order is significant.
        client.get("/search?a=1&b=2")
        client.get("/search?b=2&a=1")
        assert calls == 4

    def test_method_is_part_of_key(self, app, client):
        calls = 0

        @app.route("/m")
        def m():
            nonlocal calls
            calls += 1
            return "body"

        client.get("/m")
        client.get("/m")
        head = client.head("/m")
        head2 = client.head("/m")

        assert calls == 2
        assert head.get_data() == head2.get_data() == b""

    def test_non_idempotent_methods_never_cached(self, app, client):
        calls = 0

        @app.route("/write", methods=["GET", "POST", "PUT", "DELETE"])
        def write():
            nonlocal calls
            calls += 1
            return str(calls)

        client.post("/write")
        client.post("/write")
        client.put("/write")
        client.delete("/write")

        assert calls == 4
        assert app.get_response_cache_stats()["entries"] == 0

    def test_before_request_short_circuit_not_cached(self, app, client):
        calls = 0

        @app.before_request
        def gate():
            if request.path == "/gate":
                return "denied", 401
            return None

        @app.route("/gate")
        def gate_view():
            nonlocal calls
            calls += 1
            return "ok"

        first = client.get("/gate")
        second = client.get("/gate")

        assert first.status_code == second.status_code == 401
        assert calls == 0
        assert app.get_response_cache_stats()["entries"] == 0

    def test_each_hit_is_fresh_response_object(self, app, client):
        @app.route("/o")
        def o():
            return "x"

        first = client.get("/o")
        second = client.get("/o")
        third = client.get("/o")

        assert first.headers is not second.headers
        assert second.get_data() == third.get_data() == b"x"


class TestHeadersAndVary:
    def test_configured_request_headers_always_keyed(self):
        app = enable(make_app(), RESPONSE_CACHE_HEADERS=("X-Tenant",))
        client = app.test_client()
        calls = 0

        @app.route("/t")
        def t():
            nonlocal calls
            calls += 1
            return "ok"

        assert client.get("/t", headers={"X-Tenant": "a"}).get_data() == b"ok"
        assert client.get("/t", headers={"X-Tenant": "a"}).get_data() == b"ok"
        assert client.get("/t", headers={"X-Tenant": "b"}).get_data() == b"ok"
        assert client.get("/t").get_data() == b"ok"
        assert calls == 3

        stats = app.get_response_cache_stats()
        assert stats["misses"] == 3
        assert stats["hits"] == 1

    def test_vary_header_normalizes_key(self, app, client):
        calls = 0

        @app.route("/v")
        def v():
            nonlocal calls
            calls += 1
            resp = app.make_response("ok")
            resp.headers["Vary"] = "Accept-Encoding"
            return resp

        gzip = {"Accept-Encoding": "gzip"}
        br = {"Accept-Encoding": "br"}

        client.get("/v", headers=gzip)  # miss, learns Vary
        client.get("/v", headers=br)  # miss, different variant
        client.get("/v", headers=gzip)  # hit, original variant
        client.get("/v")  # miss, no header
        client.get("/v", headers=br)  # hit

        assert calls == 3
        stats = app.get_response_cache_stats()
        assert stats["hits"] == 2
        assert stats["misses"] == 3

    def test_vary_wildcard_not_cached(self, app, client):
        calls = 0

        @app.route("/star")
        def star():
            nonlocal calls
            calls += 1
            resp = app.make_response("ok")
            resp.headers["Vary"] = "*"
            return resp

        client.get("/star")
        client.get("/star")

        assert calls == 2
        assert app.get_response_cache_stats()["entries"] == 0


class TestSessionsAndCookies:
    def test_session_write_response_not_cached(self):
        app = enable(make_app(SECRET_KEY="x", TESTING=True))
        client = app.test_client()
        calls = 0

        @app.route("/login")
        def login():
            nonlocal calls
            calls += 1
            session["user"] = str(calls)
            return "ok"

        first = client.get("/login")
        second = client.get("/login")

        assert calls == 2
        assert "Set-Cookie" in first.headers
        assert "Set-Cookie" in second.headers
        assert app.get_response_cache_stats()["entries"] == 0

    def test_request_with_session_cookie_bypasses_cache(self):
        app = enable(make_app(SECRET_KEY="x", TESTING=True))
        client = app.test_client()
        calls = 0

        @app.route("/me")
        def me():
            nonlocal calls
            calls += 1
            return "profile"

        with client.session_transaction() as sess:
            sess["user"] = "alice"

        first = client.get("/me")
        second = client.get("/me")

        assert first.get_data() == second.get_data() == b"profile"
        assert calls == 2
        assert app.get_response_cache_stats()["entries"] == 0

    def test_set_cookie_response_not_cached(self, app, client):
        calls = 0

        @app.route("/c")
        def c():
            nonlocal calls
            calls += 1
            resp = app.make_response("ok")
            resp.set_cookie("tracking", str(calls))
            return resp

        first = client.get("/c")
        second = client.get("/c")

        assert calls == 2
        assert first.headers["Set-Cookie"] != second.headers["Set-Cookie"]
        assert app.get_response_cache_stats()["entries"] == 0


class TestStreamAndFiles:
    def test_streaming_generator_not_cached_or_consumed(self, app, client):
        counter = iter(range(100))
        calls = 0

        @app.route("/stream")
        def stream():
            nonlocal calls
            calls += 1

            def gen():
                yield f"chunk-{next(counter)}".encode()

            return app.response_class(gen(), mimetype="text/plain")

        first = client.get("/stream")
        second = client.get("/stream")

        assert first.get_data() == b"chunk-0"
        assert second.get_data() == b"chunk-1"
        assert calls == 2
        assert app.get_response_cache_stats()["entries"] == 0

    def test_file_passthrough_not_cached(self, app, client):
        calls = 0

        @app.route("/file")
        def file():
            nonlocal calls
            calls += 1
            return send_file(io.BytesIO(b"file-bytes"), download_name="f.bin")

        first = client.get("/file")
        second = client.get("/file")

        assert first.get_data() == second.get_data() == b"file-bytes"
        assert calls == 2
        assert app.get_response_cache_stats()["entries"] == 0


class TestStatusCodes:
    def test_error_status_not_cached(self, app, client):
        calls = 0

        @app.route("/fail")
        def fail():
            nonlocal calls
            calls += 1
            return "boom", 500

        client.get("/fail")
        client.get("/fail")
        assert calls == 2

    def test_error_does_not_pollute_later_success(self, app, client):
        calls = 0

        @app.route("/toggle")
        def toggle():
            nonlocal calls
            calls += 1
            if calls == 1:
                return "err", 500
            return "ok", 200

        assert client.get("/toggle").status_code == 500
        second = client.get("/toggle")
        assert second.status_code == 200
        assert second.get_data() == b"ok"
        third = client.get("/toggle")
        assert third.get_data() == b"ok"
        assert calls == 2

    def test_redirect_not_cached_by_default(self, app, client):
        calls = 0

        @app.route("/go")
        def go():
            nonlocal calls
            calls += 1
            return redirect("/target")

        client.get("/go")
        client.get("/go")
        assert calls == 2

    def test_redirect_cached_when_configured(self):
        app = enable(
            make_app(TESTING=True),
            RESPONSE_CACHE_STATUS_CODES=frozenset({200, 302}),
        )
        client = app.test_client()
        calls = 0

        @app.route("/go")
        def go():
            nonlocal calls
            calls += 1
            return redirect("/target", code=302)

        first = client.get("/go")
        second = client.get("/go")

        assert calls == 1
        assert first.status_code == second.status_code == 302
        assert second.headers["Location"] == "/target"


class TestTtlAndLru:
    def test_ttl_expiry(self):
        app = enable(make_app(TESTING=True), RESPONSE_CACHE_TTL=0.05)
        client = app.test_client()
        calls = 0

        @app.route("/t")
        def t():
            nonlocal calls
            calls += 1
            return "x"

        client.get("/t")
        client.get("/t")
        assert calls == 1

        # Override the TTL deterministically instead of relying on wall clock.
        for entry in app.response_cache._entries.values():
            entry.expires_at = 0

        client.get("/t")
        assert calls == 2

        stats = app.get_response_cache_stats()
        assert stats["entries"] == 1
        assert stats["misses"] == 2

    def test_lru_eviction_by_insert_order(self):
        app = enable(make_app(TESTING=True), RESPONSE_CACHE_MAX_ENTRIES=2)
        client = app.test_client()

        @app.route("/<path:name>")
        def page(name):
            return name

        client.get("/a")
        client.get("/b")
        client.get("/c")  # evicts /a

        assert app.get_response_cache_stats()["entries"] == 2

        client.get("/b")  # still cached, hit
        client.get("/c")  # still cached, hit
        client.get("/a")  # evicted, miss

        stats = app.get_response_cache_stats()
        assert stats["misses"] == 4
        assert stats["hits"] == 2

    def test_lru_respects_recency(self):
        app = enable(make_app(TESTING=True), RESPONSE_CACHE_MAX_ENTRIES=2)
        client = app.test_client()

        @app.route("/<path:name>")
        def page(name):
            return name

        client.get("/a")
        client.get("/b")
        client.get("/a")  # /a most recently used now
        client.get("/c")  # evicts /b, not /a

        client.get("/a")  # hit
        client.get("/b")  # miss

        stats = app.get_response_cache_stats()
        assert stats["entries"] == 2
        assert stats["hits"] == 2
        assert stats["misses"] == 4

    def test_timedelta_ttl_valid(self):
        app = make_app(TESTING=True)
        app.config.update(
            RESPONSE_CACHE_ENABLED=True,
            RESPONSE_CACHE_TTL=timedelta(minutes=5),
        )
        app.init_response_cache()
        assert app.response_cache._ttl == 300


class TestMatchingConfig:
    def test_endpoint_allowlist(self):
        app = enable(
            make_app(TESTING=True),
            RESPONSE_CACHE_ENDPOINTS=("cached",),
        )
        client = app.test_client()
        calls = {"cached": 0, "other": 0}

        @app.route("/yes", endpoint="cached")
        def yes():
            calls["cached"] += 1
            return "y"

        @app.route("/no")
        def other():
            calls["other"] += 1
            return "n"

        client.get("/yes")
        client.get("/yes")
        client.get("/no")
        client.get("/no")

        assert calls == {"cached": 1, "other": 2}

    def test_path_allowlist_and_excludes(self):
        app = enable(
            make_app(TESTING=True),
            RESPONSE_CACHE_PATHS=("/api/",),
            RESPONSE_CACHE_EXCLUDE_PATHS=("/api/no/",),
        )
        client = app.test_client()
        calls = []

        @app.route("/api/yes")
        def yes():
            calls.append("yes")
            return "y"

        @app.route("/api/no/thing")
        def no():
            calls.append("no")
            return "n"

        @app.route("/web")
        def web():
            calls.append("web")
            return "w"

        for _ in range(2):
            client.get("/api/yes")
            client.get("/api/no/thing")
            client.get("/web")

        assert calls == ["yes", "no", "web", "no", "web"]

    def test_static_endpoint_excluded_by_default(self):
        app = enable(make_app(TESTING=True))
        assert "static" in app.response_cache._exclude_endpoints


class TestClearAndStats:
    def test_clear_by_endpoint_and_prefix(self, app, client):
        calls = []

        @app.route("/a")
        def a():
            calls.append("a")
            return "a"

        @app.route("/b")
        def b():
            return "b"

        @app.route("/api/x")
        def api_x():
            return "x"

        client.get("/a")
        client.get("/b")
        client.get("/api/x")
        assert app.get_response_cache_stats()["entries"] == 3

        assert app.clear_response_cache(endpoint="a") == 1
        assert app.get_response_cache_stats()["entries"] == 2
        client.get("/a")  # re-executed because it was cleared
        assert calls == ["a", "a"]
        assert app.get_response_cache_stats()["entries"] == 3

        assert app.clear_response_cache(prefix="/api") == 1
        assert app.get_response_cache_stats()["entries"] == 2

        assert app.clear_response_cache() == 2
        assert app.get_response_cache_stats()["entries"] == 0

    def test_clear_combines_filters(self, app, client):
        @app.route("/api/a")
        def api_a():
            return "a"

        @app.route("/web/b")
        def web_b():
            return "b"

        client.get("/api/a")
        client.get("/web/b")

        removed = app.clear_response_cache(endpoint="api_a", prefix="/web")
        assert removed == 0
        assert app.get_response_cache_stats()["entries"] == 2

    def test_stats_are_accurate(self, app, client):
        @app.route("/hit")
        def hit():
            return "h"

        client.get("/hit")
        client.get("/hit")
        client.get("/hit")
        client.get("/missing-404")

        stats = app.get_response_cache_stats()
        assert stats["hits"] == 2
        assert stats["misses"] == 1
        assert stats["entries"] == 1


class TestContextIsolation:
    def test_snapshot_does_not_see_later_context(self, app, client):
        counter = iter(range(1, 100))

        @app.before_request
        def set_g():
            g.rid = next(counter)

        @app.after_request
        def tag(response):
            response.headers["X-G-Now"] = str(g.rid)
            return response

        @app.route("/g")
        def gv():
            return str(g.rid)

        first = client.get("/g")
        assert first.get_data() == b"1"
        assert first.headers["X-G-Now"] == "1"

        second = client.get("/g")
        # Body is the snapshot from the first request, downstream handler
        # sees the second request's context.
        assert second.get_data() == b"1"
        assert second.headers["X-G-Now"] == "2"

    def test_downstream_mutation_not_written_back(self, app, client):
        counter = iter(range(1, 100))

        @app.after_request
        def mutate(response):
            # Runs for every request, including cache hits. Mutating the copy
            # returned for a hit must not change the cached snapshot.
            response.headers["X-Mut"] = str(next(counter))
            return response

        @app.route("/m")
        def m():
            return "ok"

        first = client.get("/m")  # miss, finalized response is snapshotted
        second = client.get("/m")  # hit, copy mutated independently
        third = client.get("/m")  # hit, starts from the snapshot again

        assert first.headers["X-Mut"] == "1"
        assert second.headers["X-Mut"] == "2"
        assert third.headers["X-Mut"] == "3"
        assert third.get_data() == b"ok"

        snapshot = next(iter(app.response_cache._entries.values())).snapshot
        assert snapshot.body == b"ok"
        assert dict(snapshot.headers)["X-Mut"] == "1"

    def test_request_hooks_still_run_on_hit(self, app, client):
        events = []

        @app.before_request
        def before():
            events.append("before")

        @app.after_request
        def after(response):
            events.append("after")
            return response

        @app.route("/e")
        def e():
            events.append("view")
            return "e"

        client.get("/e")
        client.get("/e")

        assert events == ["before", "view", "after", "before", "after"]


class TestCoalescing:
    def test_concurrent_requests_execute_view_once(self):
        app = enable(make_app(TESTING=True))

        n = 6
        barrier = threading.Barrier(n)

        @app.before_request
        def synchronize():
            if request.path == "/slow":
                barrier.wait(timeout=5)

        calls = 0
        started = threading.Event()

        @app.route("/slow")
        def slow():
            nonlocal calls
            calls += 1
            started.set()
            # Give the other threads time to reach the in-progress flight.
            started.wait(2)
            threading.Event().wait(0.5)
            return "slow-result"

        def do_request():
            client = app.test_client()
            return client.get("/slow").get_data()

        with ThreadPoolExecutor(max_workers=n) as pool:
            results = list(pool.map(lambda _: do_request(), range(n)))

        assert results == [b"slow-result"] * n
        assert calls == 1
        stats = app.get_response_cache_stats()
        assert stats["misses"] == 1
        assert stats["hits"] == n - 1

    def test_failed_leader_releases_waiters(self):
        # Errors must propagate, not leave concurrent requests hanging;
        # followers then execute the view themselves.
        app = make_app(TESTING=False, PROPAGATE_EXCEPTIONS=False)
        enable(app)

        n = 4
        barrier = threading.Barrier(n)

        @app.before_request
        def synchronize():
            if request.path == "/boom":
                barrier.wait(timeout=5)

        calls = 0

        @app.route("/boom")
        def boom():
            nonlocal calls
            calls += 1
            raise RuntimeError("boom")

        def do_request():
            client = app.test_client()
            return client.get("/boom").status_code

        with ThreadPoolExecutor(max_workers=n) as pool:
            statuses = list(pool.map(lambda _: do_request(), range(n)))

        assert statuses == [500] * n
        assert calls == n
        assert app.get_response_cache_stats()["entries"] == 0


class TestConfigValidation:
    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("RESPONSE_CACHE_TTL", -1),
            ("RESPONSE_CACHE_TTL", 0),
            ("RESPONSE_CACHE_TTL", "60"),
            ("RESPONSE_CACHE_TTL", True),
            ("RESPONSE_CACHE_TTL", timedelta(seconds=-1)),
            ("RESPONSE_CACHE_MAX_ENTRIES", 0),
            ("RESPONSE_CACHE_MAX_ENTRIES", -3),
            ("RESPONSE_CACHE_MAX_ENTRIES", 1.5),
            ("RESPONSE_CACHE_MAX_ENTRIES", "10"),
            ("RESPONSE_CACHE_METHODS", "GET"),
            ("RESPONSE_CACHE_METHODS", ["POST"]),
            ("RESPONSE_CACHE_METHODS", []),
            ("RESPONSE_CACHE_METHODS", [1]),
            ("RESPONSE_CACHE_HEADERS", "X-Tenant"),
            ("RESPONSE_CACHE_HEADERS", [""]),
            ("RESPONSE_CACHE_HEADERS", [1]),
            ("RESPONSE_CACHE_ENDPOINTS", "home"),
            ("RESPONSE_CACHE_PATHS", ["no-leading-slash"]),
            ("RESPONSE_CACHE_EXCLUDE_PATHS", [1]),
            ("RESPONSE_CACHE_STATUS_CODES", []),
            ("RESPONSE_CACHE_STATUS_CODES", ["200"]),
            ("RESPONSE_CACHE_STATUS_CODES", [99]),
        ],
    )
    def test_invalid_config_raises(self, key, value):
        app = make_app()
        app.config["RESPONSE_CACHE_ENABLED"] = True
        app.config[key] = value

        with pytest.raises(ValueError):
            app.init_response_cache()

    def test_init_validates_before_serving(self):
        app = make_app()
        app.config["RESPONSE_CACHE_ENABLED"] = True
        app.config["RESPONSE_CACHE_TTL"] = -1

        with pytest.raises(ValueError):
            app.init_response_cache()

        assert app.response_cache is None

    def test_reinit_resets_cache(self, app, client):
        @app.route("/r")
        def r():
            return "r"

        client.get("/r")
        assert app.get_response_cache_stats()["entries"] == 1

        app.init_response_cache()
        assert app.get_response_cache_stats()["entries"] == 0


class TestCli:
    def test_stats_and_clear(self, app):
        client = app.test_client()

        @app.route("/cli")
        def cli_view():
            return "cli"

        client.get("/cli")
        client.get("/cli")

        runner = app.test_cli_runner()

        result = runner.invoke(args=["response-cache", "stats"])
        assert result.exit_code == 0
        assert "hits:" in result.output
        assert "misses:" in result.output
        assert "entries:" in result.output

        result = runner.invoke(
            args=["response-cache", "clear", "--endpoint", "cli_view"]
        )
        assert result.exit_code == 0
        assert "Removed 1" in result.output
        assert app.get_response_cache_stats()["entries"] == 0

    def test_disabled_message(self):
        app = make_app()
        runner = app.test_cli_runner()

        result = runner.invoke(args=["response-cache", "stats"])
        assert result.exit_code == 0
        assert "disabled" in result.output

        result = runner.invoke(args=["response-cache", "clear"])
        assert result.exit_code == 0
        assert "disabled" in result.output
