from __future__ import annotations

import builtins
import json
import random
import threading
import time
from datetime import timedelta

import pytest
from werkzeug.test import run_wsgi_app

from flask import Blueprint
from flask import Flask
from flask.concurrency import CONCURRENCY_QUOTA_ATTRIBUTE
from flask.concurrency import ConcurrencyQuota
from flask.testing import EnvironBuilder

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_environ(app, path="/", method="GET"):
    builder = EnvironBuilder(app, path, method=method)

    try:
        return builder.get_environ()
    finally:
        builder.close()


def status_of(rv):
    return int(rv[1].split(None, 1)[0])


def body_of(rv):
    return json.loads(b"".join(rv[0]))


def make_app(**kwargs):
    app = Flask(__name__)
    app.config["TESTING"] = True
    return app


def flatten_exception(exc):
    group_type = getattr(builtins, "BaseExceptionGroup", ())

    if isinstance(exc, group_type):
        for inner in exc.exceptions:
            yield from flatten_exception(inner)
    else:
        yield exc


def wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout

    while time.monotonic() < end:
        if predicate():
            return

        time.sleep(0.005)

    assert predicate(), "timed out waiting for condition"


class RequestWorker:
    """Run one WSGI request in a thread and store its result or exception."""

    def __init__(self, app, environ, buffered=True):
        self.app = app
        self.environ = environ
        self.buffered = buffered
        self.result = None
        self.exc = None
        self.thread = threading.Thread(target=self._run)

    def start(self):
        self.thread.start()
        return self

    def _run(self):
        try:
            self.result = run_wsgi_app(self.app, self.environ, buffered=self.buffered)
        except BaseException as e:
            self.exc = e

    def join(self, timeout=10.0):
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "request thread hung"


def occupy_slot(app, path="/slow"):
    """Start a request that blocks inside its view until the returned release
    event is set. Returns ``(worker, entered_event, release_event)``."""
    entered = threading.Event()
    release = threading.Event()

    @app.route(path)
    def slow():  # type: ignore[unused-ignore]
        entered.set()
        release.wait(10.0)
        return "done"

    worker = RequestWorker(app, make_environ(app, path)).start()
    assert entered.wait(5.0)
    return worker, release


# ---------------------------------------------------------------------------
# No declaration: behavior unchanged
# ---------------------------------------------------------------------------


def test_head_request_with_empty_body_releases_slot(app):
    app.limit_concurrency(1, wait=False)

    @app.route("/item")
    def item():
        return "item"

    for _ in range(3):
        rv = run_wsgi_app(app, make_environ(app, "/item", method="HEAD"), buffered=True)
        assert status_of(rv) == 200
        assert b"".join(rv[0]) == b""

    assert app.get_concurrency_stats()["app"]["in_flight"] == 0


def test_no_quota_has_no_effect(app, client):
    @app.route("/")
    def index():
        return "ok", {"X-Custom": "yes"}

    rv = client.get("/")

    assert rv.status_code == 200
    assert rv.headers["X-Custom"] == "yes"
    assert "Retry-After" not in rv.headers
    assert app.get_concurrency_stats() == {}
    # No manager is built for an application without any declaration.
    assert app._concurrency_manager is None


# ---------------------------------------------------------------------------
# Immediate rejection
# ---------------------------------------------------------------------------


def test_immediate_rejection(app):
    app.limit_concurrency(1, wait=False)
    worker, release = occupy_slot(app)

    rv = run_wsgi_app(app, make_environ(app, "/slow"), buffered=True)

    assert status_of(rv) == 503
    assert rv[2]["Retry-After"] == "1"
    body = body_of(rv)
    assert body["error"]["reason"] == "in_flight"
    assert body["error"]["quota"] == "app"
    assert body["error"]["retry_after"] == 1

    release.set()
    worker.join()

    stats = app.get_concurrency_stats()["app"]
    assert stats["in_flight"] == 0
    assert stats["waiting"] == 0
    assert stats["rejected"] == 1


def test_rejected_request_runs_after_request_and_teardown(app):
    app.limit_concurrency(1, wait=False)
    calls = []

    @app.after_request
    def after(response):
        calls.append("after")
        return response

    @app.teardown_request
    def teardown(exc):
        calls.append("teardown")

    worker, release = occupy_slot(app)
    rv = run_wsgi_app(app, make_environ(app, "/slow"), buffered=True)

    assert status_of(rv) == 503
    assert "after" in calls
    release.set()
    worker.join()
    assert "teardown" in calls


def test_custom_status_and_retry_after(app):
    app.limit_concurrency(1, wait=False, reject_status=429, retry_after=7)
    worker, release = occupy_slot(app)

    rv = run_wsgi_app(app, make_environ(app, "/slow"), buffered=True)

    assert status_of(rv) == 429
    assert rv[2]["Retry-After"] == "7"
    assert body_of(rv)["error"]["code"] == 429

    release.set()
    worker.join()


# ---------------------------------------------------------------------------
# Waiting, queue limits, timeouts, FIFO
# ---------------------------------------------------------------------------


def test_waiting_request_succeeds_when_slot_frees(app):
    app.limit_concurrency(1, wait=True, wait_timeout=5.0)
    entered = threading.Event()
    release = threading.Event()

    @app.route("/slow")
    def slow():
        entered.set()
        release.wait(5.0)
        return "done"

    first = RequestWorker(app, make_environ(app, "/slow")).start()
    assert entered.wait(5.0)

    start = time.monotonic()

    def free():
        time.sleep(0.2)
        release.set()

    freer = threading.Thread(target=free)
    freer.start()
    second = RequestWorker(app, make_environ(app, "/slow")).start()
    second.join()
    first.join()
    freer.join()

    assert second.exc is None
    assert status_of(second.result) == 200
    assert time.monotonic() - start >= 0.15
    assert app.get_concurrency_stats()["app"]["rejected"] == 0


def test_wait_timeout_rejects_and_skips_view(app):
    app.limit_concurrency(1, wait=True, wait_timeout=0.2)
    view_ran = []
    entered = threading.Event()
    release = threading.Event()

    @app.route("/slow")
    def slow():
        view_ran.append(True)
        entered.set()
        release.wait(5.0)
        return "done"

    first = RequestWorker(app, make_environ(app, "/slow")).start()
    assert entered.wait(5.0)

    start = time.monotonic()
    rv = run_wsgi_app(app, make_environ(app, "/slow"), buffered=True)
    elapsed = time.monotonic() - start

    assert status_of(rv) == 503
    assert body_of(rv)["error"]["reason"] == "wait_timeout"
    assert elapsed >= 0.15
    # The timed-out request never executed the view.
    assert view_ran == [True]

    stats = app.get_concurrency_stats()["app"]
    assert stats["in_flight"] == 1
    assert stats["waiting"] == 0
    assert stats["rejected"] == 1

    release.set()
    first.join()
    assert app.get_concurrency_stats()["app"]["in_flight"] == 0


def test_full_waiting_queue_rejects_immediately(app):
    app.limit_concurrency(1, wait=True, wait_timeout=5.0, max_waiting=1)
    worker, release = occupy_slot(app)

    waiter = RequestWorker(app, make_environ(app, "/slow")).start()
    wait_for(lambda: app.get_concurrency_stats()["app"]["waiting"] == 1)

    start = time.monotonic()
    rv = run_wsgi_app(app, make_environ(app, "/slow"), buffered=True)

    assert status_of(rv) == 503
    assert body_of(rv)["error"]["reason"] == "queue_full"
    assert time.monotonic() - start < 1.0

    stats = app.get_concurrency_stats()["app"]
    assert stats["in_flight"] == 1
    assert stats["waiting"] == 1
    assert stats["rejected"] == 1

    release.set()
    worker.join()
    waiter.join()
    assert app.get_concurrency_stats()["app"]["in_flight"] == 0


def test_waiting_queue_is_fifo(app):
    app.limit_concurrency(1, wait=True, wait_timeout=5.0, max_waiting=10)

    lock = threading.Lock()
    next_id = [0]
    started = []
    finished = []
    releases = [threading.Event() for _ in range(3)]

    @app.route("/slow")
    def slow():
        with lock:
            idx = next_id[0]
            next_id[0] += 1
            started.append(idx)
        releases[idx].wait(5.0)
        finished.append(idx)
        return f"{idx}"

    first = RequestWorker(app, make_environ(app, "/slow")).start()
    wait_for(lambda: started == [0])

    workers = [first]
    for _ in range(2):
        w = RequestWorker(app, make_environ(app, "/slow")).start()
        workers.append(w)

    wait_for(lambda: app.get_concurrency_stats()["app"]["waiting"] == 2)

    for event in releases:
        event.set()
        # Give the next request time to run and finish before releasing it.
        time.sleep(0.05)

    for w in workers:
        w.join()

    assert started == [0, 1, 2]
    assert finished == [0, 1, 2]
    for w in workers[1:]:
        assert status_of(w.result) == 200
    assert app.get_concurrency_stats()["app"]["rejected"] == 0


def test_registered_error_handler_handles_rejection(app):
    app.limit_concurrency(1, wait=False, retry_after=9)

    handled = []

    @app.errorhandler(503)
    def busy(exc):
        handled.append((exc.reason, exc.quota))
        return {"busy": True}, 503

    worker, release = occupy_slot(app)
    rv = run_wsgi_app(app, make_environ(app, "/slow"), buffered=True)

    assert status_of(rv) == 503
    assert body_of(rv) == {"busy": True}
    # The retry hint survives a response built by a custom error handler.
    assert rv[2]["Retry-After"] == "9"
    assert handled == [("in_flight", "app")]

    release.set()
    worker.join()


def test_wait_timeout_race_does_not_leak_slots(app):
    # Holder durations straddle the wait timeout, so releases and waiter
    # timeouts frequently race. A slot handed to a request as it times out
    # must be forwarded instead of being held forever by that request.
    app.limit_concurrency(1, wait=True, wait_timeout=0.01, max_waiting=100)

    @app.route("/busy")
    def busy():
        time.sleep(random.uniform(0.008, 0.014))
        return "ok"

    def hit():
        try:
            run_wsgi_app(app, make_environ(app, "/busy"), buffered=True)
        except BaseException:
            pass

    threads = []

    for _ in range(300):
        thread = threading.Thread(target=hit)
        thread.start()
        threads.append(thread)
        time.sleep(random.uniform(0.0, 0.003))

    for thread in threads:
        thread.join(15.0)
        assert not thread.is_alive()

    stats = app.get_concurrency_stats()["app"]
    assert stats["waiting"] == 0
    assert stats["in_flight"] == 0

    # Capacity was not lost: a final request is handled normally.
    rv = run_wsgi_app(app, make_environ(app, "/busy"), buffered=True)
    assert status_of(rv) == 200
    assert app.get_concurrency_stats()["app"]["in_flight"] == 0


def test_zero_timeout_rejects_immediately(app):
    app.limit_concurrency(1, wait=True, wait_timeout=0)
    worker, release = occupy_slot(app)

    rv = run_wsgi_app(app, make_environ(app, "/slow"), buffered=True)

    assert status_of(rv) == 503
    assert body_of(rv)["error"]["reason"] == "in_flight"

    release.set()
    worker.join()


# ---------------------------------------------------------------------------
# Precedence: endpoint > blueprint > app
# ---------------------------------------------------------------------------


def test_blueprint_and_endpoint_precedence(app):
    bp = Blueprint("bp", __name__)
    bp.limit_concurrency(1, wait=False)

    @bp.route("/plain")
    def plain():
        entered_plain.set()
        release_plain.wait(5.0)
        return "plain"

    @bp.route("/special")
    @bp.concurrent(1, wait=False)
    def special():
        entered_special.set()
        release_special.wait(5.0)
        return "special"

    entered_plain = threading.Event()
    release_plain = threading.Event()
    entered_special = threading.Event()
    release_special = threading.Event()

    app.register_blueprint(bp, url_prefix="/bp")

    stats = app.get_concurrency_stats()
    assert "blueprint:bp" in stats
    assert "endpoint:bp.special" in stats

    # Holding a plain endpoint consumes the blueprint quota.
    worker = RequestWorker(app, make_environ(app, "/bp/plain")).start()
    assert entered_plain.wait(5.0)
    rv = run_wsgi_app(app, make_environ(app, "/bp/plain"), buffered=True)
    assert body_of(rv)["error"]["quota"] == "blueprint:bp"
    release_plain.set()
    worker.join()

    # The endpoint declaration uses a separate quota.
    worker = RequestWorker(app, make_environ(app, "/bp/special")).start()
    assert entered_special.wait(5.0)
    rv = run_wsgi_app(app, make_environ(app, "/bp/special"), buffered=True)
    assert body_of(rv)["error"]["quota"] == "endpoint:bp.special"
    release_special.set()
    worker.join()


def test_application_quota_is_the_default(app):
    app.limit_concurrency(1, wait=False)

    @app.route("/two")
    def two():
        return "two"

    worker, release = occupy_slot(app, "/slow")
    rv = run_wsgi_app(app, make_environ(app, "/slow"), buffered=True)
    assert body_of(rv)["error"]["quota"] == "app"

    release.set()
    worker.join()

    rv = run_wsgi_app(app, make_environ(app, "/two"), buffered=True)
    assert status_of(rv) == 200


def test_nested_blueprint_inner_quota_wins(app):
    parent = Blueprint("parent", __name__)
    child = Blueprint("child", __name__)
    parent.limit_concurrency(1, wait=False)
    child.limit_concurrency(1, wait=False)

    entered = threading.Event()
    release = threading.Event()

    @child.route("/slow")
    def slow():
        entered.set()
        release.wait(5.0)
        return "slow"

    parent.register_blueprint(child, url_prefix="/child")
    app.register_blueprint(parent, url_prefix="/parent")

    worker = RequestWorker(app, make_environ(app, "/parent/child/slow")).start()
    assert entered.wait(5.0)

    rv = run_wsgi_app(app, make_environ(app, "/parent/child/slow"), buffered=True)
    assert body_of(rv)["error"]["quota"] == "blueprint:parent.child"

    release.set()
    worker.join()


def test_app_quota_covers_unmatched_url(app):
    app.limit_concurrency(1, wait=False)
    worker, release = occupy_slot(app)

    rv = run_wsgi_app(app, make_environ(app, "/does-not-exist"), buffered=True)

    assert status_of(rv) == 503
    release.set()
    worker.join()


# ---------------------------------------------------------------------------
# Release on every exit path, including streaming
# ---------------------------------------------------------------------------


def test_slot_released_after_view_exception(app):
    app.limit_concurrency(1, wait=False)

    @app.route("/boom")
    def boom():
        raise RuntimeError("boom")

    worker = RequestWorker(app, make_environ(app, "/boom")).start()
    worker.join()

    assert isinstance(worker.exc, RuntimeError)
    assert app.get_concurrency_stats()["app"]["in_flight"] == 0


def test_slot_released_when_error_handler_raises(app):
    app.limit_concurrency(1, wait=False)

    @app.errorhandler(Exception)
    def handle(exc):
        raise ValueError("handler failed")

    @app.route("/boom")
    def boom():
        raise RuntimeError("boom")

    worker = RequestWorker(app, make_environ(app, "/boom")).start()
    worker.join()

    assert isinstance(worker.exc, ValueError)
    assert app.get_concurrency_stats()["app"]["in_flight"] == 0


def test_slot_released_when_after_request_raises(app):
    app.limit_concurrency(1, wait=False)

    @app.after_request
    def after(response):
        raise RuntimeError("after failed")

    worker, release = occupy_slot(app, "/ok")
    release.set()
    worker.join()

    assert isinstance(worker.exc, RuntimeError)
    assert app.get_concurrency_stats()["app"]["in_flight"] == 0


def test_slot_released_when_teardown_raises(app):
    app.limit_concurrency(1, wait=False)

    @app.teardown_request
    def teardown(exc):
        raise RuntimeError("teardown failed")

    worker, release = occupy_slot(app)
    release.set()
    worker.join()

    exc = worker.exc

    # Python 3.11+ collects teardown errors in nested exception groups.
    assert any(isinstance(e, RuntimeError) for e in flatten_exception(exc))
    assert app.get_concurrency_stats()["app"]["in_flight"] == 0


def test_slot_held_until_streaming_body_consumed(app):
    app.limit_concurrency(1, wait=False)

    consumed_second = threading.Event()

    def generate():
        yield b"first"
        consumed_second.wait(5.0)
        yield b"second"

    @app.route("/stream")
    def stream():
        return app.response_class(generate())

    app_iter, status, _ = run_wsgi_app(
        app, make_environ(app, "/stream"), buffered=False
    )
    assert int(status.split()[0]) == 200

    # Only the first chunk has been consumed; the slot must remain held.
    assert app.get_concurrency_stats()["app"]["in_flight"] == 1

    consumed_second.set()
    assert b"".join(app_iter) == b"firstsecond"
    app_iter.close()

    assert app.get_concurrency_stats()["app"]["in_flight"] == 0
    assert app.get_concurrency_stats()["app"]["rejected"] == 0


def test_slot_released_on_client_disconnect_during_stream(app):
    app.limit_concurrency(1, wait=False)
    reached = threading.Event()

    def generate():
        reached.set()
        yield b"first"
        time.sleep(5.0)
        yield b"second"

    @app.route("/stream")
    def stream():
        return app.response_class(generate())

    app_iter, _, _ = run_wsgi_app(app, make_environ(app, "/stream"), buffered=False)
    assert reached.wait(5.0)
    assert app.get_concurrency_stats()["app"]["in_flight"] == 1

    # Simulate the client closing the connection while the body streams.
    app_iter.close()

    assert app.get_concurrency_stats()["app"]["in_flight"] == 0


# ---------------------------------------------------------------------------
# Stats and isolation
# ---------------------------------------------------------------------------


def test_stats_reflect_in_flight_and_waiting(app):
    app.limit_concurrency(1, wait=True, wait_timeout=5.0, max_waiting=2)
    worker, release = occupy_slot(app)

    waiter = RequestWorker(app, make_environ(app, "/slow")).start()
    wait_for(lambda: app.get_concurrency_stats()["app"]["waiting"] == 1)

    stats = app.get_concurrency_stats()["app"]
    assert stats["limit"] == 1
    assert stats["max_waiting"] == 2
    assert stats["in_flight"] == 1
    assert stats["waiting"] == 1
    assert stats["rejected"] == 0

    release.set()
    worker.join()
    waiter.join()


def test_stats_isolated_between_apps(app):
    app.limit_concurrency(1, wait=False)
    other = make_app()
    other.limit_concurrency(1, wait=False)

    @other.route("/ok")
    def ok():
        return "ok"

    worker, release = occupy_slot(app, path="/slow")
    assert app.get_concurrency_stats()["app"]["in_flight"] == 1

    # The other app has no request in flight; no rejection.
    rv = run_wsgi_app(other, make_environ(other, "/ok"), buffered=True)
    assert status_of(rv) == 200
    assert other.get_concurrency_stats()["app"]["in_flight"] == 0
    assert other.get_concurrency_stats()["app"]["rejected"] == 0

    release.set()
    worker.join()


# ---------------------------------------------------------------------------
# Validation and setup-time errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_in_flight": 0},
        {"max_in_flight": -1},
        {"max_in_flight": "2"},
        {"max_in_flight": True},
        {"max_in_flight": 1, "wait": "yes"},
        {"max_in_flight": 1, "wait_timeout": -1},
        {"max_in_flight": 1, "wait_timeout": "soon"},
        {"max_in_flight": 1, "wait_timeout": timedelta(seconds=-1)},
        {"max_in_flight": 1, "max_waiting": -1},
        {"max_in_flight": 1, "reject_status": 399},
        {"max_in_flight": 1, "reject_status": 600},
        {"max_in_flight": 1, "retry_after": -1},
    ],
)
def test_invalid_declaration_raises(kwargs):
    with pytest.raises(ValueError):
        ConcurrencyQuota.configure(**kwargs)


def test_declaring_after_first_request_raises(app, client):
    @app.route("/")
    def index():
        return "ok"

    client.get("/")

    with pytest.raises(AssertionError):
        app.limit_concurrency(1)


def test_declaring_blueprint_after_registration_raises(app):
    bp = Blueprint("bp", __name__)
    app.register_blueprint(bp)

    with pytest.raises(AssertionError):
        bp.limit_concurrency(1)


def test_timedelta_and_defaults_normalized():
    quota = ConcurrencyQuota.configure(
        3, wait=True, wait_timeout=timedelta(milliseconds=2500)
    )
    assert quota.wait_timeout == 2.5
    assert quota.retry_after == 3
    assert quota.max_waiting == 3

    quota = ConcurrencyQuota.configure(2, wait=False)
    assert quota.retry_after == 1


def test_endpoint_decorator_sets_marker(app):
    @app.route("/export")
    @app.concurrent(2, wait=False)
    def export():
        return "export"

    quota = getattr(app.view_functions["export"], CONCURRENCY_QUOTA_ATTRIBUTE)
    assert isinstance(quota, ConcurrencyQuota)
    assert quota.max_in_flight == 2
    assert quota.wait is False


# ---------------------------------------------------------------------------
# Testing and debug mode behave the same
# ---------------------------------------------------------------------------


def test_rejection_works_in_testing_mode(app):
    app.limit_concurrency(1, wait=False)
    worker, release = occupy_slot(app)

    rv = run_wsgi_app(app, make_environ(app, "/slow"), buffered=True)
    assert status_of(rv) == 503

    release.set()
    worker.join()


def test_rejection_works_without_testing_mode():
    app = Flask(__name__)
    app.config["TESTING"] = False
    app.config["DEBUG"] = False
    app.limit_concurrency(1, wait=False)

    entered = threading.Event()
    release = threading.Event()

    @app.route("/slow")
    def slow():
        entered.set()
        release.wait(5.0)
        return "done"

    worker = RequestWorker(app, make_environ(app, "/slow")).start()
    assert entered.wait(5.0)

    rv = run_wsgi_app(app, make_environ(app, "/slow"), buffered=True)
    assert status_of(rv) == 503
    assert body_of(rv)["error"]["reason"] == "in_flight"

    release.set()
    worker.join()
    assert app.get_concurrency_stats()["app"]["in_flight"] == 0
