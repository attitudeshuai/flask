import json
import os

import pytest
from click.testing import CliRunner
from werkzeug.exceptions import NotFound

from flask import Blueprint
from flask import Flask
from flask.cli import cli
from flask.cli import ScriptInfo
from flask.snapshot import diff_snapshots
from flask.snapshot import format_snapshot_diff
from flask.snapshot import REDACTED
from flask.snapshot import SNAPSHOT_SCHEMA_VERSION
from flask.snapshot import SnapshotError
from flask.snapshot import take_snapshot

ROOT_PATH = os.path.dirname(__file__)

HOOK_GROUPS = (
    "before_request",
    "after_request",
    "teardown_request",
    "url_value_preprocessor",
    "url_defaults",
    "template_context_processor",
    "teardown_appcontext",
    "shell_context_processor",
    "url_build_error_handler",
)
SECTIONS = (
    "routes",
    "blueprints",
    "hooks",
    "error_handlers",
    "templates",
    "static",
    "extensions",
    "commands",
)


class CustomError(Exception):
    pass


def make_app(name="snapshotapp", **kwargs):
    kwargs.setdefault("root_path", ROOT_PATH)
    return Flask(name, **kwargs)


def by_key(entries, key):
    return {entry[key]: entry for entry in entries}


# ---------------------------------------------------------------------------
# Determinism and structure
# ---------------------------------------------------------------------------


def test_snapshot_is_deterministic(app):
    @app.route("/")
    def index():
        return "ok"

    first = app.registration_snapshot()
    second = app.registration_snapshot()

    assert first == second
    assert json.dumps(first, indent=2, ensure_ascii=False) == json.dumps(
        second, indent=2, ensure_ascii=False
    )


def test_snapshot_sections(app):
    snapshot = app.registration_snapshot()

    assert list(snapshot) == ["meta", "app", *SECTIONS]
    assert snapshot["meta"]["schema_version"] == SNAPSHOT_SCHEMA_VERSION
    assert isinstance(snapshot["meta"]["flask_version"], str)
    assert snapshot["app"] == {
        "name": "flask_test",
        "import_name": "flask_test",
        "root_path": ROOT_PATH,
        "instance_path": snapshot["app"]["instance_path"],
        "host_matching": False,
        "subdomain_matching": False,
    }
    assert set(snapshot["hooks"]) == set(HOOK_GROUPS)
    for group in HOOK_GROUPS:
        assert isinstance(snapshot["hooks"][group], list)


def test_snapshot_json_serializable(app):
    snapshot = app.registration_snapshot()
    json.dumps(snapshot)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_route_entries(app):
    @app.route(
        "/items/<int:item_id>",
        methods=["post", "put"],
        subdomain="api",
        defaults={"page": 2, "api_token": "secret-value"},
    )
    def item(item_id):
        return "ok"

    snapshot = app.registration_snapshot()
    entry = next(
        route for route in snapshot["routes"] if route["endpoint"] == "item"
    )

    assert entry["rule"] == "/items/<int:item_id>"
    assert entry["methods"] == ["OPTIONS", "POST", "PUT"]
    assert entry["subdomain"] == "api"
    assert entry["host"] is None
    # Werkzeug includes default keys in the rule arguments.
    assert entry["arguments"] == ["api_token", "item_id", "page"]
    defaults = {pair["key"]: pair["value"] for pair in entry["defaults"]}
    assert defaults["page"] == 2
    assert defaults["api_token"] == REDACTED
    assert entry["view"]["qualname"].endswith("item")
    assert entry["view"]["file"] == __file__
    assert isinstance(entry["seq"], int)

    full = app.registration_snapshot(include_sensitive=True)
    full_entry = next(
        route for route in full["routes"] if route["endpoint"] == "item"
    )
    full_defaults = {
        pair["key"]: pair["value"] for pair in full_entry["defaults"]
    }
    assert full_defaults["api_token"] == "secret-value"


def test_route_host_matching():
    app = make_app(
        host_matching=True, static_folder=None, template_folder=None
    )

    @app.route("/", host="example.com")
    def hosted():
        return "ok"

    @app.route("/other")
    def other():
        return "ok"

    routes = by_key(app.registration_snapshot()["routes"], "endpoint")
    assert routes["hosted"]["host"] == "example.com"
    assert routes["hosted"]["subdomain"] == ""
    assert routes["other"]["host"] is None


def test_route_unserializable_default_is_typed(app):
    class Marker:
        pass

    marker = Marker()
    app.add_url_rule("/m", endpoint="marker", defaults={"obj": marker})

    entry = next(
        route
        for route in app.registration_snapshot()["routes"]
        if route["endpoint"] == "marker"
    )
    defaults = {pair["key"]: pair["value"] for pair in entry["defaults"]}
    assert defaults["obj"]["type"].endswith("Marker")


def test_route_registration_order_is_stable(app):
    for name in ("z", "a", "m"):
        app.add_url_rule(f"/{name}", endpoint=name)

    endpoints = [
        route["endpoint"]
        for route in app.registration_snapshot()["routes"]
        if route["endpoint"] in {"z", "a", "m"}
    ]
    assert endpoints == ["z", "a", "m"]


# ---------------------------------------------------------------------------
# Blueprints
# ---------------------------------------------------------------------------


def test_blueprints_and_nesting(app):
    parent = Blueprint("parent", __name__, url_prefix="/parent")
    child = Blueprint("child", __name__, url_prefix="/child", subdomain="sub")

    parent.register_blueprint(child, name="child")
    app.register_blueprint(parent)

    blueprints = by_key(app.registration_snapshot()["blueprints"], "name")
    assert list(blueprints) == ["parent", "parent.child"]
    assert blueprints["parent"]["parent"] is None
    assert blueprints["parent"]["url_prefix"] == "/parent"
    assert blueprints["parent.child"]["parent"] == "parent"
    assert blueprints["parent.child"]["url_prefix"] == "/child"
    assert blueprints["parent.child"]["subdomain"] == "sub"
    assert [bp["seq"] for bp in blueprints.values()] == [0, 1]


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


def test_hooks_scope_order_and_source(app):
    bp = Blueprint("bp", __name__)

    @app.before_request
    def app_before():
        pass

    @bp.before_request
    def bp_before():
        pass

    @bp.before_app_request
    def global_before():
        pass

    @app.teardown_appcontext
    def app_teardown(exc):
        pass

    app.register_blueprint(bp)

    hooks = app.registration_snapshot()["hooks"]
    before = hooks["before_request"]

    app_entries = [entry for entry in before if entry["scope"] is None]
    bp_entries = [entry for entry in before if entry["scope"] == "bp"]

    names = [entry["name"] for entry in app_entries]
    assert "app_before" in names and "global_before" in names
    assert [entry["name"] for entry in bp_entries] == ["bp_before"]
    for group in before:
        assert isinstance(group["seq"], int)
        assert group["file"] == __file__

    scopes = [entry["scope"] for entry in before]
    # Application scope always comes before blueprint scopes.
    app_index = scopes.index(None)
    assert app_index < next(i for i, scope in enumerate(scopes) if scope == "bp")

    teardown = hooks["teardown_appcontext"]
    assert teardown[-1]["name"] == "app_teardown"
    assert teardown[-1]["scope"] is None

    for group in HOOK_GROUPS:
        for entry in hooks[group]:
            assert "module" in entry and "qualname" in entry


def test_hook_groups_all_present(app):
    snapshot = app.registration_snapshot()
    # The default template context processor is registered for the app.
    processors = snapshot["hooks"]["template_context_processor"]
    assert processors[0]["name"] == "_default_template_ctx_processor"
    assert processors[0]["scope"] is None


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------


def test_error_handler_overwrite_visible(app):
    from werkzeug.exceptions import default_exceptions

    assert 404 in default_exceptions

    @app.errorhandler(404)
    def old_not_found(error):
        return "old", 404

    @app.errorhandler(404)
    def new_not_found(error):
        return "new", 404

    entry = next(
        handler
        for handler in app.registration_snapshot()["error_handlers"]
        if handler["scope"] is None and handler["code"] == 404
    )
    assert entry["exception"]["qualname"] == "werkzeug.exceptions.NotFound"
    assert [handler["handler"]["name"] for handler in entry["handlers"]] == [
        "old_not_found",
        "new_not_found",
    ]
    assert entry["handlers"][0]["active"] is False
    assert entry["handlers"][1]["active"] is True

    # The history does not change which handler is actually active.
    assert (
        app.error_handler_spec[None][404][NotFound] is new_not_found
    )


def test_error_handler_custom_exception(app):
    @app.errorhandler(CustomError)
    def handle_custom(error):
        return "custom", 400

    entry = next(
        handler
        for handler in app.registration_snapshot()["error_handlers"]
        if handler["exception"]["name"] == "CustomError"
    )
    assert entry["code"] is None
    assert entry["handlers"][0]["active"] is True


def test_blueprint_error_handler_scope(app):
    bp = Blueprint("bp", __name__)

    @app.errorhandler(403)
    def app_forbidden(error):
        return "app", 403

    @bp.errorhandler(403)
    def bp_forbidden(error):
        return "bp", 403

    @bp.app_errorhandler(500)
    def app_server_error(error):
        return "app500", 500

    app.register_blueprint(bp)

    handlers = app.registration_snapshot()["error_handlers"]
    bp_403 = next(
        handler
        for handler in handlers
        if handler["scope"] == "bp" and handler["code"] == 403
    )
    assert bp_403["handlers"][0]["handler"]["name"] == "bp_forbidden"

    app_403 = next(
        handler
        for handler in handlers
        if handler["scope"] is None and handler["code"] == 403
    )
    assert app_403["handlers"][0]["handler"]["name"] == "app_forbidden"

    app_500 = next(
        handler
        for handler in handlers
        if handler["scope"] is None and handler["code"] == 500
    )
    assert app_500["handlers"][0]["handler"]["name"] == "app_server_error"


# ---------------------------------------------------------------------------
# Template loaders and static folders
# ---------------------------------------------------------------------------


def test_template_loader_chain(app):
    bp = Blueprint(
        "bp", __name__, template_folder="templates", static_folder="static"
    )
    app.register_blueprint(bp)

    chain = app.registration_snapshot()["templates"]
    assert chain[0]["scope"] is None
    assert chain[0]["loader_type"] == "jinja2.loaders.FileSystemLoader"
    assert chain[0]["paths"] == [
        os.path.join(ROOT_PATH, "templates")
    ]
    bp_loader = next(entry for entry in chain if entry["scope"] == "bp")
    assert bp_loader["loader_type"] == "jinja2.loaders.FileSystemLoader"
    assert bp_loader["paths"] == [
        os.path.join(os.path.dirname(__file__), "templates")
    ]
    assert [entry["seq"] for entry in chain] == list(range(len(chain)))


def test_static_directories(app):
    bp = Blueprint("bp", __name__, static_folder="static")
    app.register_blueprint(bp)

    static = app.registration_snapshot()["static"]
    scopes = by_key(static, "scope")
    assert scopes[None]["has_static_folder"] is True
    assert scopes[None]["static_url_path"] == "/static"
    assert scopes["bp"]["static_url_path"] == "/static"
    assert scopes["bp"]["static_folder"].replace("\\", "/").endswith(
        "/static"
    )


# ---------------------------------------------------------------------------
# Extensions and commands
# ---------------------------------------------------------------------------


def test_extensions_do_not_leak_values(app):
    app.extensions["db"] = {"connection": "secret"}
    app.extensions["cache"] = object()

    extensions = app.registration_snapshot()["extensions"]
    entries = by_key(extensions, "key")
    assert entries["db"]["value_type"] == "builtins.dict"
    assert entries["cache"]["value_type"] == "builtins.object"
    assert all("value" not in entry for entry in extensions)
    assert [entry["key"] for entry in extensions] == ["db", "cache"]


def test_commands(app):
    bp = Blueprint("bp", __name__)

    @app.cli.command("custom")
    def custom_command():
        pass

    @bp.cli.command("task")
    def task_command():
        pass

    app.register_blueprint(bp)

    commands = by_key(app.registration_snapshot()["commands"], "path")
    assert "custom" in commands
    assert commands["custom"]["kind"] == "command"
    assert commands["custom"]["scope"] is None
    assert commands["custom"]["callback"]["name"] == "custom_command"

    assert "bp" in commands
    assert commands["bp"]["kind"] == "group"
    assert commands["bp"]["scope"] == "bp"
    assert "bp.task" in commands
    assert commands["bp.task"]["scope"] == "bp"


def test_blueprint_commands_merged_into_root(app):
    bp = Blueprint("bp", __name__, cli_group=None)

    @bp.cli.command("merged")
    def merged_command():
        pass

    app.register_blueprint(bp)

    commands = by_key(app.registration_snapshot()["commands"], "path")
    assert "merged" in commands
    assert commands["merged"]["scope"] == "bp"


# ---------------------------------------------------------------------------
# Side effects
# ---------------------------------------------------------------------------


def test_snapshot_has_no_side_effects(app):
    @app.route("/")
    def index():
        return "ok"

    @app.before_request
    def before():
        pass

    before_keys = {
        registry_name: set(getattr(app, registry_name))
        for registry_name in (
            "error_handler_spec",
            "before_request_funcs",
            "after_request_funcs",
            "teardown_request_funcs",
            "url_value_preprocessors",
            "url_default_functions",
            "template_context_processors",
        )
    }

    snapshot = app.registration_snapshot()
    app.registration_snapshot()
    app.registration_snapshot()

    assert "jinja_env" not in app.__dict__
    assert "jinja_loader" not in app.__dict__

    for registry_name, keys in before_keys.items():
        assert set(getattr(app, registry_name)) == keys

    # The index view must never have been called.
    assert snapshot["routes"][0]["view"] is not None
    assert index() == "ok"  # sanity check outside the snapshot API


def test_snapshot_sends_no_signal(app, monkeypatch):
    import flask.signals as signals

    sent = []
    for name in (
        "appcontext_pushed",
        "appcontext_tearing_down",
        "request_started",
        "request_finished",
        "before_render_template",
    ):
        getattr(signals, name).connect(
            lambda *args, name=name, **kwargs: sent.append(name), app
        )

    app.registration_snapshot()

    assert sent == []


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def test_diff_identical(app):
    snapshot = app.registration_snapshot()
    result = diff_snapshots(snapshot, snapshot)

    assert result["identical"] is True
    assert result["summary"]["added"] == 0
    assert result["summary"]["removed"] == 0
    assert result["summary"]["changed"] == 0
    assert "No differences." in format_snapshot_diff(result)


def make_diff_apps():
    old = make_app()

    @old.route("/")
    def index():
        return "old"

    @old.route("/removed", methods=["POST"])
    def removed():
        return "removed"

    old_bp = Blueprint("admin", __name__)
    old.register_blueprint(old_bp)
    old.extensions["db"] = object()

    new = make_app()

    @new.route("/", endpoint="index")
    def index_new():
        return "new default"

    @new.route("/added")
    def added():
        return "added"

    new.extensions["db"] = object()
    new.extensions["cache"] = object()

    return old, new


def test_diff_added_removed_changed():
    old, new = make_diff_apps()
    result = diff_snapshots(
        old.registration_snapshot(), new.registration_snapshot()
    )

    assert result["identical"] is False
    added_labels = [item["label"] for item in result["added"]["routes"]]
    removed_labels = [item["label"] for item in result["removed"]["routes"]]
    assert any("/added" in label for label in added_labels)
    assert any("/removed" in label for label in removed_labels)

    assert any(
        item["key"][1] == "admin" for item in result["removed"]["blueprints"]
    )
    assert [item["key"][1] for item in result["added"]["extensions"]] == [
        "cache"
    ]

    text = format_snapshot_diff(result)
    assert "Summary:" in text
    assert "Added:" in text
    assert "Removed:" in text


def test_diff_route_attribute_change():
    old = make_app("sameapp")

    @old.route("/item", defaults={"page": 1})
    def item():
        return ""

    new = make_app("sameapp")

    @new.route("/item", endpoint="item", methods=["POST"], defaults={"page": 2})
    def item_new():
        return ""

    result = diff_snapshots(
        old.registration_snapshot(), new.registration_snapshot()
    )
    changed = result["changed"]["routes"]
    fields = {
        name: change
        for entry in changed
        for name, change in entry["changes"].items()
    }
    assert "methods" in fields
    assert fields["methods"]["from"] == ["GET", "HEAD", "OPTIONS"]
    assert fields["methods"]["to"] == ["OPTIONS", "POST"]
    assert "defaults" in fields


def test_diff_error_handler_change(app):
    old_app = make_app("sameapp")

    @old_app.errorhandler(404)
    def old_handler(error):
        return "old", 404

    new_app = make_app("sameapp")

    @new_app.errorhandler(404)
    def replaced_handler(error):
        return "same name, replaced"

    @new_app.errorhandler(404)
    def new_handler(error):
        return "new", 404

    result = diff_snapshots(
        old_app.registration_snapshot(), new_app.registration_snapshot()
    )
    changed = result["changed"]["error_handlers"]
    assert changed
    assert "handlers" in changed[0]["changes"]


def test_diff_result_is_json_serializable():
    old, new = make_diff_apps()
    result = diff_snapshots(
        old.registration_snapshot(), new.registration_snapshot()
    )
    json.dumps(result)


# ---------------------------------------------------------------------------
# Invalid input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    (
        None,
        [],
        "snapshot",
        42,
        {},
        {"meta": {}, "app": {}},
    ),
)
def test_diff_rejects_malformed_snapshot(app, bad):
    snapshot = app.registration_snapshot()

    with pytest.raises(SnapshotError):
        diff_snapshots(bad, snapshot)

    with pytest.raises(SnapshotError):
        diff_snapshots(snapshot, bad)


def test_diff_rejects_bad_schema_version(app):
    snapshot = app.registration_snapshot()
    snapshot["meta"]["schema_version"] = "999"

    with pytest.raises(SnapshotError, match="unsupported schema version"):
        diff_snapshots(snapshot, snapshot)


def test_diff_rejects_wrong_section_types(app):
    snapshot = app.registration_snapshot()
    snapshot["routes"] = {}

    with pytest.raises(SnapshotError, match="'routes' section must be a list"):
        diff_snapshots(snapshot, snapshot)


def test_diff_rejects_missing_hook_group(app):
    snapshot = app.registration_snapshot()
    del snapshot["hooks"]["before_request"]

    with pytest.raises(SnapshotError, match="hooks"):
        diff_snapshots(snapshot, snapshot)


def test_diff_rejects_different_applications(app):
    other = make_app("other-app")
    with pytest.raises(SnapshotError, match="different applications"):
        diff_snapshots(
            app.registration_snapshot(), other.registration_snapshot()
        )


def test_take_snapshot_accepts_dict_like_app(app):
    assert take_snapshot(app) == app.registration_snapshot()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def runner():
    return CliRunner()


def create_cli_app():
    app = make_app("cliapp")

    @app.route("/")
    def index():
        return "ok"

    return app


def create_cli_app_v2():
    app = make_app("cliapp")

    @app.route("/", endpoint="index")
    def index_v2():
        return "ok"

    @app.route("/new")
    def new():
        return "new"

    return app


def test_cli_snapshot_outputs_json(runner):
    result = runner.invoke(
        cli,
        ["snapshot"],
        obj=ScriptInfo(create_app=create_cli_app),
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["app"]["name"] == "cliapp"
    assert any(route["endpoint"] == "index" for route in data["routes"])


def test_cli_snapshot_writes_file(runner):
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli,
            ["snapshot", "--output", "snapshot.json"],
            obj=ScriptInfo(create_app=create_cli_app),
        )

        assert result.exit_code == 0, result.output
        with open("snapshot.json", encoding="utf-8") as f:
            data = json.load(f)
        assert data["app"]["name"] == "cliapp"


def test_cli_snapshot_diff(runner):
    with runner.isolated_filesystem():
        runner.invoke(
            cli,
            ["snapshot", "-o", "before.json"],
            obj=ScriptInfo(create_app=create_cli_app),
        )
        runner.invoke(
            cli,
            ["snapshot", "-o", "after.json"],
            obj=ScriptInfo(create_app=create_cli_app_v2),
        )

        result = runner.invoke(
            cli, ["snapshot-diff", "before.json", "after.json"]
        )
        assert result.exit_code == 1
        assert "Summary:" in result.output
        assert "/new" in result.output

        json_result = runner.invoke(
            cli,
            ["snapshot-diff", "before.json", "after.json", "--format", "json"],
        )
        assert json_result.exit_code == 1
        structured = json.loads(json_result.output)
        assert structured["summary"]["added"] >= 1


def test_cli_snapshot_diff_identical_exit_zero(runner):
    with runner.isolated_filesystem():
        for name in ("a.json", "b.json"):
            runner.invoke(
                cli,
                ["snapshot", "-o", name],
                obj=ScriptInfo(create_app=create_cli_app),
            )

        result = runner.invoke(
            cli, ["snapshot-diff", "a.json", "b.json"]
        )
        assert result.exit_code == 0
        assert "No differences." in result.output


def test_cli_snapshot_diff_invalid_json(runner):
    with runner.isolated_filesystem():
        with open("bad.json", "w", encoding="utf-8") as f:
            f.write("{not json")
        runner.invoke(
            cli,
            ["snapshot", "-o", "good.json"],
            obj=ScriptInfo(create_app=create_cli_app),
        )

        result = runner.invoke(
            cli, ["snapshot-diff", "bad.json", "good.json"]
        )
        assert result.exit_code == 2
        assert "not valid JSON" in result.output


def test_cli_snapshot_diff_version_mismatch(runner):
    with runner.isolated_filesystem():
        runner.invoke(
            cli,
            ["snapshot", "-o", "good.json"],
            obj=ScriptInfo(create_app=create_cli_app),
        )
        with open("old.json", "w", encoding="utf-8") as f:
            f.write(json.dumps({"meta": {"schema_version": "0"}, "app": {}}))

        result = runner.invoke(
            cli, ["snapshot-diff", "old.json", "good.json"]
        )
        assert result.exit_code == 2
        assert "unsupported schema version" in result.output


def test_cli_routes_command_unaffected(runner):
    result = runner.invoke(
        cli,
        ["routes"],
        obj=ScriptInfo(create_app=create_cli_app),
    )

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0].split() == ["Endpoint", "Methods", "Rule"]
    assert any(line.lstrip().startswith("index") for line in lines)


def test_cli_commands_listed(runner):
    result = runner.invoke(
        cli,
        ["--help"],
        obj=ScriptInfo(create_app=create_cli_app),
    )

    assert result.exit_code == 0
    assert "snapshot" in result.output
    assert "snapshot-diff" in result.output
