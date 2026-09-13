import json

import pytest
from click.testing import CliRunner

from flask import Blueprint
from flask import Flask
from flask.cli import FlaskGroup
from flask.signals import appcontext_pushed
from flask.signals import got_request_exception
from flask.signals import request_finished
from flask.signals import request_started
from flask.signals import request_tearing_down


def names(items):
    return [item["function"]["name"] for item in items]


def error_names(items):
    return [item["handler"]["name"] for item in items]


def make_nested_app():
    """App with two hooks per scope on app, parent blueprint, child
    blueprint and an unrelated sibling blueprint."""
    app = Flask(__name__, static_folder=None)
    trace = []

    def make(kind, scope, idx):
        name = f"{kind}_{scope}_{idx}"

        if kind == "uvp":

            def func(endpoint, view_args, name=name):
                trace.append(name)

        elif kind == "before":

            def func(name=name):
                trace.append(name)

        elif kind == "after":

            def func(response, name=name):
                trace.append(name)
                return response

        else:

            def func(exc, name=name):
                trace.append(name)

        func.__name__ = name
        func.__qualname__ = name
        return func

    for kind, decorator in (
        ("uvp", app.url_value_preprocessor),
        ("before", app.before_request),
        ("after", app.after_request),
        ("teardown", app.teardown_request),
    ):
        for i in (1, 2):
            decorator(make(kind, "app", i))

    parent = Blueprint("parent", __name__, url_prefix="/parent")
    child = Blueprint("child", __name__, url_prefix="/child")
    other = Blueprint("other", __name__, url_prefix="/other")

    for bp, short in ((parent, "p"), (child, "c"), (other, "o")):
        for kind, decorator in (
            ("uvp", bp.url_value_preprocessor),
            ("before", bp.before_request),
            ("after", bp.after_request),
            ("teardown", bp.teardown_request),
        ):
            for i in (1, 2):
                decorator(make(kind, short, i))

    @child.route("/item")
    def child_item():
        trace.append("view")
        return "item"

    @other.route("/item")
    def other_item():
        return "other"

    parent.register_blueprint(child)
    app.register_blueprint(parent)
    app.register_blueprint(other)
    return app, trace


class TestRouting:
    def test_matched_rule_and_view(self, app):
        @app.route("/users/<int:uid>")
        def show_user(uid):
            return "ok"

        report = app.rehearse_request("GET", "/users/42")
        routing = report["routing"]

        assert routing["status"] == "matched"
        assert routing["outcome"] == "view"
        assert routing["endpoint"] == "show_user"
        assert routing["rule"] == "/users/<int:uid>"
        assert routing["arguments"] == {"uid": 42}
        assert report["view"]["will_run"] is True
        assert report["view"]["function"]["name"] == "show_user"
        assert report["view"]["scope_kind"] == "app"
        assert report["view"]["origin_blueprint"] is None

    def test_not_found_vs_method_not_allowed(self, app):
        @app.route("/only-get", methods=["GET"])
        def only_get():
            return "ok"

        missing = app.rehearse_request("GET", "/nope")["routing"]
        assert missing["status"] == "not_found"
        assert missing["outcome"] == "http_error"
        assert missing["error_code"] == 404
        assert missing["allowed_methods"] is None

        not_allowed = app.rehearse_request("POST", "/only-get")["routing"]
        assert not_allowed["status"] == "method_not_allowed"
        assert not_allowed["error_code"] == 405
        assert "GET" in not_allowed["allowed_methods"]
        assert "HEAD" in not_allowed["allowed_methods"]

    def test_trailing_slash_redirect(self, app):
        @app.route("/slash/")
        def slash():
            return "ok"

        report = app.rehearse_request("GET", "/slash")
        routing = report["routing"]

        assert routing["status"] == "redirect"
        assert routing["outcome"] == "redirect"
        assert routing["redirect"]["code"] == 308
        assert routing["redirect"]["location"].endswith("/slash/")
        assert routing["error_handlers_bypassed"] is True
        assert report["view"] is None

    def test_automatic_options(self, app):
        @app.route("/opts", methods=["POST"])
        def opts():
            return "ok"

        report = app.rehearse_request("OPTIONS", "/opts")
        routing = report["routing"]

        assert routing["outcome"] == "automatic_options"
        assert routing["status"] == "matched"
        assert "POST" in routing["allowed_methods"]
        assert "OPTIONS" in routing["allowed_methods"]
        assert report["view"]["will_run"] is False
        assert report["view"]["reason"] == "automatic_options"

    def test_explicit_options_view_runs(self, app):
        @app.route(
            "/explicit",
            methods=["GET", "OPTIONS"],
            provide_automatic_options=False,
        )
        def explicit():
            return "ok"

        report = app.rehearse_request("OPTIONS", "/explicit")
        assert report["routing"]["outcome"] == "view"
        assert report["view"]["will_run"] is True

    def test_query_string_and_headers_echo(self, app):
        @app.route("/")
        def index():
            return "ok"

        report = app.rehearse_request("GET", "/?a=1", headers=[("X-Test", "yes")])
        assert report["request"]["query_string"] == "a=1"
        assert report["request"]["headers"] == [["X-Test", "yes"]]

    def test_subdomain_matching(self):
        app = Flask(__name__, static_folder=None)
        app.config["SERVER_NAME"] = "example.com"
        app.subdomain_matching = True
        api = Blueprint("api", __name__, subdomain="api")

        @api.route("/v")
        def v():
            return "ok"

        app.register_blueprint(api)

        matched = app.rehearse_request("GET", "/v", subdomain="api")
        assert matched["routing"]["status"] == "matched"
        assert matched["routing"]["blueprints"] == ["api"]

        no_subdomain = app.rehearse_request("GET", "/v")
        assert no_subdomain["routing"]["status"] == "not_found"


class TestChainOrder:
    def test_orders_match_real_request(self):
        app, trace = make_nested_app()
        report = app.rehearse_request("GET", "/parent/child/item")

        assert names(report["url_value_preprocessors"]["will_run"]) == [
            "uvp_app_1",
            "uvp_app_2",
            "uvp_p_1",
            "uvp_p_2",
            "uvp_c_1",
            "uvp_c_2",
        ]
        assert names(report["before_request"]["will_run"]) == [
            "before_app_1",
            "before_app_2",
            "before_p_1",
            "before_p_2",
            "before_c_1",
            "before_c_2",
        ]
        # after_request reverses within each scope, peels inner to outer.
        assert names(report["after_request"]["will_run"]) == [
            "after_c_2",
            "after_c_1",
            "after_p_2",
            "after_p_1",
            "after_app_2",
            "after_app_1",
        ]
        assert names(report["teardown_request"]["will_run"]) == [
            "teardown_c_2",
            "teardown_c_1",
            "teardown_p_2",
            "teardown_p_1",
            "teardown_app_2",
            "teardown_app_1",
        ]

        # Registration indices stay in registration order even though
        # the chain itself is reversed.
        assert [
            item["scope_index"] for item in report["after_request"]["will_run"][:2]
        ] == [1, 0]

        # Compare against a real request through the test client.
        client = app.test_client()
        response = client.get("/parent/child/item")
        assert response.status_code == 200
        assert trace == [
            "uvp_app_1",
            "uvp_app_2",
            "uvp_p_1",
            "uvp_p_2",
            "uvp_c_1",
            "uvp_c_2",
            "before_app_1",
            "before_app_2",
            "before_p_1",
            "before_p_2",
            "before_c_1",
            "before_c_2",
            "view",
            "after_c_2",
            "after_c_1",
            "after_p_2",
            "after_p_1",
            "after_app_2",
            "after_app_1",
            "teardown_c_2",
            "teardown_c_1",
            "teardown_p_2",
            "teardown_p_1",
            "teardown_app_2",
            "teardown_app_1",
        ]

    def test_inactive_blueprint_listed_separately(self):
        app, _ = make_nested_app()
        report = app.rehearse_request("GET", "/parent/child/item")

        for section in (
            "url_value_preprocessors",
            "before_request",
            "after_request",
            "teardown_request",
        ):
            inactive = report[section]["will_not_run"]
            assert {item["scope"] for item in inactive} == {"other"}
            assert all(
                item["will_run"] is False
                and item["reason"] == "blueprint_scope_not_active"
                for item in inactive
            )
            assert names(report[section]["will_run"]).count("after_o_1") == 0

    def test_no_blueprint_scope_applies_only_app_items(self, app):
        bp = Blueprint("bp", __name__, url_prefix="/bp")

        @bp.before_request
        def bp_before():
            pass

        @app.route("/root")
        def root():
            return "ok"

        app.register_blueprint(bp)

        report = app.rehearse_request("GET", "/root")
        assert report["routing"]["blueprints"] == []
        assert names(report["before_request"]["will_run"]) == []
        inactive = report["before_request"]["will_not_run"]
        assert len(inactive) == 1
        assert inactive[0]["scope"] == "bp"

    def test_blueprint_app_wide_hook_origin_and_order(self, app):
        bp = Blueprint("bp", __name__)

        @app.before_request
        def app_before_one():
            pass

        @bp.before_app_request
        def bp_app_before():
            pass

        @bp.after_app_request
        def bp_app_after(response):
            return response

        @app.route("/")
        def index():
            return "ok"

        # Deferred blueprint callbacks are appended when registered,
        # between the two app-level decorators.
        app.register_blueprint(bp)

        @app.before_request
        def app_before_two():
            pass

        report = app.rehearse_request("GET", "/")
        before = report["before_request"]["will_run"]
        assert names(before) == [
            "app_before_one",
            "bp_app_before",
            "app_before_two",
        ]
        assert before[1]["scope"] is None
        assert before[1]["scope_kind"] == "app"
        assert before[1]["origin"] == "blueprint"
        assert before[1]["origin_blueprint"] == "bp"
        assert [item["scope_index"] for item in before] == [0, 1, 2]

        after_bp = report["after_request"]["will_run"]
        # after_request is reversed, app scope contains both funcs.
        assert names(after_bp)[-1] == "bp_app_after"
        assert after_bp[-1]["origin"] == "blueprint"

    def test_after_this_request_section_is_empty_before_view(self, app):
        @app.route("/")
        def index():
            return "ok"

        section = app.rehearse_request("GET", "/")["after_this_request"]
        assert section["will_run"] == []
        assert section["will_not_run"] == []
        assert section["note"]

    def test_app_hooks_apply_on_routing_failures(self, app):
        trace = []

        @app.before_request
        def before():
            trace.append("before")

        @app.after_request
        def after(response):
            trace.append("after")
            return response

        @app.teardown_request
        def teardown(exc):
            trace.append("teardown")

        @app.errorhandler(404)
        def handle_404(e):
            trace.append("error404")
            return "404", 404

        @app.route("/slash/")
        def slash():
            trace.append("view")
            return "ok"

        # A missing path still runs the application-wide hooks, and the
        # report lists only application scope callbacks.
        report = app.rehearse_request("GET", "/missing")
        assert names(report["before_request"]["will_run"]) == ["before"]
        assert names(report["after_request"]["will_run"]) == ["after"]
        assert names(report["teardown_request"]["will_run"]) == ["teardown"]
        assert report["before_request"]["will_not_run"] == []
        assert error_names(report["error_handlers"]["lookup_order"]) == ["handle_404"]

        trace.clear()
        response = app.test_client().get("/missing")
        assert response.status_code == 404
        assert trace == ["before", "error404", "after", "teardown"]

        # A slash redirect also runs the preprocessing hooks before the
        # RoutingException short-circuits the view.
        redirect_report = app.rehearse_request("GET", "/slash")
        assert redirect_report["routing"]["status"] == "redirect"
        assert names(redirect_report["before_request"]["will_run"]) == ["before"]
        assert names(redirect_report["after_request"]["will_run"]) == ["after"]

    def test_head_still_dispatches_to_view(self, app):
        @app.route("/")
        def index():
            return "ok"

        report = app.rehearse_request("HEAD", "/")
        assert report["routing"]["outcome"] == "view"
        assert report["view"]["will_run"] is True


class TestErrorHandlers:
    def test_blueprint_before_app_specific_to_generic(self, app):
        bp = Blueprint("bp", __name__, url_prefix="/bp")

        @app.errorhandler(Exception)
        def handle_exception(e):
            return "exception", 500

        @bp.errorhandler(ValueError)
        def handle_value_error(e):
            return "value", 500

        @bp.route("/boom")
        def boom():
            raise ValueError()

        app.register_blueprint(bp)

        report = app.rehearse_request("GET", "/bp/boom", error=ValueError)
        section = report["error_handlers"]
        assert error_names(section["lookup_order"]) == [
            "handle_value_error",
            "handle_exception",
        ]
        assert section["lookup_order"][0]["will_handle"] is True
        assert section["lookup_order"][0]["scope"] == "bp"
        assert section["lookup_order"][1]["will_handle"] is False
        assert section["lookup_order"][1]["reason"] == "preceded_by_earlier_match"
        assert section["lookup_order"][1]["scope"] is None

    def test_mro_specific_to_general_within_scope(self, app):
        class SpecialError(ValueError):
            pass

        @app.errorhandler(ValueError)
        def handle_value_error(e):
            return "value", 500

        @app.errorhandler(SpecialError)
        def handle_special(e):
            return "special", 500

        @app.route("/boom")
        def boom():
            return "ok"

        section = app.rehearse_request("GET", "/boom", error=SpecialError)[
            "error_handlers"
        ]
        assert error_names(section["lookup_order"]) == [
            "handle_special",
            "handle_value_error",
        ]
        assert section["lookup_order"][0]["will_handle"] is True
        assert section["lookup_order"][1]["reason"] == "preceded_by_earlier_match"

    def test_http_code_specific_bucket_first(self, app):
        @app.errorhandler(500)
        def handle_500(e):
            return "500", 500

        @app.errorhandler(Exception)
        def handle_exception(e):
            return "exception", 500

        @app.route("/boom")
        def boom():
            return "ok"

        section = app.rehearse_request("GET", "/boom")["error_handlers"]
        assert error_names(section["lookup_order"]) == [
            "handle_500",
            "handle_exception",
        ]
        assert section["lookup_order"][0]["bucket"] == 500
        assert section["lookup_order"][1]["bucket"] is None

    def test_http_code_and_class_share_registration_key(self, app):
        # 500 and InternalServerError register in the same bucket/key,
        # the later registration replaces the earlier one.
        @app.errorhandler(500)
        def handle_500(e):
            return "500", 500

        @app.errorhandler(500)
        def handle_500_again(e):
            return "500 again", 500

        @app.route("/boom")
        def boom():
            return "ok"

        section = app.rehearse_request("GET", "/boom")["error_handlers"]
        assert error_names(section["lookup_order"]) == ["handle_500_again"]
        assert [item["handler"]["name"] for item in section["overridden"]] == [
            "handle_500"
        ]

    def test_overridden_handler_is_listed(self, app):
        @app.errorhandler(404)
        def old_404(e):
            return "old", 404

        @app.errorhandler(404)
        def new_404(e):
            return "new", 404

        @app.route("/missing-route-marker")
        def marker():
            return "ok"

        report = app.rehearse_request("GET", "/nope")
        section = report["error_handlers"]

        assert error_names(section["lookup_order"]) == ["new_404"]
        overridden = section["overridden"]
        assert len(overridden) == 1
        assert overridden[0]["handler"]["name"] == "old_404"
        assert overridden[0]["reason"] == "overwritten"
        assert overridden[0]["replaced_by"]["handler"]["name"] == "new_404"

    def test_app_errorhandler_from_blueprint_origin(self, app):
        bp = Blueprint("bp", __name__)

        @bp.app_errorhandler(403)
        def handle_403(e):
            return "403", 403

        @app.route("/")
        def index():
            return "ok"

        app.register_blueprint(bp)

        report = app.rehearse_request("GET", "/", error=403)
        entry = report["error_handlers"]["lookup_order"][0]
        assert entry["handler"]["name"] == "handle_403"
        assert entry["scope"] is None
        assert entry["origin"] == "blueprint"
        assert entry["origin_blueprint"] == "bp"

    def test_blueprint_handler_inactive_for_app_route(self, app):
        bp = Blueprint("bp", __name__, url_prefix="/bp")

        @bp.errorhandler(404)
        def bp_404(e):
            return "bp", 404

        @app.route("/")
        def index():
            return "ok"

        app.register_blueprint(bp)

        section = app.rehearse_request("GET", "/")["error_handlers"]
        assert section["lookup_order"] == []
        assert section["will_not_run"][0]["handler"]["name"] == "bp_404"
        assert section["will_not_run"][0]["scope"] == "bp"

    def test_routing_failure_considers_routing_error(self, app):
        @app.errorhandler(404)
        def handle_404(e):
            return "404", 404

        @app.errorhandler(405)
        def handle_405(e):
            return "405", 405

        @app.route("/x", methods=["POST"])
        def x():
            return "ok"

        not_found = app.rehearse_request("GET", "/missing")
        assert error_names(not_found["error_handlers"]["lookup_order"]) == [
            "handle_404"
        ]

        not_allowed = app.rehearse_request("PUT", "/x")
        assert error_names(not_allowed["error_handlers"]["lookup_order"]) == [
            "handle_405"
        ]

    def test_nested_blueprint_scope_order(self, app):
        parent = Blueprint("parent", __name__, url_prefix="/parent")
        child = Blueprint("child", __name__, url_prefix="/child")

        @app.errorhandler(Exception)
        def app_error(e):
            return "app", 500

        @parent.errorhandler(Exception)
        def parent_error(e):
            return "parent", 500

        @child.errorhandler(Exception)
        def child_error(e):
            return "child", 500

        @child.route("/boom")
        def boom():
            return "ok"

        parent.register_blueprint(child)
        app.register_blueprint(parent)

        report = app.rehearse_request("GET", "/parent/child/boom")
        assert report["routing"]["blueprints"] == ["parent.child", "parent"]

        order = error_names(report["error_handlers"]["lookup_order"])
        assert order == ["child_error", "parent_error", "app_error"]
        assert [
            entry["scope"] for entry in report["error_handlers"]["lookup_order"]
        ] == ["parent.child", "parent", None]

    def test_error_handler_will_not_run_for_unmatched_blueprint(self, app):
        bp = Blueprint("bp", __name__, url_prefix="/bp")

        @bp.errorhandler(403)
        def bp_403(e):
            return "bp", 403

        @app.route("/")
        def index():
            return "ok"

        app.register_blueprint(bp)

        section = app.rehearse_request("GET", "/", error=403)["error_handlers"]
        assert section["lookup_order"] == []
        assert len(section["will_not_run"]) == 1
        assert section["will_not_run"][0]["scope"] == "bp"
        assert section["will_not_run"][0]["bucket"] == 403

    def test_invalid_error_code(self, app):
        with pytest.raises(ValueError):
            app.rehearse_request("GET", "/", error=599)


class TestNoSideEffects:
    def test_nothing_executes_or_changes(self, app):
        side_effects = []
        fired = []

        @app.url_value_preprocessor
        def uvp(endpoint, args):
            side_effects.append("uvp")

        @app.before_request
        def before():
            side_effects.append("before")

        @app.after_request
        def after(response):
            side_effects.append("after")
            return response

        @app.teardown_request
        def teardown(exc):
            side_effects.append("teardown")

        @app.route("/")
        def index():
            side_effects.append("view")
            return "ok"

        connected = []

        for signal in (
            request_started,
            request_finished,
            request_tearing_down,
            appcontext_pushed,
            got_request_exception,
        ):

            def receiver(*a, _name=signal.name, **kw):
                fired.append(_name)

            signal.connect(receiver, weak=False)
            connected.append((signal, receiver))

        try:
            before_first = app._got_first_request
            report = app.rehearse_request("GET", "/")
            assert report["dry_run"] is True

            assert side_effects == []
            assert fired == []
            assert app._got_first_request is before_first

            # A real request afterwards still works and records everything.
            response = app.test_client().get("/")
            assert response.status_code == 200
            assert side_effects == [
                "uvp",
                "before",
                "view",
                "after",
                "teardown",
            ]
            assert {
                "request-started",
                "request-finished",
                "request-tearing-down",
                "appcontext-pushed",
            } <= set(fired)
            assert "got-request-exception" not in fired
        finally:
            for signal, receiver in connected:
                signal.disconnect(receiver)

    def test_rehearsal_does_not_require_pushed_context(self, app):
        @app.route("/")
        def index():
            return "ok"

        # Must work with no app or request context pushed at all.
        report = app.rehearse_request("GET", "/")
        assert report["routing"]["status"] == "matched"

    def test_report_is_stable_json(self, app):
        @app.route("/<name>")
        def hello(name):
            return "ok"

        first = app.rehearse_request("GET", "/world")
        second = app.rehearse_request("GET", "/world")

        assert first.to_dict() == second.to_dict()
        parsed = json.loads(first.to_json())
        assert parsed["schema_version"] >= 1
        assert parsed["routing"]["arguments"] == {"name": "world"}


class TestCli:
    def test_json_output(self):
        app = Flask(__name__, static_folder=None)

        @app.route("/hello/<name>")
        def hello(name):
            return "ok"

        cli = FlaskGroup(create_app=lambda: app)
        result = CliRunner().invoke(cli, ["rehearse", "/hello/world", "--json"])
        assert result.exit_code == 0, result.output
        report = json.loads(result.output)
        assert report["routing"]["endpoint"] == "hello"
        assert report["routing"]["arguments"] == {"name": "world"}
        assert report["view"]["function"]["name"] == "hello"

    def test_text_output(self):
        app = Flask(__name__, static_folder=None)

        @app.route("/")
        def index():
            return "ok"

        cli = FlaskGroup(create_app=lambda: app)
        result = CliRunner().invoke(cli, ["rehearse", "/"])
        assert result.exit_code == 0, result.output
        assert "Request: GET /" in result.output
        assert "Routing:" in result.output

    def test_method_and_method_not_allowed(self):
        app = Flask(__name__, static_folder=None)

        @app.route("/only", methods=["POST"])
        def only():
            return "ok"

        cli = FlaskGroup(create_app=lambda: app)
        result = CliRunner().invoke(cli, ["rehearse", "/only", "-m", "PUT", "--json"])
        assert result.exit_code == 0, result.output
        report = json.loads(result.output)
        assert report["routing"]["status"] == "method_not_allowed"
        assert "POST" in report["routing"]["allowed_methods"]
