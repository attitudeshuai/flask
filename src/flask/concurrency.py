from __future__ import annotations

import math
import threading
import typing as t
from collections import deque
from dataclasses import dataclass
from datetime import timedelta

from werkzeug.exceptions import default_exceptions

if t.TYPE_CHECKING:  # pragma: no cover
    from .wrappers import Request

#: Attribute set on a view function by :meth:`Scaffold.concurrent`. The
#: attached :class:`ConcurrencyQuota` declares the endpoint-level quota.
CONCURRENCY_QUOTA_ATTRIBUTE = "_flask_concurrency_quota"

#: Reasons a request was rejected by a quota. Used to build the rejection
#: response and its explanatory message.
REJECTED_IN_FLIGHT = "in_flight"
REJECTED_QUEUE_FULL = "queue_full"
REJECTED_WAIT_TIMEOUT = "wait_timeout"


def _check_int(value: t.Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"'{name}' must be an integer, got {value!r}.")

    return value


def _check_seconds(value: timedelta | int | float, name: str) -> float:
    if isinstance(value, timedelta):
        value = value.total_seconds()
    elif isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(
            f"'{name}' must be a number of seconds or a timedelta, got {value!r}."
        )

    if not math.isfinite(value) or value < 0:
        raise ValueError(f"'{name}' must be a non-negative number, got {value!r}.")

    return float(value)


@dataclass(frozen=True, slots=True)
class ConcurrencyQuota:
    """A normalized declaration of the maximum number of requests that may be
    handled simultaneously within the same quota. Instances are created
    through :meth:`configure`, which accepts and validates the public
    declaration arguments (seconds as numbers or :class:`~datetime.timedelta`).

    A quota is declared on an application, blueprint, or individual endpoint.
    When the number of in-flight requests reaches :attr:`max_in_flight`,
    further requests either wait for a slot or are rejected.

    :param max_in_flight: Maximum number of requests handled at the same time.
        Must be at least ``1``.
    :param wait: Wait for a slot when the limit is reached. If ``False``,
        requests are rejected immediately.
    :param wait_timeout: Maximum time in seconds a request may wait. ``0``
        rejects immediately even when ``wait`` is true.
    :param max_waiting: Maximum number of requests that may wait at the same
        time. Requests arriving when the queue is full are rejected.
    :param reject_status: HTTP status code used for rejected requests.
    :param retry_after: Time in seconds suggested to the client through the
        ``Retry-After`` response header and body.

    .. versionadded:: 3.2
    """

    max_in_flight: int
    wait: bool = True
    wait_timeout: float = 30.0
    max_waiting: int = 0
    reject_status: int = 503
    retry_after: int = 1

    def __post_init__(self) -> None:
        max_in_flight = _check_int(self.max_in_flight, "max_in_flight")

        if max_in_flight < 1:
            raise ValueError(
                f"'max_in_flight' must be at least 1, got {max_in_flight}."
            )

        if not isinstance(self.wait, bool):
            raise ValueError(f"'wait' must be a boolean, got {self.wait!r}.")

        wait_timeout = _check_seconds(self.wait_timeout, "wait_timeout")
        max_waiting = _check_int(self.max_waiting, "max_waiting")

        if max_waiting < 0:
            raise ValueError(f"'max_waiting' must be at least 0, got {max_waiting}.")

        reject_status = _check_int(self.reject_status, "reject_status")

        if reject_status not in default_exceptions or not 400 <= reject_status < 600:
            raise ValueError(
                "'reject_status' must be a recognized HTTP error status code"
                f" between 400 and 599, got {reject_status}."
            )

        retry_after = _check_int(self.retry_after, "retry_after")

        if retry_after < 1:
            raise ValueError(f"'retry_after' must be at least 1, got {retry_after}.")

        object.__setattr__(self, "max_in_flight", max_in_flight)
        object.__setattr__(self, "wait_timeout", wait_timeout)
        object.__setattr__(self, "max_waiting", max_waiting)
        object.__setattr__(self, "reject_status", reject_status)
        object.__setattr__(self, "retry_after", retry_after)

    @classmethod
    def configure(
        cls,
        max_in_flight: int,
        *,
        wait: bool = True,
        wait_timeout: timedelta | int | float = 30.0,
        max_waiting: int | None = None,
        reject_status: int = 503,
        retry_after: timedelta | int | float | None = None,
    ) -> ConcurrencyQuota:
        """Validate declaration arguments and build a normalized quota.

        :param wait_timeout: Maximum waiting time as seconds or a timedelta.
        :param max_waiting: Maximum queue length. Defaults to
            ``max_in_flight``.
        :param retry_after: Suggested retry time as seconds or a timedelta.
            Defaults to ``wait_timeout`` (at least ``1``) when waiting,
            otherwise ``1``.
        """
        timeout = _check_seconds(wait_timeout, "wait_timeout")

        if max_waiting is None:
            waiting = max_in_flight
        else:
            waiting = max_waiting

        if retry_after is None:
            after = 1 if not wait or timeout == 0 else math.ceil(timeout)
        else:
            after = math.ceil(_check_seconds(retry_after, "retry_after"))

        return cls(
            max_in_flight=max_in_flight,
            wait=wait,
            wait_timeout=timeout,
            max_waiting=waiting,
            reject_status=reject_status,
            retry_after=after,
        )


class _Waiter:
    """A request waiting in a quota's FIFO queue."""

    __slots__ = ("event", "granted")

    def __init__(self) -> None:
        #: Set by the releasing request when the slot is handed over.
        self.event = threading.Event()
        #: Whether ownership of a slot was granted.
        self.granted = False


class _Grant:
    """Ownership of one in-flight slot. Releasing is idempotent so that the
    various request exit paths cannot release a slot twice.
    """

    __slots__ = ("_state", "_released", "_lock")

    def __init__(self, state: _QuotaState) -> None:
        self._state = state
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return

            self._released = True

        self._state._release()


@dataclass(frozen=True, slots=True)
class _Rejection:
    """The result of entering a quota when no slot was acquired."""

    state: _QuotaState
    reason: str


class _QuotaState:
    """The runtime state of one quota: its slots, FIFO waiting queue, and
    counters. All state changes happen under :attr:`_lock`.
    """

    def __init__(self, key: tuple[str, str], quota: ConcurrencyQuota) -> None:
        self.key = key
        self.quota = quota
        self._lock = threading.Lock()
        self._waiters: deque[_Waiter] = deque()
        #: Requests currently holding a slot.
        self.in_flight = 0
        #: Total requests rejected (immediately, queue full, or timeout).
        self.rejected = 0

    def enter(self) -> _Grant | _Rejection:
        quota = self.quota

        with self._lock:
            if self.in_flight < quota.max_in_flight:
                self.in_flight += 1
                return _Grant(self)

            if not quota.wait or quota.wait_timeout <= 0:
                self.rejected += 1
                return _Rejection(self, REJECTED_IN_FLIGHT)

            if len(self._waiters) >= quota.max_waiting:
                self.rejected += 1
                return _Rejection(self, REJECTED_QUEUE_FULL)

            waiter = _Waiter()
            self._waiters.append(waiter)

        granted = waiter.event.wait(quota.wait_timeout)

        if granted:
            # The slot was handed over while the lock was held, so the
            # in-flight count already includes this request.
            return _Grant(self)

        with self._lock:
            if waiter.granted:
                # A release handed the slot over just as the wait timed out.
                # The request must not run the view, so pass the slot to the
                # next waiter or free it instead of holding it as a ghost.
                self._hand_over_or_release_locked()
            else:
                # The waiter is only ever removed by a release, which also
                # marks it as granted, so it is still queued here.
                self._waiters.remove(waiter)

            self.rejected += 1
            return _Rejection(self, REJECTED_WAIT_TIMEOUT)

    def _hand_over_or_release_locked(self) -> None:
        """Hand the slot to the longest waiting request, keeping the in-flight
        count constant, or free the slot if nobody is waiting. The caller must
        hold :attr:`_lock`."""
        while self._waiters:
            waiter = self._waiters.popleft()
            waiter.granted = True
            waiter.event.set()
            return

        self.in_flight -= 1

    def _release(self) -> None:
        """Release a slot when a request leaves the quota."""
        with self._lock:
            self._hand_over_or_release_locked()

    def snapshot(self) -> dict[str, t.Any]:
        with self._lock:
            return {
                "limit": self.quota.max_in_flight,
                "wait": self.quota.wait,
                "wait_timeout": self.quota.wait_timeout,
                "max_waiting": self.quota.max_waiting,
                "reject_status": self.quota.reject_status,
                "retry_after": self.quota.retry_after,
                "in_flight": self.in_flight,
                "waiting": len(self._waiters),
                "rejected": self.rejected,
            }


def _quota_display_name(key: tuple[str, str]) -> str:
    scope, name = key

    if scope == "app":
        return "app"

    return f"{scope}:{name}"


class ConcurrencyManager:
    """Owns the runtime state of every quota declared on an application.

    A manager is created per :class:`~flask.Flask` instance and is never
    shared between processes or application instances.
    """

    def __init__(self, app: t.Any) -> None:
        self._app = app
        self._states: dict[tuple[str, str], _QuotaState] = {}
        self._build()

    def _build(self) -> None:
        app_quota: ConcurrencyQuota | None = self._app._concurrency_limit

        if app_quota is not None:
            self._states[("app", "")] = _QuotaState(("app", ""), app_quota)

        for name, quota in self._app._blueprint_concurrency_limits.items():
            self._states[("blueprint", name)] = _QuotaState(("blueprint", name), quota)

        for endpoint, view_func in self._app.view_functions.items():
            quota = getattr(view_func, CONCURRENCY_QUOTA_ATTRIBUTE, None)

            if quota is not None:
                self._states[("endpoint", endpoint)] = _QuotaState(
                    ("endpoint", endpoint), quota
                )

    def _resolve(self, request: Request) -> _QuotaState | None:
        """Resolve the most specific quota for a request: endpoint, then the
        innermost blueprint, then the application. ``request.blueprints`` is
        ordered innermost first."""
        endpoint = request.endpoint

        if endpoint is not None:
            state = self._states.get(("endpoint", endpoint))

            if state is not None:
                return state

            for name in request.blueprints:
                state = self._states.get(("blueprint", name))

                if state is not None:
                    return state

        return self._states.get(("app", ""))

    def enter(self, request: Request) -> _Grant | _Rejection | None:
        """Acquire a slot for the request. Returns ``None`` when the request is
        not governed by any quota, a :class:`_Grant` on success, or a
        :class:`_Rejection` when the request must not be handled."""
        state = self._resolve(request)

        if state is None:
            return None

        return state.enter()

    def has_quotas(self) -> bool:
        return bool(self._states)

    def stats(self) -> dict[str, dict[str, t.Any]]:
        """Return a read-only snapshot of every quota's configuration and
        current occupancy, keyed by a display name such as ``"app"``,
        ``"blueprint:admin"``, or ``"endpoint:admin.export"``."""
        return {
            _quota_display_name(key): {
                "scope": key[0] if key[0] != "app" else "application",
                "name": "" if key[0] == "app" else key[1],
                **state.snapshot(),
            }
            for key, state in self._states.items()
        }
