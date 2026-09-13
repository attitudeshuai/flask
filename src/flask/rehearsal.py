"""Request handling chain rehearsal.

The :func:`rehearse_request` function reports everything Flask would do
for a request -- the matched route, URL value preprocessors, ``before
_request`` functions, the view, ``after_request`` functions,
``teardown_request`` functions, and error handler lookup -- without
actually executing any of it. No view, hook, signal, or other callback
is invoked, and no application state such as ``_got_first_request`` is
changed.

The returned :class:`RequestRehearsal` exposes a stable, machine
readable report through :meth:`RequestRehearsal.to_dict` and
:meth:`RequestRehearsal.to_json`, as well as a human readable rendering
through :meth:`RequestRehearsal.to_text`.

The iteration rules are deliberately copied from
:meth:`flask.Flask.preprocess_request`, :meth:`flask.Flask.process_response`,
:meth:`flask.Flask.do_teardown_request` and
:meth:`flask.sansio.app.App._find_error_handler`, so the reported order
matches the order in which a real request executes the callbacks.

.. versionadded:: 3.2
"""

from __future__ import annotations

import collections.abc as cabc
import copy
import inspect
import json
import os
import typing as t

from werkzeug.exceptions import default_exceptions
from werkzeug.exceptions import HTTPException
from werkzeug.exceptions import MethodNotAllowed
from werkzeug.exceptions import NotFound
from werkzeug.routing import RequestRedirect

if t.TYPE_CHECKING:  # pragma: no cover
    from .app import Flask

#: Version of the machine readable report. It is bumped when a released
#: version changes the meaning of a field or removes a field. Adding
#: fields or possible values does not bump it.
SCHEMA_VERSION = 1

#: Maps the public callback kind to the attribute on the application
#: holding the merged ``{scope: [functions]}`` registries.
_HOOK_ATTRS: dict[str, str] = {
    "url_value_preprocessors": "url_value_preprocessors",
    "before_request": "before_request_funcs",
    "after_request": "after_request_funcs",
    "teardown_request": "teardown_request_funcs",
}

#: Stable strings explaining why a registered callback does not run.
_REASON_BLUEPRINT_INACTIVE = "blueprint_scope_not_active"
_REASON_PRECEDENCE = "preceded_by_earlier_match"
_REASON_NOT_CONSIDERED = "not_matched_for_considered_exception"
_REASON_AUTOMATIC_OPTIONS = "automatic_options"
_REASON_OVERWRITTEN = "overwritten"


class RequestRehearsal(cabc.Mapping):
    """The result of :meth:`flask.Flask.rehearse_request`.

    Behaves as a read-only mapping of the stable, machine readable
    report. Use :meth:`to_dict` for a JSON native copy,
    :meth:`to_json` for a serialized string, or :meth:`to_text` for a
    human readable rendering used by the ``flask rehearse`` CLI
    command.

    .. versionadded:: 3.2
    """

    def __init__(self, report: dict[str, t.Any]) -> None:
        self._report = report

    def to_dict(self) -> dict[str, t.Any]:
        """Return a deep copy of the machine readable report."""
        return copy.deepcopy(self._report)

    def to_json(self, **kwargs: t.Any) -> str:
        """Serialize the report to JSON.

        Extra keyword arguments are forwarded to :func:`json.dumps`.
        """
        kwargs.setdefault("indent", 2)
        return json.dumps(self._report, default=str, **kwargs)

    def to_text(self) -> str:
        """Render the report as human readable text."""
        return _render_text(self._report)

    def __getitem__(self, key: str) -> t.Any:
        return self._report[key]

    def __iter__(self) -> cabc.Iterator[str]:
        return iter(self._report)

    def __len__(self) -> int:
        return len(self._report)

    def __repr__(self) -> str:
        routing = self._report["routing"]
        return (
            "<RequestRehearsal"
            f" {self._report['request']['method']}"
            f" {self._report['request']['path']!r}"
            f" {routing['status']}>"
        )


def rehearse_request(
    app: Flask,
    *,
    method: str = "GET",
    path: str = "/",
    headers: cabc.Mapping[str, str] | cabc.Iterable[tuple[str, str]] | None = None,
    host: str | None = None,
    subdomain: str | None = None,
    base_url: str | None = None,
    url_scheme: str | None = None,
    error: type[Exception] | Exception | int | None = None,
) -> RequestRehearsal:
    """Rehearse the complete request handling chain without executing it.

    Match ``method`` and ``path`` (with ``host``/``subdomain`` and
    optional ``headers``) the same way a real incoming request would be
    matched, then report every callback that would run and its
    registration source. The view, hooks, signals, sessions and error
    handlers are never called.

    :param method: HTTP method, defaults to ``"GET"``.
    :param path: Request path, possibly including a query string.
    :param headers: Optional request headers as a mapping or an
        iterable of ``(name, value)`` pairs.
    :param host: Explicit ``Host`` header value. Mutually exclusive with
        ``subdomain`` when constructing the host.
    :param subdomain: Subdomain prepended to :data:`SERVER_NAME`.
    :param base_url: Complete base URL, as taken by the test client.
    :param url_scheme: Scheme used when constructing the base URL.
    :param error: Exception class, exception instance, or HTTP status
        code to evaluate the error handler lookup order for. Defaults to
        the routing error (``404``/``405``) when routing fails, or
        ``500`` otherwise.

    .. versionadded:: 3.2
    """
    environ = _build_environ(
        app,
        method=method,
        path=path,
        headers=headers,
        host=host,
        subdomain=subdomain,
        base_url=base_url,
        url_scheme=url_scheme,
    )

    # Build the context object exactly like a real request would, but
    # never push it. Pushing sends signals, opens the session and (on
    # pop) runs teardown callbacks; none of that may happen here.
    request = app.request_class(environ)
    request.json_module = app.json
    # Import locally to avoid a circular import at module load time.
    from .ctx import AppContext

    ctx = AppContext(app, request=request)

    try:
        if ctx.url_adapter is not None:
            # Pure routing computation, same call as AppContext.push().
            ctx.match_request()

        routing_exception = request.routing_exception
        rule = request.url_rule

        bps = list(request.blueprints)

        routing = _build_routing(ctx, rule, routing_exception, bps)
        considered_class, considered_code = _resolve_considered_error(
            app, error, routing["status"], routing_exception
        )

        origin_map = _build_app_callback_origins(app)

        report: dict[str, t.Any] = {
            "schema_version": SCHEMA_VERSION,
            "dry_run": True,
            "request": {
                "method": request.method,
                "path": request.path,
                "query_string": request.query_string.decode("utf-8"),
                "host": request.host,
                "subdomain": subdomain,
                "headers": _normalize_headers(headers),
            },
            "routing": routing,
            "view": _build_view(app, rule, routing),
            "url_value_preprocessors": _build_hook_chain(
                app,
                "url_value_preprocessors",
                bps,
                ordered_scopes=(None, *reversed(bps)),
                reverse_within_scope=False,
                origin_map=origin_map,
            ),
            "before_request": _build_hook_chain(
                app,
                "before_request",
                bps,
                ordered_scopes=(None, *reversed(bps)),
                reverse_within_scope=False,
                origin_map=origin_map,
            ),
            "after_this_request": {
                "will_run": [],
                "will_not_run": [],
                "note": (
                    "Callbacks registered with 'after_this_request' while"
                    " handling a request are not known before the view runs."
                ),
            },
            "after_request": _build_hook_chain(
                app,
                "after_request",
                bps,
                ordered_scopes=(*bps, None),
                reverse_within_scope=True,
                origin_map=origin_map,
            ),
            "teardown_request": _build_hook_chain(
                app,
                "teardown_request",
                bps,
                ordered_scopes=(*bps, None),
                reverse_within_scope=True,
                origin_map=origin_map,
            ),
            "error_handlers": _build_error_handlers(
                app, bps, considered_class, considered_code
            ),
        }
    finally:
        # No context was pushed, so only close the (temporary) request.
        request.close()

    return RequestRehearsal(report)


def _build_environ(
    app: Flask,
    *,
    method: str,
    path: str,
    headers: cabc.Mapping[str, str] | cabc.Iterable[tuple[str, str]] | None,
    host: str | None,
    subdomain: str | None,
    base_url: str | None,
    url_scheme: str | None,
) -> dict[str, t.Any]:
    from .testing import EnvironBuilder

    kwargs: dict[str, t.Any] = {"method": method.upper()}

    if headers is not None:
        kwargs["headers"] = headers

    if base_url is not None:
        # EnvironBuilder forbids combining base_url with subdomain or
        # url_scheme.
        builder = EnvironBuilder(app, path=path, base_url=base_url, **kwargs)
    elif host is not None:
        scheme = url_scheme or app.config["PREFERRED_URL_SCHEME"]
        root = app.config["APPLICATION_ROOT"]
        builder = EnvironBuilder(
            app,
            path=path,
            base_url=f"{scheme}://{host}/{root.lstrip('/')}",
            **kwargs,
        )
    else:
        builder = EnvironBuilder(
            app,
            path=path,
            subdomain=subdomain,
            url_scheme=url_scheme,
            **kwargs,
        )

    try:
        return builder.get_environ()
    finally:
        builder.close()


def _normalize_headers(
    headers: cabc.Mapping[str, str] | cabc.Iterable[tuple[str, str]] | None,
) -> list[list[str]]:
    if headers is None:
        return []

    if isinstance(headers, cabc.Mapping):
        pairs = headers.items()
    else:
        pairs = headers

    return [[str(name), str(value)] for name, value in pairs]


def _func_info(func: t.Callable[..., t.Any]) -> dict[str, t.Any]:
    """Return stable identifying information for a callback."""
    filename: str | None = None
    source_line: int | None = None

    try:
        filename = inspect.getsourcefile(func) or inspect.getfile(func)
    except (OSError, TypeError):
        filename = None

    if filename is not None:
        filename = os.path.abspath(filename)
        try:
            source_line = inspect.getsourcelines(func)[1]
        except (OSError, TypeError):
            source_line = None

    return {
        "name": getattr(func, "__name__", "<anonymous>"),
        "qualname": getattr(func, "__qualname__", getattr(func, "__name__", "")),
        "module": getattr(func, "__module__", None),
        "source_file": filename,
        "source_line": source_line,
    }


def _callback_item(
    func: t.Callable[..., t.Any],
    *,
    kind: str,
    scope: str | None,
    scope_index: int,
    origin_map: dict[int, str],
    chain_index: int | None = None,
    will_run: bool = True,
    reason: str | None = None,
) -> dict[str, t.Any]:
    if scope is None:
        origin_blueprint = origin_map.get(id(func))
        origin = "blueprint" if origin_blueprint is not None else "app"
    else:
        origin = "blueprint"
        origin_blueprint = scope

    item = {
        "kind": kind,
        "function": _func_info(func),
        "scope": scope,
        "scope_kind": "app" if scope is None else "blueprint",
        "origin": origin,
        "origin_blueprint": origin_blueprint,
        "scope_index": scope_index,
        "will_run": will_run,
        "reason": reason,
    }

    if chain_index is not None:
        item["chain_index"] = chain_index

    return item


def _build_app_callback_origins(app: Flask) -> dict[int, str]:
    """Map the ``id()`` of callbacks registered through a blueprint's
    ``*_app_request`` decorators to the (first) blueprint name they
    were registered for.
    """
    origin_map: dict[int, str] = {}

    for blueprint_name, kinds in app._blueprint_app_callbacks.items():
        for funcs in kinds.values():
            for func in funcs:
                origin_map.setdefault(id(func), blueprint_name)

    return origin_map


def _build_hook_chain(
    app: Flask,
    kind: str,
    blueprints: list[str],
    *,
    ordered_scopes: tuple[str | None, ...],
    reverse_within_scope: bool,
    origin_map: dict[int, str],
) -> dict[str, list[dict[str, t.Any]]]:
    """Mirror one of the request hook iteration loops in ``app.py``."""
    registry = getattr(app, _HOOK_ATTRS[kind])
    active_scopes = {None, *blueprints}

    will_run: list[dict[str, t.Any]] = []

    for scope in ordered_scopes:
        funcs = registry.get(scope)

        if not funcs:
            continue

        indexed = list(enumerate(funcs))

        if reverse_within_scope:
            indexed.reverse()

        for scope_index, func in indexed:
            will_run.append(
                _callback_item(
                    func,
                    kind=kind,
                    scope=scope,
                    scope_index=scope_index,
                    origin_map=origin_map,
                    chain_index=len(will_run),
                )
            )

    will_not_run: list[dict[str, t.Any]] = []

    for scope, funcs in registry.items():
        if scope in active_scopes or not funcs:
            continue

        for scope_index, func in enumerate(funcs):
            will_not_run.append(
                _callback_item(
                    func,
                    kind=kind,
                    scope=scope,
                    scope_index=scope_index,
                    origin_map=origin_map,
                    will_run=False,
                    reason=_REASON_BLUEPRINT_INACTIVE,
                )
            )

    will_not_run.sort(key=lambda item: (str(item["scope"]), item["scope_index"]))

    return {"will_run": will_run, "will_not_run": will_not_run}


def _build_routing(
    ctx: t.Any,
    rule: t.Any,
    routing_exception: HTTPException | None,
    blueprints: list[str],
) -> dict[str, t.Any]:
    """Describe the routing result without dispatching anything."""
    routing: dict[str, t.Any] = {
        "status": "matched",
        "endpoint": None,
        "rule": None,
        "subdomain": None,
        "host": None,
        "arguments": {},
        "blueprints": blueprints,
        "allowed_methods": None,
        "outcome": "view",
        "redirect": None,
        "error_code": None,
        "error_handlers_bypassed": False,
    }

    if routing_exception is not None:
        if isinstance(routing_exception, RequestRedirect):
            routing.update(
                status="redirect",
                outcome="redirect",
                redirect={
                    "code": routing_exception.code,
                    "location": getattr(routing_exception, "new_url", None),
                },
                # RoutingException is re-raised/returned before error
                # handler lookup in handle_http_exception.
                error_handlers_bypassed=True,
            )
            return routing

        if isinstance(routing_exception, MethodNotAllowed):
            routing.update(
                status="method_not_allowed",
                outcome="http_error",
                error_code=routing_exception.code,
                allowed_methods=sorted(routing_exception.valid_methods),
            )
            return routing

        if isinstance(routing_exception, NotFound):
            routing.update(
                status="not_found",
                outcome="http_error",
                error_code=routing_exception.code,
            )
            return routing

        # Any other routing exception, e.g. a trusted-host 400 raised
        # while binding the adapter.
        routing.update(
            status="http_error",
            outcome="http_error",
            error_code=getattr(routing_exception, "code", None),
        )
        return routing

    routing["endpoint"] = rule.endpoint
    routing["rule"] = rule.rule
    routing["subdomain"] = rule.subdomain
    routing["host"] = rule.host
    routing["arguments"] = dict(ctx.request.view_args or {})

    if ctx.request.method == "OPTIONS" and getattr(
        rule, "provide_automatic_options", False
    ):
        routing["outcome"] = "automatic_options"
        # make_default_options_response uses the bound adapter rather
        # than the single matched rule.
        routing["allowed_methods"] = sorted(ctx.url_adapter.allowed_methods())

    return routing


def _build_view(
    app: Flask, rule: t.Any, routing: dict[str, t.Any]
) -> dict[str, t.Any] | None:
    if rule is None:
        return None

    endpoint = rule.endpoint
    func = app.view_functions.get(endpoint)
    blueprint_name = endpoint.rpartition(".")[0] if "." in endpoint else None

    automatic_options = routing["outcome"] == "automatic_options"

    view: dict[str, t.Any] = {
        "endpoint": endpoint,
        "function": _func_info(func) if func is not None else None,
        "scope_kind": "blueprint" if blueprint_name is not None else "app",
        "origin_blueprint": blueprint_name,
        "will_run": not automatic_options,
        "reason": _REASON_AUTOMATIC_OPTIONS if automatic_options else None,
    }
    return view


def _resolve_considered_error(
    app: Flask,
    error: type[Exception] | Exception | int | None,
    routing_status: str,
    routing_exception: HTTPException | None,
) -> tuple[type[Exception], int | None]:
    """Choose the exception the error handler lookup section describes."""
    if error is not None:
        if isinstance(error, int):
            try:
                exc_class = default_exceptions[error]
            except KeyError:
                raise ValueError(
                    f"'{error}' is not a recognized HTTP error code. Use a"
                    " subclass of Exception with that code instead."
                ) from None
        elif isinstance(error, Exception) and not isinstance(error, type):
            exc_class = type(error)
        else:
            exc_class = t.cast(type[Exception], error)
    elif routing_status == "method_not_allowed":
        exc_class = MethodNotAllowed
    elif routing_status in {"not_found", "http_error"}:
        exc_class = type(routing_exception) if routing_exception else NotFound
    else:
        # Matched request and slash redirect: describe what happens if
        # the view raises a generic server error. InternalServerError
        # exercises both the code-specific (500) and generic lookup
        # buckets.
        from werkzeug.exceptions import InternalServerError

        exc_class = InternalServerError

    return app._get_exc_class_and_code(exc_class)


def _scope_error_records(app: Flask, scope: str | None) -> list[dict[str, t.Any]]:
    """Error handler registration log for a final, post-merge scope."""
    if scope is None:
        return app._error_handler_registrations  # type: ignore[no-any-return]

    blueprint = app.blueprints.get(scope)

    if blueprint is None:
        return []

    return blueprint._error_handler_registrations  # type: ignore[no-any-return]


def _error_handler_entry(
    record: dict[str, t.Any],
    *,
    scope: str | None,
    chain_index: int | None = None,
    will_handle: bool | None = None,
    reason: str | None = None,
) -> dict[str, t.Any]:
    func = record["func"]
    exc_class = record["exception_class"]

    if scope is None:
        origin_blueprint = record.get("origin_blueprint")
        origin = "blueprint" if origin_blueprint is not None else "app"
    else:
        origin = "blueprint"
        origin_blueprint = scope

    entry = {
        "bucket": record["code"],
        "scope": scope,
        "scope_kind": "app" if scope is None else "blueprint",
        "origin": origin,
        "origin_blueprint": origin_blueprint,
        "exception_module": exc_class.__module__,
        "exception_class": exc_class.__qualname__,
        "handler": _func_info(func),
        "registration_index": record["sequence"],
        "will_handle": will_handle,
        "reason": reason,
    }

    if chain_index is not None:
        entry["chain_index"] = chain_index

    return entry


def _build_error_handlers(
    app: Flask,
    blueprints: list[str],
    exc_class: type[Exception],
    code: int | None,
) -> dict[str, t.Any]:
    """Report error handler lookup in the same order as
    ``App._find_error_handler``."""
    # _find_error_handler searches deepest blueprint first, app last.
    ordered_scopes = (*blueprints, None)
    active_scopes = set(ordered_scopes)
    buckets = (code, None) if code is not None else (None,)
    mro = list(exc_class.__mro__)

    # Replay registrations per final scope to tell surviving handlers
    # from ones overwritten by a later same-key registration.
    records_by_scope: dict[str | None, list[dict[str, t.Any]]] = {
        scope: _scope_error_records(app, scope) for scope in ordered_scopes
    }

    survivors: dict[tuple[str | None, int | None, type[Exception]], t.Any] = {}

    for scope, records in records_by_scope.items():
        for record in records:
            survivors[(scope, record["code"], record["exception_class"])] = record

    overridden: list[dict[str, t.Any]] = []

    for scope, records in records_by_scope.items():
        for record in records:
            key = (scope, record["code"], record["exception_class"])
            survivor = survivors[key]

            if survivor["func"] is not record["func"]:
                entry = _error_handler_entry(
                    record,
                    scope=scope,
                    will_handle=False,
                    reason=_REASON_OVERWRITTEN,
                )
                entry["replaced_by"] = _error_handler_entry(survivor, scope=scope)
                overridden.append(entry)

    lookup_order: list[dict[str, t.Any]] = []
    matched = False

    for bucket in buckets:
        for scope in ordered_scopes:
            scope_spec = app.error_handler_spec.get(scope)
            class_map = scope_spec.get(bucket) if scope_spec is not None else None

            if not class_map:
                continue

            for candidate_class in mro:
                func = class_map.get(candidate_class)

                if func is None:
                    continue

                record = _record_for(
                    records_by_scope[scope], bucket, candidate_class, func
                )
                will_handle = not matched
                reason = None if will_handle else _REASON_PRECEDENCE
                lookup_order.append(
                    _error_handler_entry(
                        record,
                        scope=scope,
                        chain_index=len(lookup_order),
                        will_handle=will_handle,
                        reason=reason,
                    )
                )
                matched = True

    # In-scope surviving registrations that are not probed for the
    # considered exception (different bucket or unrelated class).
    other: list[dict[str, t.Any]] = []
    probed = {
        (
            entry["scope"],
            entry["bucket"],
            (entry["exception_module"], entry["exception_class"]),
        )
        for entry in lookup_order
    }

    for scope in ordered_scopes:
        for record in records_by_scope[scope]:
            key = (
                scope,
                record["code"],
                (
                    record["exception_class"].__module__,
                    record["exception_class"].__qualname__,
                ),
            )
            survivor = survivors[(scope, record["code"], record["exception_class"])]

            if survivor["func"] is not record["func"]:
                # Replaced by a later registration; reported in
                # "overridden" instead.
                continue

            if key in probed:
                continue

            other.append(
                _error_handler_entry(
                    record,
                    scope=scope,
                    will_handle=False,
                    reason=_REASON_NOT_CONSIDERED,
                )
            )

    # Registrations on blueprints that do not handle the request never
    # participate in lookup.
    will_not_run: list[dict[str, t.Any]] = []

    for scope in app.error_handler_spec:
        if scope in active_scopes:
            continue

        records = _scope_error_records(app, scope)
        scope_survivors: dict[tuple[int | None, type[Exception]], dict[str, t.Any]] = {}

        for record in records:
            scope_survivors[(record["code"], record["exception_class"])] = record

        for record in records:
            survivor = scope_survivors[(record["code"], record["exception_class"])]

            if survivor["func"] is not record["func"]:
                # Replaced within the inactive scope; not an active
                # registration at all.
                continue

            will_not_run.append(
                _error_handler_entry(
                    record,
                    scope=scope,
                    will_handle=False,
                    reason=_REASON_BLUEPRINT_INACTIVE,
                )
            )

    will_not_run.sort(
        key=lambda item: (
            str(item["scope"]),
            item["bucket"] if item["bucket"] is not None else -1,
            item["registration_index"],
        )
    )

    return {
        "considered": {
            "exception_module": exc_class.__module__,
            "exception_class": exc_class.__qualname__,
            "code": code,
        },
        "lookup_order": lookup_order,
        "other": other,
        "will_not_run": will_not_run,
        "overridden": overridden,
    }


def _record_for(
    records: list[dict[str, t.Any]],
    bucket: int | None,
    exc_class: type[Exception],
    func: t.Callable[..., t.Any],
) -> dict[str, t.Any]:
    """Find the registration record responsible for a spec value."""
    for record in reversed(records):
        if (
            record["code"] == bucket
            and record["exception_class"] is exc_class
            and record["func"] is func
        ):
            return record

    # Fallback: synthesize a record if the mapping was populated
    # through another supported path. Should not occur with Flask's
    # own decorators.
    return {
        "sequence": -1,
        "code": bucket,
        "exception_class": exc_class,
        "func": func,
        "origin_blueprint": None,
    }


def _render_text(report: dict[str, t.Any]) -> str:
    """Render a human readable version of the report."""
    lines: list[str] = []
    request = report["request"]
    lines.append(
        f"Request: {request['method']} {request['path']}" f" (host: {request['host']})"
    )

    routing = report["routing"]
    lines.append("")
    lines.append("Routing:")
    lines.append(f"  status: {routing['status']}")

    if routing["rule"] is not None:
        lines.append(f"  rule: {routing['rule']}")
        lines.append(f"  endpoint: {routing['endpoint']}")

        if routing["arguments"]:
            args = ", ".join(
                f"{key}={value!r}" for key, value in routing["arguments"].items()
            )
            lines.append(f"  arguments: {args}")

    if routing["blueprints"]:
        lines.append(f"  blueprints: {' -> '.join(routing['blueprints'])}")

    if routing["status"] == "method_not_allowed":
        lines.append(
            "  allowed methods: "
            f"{', '.join(routing['allowed_methods'] or []) or '(none)'}"
        )

    if routing["status"] == "redirect":
        redirect = routing["redirect"]
        lines.append(f"  {redirect['code']} redirect to: {redirect['location']}")
        lines.append("  (error handlers are bypassed for routing redirects)")

    if routing["outcome"] == "automatic_options":
        lines.append(
            "  automatic OPTIONS response; the view is not called; allow:"
            f" {', '.join(routing['allowed_methods'] or []) or '(none)'}"
        )

    if routing["status"] in {"not_found", "http_error", "method_not_allowed"}:
        lines.append(
            "  no blueprint scope matched; only application-wide callbacks" " apply"
        )

    view = report["view"]

    if view is not None:
        lines.append("")
        lines.append("View:")

        if view["will_run"]:
            lines.append(f"  {_format_function(view['function'])}")
        else:
            lines.append(
                f"  not called ({view['reason']}):"
                f" {_format_function(view['function'])}"
            )

    for title, key in (
        ("URL value preprocessors", "url_value_preprocessors"),
        ("Before request", "before_request"),
        ("After request", "after_request"),
        ("Teardown request", "teardown_request"),
    ):
        section = report[key]
        lines.append("")
        lines.append(f"{title}:")
        lines.extend(_render_chain(section["will_run"]))

        for item in section["will_not_run"]:
            lines.append(
                "  [won't run]"
                f" [{_format_scope(item)}]"
                f" {_format_function(item['function'])}"
                f" ({item['reason']})"
            )

        if not section["will_run"] and not section["will_not_run"]:
            lines.append("  (none registered)")

    errors = report["error_handlers"]
    considered = errors["considered"]
    lines.append("")
    lines.append(
        "Error handler lookup for"
        f" {considered['exception_module']}.{considered['exception_class']}"
        " (first match wins):"
    )

    if errors["lookup_order"]:
        for entry in errors["lookup_order"]:
            marker = "WILL HANDLE" if entry["will_handle"] else "skipped"
            reason = f" ({entry['reason']})" if entry["reason"] else ""
            lines.append(
                f"  #{entry['chain_index']} [{_format_scope(entry)}]"
                f" bucket={entry['bucket']}"
                f" {entry['exception_module']}.{entry['exception_class']}"
                f" -> {_format_function(entry['handler'])}"
                f" [{marker}]{reason}"
            )
    else:
        lines.append("  (no matching handler registered)")

    for entry in errors["other"]:
        lines.append(
            "  [not considered]"
            f" [{_format_scope(entry)}]"
            f" bucket={entry['bucket']}"
            f" {entry['exception_module']}.{entry['exception_class']}"
            f" -> {_format_function(entry['handler'])}"
            f" ({entry['reason']})"
        )

    for entry in errors["will_not_run"]:
        lines.append(
            "  [won't run]"
            f" [{_format_scope(entry)}]"
            f" bucket={entry['bucket']}"
            f" {entry['exception_module']}.{entry['exception_class']}"
            f" -> {_format_function(entry['handler'])}"
            f" ({entry['reason']})"
        )

    for entry in errors["overridden"]:
        replaced_by = entry["replaced_by"]
        lines.append(
            "  [overridden, never effective]"
            f" [{_format_scope(entry)}]"
            f" bucket={entry['bucket']}"
            f" {entry['exception_module']}.{entry['exception_class']}"
            f" -> {_format_function(entry['handler'])}"
            " replaced by"
            f" {_format_function(replaced_by['handler'])}"
        )

    return "\n".join(lines)


def _render_chain(items: list[dict[str, t.Any]]) -> list[str]:
    lines = []

    for item in items:
        lines.append(
            f"  #{item['chain_index']} [{_format_scope(item)}]"
            f" reg#{item['scope_index']}"
            f" {_format_function(item['function'])}"
        )

    return lines


def _format_scope(item: dict[str, t.Any]) -> str:
    if item["scope"] is None:
        if item["origin"] == "blueprint":
            return f"app (from blueprint '{item['origin_blueprint']}')"

        return "app"

    return f"blueprint '{item['scope']}'"


def _format_function(func: dict[str, t.Any] | None) -> str:
    if func is None:
        return "(no function registered)"

    location = func["source_file"] or func["module"] or "<unknown>"

    if func["source_line"]:
        location = f"{location}:{func['source_line']}"

    return f"{func['qualname']} ({location})"
