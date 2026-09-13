from __future__ import annotations

import json
import threading

import pytest
from werkzeug.exceptions import NotAcceptable

from flask import Blueprint
from flask import jsonify
from flask import Representation
from flask import representations
from flask import request
from flask import Response
from flask import stream_with_context
from flask.negotiation import RepresentationMap
from flask.negotiation import REPRESENTATIONS_ATTR
from flask.views import MethodView

pytest.importorskip("asgiref")


def json_rep(resource):
    return jsonify(resource)


def html_rep(resource):
    return f"<h1>{resource['name']}</h1>"


@pytest.fixture
def negotiation_app(app):
    @app.route("/resource")
    @representations(
        Representation("application/json", json_rep),
        Representation("text/html", html_rep),
    )
    def resource():
        return {"name": "thing"}

    @app.route("/plain")
    def plain():
        return {"name": "plain"}

    return app


# ---------------------------------------------------------------------------
# Endpoints without declarations stay exactly as before.
# ---------------------------------------------------------------------------


def test_undeclared_str_is_html(app, client):
    @app.route("/s")
    def s():
        return "hello"

    response = client.get("/s")
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert response.data == b"hello"
    assert "Vary" not in response.headers


def test_undeclared_dict_is_json(app, client):
    @app.route("/d")
    def d():
        return {"a": 1}

    response = client.get("/d", headers={"Accept": "text/html"})
    assert response.mimetype == "application/json"
    assert response.get_json() == {"a": 1}
    assert "Vary" not in response.headers


def test_undeclared_response_not_rewritten(app, client):
    @app.route("/r")
    def r():
        response = Response("x", mimetype="text/x-custom")
        response.headers["X-Custom"] = "kept"
        return response

    response = client.get("/r", headers={"Accept": "application/json"})
    assert response.mimetype == "text/x-custom"
    assert response.headers["X-Custom"] == "kept"
    assert "Vary" not in response.headers


# ---------------------------------------------------------------------------
# Basic selection.
# ---------------------------------------------------------------------------


def test_select_explicit_accept(negotiation_app, client):
    response = client.get("/resource", headers={"Accept": "application/json"})
    assert response.status_code == 200
    assert response.mimetype == "application/json"
    assert response.get_json() == {"name": "thing"}
    assert response.headers["Vary"] == "Accept"

    response = client.get("/resource", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert response.data == b"<h1>thing</h1>"
    assert response.headers["Vary"] == "Accept"


def test_select_quality_factors(negotiation_app, client):
    response = client.get(
        "/resource",
        headers={"Accept": "application/json;q=0.1, text/html;q=0.9"},
    )
    assert response.mimetype == "text/html"

    response = client.get(
        "/resource",
        headers={"Accept": "application/json;q=0.9, text/html;q=0.1"},
    )
    assert response.mimetype == "application/json"


def test_quality_zero_not_selected(negotiation_app, client):
    response = client.get(
        "/resource",
        headers={"Accept": "application/json;q=0, text/html"},
    )
    assert response.mimetype == "text/html"

    response = client.get("/resource", headers={"Accept": "application/json;q=0"})
    assert response.status_code == 406


def test_wildcard_uses_declaration_order(negotiation_app, client):
    # application/json is declared first.
    response = client.get("/resource", headers={"Accept": "*/*"})
    assert response.mimetype == "application/json"

    # A type wildcard still matches the only matching declaration.
    response = client.get("/resource", headers={"Accept": "text/*"})
    assert response.mimetype == "text/html"


def test_equal_quality_is_stable_by_declaration_order(negotiation_app, client):
    # Client order must not change which declaration is picked.
    response = client.get(
        "/resource", headers={"Accept": "text/html, application/json"}
    )
    assert response.mimetype == "application/json"


def test_missing_accept_uses_default(negotiation_app, client):
    response = client.get("/resource")
    assert response.mimetype == "application/json"
    assert response.get_json() == {"name": "thing"}
    # Negotiated responses always declare variability for cache safety.
    assert response.headers["Vary"] == "Accept"


def test_empty_accept_uses_default(negotiation_app, client):
    response = client.get("/resource", headers={"Accept": ""})
    assert response.mimetype == "application/json"


# ---------------------------------------------------------------------------
# 406 Not Acceptable.
# ---------------------------------------------------------------------------


def test_not_acceptable_lists_representations(negotiation_app, client):
    response = client.get("/resource", headers={"Accept": "application/xml"})
    assert response.status_code == 406
    body = response.get_data(as_text=True)
    assert "application/json" in body
    assert "text/html" in body
    assert response.headers["Vary"] == "Accept"


def test_not_acceptable_does_not_call_generators(app, client):
    calls = []

    def gen1(resource):
        calls.append("json")
        return jsonify(resource)

    def gen2(resource):
        calls.append("html")
        return "html"

    @app.route("/v")
    @representations(
        Representation("application/json", gen1),
        Representation("text/html", gen2),
    )
    def v():
        calls.append("view")
        return {"name": "v"}

    response = client.get("/v", headers={"Accept": "application/xml"})
    assert response.status_code == 406
    assert calls == ["view"]


def test_custom_406_error_handler(app, client):
    @app.route("/v")
    @representations(Representation("application/json", json_rep))
    def v():
        return {"name": "v"}

    @app.errorhandler(406)
    def handle_406(error):
        assert isinstance(error, NotAcceptable)
        return {"custom": True}, 406

    response = client.get("/v", headers={"Accept": "application/xml"})
    assert response.status_code == 406
    assert response.get_json() == {"custom": True}


# ---------------------------------------------------------------------------
# Explicit responses, streaming and files bypass negotiation.
# ---------------------------------------------------------------------------


def test_explicit_response_bypasses_selection(app, client):
    calls = []

    @app.route("/e")
    @representations(
        Representation("application/json", lambda rv: (calls.append(1), rv)[1]),
    )
    def e():
        return Response("explicit", mimetype="text/x-custom")

    response = client.get("/e", headers={"Accept": "application/json"})
    assert response.mimetype == "text/x-custom"
    assert response.data == b"explicit"
    assert "Vary" not in response.headers
    assert calls == []


def test_streaming_response_not_renegotiated(app, client):
    @app.route("/s")
    @representations(Representation("application/json", json_rep))
    def s():
        def generate():
            yield "a"
            yield "b"

        return stream_with_context(generate())

    response = client.get("/s", headers={"Accept": "application/json"})
    assert response.status_code == 200
    assert response.data == b"ab"
    assert response.mimetype == "text/html"
    assert "Vary" not in response.headers


def test_file_response_not_rewritten(app, client):
    from pathlib import Path

    static_json = Path(__file__).parent / "static" / "config.json"

    @app.route("/f")
    @representations(Representation("application/json", json_rep))
    def f():
        from flask import send_file

        return send_file(static_json)

    with client.get("/f", headers={"Accept": "application/json"}) as response:
        assert response.status_code == 200
        assert json.loads(response.get_data(as_text=True))["TEST_KEY"] == "foo"
        assert "Vary" not in response.headers


# ---------------------------------------------------------------------------
# Tuple returns carry status and headers into the generated response.
# ---------------------------------------------------------------------------


def test_tuple_status_and_headers_preserved(app, client):
    @app.route("/t")
    @representations(
        Representation("application/json", json_rep),
        Representation("text/html", html_rep),
    )
    def t():
        return {"name": "t"}, 201, {"X-Custom": "yes"}

    response = client.get("/t", headers={"Accept": "text/html"})
    assert response.status_code == 201
    assert response.mimetype == "text/html"
    assert response.headers["X-Custom"] == "yes"
    assert response.headers["Vary"] == "Accept"


def test_two_tuple_body_and_status(app, client):
    @app.route("/t")
    @representations(Representation("application/json", json_rep))
    def t():
        return {"name": "t"}, 202

    response = client.get("/t", headers={"Accept": "application/json"})
    assert response.status_code == 202
    assert response.get_json() == {"name": "t"}


# ---------------------------------------------------------------------------
# Error handlers respect content type.
# ---------------------------------------------------------------------------


def test_error_handler_dict_is_negotiated(app, client):
    def error_html(resource):
        return f"<p>{resource['error']}</p>"

    @app.route("/missing")
    @representations(
        Representation("application/json", json_rep),
        Representation("text/html", error_html),
    )
    def missing():
        from flask import abort

        abort(404)

    @app.errorhandler(404)
    def handle_404(error):
        return {"error": "not found"}, 404

    response = client.get("/missing", headers={"Accept": "application/json"})
    assert response.status_code == 404
    assert response.mimetype == "application/json"
    assert response.get_json() == {"error": "not found"}
    assert response.headers["Vary"] == "Accept"

    response = client.get("/missing", headers={"Accept": "text/html"})
    assert response.status_code == 404
    assert response.mimetype == "text/html"


def test_error_handler_explicit_response_preserved(app, client):
    @app.route("/missing")
    @representations(
        Representation("application/json", json_rep),
        Representation("text/html", html_rep),
    )
    def missing():
        from flask import abort

        abort(404)

    @app.errorhandler(404)
    def handle_404(error):
        return Response("custom error", mimetype="text/x-error", status=404)

    response = client.get("/missing", headers={"Accept": "application/json"})
    assert response.status_code == 404
    assert response.mimetype == "text/x-error"
    assert response.data == b"custom error"
    assert "Vary" not in response.headers


def test_default_404_with_app_representation(app, client):
    app.add_representation("application/json", json_rep)

    @app.errorhandler(404)
    def handle_404(error):
        return {"error": "not found"}, 404

    response = client.get("/no-such-url", headers={"Accept": "application/json"})
    assert response.status_code == 404
    assert response.mimetype == "application/json"


def test_generator_exception_uses_error_handler(app, client):
    def broken(resource):
        raise RuntimeError("broken generator")

    @app.route("/v")
    @representations(Representation("application/json", broken))
    def v():
        return {"name": "v"}

    @app.errorhandler(RuntimeError)
    def handle_runtime(error):
        return {"error": str(error)}, 500

    response = client.get("/v", headers={"Accept": "application/json"})
    assert response.status_code == 500
    assert response.get_json() == {"error": "broken generator"}


# ---------------------------------------------------------------------------
# Application and blueprint defaults.
# ---------------------------------------------------------------------------


def test_app_default_representation(app, client):
    app.add_representation("application/json", json_rep)

    @app.route("/a")
    def a():
        return {"name": "a"}

    response = client.get("/a", headers={"Accept": "application/json"})
    assert response.mimetype == "application/json"
    assert response.get_json() == {"name": "a"}
    assert response.headers["Vary"] == "Accept"

    assert client.get("/a", headers={"Accept": "text/plain"}).status_code == 406


def test_app_representation_decorator(app, client):
    @app.representation("application/json")
    def as_json(resource):
        return jsonify(resource)

    @app.route("/a")
    def a():
        return {"name": "a"}

    response = client.get("/a", headers={"Accept": "application/json"})
    assert response.get_json() == {"name": "a"}


def test_blueprint_default_representation(app, client):
    bp = Blueprint("bp", __name__)
    bp.add_representation("application/xml", lambda o: f"<x>{o['name']}</x>")

    @bp.get("/b")
    def b():
        return {"name": "b"}

    app.register_blueprint(bp, url_prefix="/bp")

    response = client.get("/bp/b", headers={"Accept": "application/xml"})
    assert response.status_code == 200
    assert response.mimetype == "application/xml"
    assert response.data == b"<x>b</x>"
    assert response.headers["Vary"] == "Accept"


def test_endpoint_overrides_blueprint_default(app, client):
    bp = Blueprint("bp", __name__)
    bp.add_representation("application/xml", lambda o: "<x/>")

    @bp.get("/b")
    @bp.representations(Representation("application/json", json_rep))
    def b():
        return {"name": "b"}

    app.register_blueprint(bp, url_prefix="/bp")

    response = client.get("/bp/b", headers={"Accept": "application/json"})
    assert response.mimetype == "application/json"
    assert response.get_json() == {"name": "b"}

    # The blueprint's set is entirely replaced by the endpoint's set.
    response = client.get("/bp/b", headers={"Accept": "application/xml"})
    assert response.status_code == 406


def test_app_default_only_without_more_specific_declaration(app, client):
    app.add_representation("application/json", json_rep)

    bp = Blueprint("bp", __name__)
    bp.add_representation("application/xml", lambda o: "<x/>")

    @bp.get("/b")
    def b():
        return {"name": "b"}

    @app.get("/a")
    def a():
        return {"name": "a"}

    app.register_blueprint(bp, url_prefix="/bp")

    # App default applies to non-blueprint endpoints.
    response = client.get("/a", headers={"Accept": "application/json"})
    assert response.mimetype == "application/json"

    # The blueprint's declaration replaces the app default.
    response = client.get("/bp/b", headers={"Accept": "application/json"})
    assert response.status_code == 406

    response = client.get("/bp/b", headers={"Accept": "application/xml"})
    assert response.mimetype == "application/xml"


def test_nested_blueprint_most_specific_wins(app, client):
    outer = Blueprint("outer", __name__)
    outer.add_representation("application/json", json_rep)
    inner = Blueprint("inner", __name__)
    inner.add_representation("application/xml", lambda o: "<x/>")

    @inner.get("/x")
    def x():
        return {"name": "x"}

    # A nested blueprint without declarations falls back to the outer
    # blueprint's defaults.
    plain = Blueprint("plain", __name__)

    @plain.get("/z")
    def z():
        return {"name": "z"}

    outer.register_blueprint(inner, url_prefix="/inner")
    outer.register_blueprint(plain, url_prefix="/plain")
    app.register_blueprint(outer, url_prefix="/outer")

    # The inner blueprint's set entirely replaces the outer one for its
    # endpoints.
    response = client.get("/outer/inner/x", headers={"Accept": "application/xml"})
    assert response.mimetype == "application/xml"

    response = client.get("/outer/inner/x", headers={"Accept": "application/json"})
    assert response.status_code == 406

    response = client.get("/outer/plain/z", headers={"Accept": "application/json"})
    assert response.status_code == 200
    assert response.mimetype == "application/json"


def test_method_without_declaration_falls_back_to_blueprint(app, client):
    bp = Blueprint("bp", __name__)
    bp.add_representation("application/json", json_rep)

    class View(MethodView):
        def get(self):
            return {"name": "get"}

        def post(self):
            return {"name": "post"}

    bp.add_url_rule("/v", view_func=View.as_view("v"))
    app.register_blueprint(bp)

    response = client.post("/v", headers={"Accept": "application/json"})
    assert response.mimetype == "application/json"
    assert response.get_json() == {"name": "post"}


# ---------------------------------------------------------------------------
# Class-based views.
# ---------------------------------------------------------------------------


def test_methodview_per_method_representations(app, client):
    class View(MethodView):
        @representations(
            Representation("application/json", json_rep),
            Representation("text/html", html_rep),
        )
        def get(self):
            return {"name": "get"}

        @representations(Representation("application/json", json_rep))
        def post(self):
            return {"created": True}

    app.add_url_rule("/v", view_func=View.as_view("v"))

    response = client.get("/v", headers={"Accept": "text/html"})
    assert response.mimetype == "text/html"
    assert response.data == b"<h1>get</h1>"

    response = client.get("/v", headers={"Accept": "application/json"})
    assert response.get_json() == {"name": "get"}

    response = client.post("/v", headers={"Accept": "application/json"})
    assert response.status_code == 200
    assert response.get_json() == {"created": True}

    # POST did not declare text/html.
    response = client.post("/v", headers={"Accept": "text/html"})
    assert response.status_code == 406


def test_methodview_head_uses_get_representations(app, client):
    class View(MethodView):
        @representations(
            Representation("application/json", json_rep),
            Representation("text/html", html_rep),
        )
        def get(self):
            return {"name": "get"}

    app.add_url_rule("/v", view_func=View.as_view("v"))

    response = client.open("/v", method="HEAD", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert response.mimetype == "text/html"


def test_generic_view_dispatch_request_declaration(app, client):
    from flask.views import View

    class CustomView(View):
        @representations(Representation("application/json", json_rep))
        def dispatch_request(self):
            return {"name": "custom"}

    app.add_url_rule("/c", view_func=CustomView.as_view("c"))
    response = client.get("/c", headers={"Accept": "application/json"})
    assert response.status_code == 200
    assert response.get_json() == {"name": "custom"}


# ---------------------------------------------------------------------------
# Invalid declarations fail before serving.
# ---------------------------------------------------------------------------


def test_duplicate_content_type_error():
    with pytest.raises(ValueError, match="already declared"):
        representations(
            Representation("application/json", json_rep),
            Representation("application/json", json_rep),
        )

    with pytest.raises(ValueError, match="already declared"):
        representations(
            Representation("application/json; charset=utf-8", json_rep),
            Representation("application/json", json_rep),
        )


def test_missing_generator_error():
    with pytest.raises(ValueError, match="callable"):
        Representation("application/json", None)


@pytest.mark.parametrize("content_type", ["not-a-type", "json", "text/", "/json"])
def test_unparseable_content_type_error(content_type):
    with pytest.raises(ValueError, match="Invalid representation"):
        Representation(content_type, json_rep)


def test_wildcard_declaration_error():
    with pytest.raises(ValueError, match="Wildcards? .*not"):
        Representation("text/*", json_rep)
    with pytest.raises(ValueError, match="Wildcards? .*not"):
        Representation("*/*", json_rep)


def test_bad_parameter_error():
    with pytest.raises(ValueError, match="parameter"):
        Representation("text/html; charset", json_rep)


def test_empty_declaration_error():
    with pytest.raises(ValueError, match="At least one"):
        representations()


def test_tuple_shorthand_requires_two_items():
    with pytest.raises(ValueError):
        representations(("application/json",))


def test_add_representation_after_first_request(app, client):
    client.get("/")  # marks setup finished

    with pytest.raises(AssertionError):
        app.add_representation("application/json", json_rep)


# ---------------------------------------------------------------------------
# No shared mutable state between requests.
# ---------------------------------------------------------------------------


def test_concurrent_requests_are_isolated(app, client):
    bp = Blueprint("bp", __name__)
    bp.add_representation("application/json", json_rep)
    bp.add_representation("application/xml", lambda o: f"<x>{o['name']}</x>")

    @bp.get("/v")
    def v():
        return {"name": "v"}

    app.register_blueprint(bp)

    results = []

    def worker(accept):
        local_client = app.test_client()
        for _ in range(50):
            response = local_client.get("/v", headers={"Accept": accept})
            results.append((accept, response.mimetype, response.status_code))

    threads = [
        threading.Thread(target=worker, args=("application/json",)),
        threading.Thread(target=worker, args=("application/xml",)),
        threading.Thread(
            target=worker, args=("application/json;q=0, application/xml",)
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 150
    assert all(
        (
            accept == "application/json"
            and mimetype == "application/json"
            and status == 200
        )
        or (
            accept == "application/xml"
            and mimetype == "application/xml"
            and status == 200
        )
        or (
            accept == "application/json;q=0, application/xml"
            and mimetype == "application/xml"
            and status == 200
        )
        for accept, mimetype, status in results
    )


# ---------------------------------------------------------------------------
# RepresentationMap selection unit tests.
# ---------------------------------------------------------------------------


def test_representation_map_select():
    mapping = RepresentationMap(
        [
            Representation("application/json", json_rep),
            Representation("text/html", html_rep),
        ]
    )
    from werkzeug.datastructures import MIMEAccept

    assert mapping.select(MIMEAccept([])).content_type == "application/json"
    selected = mapping.select(MIMEAccept([("application/json", 1)]))
    assert selected.content_type == "application/json"
    assert mapping.select(MIMEAccept([("application/xml", 1)])) is None
    zero = mapping.select(MIMEAccept([("application/json", 0)]))
    assert zero is None
    wild = mapping.select(MIMEAccept([("*/*", 1)]))
    assert wild.content_type == "application/json"


def test_declaration_marker_attribute():
    @representations(Representation("application/json", json_rep))
    def view():
        pass

    assert isinstance(getattr(view, REPRESENTATIONS_ATTR), RepresentationMap)


# ---------------------------------------------------------------------------
# Declared content type parameters.
# ---------------------------------------------------------------------------


def test_declared_content_type_parameters(app, client):
    @app.route("/v")
    @representations(
        Representation(
            "application/hal+json; charset=utf-8",
            lambda o: json.dumps(o),
        )
    )
    def v():
        return {"name": "v"}

    response = client.get("/v", headers={"Accept": "application/hal+json"})
    assert response.status_code == 200
    assert response.content_type == "application/hal+json; charset=utf-8"
    assert response.get_json() == {"name": "v"}


# ---------------------------------------------------------------------------
# Async views and generators.
# ---------------------------------------------------------------------------


async def async_json(resource):
    return jsonify(resource)


def test_before_request_short_circuit_is_negotiated(app, client):
    @app.route("/v")
    @representations(
        Representation("application/json", json_rep),
        Representation("text/html", html_rep),
    )
    def v():
        return {"name": "view"}

    @app.before_request
    def short_circuit():
        if request.path == "/v":
            return {"name": "before"}

    response = client.get("/v", headers={"Accept": "application/json"})
    assert response.get_json() == {"name": "before"}
    assert response.mimetype == "application/json"
    assert response.headers["Vary"] == "Accept"


def test_existing_vary_header_is_extended(app, client):
    @app.route("/v")
    @representations(Representation("application/json", json_rep))
    def v():
        return {"name": "v"}, {"Vary": "Cookie"}

    response = client.get("/v", headers={"Accept": "application/json"})
    vary = {part.strip() for part in response.headers["Vary"].split(",")}
    assert vary == {"Accept", "Cookie"}


def test_blueprint_representation_decorator(app, client):
    bp = Blueprint("bp", __name__)

    @bp.representation("application/json")
    def as_json(resource):
        return jsonify(resource)

    @bp.get("/b")
    def b():
        return {"name": "b"}

    app.register_blueprint(bp)

    response = client.get("/b", headers={"Accept": "application/json"})
    assert response.get_json() == {"name": "b"}
    assert response.headers["Vary"] == "Accept"


def test_async_view_and_generator(app, client):
    @app.route("/v")
    @representations(
        Representation("application/json", async_json),
        Representation("text/html", html_rep),
    )
    async def v():
        return {"name": "async"}

    response = client.get("/v", headers={"Accept": "application/json"})
    assert response.status_code == 200
    assert response.get_json() == {"name": "async"}
    assert response.headers["Vary"] == "Accept"

    response = client.get("/v", headers={"Accept": "text/html"})
    assert response.mimetype == "text/html"


async def async_raise(resource):
    raise RuntimeError("async broken")


def test_async_generator_exception(app, client):
    @app.route("/v")
    @representations(Representation("application/json", async_raise))
    async def v():
        return {"name": "v"}

    @app.errorhandler(RuntimeError)
    def handle_runtime(error):
        return {"error": str(error)}, 500

    response = client.get("/v", headers={"Accept": "application/json"})
    assert response.status_code == 500
    assert response.get_json() == {"error": "async broken"}
