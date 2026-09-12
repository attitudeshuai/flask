from __future__ import annotations

import hashlib
import threading
import time
import typing as t
from collections import OrderedDict
from collections.abc import Iterable
from datetime import timedelta

if t.TYPE_CHECKING:  # pragma: no cover
    from .app import Flask
    from .ctx import AppContext
    from .wrappers import Request
    from .wrappers import Response

#: HTTP methods that are safe to cache. Only idempotent methods may be
#: configured, responses to non-idempotent requests are never stored.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_EMPTY_FROZENSET: frozenset[str] = frozenset()


def _canonical_header_name(name: str) -> str:
    """Canonicalize an HTTP header name, e.g. ``accept-encoding`` becomes
    ``Accept-Encoding``. Header lookup on :class:`~werkzeug.Headers` is case
    insensitive, but canonical names keep cache keys stable.
    """
    return "-".join(part.capitalize() for part in name.strip().split("-"))


def _parse_vary(response: Response) -> frozenset[str] | None:
    """Parse the ``Vary`` response header into a set of canonical request
    header names. Return ``None`` if the header contains the ``*`` wildcard,
    which means the response varies on something that cannot be used as a key.
    """
    names: set[str] = set()

    for value in response.headers.getlist("Vary"):
        for part in value.split(","):
            token = part.strip()

            if not token:
                continue

            if token == "*":
                return None

            names.add(_canonical_header_name(token))

    return frozenset(names)


class _Snapshot:
    """An immutable, context-independent copy of a cached response."""

    __slots__ = ("status_code", "headers", "body", "vary", "values")

    def __init__(
        self,
        status_code: int,
        headers: tuple[tuple[str, str], ...],
        body: bytes,
        vary: frozenset[str],
        values: tuple[tuple[str, str | None], ...],
    ) -> None:
        self.status_code = status_code
        self.headers = headers
        self.body = body
        #: Effective request header names that participate in the key: the
        #: configured key headers union the response-declared ``Vary`` names.
        self.vary = vary
        #: Values of the effective request headers for the request that created
        #: the snapshot, used to match subsequent requests.
        self.values = values


class _Entry:
    __slots__ = ("snapshot", "base_key", "path", "endpoint", "expires_at")

    def __init__(
        self,
        snapshot: _Snapshot,
        base_key: str,
        path: str,
        endpoint: str | None,
        expires_at: float | None,
    ) -> None:
        self.snapshot = snapshot
        self.base_key = base_key
        self.path = path
        self.endpoint = endpoint
        self.expires_at = expires_at


class _Flight:
    """A single in-progress execution for a cache key. Concurrent requests
    that produce the same signature wait on :attr:`event` and reuse the
    leader's snapshot instead of executing the view themselves.
    """

    __slots__ = ("key", "event")

    def __init__(self, key: tuple[str, str]) -> None:
        self.key = key
        self.event = threading.Event()


class ResponseCache:
    """An in-process, application-level cache for finalized responses.

    The cache is opt-in through :data:`~flask.Flask.config` and is not created
    unless ``RESPONSE_CACHE_ENABLED`` is true. It only stores fully materialized
    successful responses for idempotent methods; responses that set cookies,
    stream, pass through files, use sessions, or declare ``Vary: *`` are never
    stored.

    All public methods are safe to call concurrently from multiple threads.

    :param app: The application to read configuration from.
    """

    def __init__(self, app: Flask) -> None:
        self._app = app
        config = app.config

        self._ttl: float | None = self._validate_ttl(config["RESPONSE_CACHE_TTL"])
        self._max_entries: int = self._validate_max_entries(
            config["RESPONSE_CACHE_MAX_ENTRIES"]
        )
        self._methods: frozenset[str] = self._validate_methods(
            config["RESPONSE_CACHE_METHODS"]
        )
        self._headers: tuple[str, ...] = tuple(
            sorted(
                {
                    _canonical_header_name(n)
                    for n in self._validate_str_list(
                        config["RESPONSE_CACHE_HEADERS"], "RESPONSE_CACHE_HEADERS"
                    )
                }
            )
        )
        self._headers_set = frozenset(self._headers)
        self._endpoints = frozenset(
            self._validate_str_list(
                config.get("RESPONSE_CACHE_ENDPOINTS") or (),
                "RESPONSE_CACHE_ENDPOINTS",
            )
        )
        self._exclude_endpoints = frozenset(
            self._validate_str_list(
                config.get("RESPONSE_CACHE_EXCLUDE_ENDPOINTS") or (),
                "RESPONSE_CACHE_EXCLUDE_ENDPOINTS",
            )
        )
        self._paths = tuple(
            self._validate_paths(
                config.get("RESPONSE_CACHE_PATHS") or (), "RESPONSE_CACHE_PATHS"
            )
        )
        self._exclude_paths = tuple(
            self._validate_paths(
                config.get("RESPONSE_CACHE_EXCLUDE_PATHS") or (),
                "RESPONSE_CACHE_EXCLUDE_PATHS",
            )
        )
        self._status_codes = frozenset(
            self._validate_status_codes(config["RESPONSE_CACHE_STATUS_CODES"])
        )
        self._session_cookie_name = config["SESSION_COOKIE_NAME"]

        self._lock = threading.RLock()
        #: storage key -> entry, most recently used last.
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        #: base key -> set of storage keys (one per ``Vary`` variant).
        self._index: dict[str, set[str]] = {}
        #: base key -> response-declared Vary names learned so far, used to
        #: coalesce concurrent requests before the first response exists.
        self._learned: dict[str, frozenset[str]] = {}
        #: in-progress executions keyed by (base key, signature).
        self._flights: dict[tuple[str, str], _Flight] = {}

        self._hits = 0
        self._misses = 0

    # ------------------------------------------------------------------
    # Configuration validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_ttl(value: t.Any) -> float | None:
        if value is None:
            return None

        if isinstance(value, bool) or not isinstance(value, int | float | timedelta):
            raise ValueError(
                "'RESPONSE_CACHE_TTL' must be a number of seconds,"
                " a timedelta, or None."
            )

        if isinstance(value, timedelta):
            seconds = value.total_seconds()
        else:
            seconds = float(value)

        if seconds <= 0:
            raise ValueError("'RESPONSE_CACHE_TTL' must be greater than 0 or None.")

        return seconds

    @staticmethod
    def _validate_max_entries(value: t.Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("'RESPONSE_CACHE_MAX_ENTRIES' must be an integer.")

        if value < 1:
            raise ValueError("'RESPONSE_CACHE_MAX_ENTRIES' must be at least 1.")

        return value

    @staticmethod
    def _validate_methods(value: t.Any) -> frozenset[str]:
        if isinstance(value, str) or not isinstance(value, Iterable):
            raise ValueError("'RESPONSE_CACHE_METHODS' must be an iterable.")

        methods: set[str] = set()

        for item in value:
            if not isinstance(item, str) or not item:
                raise ValueError(
                    "'RESPONSE_CACHE_METHODS' must contain non-empty strings."
                )

            method = item.upper()

            if method not in SAFE_METHODS:
                raise ValueError(
                    f"Method {item!r} in 'RESPONSE_CACHE_METHODS' is not"
                    " idempotent and cannot be cached."
                )

            methods.add(method)

        if not methods:
            raise ValueError("'RESPONSE_CACHE_METHODS' must not be empty.")

        return frozenset(methods)

    @staticmethod
    def _validate_str_list(value: t.Any, name: str) -> list[str]:
        if isinstance(value, str) or not isinstance(value, Iterable):
            raise ValueError(f"'{name}' must be an iterable of strings.")

        items = list(value)

        for item in items:
            if not isinstance(item, str) or not item:
                raise ValueError(f"'{name}' must contain non-empty strings.")

        return items

    @staticmethod
    def _validate_paths(value: t.Any, name: str) -> list[str]:
        items = ResponseCache._validate_str_list(value, name)

        for item in items:
            if not item.startswith("/"):
                raise ValueError(f"Path {item!r} in '{name}' must start with '/'.")

        return items

    @staticmethod
    def _validate_status_codes(value: t.Any) -> list[int]:
        if isinstance(value, str) or not isinstance(value, Iterable):
            raise ValueError(
                "'RESPONSE_CACHE_STATUS_CODES' must be an iterable of integers."
            )

        codes = list(value)

        for code in codes:
            if isinstance(code, bool) or not isinstance(code, int):
                raise ValueError("'RESPONSE_CACHE_STATUS_CODES' must contain integers.")

            if not 100 <= code <= 599:
                raise ValueError(
                    f"Status code {code} in 'RESPONSE_CACHE_STATUS_CODES' is"
                    " not a valid HTTP status code."
                )

        if not codes:
            raise ValueError("'RESPONSE_CACHE_STATUS_CODES' must not be empty.")

        return codes

    # ------------------------------------------------------------------
    # Key construction
    # ------------------------------------------------------------------

    def _base_key(self, req: Request) -> str:
        """Build the Vary-independent part of the key from the request
        method, path, query string and configured key headers.
        """
        h = hashlib.sha256()
        h.update(req.method.upper().encode("ascii"))
        h.update(b"\x00")
        h.update(req.path.encode("utf-8"))
        h.update(b"\x00")
        h.update(bytes(req.query_string))
        h.update(b"\x00")

        for name in self._headers:
            h.update(name.encode("ascii"))
            h.update(b"=")
            h.update((req.headers.get(name) or "").encode("utf-8", "replace"))
            h.update(b"\x00")

        return h.hexdigest()

    def _variant_signature(self, req: Request, names: frozenset[str]) -> str:
        """Hash the values of the given request header names. Used as the
        variant part of the storage key and the coalescing signature.
        """
        h = hashlib.sha256()

        for name in sorted(names):
            h.update(name.encode("ascii"))
            h.update(b"=")
            h.update((req.headers.get(name) or "").encode("utf-8", "replace"))
            h.update(b"\x00")

        return h.hexdigest()

    def _storage_key(self, req: Request, base: str, names: frozenset[str]) -> str:
        return f"{base}:{self._variant_signature(req, names)}"

    # ------------------------------------------------------------------
    # Eligibility
    # ------------------------------------------------------------------

    def _eligible(self, ctx: AppContext) -> bool:
        """Whether the request may participate in the cache at all."""
        req = ctx.request

        # Unrouted requests and routing exceptions (404/405 redirects) are
        # dispatched through exception handling and are not cached.
        if req.routing_exception is not None or req.endpoint is None:
            return False

        if req.method.upper() not in self._methods:
            return False

        # A request carrying a session may depend on per-user state; never
        # serve it a shared cached response or cache its response.
        if req.cookies.get(self._session_cookie_name) is not None:
            return False

        endpoint = req.endpoint

        if self._endpoints and endpoint not in self._endpoints:
            return False

        if endpoint in self._exclude_endpoints:
            return False

        path = req.path

        if self._paths and not any(path.startswith(p) for p in self._paths):
            return False

        if any(path.startswith(p) for p in self._exclude_paths):
            return False

        return True

    # ------------------------------------------------------------------
    # Request pipeline integration
    # ------------------------------------------------------------------

    def acquire(self, ctx: AppContext) -> tuple[Response | None, _Flight | None]:
        """Look up a cached response for the current request.

        Returns ``(response, None)`` when a cached response can be used (this
        includes concurrent requests that join an in-progress execution),
        ``(None, flight)`` when the caller is the leader and must execute the
        view followed by :meth:`commit`, or ``(None, None)`` when the request
        is not eligible for caching.
        """
        if not self._eligible(ctx):
            return None, None

        req = ctx.request
        base = self._base_key(req)

        while True:
            with self._lock:
                snapshot = self._find_locked(req, base)

                if snapshot is not None:
                    self._hits += 1
                    return self._reconstruct(snapshot), None

                names = self._headers_set | self._learned.get(base, _EMPTY_FROZENSET)
                signature = self._variant_signature(req, names)
                key = (base, signature)
                flight = self._flights.get(key)

                if flight is None:
                    flight = _Flight(key)
                    self._flights[key] = flight
                    self._misses += 1
                    return None, flight

            # Another request with the same key is executing the view. Wait
            # for it and then look the snapshot up again.
            flight.event.wait()

    def commit(self, flight: _Flight, ctx: AppContext, response: Response) -> None:
        """Store the leader's finalized response if it is cacheable and
        release all requests waiting on the flight. A non-cacheable response
        releases the waiters without storing anything, so they execute the
        view themselves.
        """
        try:
            snapshot = self._make_snapshot(ctx, response)
        except Exception:
            self._app.logger.debug(
                "Response cache failed to snapshot response", exc_info=True
            )
            snapshot = None

        with self._lock:
            self._flights.pop(flight.key, None)

            if snapshot is not None:
                req = ctx.request
                base = self._base_key(req)
                key = self._storage_key(req, base, snapshot.vary)
                self._store_locked(key, base, snapshot, req)

            flight.event.set()

    def abort(self, flight: _Flight) -> None:
        """Release a flight without storing a response, such as when the
        leader's request failed before a response was finalized.
        """
        with self._lock:
            self._flights.pop(flight.key, None)

        flight.event.set()

    def _make_snapshot(self, ctx: AppContext, response: Response) -> _Snapshot | None:
        """Return an immutable snapshot of the response, or ``None`` if it
        must not be cached.
        """
        if response.status_code not in self._status_codes:
            return None

        # Streaming/generator responses would be consumed by reading the
        # body, and direct passthrough responses wrap files or WSGI callables.
        if response.direct_passthrough or response.is_streamed:
            return None

        # Any cookie writing makes the response client-specific.
        if "Set-Cookie" in response.headers:
            return None

        # A modified server-side or cookie session is user-specific state.
        session = ctx._session

        if (
            session is not None
            and not self._app.session_interface.is_null_session(session)
            and getattr(session, "modified", False)
        ):
            return None

        vary = _parse_vary(response)

        if vary is None:
            # Vary: * -- the response cannot be keyed safely.
            return None

        effective = self._headers_set | vary
        req = ctx.request
        values = tuple(sorted((name, req.headers.get(name)) for name in effective))

        # The response is known to be a fully materialized non-passthrough
        # response here, so reading the data does not consume a generator.
        body = response.get_data()
        headers = tuple(response.headers)

        return _Snapshot(response.status_code, headers, body, effective, values)

    def _reconstruct(self, snapshot: _Snapshot) -> Response:
        """Create a fresh response object from a snapshot. Each caller gets
        its own object so in-place modifications by downstream handlers do
        not leak back into the cache or between requests.
        """
        return self._app.response_class(
            snapshot.body,
            status=snapshot.status_code,
            headers=list(snapshot.headers),
        )

    # ------------------------------------------------------------------
    # Storage operations (call with self._lock held)
    # ------------------------------------------------------------------

    def _find_locked(self, req: Request, base: str) -> _Snapshot | None:
        keys = self._index.get(base)

        if not keys:
            return None

        now = time.monotonic()
        dead: list[str] = []
        match: _Snapshot | None = None

        for key in keys:
            entry = self._entries.get(key)

            if entry is None:
                dead.append(key)
                continue

            if entry.expires_at is not None and entry.expires_at <= now:
                dead.append(key)
                continue

            values = entry.snapshot.values

            if all(req.headers.get(name) == value for name, value in values):
                match = entry.snapshot
                self._entries.move_to_end(key)
                break

        self._remove_keys_locked(dead)
        return match

    def _store_locked(
        self,
        key: str,
        base: str,
        snapshot: _Snapshot,
        req: Request,
    ) -> None:
        self._entries[key] = _Entry(
            snapshot,
            base,
            req.path,
            req.endpoint,
            time.monotonic() + self._ttl if self._ttl is not None else None,
        )
        self._entries.move_to_end(key)
        self._index.setdefault(base, set()).add(key)
        self._learned[base] = self._learned.get(base, _EMPTY_FROZENSET) | snapshot.vary

        if len(self._entries) > self._max_entries:
            # Drop expired entries first so live entries are not evicted
            # merely because of stale data.
            self._purge_expired_locked()

        while len(self._entries) > self._max_entries:
            evicted_key, _ = self._entries.popitem(last=False)
            self._remove_keys_locked([evicted_key])

    def _remove_keys_locked(self, keys: Iterable[str]) -> None:
        for key in keys:
            entry = self._entries.pop(key, None)

            if entry is None:
                continue

            bases = self._index.get(entry.base_key)

            if bases is not None:
                bases.discard(key)

                if not bases:
                    del self._index[entry.base_key]
                    self._learned.pop(entry.base_key, None)

    def _purge_expired_locked(self) -> int:
        now = time.monotonic()
        expired = [
            key
            for key, entry in self._entries.items()
            if entry.expires_at is not None and entry.expires_at <= now
        ]
        self._remove_keys_locked(expired)
        return len(expired)

    # ------------------------------------------------------------------
    # Management API
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        """Return cache statistics: number of hits, misses and the current
        number of live entries.
        """
        with self._lock:
            self._purge_expired_locked()
            return {
                "hits": self._hits,
                "misses": self._misses,
                "entries": len(self._entries),
                "max_entries": self._max_entries,
            }

    def clear(self, endpoint: str | None = None, prefix: str | None = None) -> int:
        """Remove cached entries.

        With no arguments all entries are removed. When ``endpoint`` is
        given, only entries cached for that endpoint name are removed. When
        ``prefix`` is given, only entries whose request path starts with it
        are removed. Both filters may be combined. Returns the number of
        removed entries.
        """
        if endpoint is None and prefix is None:
            with self._lock:
                count = len(self._entries)
                self._entries.clear()
                self._index.clear()
                self._learned.clear()
                return count

        with self._lock:
            matched = [
                key
                for key, entry in self._entries.items()
                if (endpoint is None or entry.endpoint == endpoint)
                and (prefix is None or entry.path.startswith(prefix))
            ]
            self._remove_keys_locked(matched)
            return len(matched)
