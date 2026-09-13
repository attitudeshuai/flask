from __future__ import annotations

import asyncio
import threading

import pytest

from flask import ConfigOverrideScope
from flask import current_app
from flask import Flask
from flask import request

pytest.importorskip("asgiref")

KEY = "TENANT"


@pytest.fixture()
def app():
    app = Flask(__name__)
    app.config[KEY] = "default"
    return app


# -- explicit scopes --------------------------------------------------------


def test_explicit_scope_overrides_and_restores(app):
    config = app.config

    with config.override({KEY: "a"}):
        assert config[KEY] == "a"
        assert config.get(KEY) == "a"

    assert config[KEY] == "default"
    assert config.get(KEY) == "default"


def test_explicit_scope_restores_on_exception(app):
    config = app.config

    with pytest.raises(RuntimeError):
        with config.override(TENANT="a"):
            assert config[KEY] == "a"
            raise RuntimeError("boom")

    assert config[KEY] == "default"


def test_explicit_scope_accepts_mapping_and_kwargs(app):
    with app.config.override({"A": 1}, B=2):
        assert app.config["A"] == 1
        assert app.config["B"] == 2


def test_override_scope_alias(app):
    with app.config.override_scope(TENANT="a"):
        assert app.config[KEY] == "a"

    assert app.config[KEY] == "default"


def test_nested_scopes_inner_wins(app):
    config = app.config

    with config.override({KEY: "outer", "OUTER": 1}, name="outer") as outer:
        assert config.override_source(KEY) is outer

        with config.override({KEY: "inner"}, name="inner") as inner:
            assert config[KEY] == "inner"
            assert config["OUTER"] == 1
            assert config.override_source(KEY) is inner
            assert config.override_source("OUTER") is outer

        # The inner layer is gone, the outer layer still applies.
        assert config[KEY] == "outer"
        assert config.override_source(KEY) is outer

    assert config[KEY] == "default"
    assert config.override_source(KEY) is None


def test_scope_declare_and_config_declare(app):
    config = app.config

    with config.override() as scope:
        scope.declare("A", 1)
        config.declare_override("B", 2)
        config.declare_overrides({"C": 3}, D=4)
        assert config["A"] == 1
        assert config["B"] == 2
        assert config["C"] == 3
        assert config["D"] == 4

    assert "A" not in config
    assert "B" not in config


def test_declare_overrides_is_atomic(app):
    config = app.config

    with config.override(A=1) as scope:
        with pytest.raises(ValueError):
            # B would be valid, A collides; nothing should be written.
            config.declare_overrides({"B": 2, "A": 3})

        assert "B" not in scope
        assert "B" not in config


def test_override_adds_keys_absent_from_app(app):
    with app.config.override(NEW_KEY="x"):
        assert app.config["NEW_KEY"] == "x"

    with pytest.raises(KeyError):
        app.config["NEW_KEY"]


# -- resolution source ------------------------------------------------------


def test_override_source(app):
    config = app.config

    assert config.override_source(KEY) is None
    assert config.override_source("MISSING") is None

    with config.override(TENANT="a", name="named") as scope:
        source = config.override_source(KEY)
        assert source is scope
        assert isinstance(source, ConfigOverrideScope)
        assert source.kind == "scope"
        assert source.name == "named"
        assert dict(source.values) == {KEY: "a"}

    assert config.override_source(KEY) is None


def test_attribute_style_read_uses_same_path(app):
    # ConfigAttribute (and App properties such as debug) read through
    # config[key], so overrides apply without a separate code path.
    app.config["DEBUG"] = False
    assert app.debug is False

    with app.config.override(DEBUG=True):
        assert app.debug is True
        assert app.config.override_source("DEBUG").kind == "scope"

    assert app.debug is False
    assert app.config.override_source("DEBUG") is None


# -- dictionary view --------------------------------------------------------


def test_mapping_views_during_scope(app):
    config = app.config
    config["ONLY_NS_X"] = "base"
    base = dict.copy(config)

    with config.override({KEY: "a", "ONLY_SCOPE": 1}):
        # Membership and length reflect the resolved mapping.
        assert KEY in config
        assert "ONLY_SCOPE" in config
        assert len(config) == len(base) + 1

        # Iteration order: application keys first, then scope-only keys.
        assert list(config) == list(base) + ["ONLY_SCOPE"]

        resolved = dict(base, TENANT="a", ONLY_SCOPE=1)
        assert dict(config) == resolved
        assert set(config.keys()) == set(resolved)
        assert dict(config.items()) == resolved
        assert set(config.values()) == set(resolved.values())

        # Views behave like real dict views.
        assert config.keys() == resolved.keys()
        assert config.items() == resolved.items()
        assert config == resolved
        assert repr(config) == f"<Config {resolved!r}>"

        # get_namespace reads through items().
        with config.override(ONLY_NS_X="scope"):
            assert config.get_namespace("ONLY_NS_") == {"x": "scope"}

    # Everything is back to the application view.
    assert "ONLY_SCOPE" not in config
    assert len(config) == len(base)
    assert dict(config) == base
    assert config == base
    assert repr(config) == f"<Config {base!r}>"


def test_mapping_views_for_nested_scopes(app):
    config = app.config
    base_len = len(dict.copy(config))

    with config.override(OUTER=1, BOTH="outer"):
        with config.override(INNER=2, BOTH="inner"):
            resolved = dict(config)
            assert resolved["OUTER"] == 1
            assert resolved["INNER"] == 2
            assert resolved["BOTH"] == "inner"
            # OUTER, BOTH and INNER are all new keys; BOTH is shadowed.
            assert len(config) == base_len + 3


def test_dict_behavior_without_overrides_unchanged(app):
    config = app.config
    assert type(config.keys()) is type({}.keys())
    assert type(config.items()) is type({}.items())
    assert type(config.values()) is type({}.values())
    assert dict.copy(config) == dict(config)
    assert repr(config) == f"<Config {dict.__repr__(config)}>"


# -- request binding --------------------------------------------------------


def test_request_bound_override(app):
    seen = {}

    @app.before_request
    def declare():
        current_app.config.declare_override(KEY, request.headers["X-Tenant"])
        assert current_app.config[KEY] == request.headers["X-Tenant"]

    @app.after_request
    def after(response):
        seen["after"] = current_app.config[KEY]
        assert current_app.config.override_source(KEY).kind == "request"
        return response

    @app.teardown_request
    def teardown(exc):
        seen["teardown"] = current_app.config[KEY]

    @app.route("/")
    def index():
        assert current_app.config[KEY] == request.headers["X-Tenant"]
        return current_app.config[KEY]

    client = app.test_client()

    response = client.get("/", headers={"X-Tenant": "acme"})
    assert response.status_code == 200
    assert response.get_data(as_text=True) == "acme"
    assert seen == {"after": "acme", "teardown": "acme"}

    # The application value is restored after the request.
    assert app.config[KEY] == "default"

    # A subsequent request sees the application value unless it declares.
    response = client.get("/", headers={"X-Tenant": "globex"})
    assert response.get_data(as_text=True) == "globex"
    assert app.config[KEY] == "default"


def test_explicit_scope_inside_request(app):
    @app.route("/")
    def index():
        config = current_app.config
        assert config[KEY] == "default"

        with config.override(TENANT="scoped"):
            assert config[KEY] == "scoped"
            assert config.override_source(KEY).kind == "scope"

        # The explicit scope exited; no request scope was created.
        assert config[KEY] == "default"
        assert config.override_source(KEY) is None
        return "ok"

    response = app.test_client().get("/")
    assert response.status_code == 200
    assert app.config[KEY] == "default"


def test_request_scope_with_nested_explicit_scope(app):
    @app.before_request
    def declare():
        current_app.config.declare_override(KEY, "request")

    @app.route("/")
    def index():
        config = current_app.config
        assert config[KEY] == "request"

        with config.override(TENANT="scope"):
            assert config.override_source(KEY).kind == "scope"
            assert config[KEY] == "scope"

        assert config.override_source(KEY).kind == "request"
        assert config[KEY] == "request"
        return "ok"

    assert app.test_client().get("/").status_code == 200
    assert app.config[KEY] == "default"


# -- restoration on every failure path --------------------------------------


def _assert_restored(app):
    assert app.config[KEY] == "default"
    assert app.config.override_source(KEY) is None


def _exception_messages(error: BaseException) -> list[str]:
    messages = [str(error)]

    for sub in getattr(error, "exceptions", ()):
        messages.extend(_exception_messages(sub))

    return messages


def test_restored_when_view_raises(app):
    @app.before_request
    def declare():
        current_app.config.declare_override(KEY, "request")

    @app.route("/")
    def index():
        raise RuntimeError("view failed")

    app.config["PROPAGATE_EXCEPTIONS"] = True

    with pytest.raises(RuntimeError, match="view failed"):
        app.test_client().get("/")

    _assert_restored(app)


def test_restored_when_error_handler_raises(app):
    class AppError(Exception):
        pass

    @app.before_request
    def declare():
        current_app.config.declare_override(KEY, "request")

    @app.route("/")
    def index():
        raise AppError()

    @app.errorhandler(AppError)
    def handle(error):
        raise RuntimeError("handler failed")

    app.config["PROPAGATE_EXCEPTIONS"] = True

    with pytest.raises(RuntimeError, match="handler failed"):
        app.test_client().get("/")

    _assert_restored(app)


def test_restored_when_after_request_raises(app):
    @app.before_request
    def declare():
        current_app.config.declare_override(KEY, "request")

    @app.after_request
    def after(response):
        raise RuntimeError("after failed")

    @app.route("/")
    def index():
        return "ok"

    app.config["PROPAGATE_EXCEPTIONS"] = True

    with pytest.raises(RuntimeError, match="after failed"):
        app.test_client().get("/")

    _assert_restored(app)


def test_restored_when_teardown_request_raises(app):
    @app.before_request
    def declare():
        current_app.config.declare_override(KEY, "request")

    @app.teardown_request
    def teardown(exc):
        raise RuntimeError("teardown failed")

    @app.route("/")
    def index():
        return "ok"

    with pytest.raises(BaseException) as exc_info:
        app.test_client().get("/")

    assert "teardown failed" in _exception_messages(exc_info.value)
    _assert_restored(app)


def test_restored_when_teardown_appcontext_raises(app):
    @app.before_request
    def declare():
        current_app.config.declare_override(KEY, "request")

    @app.teardown_appcontext
    def teardown(exc):
        raise RuntimeError("app teardown failed")

    @app.route("/")
    def index():
        return "ok"

    with pytest.raises(BaseException) as exc_info:
        app.test_client().get("/")

    assert "app teardown failed" in _exception_messages(exc_info.value)
    _assert_restored(app)


# -- concurrency isolation --------------------------------------------------


def test_overrides_are_isolated_between_threads(app):
    barrier = threading.Barrier(2)
    results = {}

    @app.before_request
    def declare():
        current_app.config.declare_override(KEY, request.headers["X-Tenant"])

    @app.route("/")
    def index():
        tenant = current_app.config[KEY]
        # Hold both requests in flight so the overrides overlap in time.
        barrier.wait(timeout=5)
        assert current_app.config[KEY] == tenant
        assert current_app.config.override_source(KEY).kind == "request"
        return tenant

    def worker(tenant):
        client = app.test_client()
        response = client.get("/", headers={"X-Tenant": tenant})
        results[tenant] = response.get_data(as_text=True)

    threads = [
        threading.Thread(target=worker, args=("acme",)),
        threading.Thread(target=worker, args=("globex",)),
    ]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    assert results == {"acme": "acme", "globex": "globex"}
    assert app.config[KEY] == "default"


def test_explicit_scope_does_not_leak_to_thread(app):
    seen = {}

    def check():
        seen["thread"] = app.config[KEY]

    with app.config.override(TENANT="scoped"):
        thread = threading.Thread(target=check)
        thread.start()
        thread.join()
        assert app.config[KEY] == "scoped"

    assert seen == {"thread": "default"}
    assert app.config[KEY] == "default"


async def _async_isolation(app):
    # Two tasks hold overlapping overrides at the same time; neither may see
    # the other's value and the parent context must not be affected.
    ready = asyncio.Event()
    done = asyncio.Event()
    order = []

    async def named_task(value):
        with app.config.override(TENANT=value, name=f"task-{value}"):
            order.append(("enter", value, app.config[KEY]))
            ready.set()
            await done.wait()
            order.append(("exit", value, app.config[KEY]))
            return app.config[KEY]

    task1 = asyncio.create_task(named_task("a"))
    await ready.wait()
    task2 = asyncio.create_task(named_task("b"))
    await asyncio.sleep(0)
    done.set()
    results = await asyncio.gather(task1, task2)

    assert set(results) == {"a", "b"}
    assert app.config[KEY] == "default"
    assert {entry for entry in order if entry[0] == "enter"} == {
        ("enter", "a", "a"),
        ("enter", "b", "b"),
    }
    assert {entry for entry in order if entry[0] == "exit"} == {
        ("exit", "a", "a"),
        ("exit", "b", "b"),
    }


def test_overrides_are_isolated_between_async_tasks(app):
    asyncio.run(_async_isolation(app))
    assert app.config[KEY] == "default"


def test_async_view_sees_request_override_and_restores(app):
    @app.before_request
    def declare():
        current_app.config.declare_override(KEY, request.headers["X-Tenant"])

    @app.route("/")
    async def index():
        await asyncio.sleep(0)
        assert current_app.config[KEY] == request.headers["X-Tenant"]
        assert current_app.config.override_source(KEY).kind == "request"
        return current_app.config[KEY]

    response = app.test_client().get("/", headers={"X-Tenant": "acme"})
    assert response.status_code == 200
    assert response.get_data(as_text=True) == "acme"
    # The request layer may have been pushed inside the async task's copied
    # context; it must still be removed from the request thread.
    assert app.config[KEY] == "default"
    assert app.config.override_source(KEY) is None


def test_request_override_declared_inside_async_view(app):
    @app.teardown_request
    def teardown(exc):
        # The request scope exists until teardown finishes; pop restores it.
        if exc is None:
            assert current_app.config[KEY] == "async-declared"

    @app.route("/")
    async def index():
        current_app.config.declare_override(KEY, "async-declared")
        assert current_app.config[KEY] == "async-declared"
        return "ok"

    response = app.test_client().get("/")
    assert response.status_code == 200
    assert app.config[KEY] == "default"


# -- multiple apps ----------------------------------------------------------


def test_override_does_not_affect_other_app(app):
    other = Flask(__name__)
    other.config[KEY] = "other-default"

    with app.config.override(TENANT="a"):
        assert app.config[KEY] == "a"
        assert other.config[KEY] == "other-default"

    assert other.config[KEY] == "other-default"


# -- invalid declarations ---------------------------------------------------


def test_declare_outside_scope_or_request_raises(app):
    with pytest.raises(RuntimeError, match="outside of an override scope"):
        app.config.declare_override(KEY, "a")

    with app.app_context():
        with pytest.raises(RuntimeError, match="outside of an override scope"):
            app.config.declare_overrides({KEY: "a"})


def test_duplicate_declaration_in_same_scope_raises(app):
    with pytest.raises(ValueError, match="more than once"):
        with app.config.override({KEY: "a"}, TENANT="b"):
            pass

    with app.config.override({KEY: "a"}) as scope:
        with pytest.raises(ValueError, match="already declared"):
            app.config.declare_override(KEY, "b")
        with pytest.raises(ValueError, match="already declared"):
            scope.declare(KEY, "b")

    # Shadowing the same key in a nested scope is allowed.
    with app.config.override({KEY: "a"}):
        with app.config.override({KEY: "b"}):
            assert app.config[KEY] == "b"
        assert app.config[KEY] == "a"


def test_non_mapping_argument_raises(app):
    with pytest.raises(TypeError, match="mapping"):
        with app.config.override([(KEY, "a")]):
            pass

    with app.config.override():
        with pytest.raises(TypeError, match="mapping"):
            app.config.declare_overrides([(KEY, "a")])


def test_non_string_key_raises(app):
    with pytest.raises(TypeError, match="strings"):
        with app.config.override({1: "a"}):
            pass

    with app.config.override():
        with pytest.raises(TypeError, match="strings"):
            app.config.declare_override(1, "a")


def test_request_scope_invalid_declaration_does_not_leak(app):
    @app.route("/")
    def index():
        current_app.config.declare_override(KEY, "request")

        with pytest.raises(ValueError, match="already declared"):
            current_app.config.declare_override(KEY, "again")

        return "ok"

    assert app.test_client().get("/").status_code == 200
    assert app.config[KEY] == "default"


# -- other edges ------------------------------------------------------------


def test_nested_request_contexts(app):
    config = app.config

    with app.test_request_context(headers={"X-Tenant": "outer"}):
        config.declare_override(KEY, "outer")
        assert config[KEY] == "outer"

        with app.test_request_context(headers={"X-Tenant": "inner"}):
            config.declare_override(KEY, "inner")
            assert config[KEY] == "inner"
            assert config.override_source(KEY).name == "request"

        # The inner request layer is gone, the outer one still applies.
        assert config[KEY] == "outer"

    assert config[KEY] == "default"


def test_standalone_config_scope(tmp_path):
    from flask import Config

    config = Config(tmp_path, {"A": 1})

    with config.override(A=2):
        assert config["A"] == 2
        assert config.override_source("A").kind == "scope"

    assert config["A"] == 1
    assert config.override_source("A") is None
