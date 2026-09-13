from __future__ import annotations

import functools
import importlib.metadata
import json
import os
import re
import typing as t
from collections import defaultdict

from jinja2 import FileSystemLoader

from .sansio.scaffold import _sentinel

if t.TYPE_CHECKING:  # pragma: no cover
    import click

    from .sansio.app import App
    from .sansio.scaffold import Scaffold

#: Version of the snapshot structure produced by this module. Snapshots
#: carrying a different version are rejected by :func:`diff_snapshots`.
SNAPSHOT_SCHEMA_VERSION = "1"

#: Returned in place of a value that may contain sensitive information.
REDACTED = "***redacted***"

#: All top-level sections that must be present in a valid snapshot, in the
#: order in which they are serialized.
_LIST_SECTIONS = (
    "routes",
    "blueprints",
    "error_handlers",
    "templates",
    "static",
    "extensions",
    "commands",
)
_HOOK_GROUPS = (
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

_SECRET_WORDS = (
    "secret",
    "password",
    "passwd",
    "token",
    "credential",
    "apikey",
    "privatekey",
)
_SECRET_RE = re.compile(r"[^a-z0-9]+")


class SnapshotError(ValueError):
    """Raised when a snapshot cannot be parsed, is unsupported, or two
    snapshots cannot be compared because they do not describe the same
    application.

    .. versionadded:: 3.2
    """


# ---------------------------------------------------------------------------
# Stable value conversion
# ---------------------------------------------------------------------------


def _is_secret_key(key: str) -> bool:
    normalized = _SECRET_RE.sub("", key.lower())
    return any(word in normalized for word in _SECRET_WORDS)


def _type_ref(obj: object) -> str:
    cls = type(obj)
    return f"{cls.__module__}.{cls.__qualname__}"


def _stable(
    value: t.Any, key: str | None = None, *, include_sensitive: bool = False
) -> t.Any:
    """Convert an arbitrary registration value to a JSON-compatible
    representation with a deterministic structure.

    Values whose key name suggests a secret are replaced by
    :data:`REDACTED` unless ``include_sensitive`` is set. Unsupported
    objects are described by their type only, never by their value or
    ``repr``, which avoids leaking arbitrary or sensitive data.
    """
    if key is not None and not include_sensitive and _is_secret_key(key):
        return REDACTED

    if value is None or isinstance(value, str | bool | int | float):
        return value

    if isinstance(value, dict):
        items: list[tuple[str, t.Any, t.Any]] = []

        for raw_key, raw_value in value.items():
            stable_key = _stable(raw_key)
            key_name = raw_key if isinstance(raw_key, str) else None
            stable_value = _stable(
                raw_value, key_name, include_sensitive=include_sensitive
            )
            sort_key = json.dumps(stable_key, sort_keys=True)
            items.append((sort_key, stable_key, stable_value))

        items.sort(key=lambda item: item[0])
        return [{"key": item[1], "value": item[2]} for item in items]

    if isinstance(value, list | tuple):
        return [_stable(item, include_sensitive=include_sensitive) for item in value]

    if isinstance(value, set | frozenset):
        members = [_stable(item, include_sensitive=include_sensitive) for item in value]
        members.sort(key=lambda item: json.dumps(item, sort_keys=True))
        return members

    # bytes and arbitrary objects are described by their type only.
    return {"type": _type_ref(value)}


# ---------------------------------------------------------------------------
# Callable / source references
# ---------------------------------------------------------------------------


def _callable_ref(func: t.Callable[..., t.Any]) -> dict[str, t.Any]:
    """Describe a registered function without calling it.

    The combination of module, qualified name, source file and first
    line is a stable identity that survives repeated exports and allows
    diffs to match the same registration across snapshots.
    """
    if isinstance(func, functools.partial):
        return {
            "kind": "partial",
            "name": getattr(func.func, "__name__", "partial"),
            "function": _callable_ref(func.func),
        }

    target = getattr(func, "__func__", func)
    code = getattr(target, "__code__", None)
    module = getattr(target, "__module__", type(func).__module__)
    qualname = getattr(target, "__qualname__", None) or type(func).__qualname__
    name = getattr(target, "__name__", None) or qualname

    ref: dict[str, t.Any] = {
        "kind": "method" if target is not func else "function",
        "name": name,
        "module": module,
        "qualname": qualname,
        "file": code.co_filename if code is not None else None,
        "line": code.co_firstlineno if code is not None else None,
    }

    if code is None:
        # Callable instance or built-in: point at the actual class.
        ref["kind"] = "callable"
        ref["callable_type"] = _type_ref(func)

    return ref


def _exception_ref(exc_class: type[Exception]) -> dict[str, str]:
    return {
        "name": exc_class.__qualname__,
        "module": exc_class.__module__,
        "qualname": f"{exc_class.__module__}.{exc_class.__qualname__}",
    }


# ---------------------------------------------------------------------------
# Snapshot construction
# ---------------------------------------------------------------------------


def _routes_snapshot(app: App, *, include_sensitive: bool) -> list[dict[str, t.Any]]:
    rules = list(app.url_map.iter_rules())
    view_functions = app.view_functions
    entries = []

    for seq, rule in enumerate(rules):
        methods = None if rule.methods is None else sorted(rule.methods)
        redirect_to = rule.redirect_to

        if callable(redirect_to):
            redirect_ref: t.Any = {"type": _type_ref(redirect_to)}
        else:
            redirect_ref = redirect_to

        view_func = view_functions.get(rule.endpoint)
        entries.append(
            {
                "seq": seq,
                "endpoint": rule.endpoint,
                "rule": rule.rule,
                "methods": methods,
                "host": rule.host,
                "subdomain": rule.subdomain,
                "defaults": _stable(
                    rule.defaults or {}, include_sensitive=include_sensitive
                ),
                "arguments": sorted(rule.arguments),
                "redirect_to": redirect_ref,
                "alias": rule.alias,
                "build_only": rule.build_only,
                "websocket": rule.websocket,
                "merge_slashes": rule.merge_slashes,
                "provide_automatic_options": getattr(
                    rule, "provide_automatic_options", None
                ),
                "view": _callable_ref(view_func) if view_func is not None else None,
            }
        )

    return entries


def _blueprints_snapshot(
    app: App, *, include_sensitive: bool
) -> list[dict[str, t.Any]]:
    entries = []

    for seq, (name, blueprint) in enumerate(app.blueprints.items()):
        parent = name.rpartition(".")[0] or None

        if blueprint.cli_group is _sentinel:
            cli_group: str | None = blueprint.name
        else:
            cli_group = blueprint.cli_group

        entries.append(
            {
                "seq": seq,
                "name": name,
                "parent": parent,
                "import_name": blueprint.import_name,
                "blueprint_type": _type_ref(blueprint),
                "url_prefix": blueprint.url_prefix,
                "subdomain": blueprint.subdomain,
                "url_defaults": _stable(
                    blueprint.url_values_defaults,
                    include_sensitive=include_sensitive,
                ),
                "static_folder": blueprint.static_folder,
                "static_url_path": blueprint.static_url_path,
                "has_static_folder": blueprint.has_static_folder,
                "template_folder": blueprint.template_folder,
                "root_path": blueprint.root_path,
                "cli_group": cli_group,
            }
        )

    return entries


def _scope_order(registry: dict[t.Any, t.Any]) -> list[t.Any]:
    """Application scope (``None``) first, then blueprint scopes sorted by
    name. The order does not depend on dict hashing and is identical for
    repeated exports of the same application."""
    scopes = list(registry)
    return ([None] if None in scopes else []) + sorted(
        scope for scope in scopes if scope is not None
    )


def _hook_entries(
    registry: dict[t.Any, list[t.Callable[..., t.Any]]], *, scope: t.Any
) -> list[dict[str, t.Any]]:
    funcs = registry.get(scope)
    if not funcs:
        return []

    return [
        {"seq": seq, "scope": scope, **_callable_ref(func)}
        for seq, func in enumerate(funcs)
    ]


def _hooks_snapshot(app: App) -> dict[str, list[dict[str, t.Any]]]:
    snapshot: dict[str, list[dict[str, t.Any]]] = {}

    scoped_groups = (
        ("before_request", "before_request_funcs"),
        ("after_request", "after_request_funcs"),
        ("teardown_request", "teardown_request_funcs"),
        ("url_value_preprocessor", "url_value_preprocessors"),
        ("url_defaults", "url_default_functions"),
        ("template_context_processor", "template_context_processors"),
    )

    for group_name, attr_name in scoped_groups:
        registry = getattr(app, attr_name)
        entries: list[dict[str, t.Any]] = []

        for scope in _scope_order(registry):
            entries.extend(_hook_entries(registry, scope=scope))

        snapshot[group_name] = entries

    app_groups = (
        ("teardown_appcontext", "teardown_appcontext_funcs"),
        ("shell_context_processor", "shell_context_processors"),
        ("url_build_error_handler", "url_build_error_handlers"),
    )

    for group_name, attr_name in app_groups:
        funcs = getattr(app, attr_name, None) or []
        snapshot[group_name] = [
            {"seq": seq, "scope": None, **_callable_ref(func)}
            for seq, func in enumerate(funcs)
        ]

    return snapshot


def _error_handlers_snapshot(app: App) -> list[dict[str, t.Any]]:
    """Build the error handler section from registration history.

    Flask stores only the handler that won each ``(scope, code,
    exception)`` slot, so a history is recorded while handlers are
    registered. Walking that history keeps handlers that were
    overwritten by a later registration visible and marks which
    handler is currently active.
    """
    # (scope, code, exception qualname) -> ordered list of records
    grouped: dict[tuple[t.Any, int | None, str], list[dict[str, t.Any]]] = (
        defaultdict(list)
    )
    order: list[tuple[t.Any, int | None, str]] = []
    scope_seq: dict[t.Any, int] = defaultdict(int)

    def add_history(scope: t.Any, history: t.Any) -> None:
        for code, exc_class, func in history:
            exc = _exception_ref(exc_class)
            key = (scope, code, exc["qualname"])

            if key not in grouped:
                order.append(key)

            seq = scope_seq[scope]
            scope_seq[scope] = seq + 1
            grouped[key].append(
                {"seq": seq, "handler": _callable_ref(func)}
            )

    add_history(None, getattr(app, "_error_handler_history", ()))

    for blueprint_name, blueprint in app.blueprints.items():
        add_history(
            blueprint_name, getattr(blueprint, "_error_handler_history", ())
        )

    entries = []

    def error_sort_key(
        item: tuple[t.Any, int | None, str]
    ) -> tuple[str, bool, int, str]:
        return (item[0] or "", item[1] is None, item[1] or 0, item[2])

    for key in sorted(order, key=error_sort_key):
        scope, code, exc_qualname = key
        handlers = grouped[key]

        for index, record in enumerate(handlers):
            record["active"] = index == len(handlers) - 1

        module, _, qualname = exc_qualname.rpartition(".")
        entries.append(
            {
                "scope": scope,
                "code": code,
                "exception": {
                    "name": qualname,
                    "module": module,
                    "qualname": exc_qualname,
                },
                "handlers": handlers,
            }
        )

    return entries


def _loader_description(
    scaffold: Scaffold,
) -> tuple[str, list[str]] | None:
    """Describe the Jinja loader a scaffold contributes to the template
    search chain. The cached ``jinja_loader`` property is never populated
    as a side effect; the default loader is described from configuration
    if it has not been created yet."""
    if "jinja_loader" in scaffold.__dict__:
        loader = scaffold.__dict__["jinja_loader"]
    elif scaffold.template_folder is not None:
        path = os.path.join(scaffold.root_path, scaffold.template_folder)
        return ("jinja2.loaders.FileSystemLoader", [path])
    else:
        loader = None

    if loader is None:
        return None

    loader_type = _type_ref(loader)

    if isinstance(loader, FileSystemLoader):
        return loader_type, [os.fspath(path) for path in loader.searchpath]

    return loader_type, []


def _templates_snapshot(app: App) -> list[dict[str, t.Any]]:
    chain = []
    seq = 0

    app_description = _loader_description(app)

    if app_description is not None:
        loader_type, paths = app_description
        chain.append(
            {
                "seq": seq,
                "scope": None,
                "loader_type": loader_type,
                "paths": paths,
            }
        )
        seq += 1

    for blueprint_name, blueprint in app.blueprints.items():
        description = _loader_description(blueprint)

        if description is None:
            continue

        loader_type, paths = description
        chain.append(
            {
                "seq": seq,
                "scope": blueprint_name,
                "loader_type": loader_type,
                "paths": paths,
            }
        )
        seq += 1

    return chain


def _static_snapshot(app: App) -> list[dict[str, t.Any]]:
    entries = [
        {
            "seq": 0,
            "scope": None,
            "has_static_folder": app.has_static_folder,
            "static_folder": app.static_folder,
            "static_url_path": app.static_url_path,
        }
    ]

    seq = 1

    for blueprint_name, blueprint in app.blueprints.items():
        if not blueprint.has_static_folder:
            continue

        entries.append(
            {
                "seq": seq,
                "scope": blueprint_name,
                "has_static_folder": True,
                "static_folder": blueprint.static_folder,
                "static_url_path": blueprint.static_url_path,
            }
        )
        seq += 1

    return entries


def _extensions_snapshot(app: App) -> list[dict[str, t.Any]]:
    return [
        {
            "seq": seq,
            "key": key,
            "value_type": _type_ref(value),
        }
        for seq, (key, value) in enumerate(app.extensions.items())
    ]


def _commands_snapshot(app: App) -> list[dict[str, t.Any]]:
    """Flatten the Click command tree registered on ``app.cli``.

    Commands contributed by blueprints are attributed to their
    blueprint scope whether they were merged into the root group or
    added as a named group.
    """
    import click

    root = getattr(app, "cli", None)

    if root is None:
        return []

    scope_by_id: dict[int, str] = {}

    for blueprint_name, blueprint in app.blueprints.items():
        blueprint_cli = getattr(blueprint, "cli", None)

        if blueprint_cli is None:
            continue

        scope_by_id[id(blueprint_cli)] = blueprint_name

        for command in blueprint_cli.commands.values():
            scope_by_id.setdefault(id(command), blueprint_name)

    entries: list[dict[str, t.Any]] = []
    seen_groups: set[int] = set()

    def walk(
        group: click.Group, parent_path: str, scope: str | None
    ) -> None:
        for seq, (name, command) in enumerate(group.commands.items()):
            path = f"{parent_path}.{name}" if parent_path else name
            command_scope = scope_by_id.get(id(command), scope)

            params: list[dict[str, t.Any]] = []

            for param in command.params:
                if isinstance(param, click.Argument):
                    params.append(
                        {"name": param.name or "", "kind": "argument"}
                    )
                else:
                    option = t.cast(click.Option, param)
                    params.append(
                        {
                            "name": option.name or "",
                            "kind": "option",
                            "opts": list(option.opts),
                            "secondary_opts": list(option.secondary_opts),
                        }
                    )

            callback = getattr(command, "callback", None)
            entries.append(
                {
                    "seq": seq,
                    "path": path,
                    "name": name,
                    "parent": parent_path or None,
                    "scope": command_scope,
                    "kind": (
                        "group"
                        if isinstance(command, click.Group)
                        else "command"
                    ),
                    "command_type": _type_ref(command),
                    "callback": (
                        _callable_ref(callback) if callback is not None else None
                    ),
                    "params": params,
                }
            )

            if isinstance(command, click.Group):
                if id(command) in seen_groups:
                    continue

                seen_groups.add(id(command))
                walk(command, path, command_scope)

    walk(root, "", None)
    return entries


def take_snapshot(app: App, *, include_sensitive: bool = False) -> dict[str, t.Any]:
    """Export the registration state of ``app`` as a stable,
    JSON-serializable structure.

    Taking a snapshot does not execute any view, hook or signal
    handler, does not push a request or application context, and does
    not mutate the application. The output is deterministic: exporting
    the same application twice produces byte-identical results.

    :param app: The application to snapshot.
    :param include_sensitive: Set to ``True`` to keep values associated
        with secret-sounding keys (such as ``token`` or ``password``).
        They are redacted by default.

    .. versionadded:: 3.2
    """
    try:
        flask_version = importlib.metadata.version("flask")
    except importlib.metadata.PackageNotFoundError:
        flask_version = "unknown"

    return {
        "meta": {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "flask_version": flask_version,
        },
        "app": {
            "name": app.name,
            "import_name": app.import_name,
            "root_path": app.root_path,
            "instance_path": app.instance_path,
            "host_matching": bool(app.url_map.host_matching),
            "subdomain_matching": bool(app.subdomain_matching),
        },
        "routes": _routes_snapshot(app, include_sensitive=include_sensitive),
        "blueprints": _blueprints_snapshot(
            app, include_sensitive=include_sensitive
        ),
        "hooks": _hooks_snapshot(app),
        "error_handlers": _error_handlers_snapshot(app),
        "templates": _templates_snapshot(app),
        "static": _static_snapshot(app),
        "extensions": _extensions_snapshot(app),
        "commands": _commands_snapshot(app),
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SnapshotError(message)


def _validate_snapshot(snapshot: t.Any, label: str = "snapshot") -> dict[str, t.Any]:
    _require(
        isinstance(snapshot, dict),
        f"{label} must be a JSON object, got {_type_ref(snapshot)}.",
    )

    meta = snapshot.get("meta")
    _require(isinstance(meta, dict), f"{label}: 'meta' section must be an object.")

    version = meta.get("schema_version")
    _require(
        version == SNAPSHOT_SCHEMA_VERSION,
        f"{label}: unsupported schema version {version!r}; this version of Flask"
        f" supports {SNAPSHOT_SCHEMA_VERSION!r}.",
    )

    app = snapshot.get("app")
    _require(isinstance(app, dict), f"{label}: 'app' section must be an object.")
    _require(
        isinstance(app.get("name"), str) and isinstance(app.get("import_name"), str),
        f"{label}: 'app' section must define string 'name' and 'import_name'.",
    )

    for section in _LIST_SECTIONS:
        value = snapshot.get(section)
        _require(
            isinstance(value, list),
            f"{label}: '{section}' section must be a list.",
        )
        _require(
            all(isinstance(item, dict) for item in value),
            f"{label}: every entry in '{section}' must be an object.",
        )

    hooks = snapshot.get("hooks")
    _require(isinstance(hooks, dict), f"{label}: 'hooks' section must be an object.")
    _require(
        set(hooks) == set(_HOOK_GROUPS),
        f"{label}: 'hooks' section must contain exactly the groups"
        f" {', '.join(_HOOK_GROUPS)}.",
    )

    for group_name, entries in hooks.items():
        _require(
            isinstance(entries, list),
            f"{label}: hooks.{group_name} must be a list.",
        )
        _require(
            all(isinstance(item, dict) for item in entries),
            f"{label}: every entry in hooks.{group_name} must be an object.",
        )

    return t.cast(dict[str, t.Any], snapshot)


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------


def _strip_seq(value: t.Any) -> t.Any:
    if isinstance(value, dict):
        return {
            key: _strip_seq(item)
            for key, item in value.items()
            if key not in {"seq", "active"}
        }

    if isinstance(value, list):
        return [_strip_seq(item) for item in value]

    return value


def _route_key(entry: dict[str, t.Any]) -> tuple[t.Any, ...]:
    # The method set and defaults are registration attributes rather than
    # part of a rule's identity, so changing them is reported as a change.
    return (
        "route",
        entry["endpoint"],
        entry["rule"],
        entry["host"] or "",
        entry["subdomain"] or "",
    )


def _blueprint_key(entry: dict[str, t.Any]) -> tuple[t.Any, ...]:
    return ("blueprint", entry["name"])


def _hook_key(entry: dict[str, t.Any]) -> tuple[t.Any, ...]:
    return (
        "hook",
        entry["group"],
        entry["scope"] or "",
        entry["module"],
        entry["qualname"],
        entry["file"] or "",
        entry["line"] if entry["line"] is not None else -1,
        entry["kind"],
    )


def _error_handler_key(entry: dict[str, t.Any]) -> tuple[t.Any, ...]:
    return (
        "error_handler",
        entry["scope"] or "",
        entry["code"] if entry["code"] is not None else -1,
        entry["exception"]["qualname"],
    )


def _template_key(entry: dict[str, t.Any]) -> tuple[t.Any, ...]:
    return (
        "template_loader",
        entry["scope"] or "",
        entry["loader_type"],
        tuple(entry["paths"]),
    )


def _static_key(entry: dict[str, t.Any]) -> tuple[t.Any, ...]:
    return ("static", entry["scope"] or "")


def _extension_key(entry: dict[str, t.Any]) -> tuple[t.Any, ...]:
    return ("extension", entry["key"])


def _command_key(entry: dict[str, t.Any]) -> tuple[t.Any, ...]:
    return ("command", entry["path"])


_KEY_FNS: dict[str, t.Callable[[dict[str, t.Any]], tuple[t.Any, ...]]] = {
    "routes": _route_key,
    "blueprints": _blueprint_key,
    "error_handlers": _error_handler_key,
    "templates": _template_key,
    "static": _static_key,
    "extensions": _extension_key,
    "commands": _command_key,
}


def _scope_label(scope: str | None) -> str:
    return scope if scope is not None else "<app>"


_LABELS: dict[str, t.Callable[[dict[str, t.Any]], str]] = {
    "routes": lambda e: (
        f"route {','.join(e['methods'] or []) or '*'} {e['rule']}"
        f" -> {e['endpoint']}"
    ),
    "blueprints": lambda e: f"blueprint {e['name']}",
    "hooks": lambda e: (
        f"{e['group']} hook '{e['module']}.{e['qualname']}'"
        f" in scope '{_scope_label(e['scope'])}'"
    ),
    "error_handlers": lambda e: (
        f"error handler for {e['exception']['qualname']}"
        f" (code {e['code']}) in scope '{_scope_label(e['scope'])}'"
    ),
    "templates": lambda e: (
        f"template loader {e['loader_type']}"
        f" in scope '{_scope_label(e['scope'])}'"
    ),
    "static": lambda e: f"static files in scope '{_scope_label(e['scope'])}'",
    "extensions": lambda e: f"extension '{e['key']}'",
    "commands": lambda e: f"command '{e['path']}'",
}

# Fields used to identify entries within each section, so ordinal and
# identity metadata are not reported as value changes.
_ENTRY_FIELDS: dict[str, tuple[str, ...]] = {
    "routes": (
        "endpoint",
        "rule",
        "methods",
        "host",
        "subdomain",
        "defaults",
        "arguments",
        "redirect_to",
        "alias",
        "build_only",
        "websocket",
        "merge_slashes",
        "provide_automatic_options",
        "view",
    ),
    "blueprints": (
        "name",
        "parent",
        "import_name",
        "blueprint_type",
        "url_prefix",
        "subdomain",
        "url_defaults",
        "static_folder",
        "static_url_path",
        "has_static_folder",
        "template_folder",
        "root_path",
        "cli_group",
    ),
    "error_handlers": ("scope", "code", "exception", "handlers"),
    "templates": ("scope", "loader_type", "paths"),
    "static": ("scope", "has_static_folder", "static_folder", "static_url_path"),
    "extensions": ("key", "value_type"),
    "commands": (
        "path",
        "name",
        "parent",
        "scope",
        "kind",
        "command_type",
        "callback",
        "params",
    ),
}
_HOOK_FIELDS = (
    "scope",
    "kind",
    "name",
    "module",
    "qualname",
    "file",
    "line",
    "callable_type",
    "function",
)
_ENTRY_FIELDS["hooks"] = _HOOK_FIELDS

_DIFF_SECTIONS = (*_LIST_SECTIONS[:2], "hooks", *_LIST_SECTIONS[2:])


def _index_entries(
    entries: list[dict[str, t.Any]],
    key_fn: t.Callable[[dict[str, t.Any]], tuple[t.Any, ...]],
) -> dict[tuple[t.Any, ...], list[dict[str, t.Any]]]:
    indexed: dict[tuple[t.Any, ...], list[dict[str, t.Any]]] = defaultdict(list)

    for entry in entries:
        indexed[key_fn(entry)].append(entry)

    return indexed


def _field_changes(
    old: dict[str, t.Any], new: dict[str, t.Any], fields: tuple[str, ...]
) -> dict[str, dict[str, t.Any]]:
    changes = {}

    for field in fields:
        old_value = _strip_seq(old.get(field))
        new_value = _strip_seq(new.get(field))

        if old_value != new_value:
            changes[field] = {"from": old_value, "to": new_value}

    return changes


def _diff_section(
    section: str,
    old_entries: list[dict[str, t.Any]],
    new_entries: list[dict[str, t.Any]],
    key_fn: t.Callable[[dict[str, t.Any]], tuple[t.Any, ...]],
) -> dict[str, list[t.Any]]:
    old_index = _index_entries(old_entries, key_fn)
    new_index = _index_entries(new_entries, key_fn)
    label_fn = _LABELS[section]
    added: list[t.Any] = []
    removed: list[t.Any] = []
    changed: list[t.Any] = []

    for key in sorted(set(old_index) | set(new_index)):
        old_group = old_index.get(key, [])
        new_group = new_index.get(key, [])
        pair_count = min(len(old_group), len(new_group))

        for entry in new_group[pair_count:]:
            added.append({"key": list(key), "label": label_fn(entry), "entry": entry})

        for entry in old_group[pair_count:]:
            removed.append({"key": list(key), "label": label_fn(entry), "entry": entry})

        fields = _ENTRY_FIELDS[section]

        for old_entry, new_entry in zip(old_group, new_group, strict=False):
            changes = _field_changes(old_entry, new_entry, fields)

            if changes:
                changed.append(
                    {
                        "key": list(key),
                        "label": label_fn(new_entry),
                        "changes": changes,
                    }
                )

    return {"added": added, "removed": removed, "changed": changed}


def _diff_hooks(
    old_hooks: dict[str, list[dict[str, t.Any]]],
    new_hooks: dict[str, list[dict[str, t.Any]]],
) -> dict[str, list[t.Any]]:
    added: list[t.Any] = []
    removed: list[t.Any] = []
    changed: list[t.Any] = []

    for group_name in _HOOK_GROUPS:
        old_entries = [
            dict(entry, group=group_name) for entry in old_hooks[group_name]
        ]
        new_entries = [
            dict(entry, group=group_name) for entry in new_hooks[group_name]
        ]
        result = _diff_section(
            "hooks", old_entries, new_entries, _hook_key
        )
        added.extend(result["added"])
        removed.extend(result["removed"])
        changed.extend(result["changed"])

    added.sort(key=lambda item: json.dumps(item["key"], sort_keys=True))
    removed.sort(key=lambda item: json.dumps(item["key"], sort_keys=True))
    changed.sort(key=lambda item: json.dumps(item["key"], sort_keys=True))

    return {"added": added, "removed": removed, "changed": changed}


def _diff_meta(
    old_snapshot: dict[str, t.Any], new_snapshot: dict[str, t.Any]
) -> list[dict[str, t.Any]]:
    old_meta = {
        "flask_version": old_snapshot["meta"]["flask_version"],
        **{
            key: old_snapshot["app"][key]
            for key in (
                "root_path",
                "instance_path",
                "host_matching",
                "subdomain_matching",
            )
        },
    }
    new_meta = {
        "flask_version": new_snapshot["meta"]["flask_version"],
        **{
            key: new_snapshot["app"][key]
            for key in (
                "root_path",
                "instance_path",
                "host_matching",
                "subdomain_matching",
            )
        },
    }
    changes = _field_changes(old_meta, new_meta, tuple(old_meta))

    if not changes:
        return []

    return [{"key": ["meta"], "label": "application metadata", "changes": changes}]


def diff_snapshots(
    before: dict[str, t.Any], after: dict[str, t.Any]
) -> dict[str, t.Any]:
    """Compare two snapshots produced by :func:`take_snapshot`.

    Registrations are classified as ``added``, ``removed`` or
    ``changed``; every changed item names the fields that differ. The
    return value is a JSON-serializable structure. Use
    :func:`format_snapshot_diff` for a readable summary.

    :raises SnapshotError: if either snapshot is malformed, uses an
        unsupported schema version, or the snapshots belong to
        different applications.

    .. versionadded:: 3.2
    """
    old = _validate_snapshot(before, "before snapshot")
    new = _validate_snapshot(after, "after snapshot")

    if (
        old["app"]["name"] != new["app"]["name"]
        or old["app"]["import_name"] != new["app"]["import_name"]
    ):
        raise SnapshotError(
            "Snapshots belong to different applications:"
            f" {old['app']['name']!r} ({old['app']['import_name']!r})"
            f" vs {new['app']['name']!r} ({new['app']['import_name']!r})."
        )

    sections = (*_DIFF_SECTIONS, "meta")
    added: dict[str, list[t.Any]] = {section: [] for section in sections}
    removed: dict[str, list[t.Any]] = {section: [] for section in sections}
    changed: dict[str, list[t.Any]] = {section: [] for section in sections}

    for section in _DIFF_SECTIONS:
        if section == "hooks":
            result = _diff_hooks(old["hooks"], new["hooks"])
        else:
            result = _diff_section(
                section,
                old[section],
                new[section],
                _KEY_FNS[section],
            )

        added[section] = result["added"]
        removed[section] = result["removed"]
        changed[section] = result["changed"]

    changed["meta"] = _diff_meta(old, new)

    counts: dict[str, t.Any] = {
        "added": sum(len(items) for items in added.values()),
        "removed": sum(len(items) for items in removed.values()),
        "changed": sum(len(items) for items in changed.values()),
    }
    counts["by_section"] = {
        section: {
            "added": len(added[section]),
            "removed": len(removed[section]),
            "changed": len(changed[section]),
        }
        for section in (*_DIFF_SECTIONS, "meta")
    }

    return {
        "meta": {"schema_version": SNAPSHOT_SCHEMA_VERSION},
        "app": {
            "name": new["app"]["name"],
            "import_name": new["app"]["import_name"],
        },
        "identical": counts["added"]
        == counts["removed"]
        == counts["changed"]
        == 0,
        "summary": counts,
        "added": added,
        "removed": removed,
        "changed": changed,
    }


# ---------------------------------------------------------------------------
# Readable output
# ---------------------------------------------------------------------------


def format_snapshot_diff(diff: dict[str, t.Any]) -> str:
    """Render the result of :func:`diff_snapshots` as a readable
    summary.

    .. versionadded:: 3.2
    """
    lines = [
        "Registration snapshot diff for "
        f"{diff['app']['name']!r} ({diff['app']['import_name']})",
        "",
        "Summary: {added} added, {removed} removed, {changed} changed".format(
            **diff["summary"]
        ),
    ]

    headings = (
        ("added", "Added", "+"),
        ("removed", "Removed", "-"),
        ("changed", "Changed", "~"),
    )

    for kind, heading, marker in headings:
        sections = diff[kind]
        items = [
            (section, item)
            for section in (*_DIFF_SECTIONS, "meta")
            for item in sections[section]
        ]

        if not items:
            continue

        lines.extend(("", heading + ":"))

        for _section, item in items:
            if kind == "changed":
                field_names = ", ".join(item["changes"])
                lines.append(f"  {marker} {item['label']} ({field_names})")
            else:
                lines.append(f"  {marker} {item['label']}")

    if diff["identical"]:
        lines.extend(("", "No differences."))

    return "\n".join(lines) + "\n"
