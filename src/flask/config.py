from __future__ import annotations

import collections.abc as cabc
import contextvars
import errno
import json
import os
import types
import typing as t
import weakref
from contextlib import contextmanager

from werkzeug.utils import import_string

from .globals import _cv_app

if t.TYPE_CHECKING:
    import typing_extensions as te

    from .ctx import AppContext
    from .sansio.app import App


T = t.TypeVar("T")


class ConfigAttribute(t.Generic[T]):
    """Makes an attribute forward to the config"""

    def __init__(
        self, name: str, get_converter: t.Callable[[t.Any], T] | None = None
    ) -> None:
        self.__name__ = name
        self.get_converter = get_converter

    @t.overload
    def __get__(self, obj: None, owner: None) -> te.Self: ...

    @t.overload
    def __get__(self, obj: App, owner: type[App]) -> T: ...

    def __get__(self, obj: App | None, owner: type[App] | None = None) -> T | te.Self:
        if obj is None:
            return self

        rv = obj.config[self.__name__]

        if self.get_converter is not None:
            rv = self.get_converter(rv)

        return rv  # type: ignore[no-any-return]

    def __set__(self, obj: App, value: t.Any) -> None:
        obj.config[self.__name__] = value


class ConfigOverrideScope:
    """A single layer of temporary :class:`Config` value overrides.

    Scopes form a stack kept in a :class:`~contextvars.ContextVar`. When a
    key is read from the config, the scopes are searched from the innermost
    scope outward. If no scope declares the key, the application-level value
    is used.

    A scope is created explicitly with :meth:`Config.override`, or lazily for
    the current request by :meth:`Config.declare_override`. User code should
    not instantiate this class. Objects are returned by
    :meth:`Config.override` and :meth:`Config.override_source` for
    inspection.

    :param kind: ``"scope"`` for an explicit scope or ``"request"`` when the
        scope is bound to a request.
    :param name: A human-readable name, used in errors and ``repr``.
    :param values: The overrides initially declared in the scope.
    :param ctx: The request context the scope is bound to, if any.
    """

    __slots__ = (
        "kind",
        "name",
        "_values",
        "_ctx",
        "_var",
        "_token",
        "_old",
    )

    def __init__(
        self,
        kind: str,
        name: str,
        values: cabc.Mapping[str, t.Any],
        ctx: AppContext | None = None,
    ) -> None:
        self.kind: str = kind
        """``"scope"`` for an explicit scope or ``"request"`` for a scope
        bound to a request."""

        self.name: str = name
        """The name of the scope. ``"request"`` for a request scope, or the
        ``name`` passed to :meth:`Config.override` (default ``"override"``)."""

        self._values: dict[str, t.Any] = dict(values)
        self._ctx: weakref.ref[AppContext] | None = (
            weakref.ref(ctx) if ctx is not None else None
        )
        self._var: (
            contextvars.ContextVar[tuple[ConfigOverrideScope, ...] | None] | None
        ) = None
        self._token: (
            contextvars.Token[tuple[ConfigOverrideScope, ...] | None] | None
        ) = None
        self._old: tuple[ConfigOverrideScope, ...] | None = None

    @property
    def ctx(self) -> AppContext | None:
        """The request context this scope is bound to, or ``None`` for an
        explicit scope."""
        return self._ctx() if self._ctx is not None else None

    @property
    def values(self) -> cabc.Mapping[str, t.Any]:
        """A read-only mapping of the values declared in this scope."""
        return types.MappingProxyType(self._values)

    def declare(self, key: str, value: t.Any) -> None:
        """Declare ``key`` as ``value`` in this scope.

        Each key can only be declared once per scope; a nested scope may
        shadow the same key.

        :raise TypeError: if ``key`` is not a string.
        :raise ValueError: if ``key`` is already declared in this scope.
        """
        if not isinstance(key, str):
            raise TypeError(
                "Config override keys must be strings, got" f" {type(key).__name__!r}."
            )

        if key in self._values:
            raise ValueError(
                f"Config override for {key!r} is already declared in the"
                f" {self.name!r} scope; remove it or declare the override in"
                " a nested scope instead."
            )

        self._values[key] = value

    def __contains__(self, key: object) -> bool:
        return key in self._values

    def __getitem__(self, key: str) -> t.Any:
        return self._values[key]

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.kind} {self.name!r}" f" {self._values!r}>"

    def _bind(
        self,
        var: contextvars.ContextVar[tuple[ConfigOverrideScope, ...] | None],
    ) -> None:
        """Push this scope onto ``var`` and remember how to undo it."""
        self._old = var.get(None)
        self._var = var
        self._token = var.set((self._old or ()) + (self,))

    def restore(self) -> None:
        """Remove this scope, restoring the state from before it was pushed.

        Safe to call more than once. Works even if the scope was pushed in a
        different copied :class:`~contextvars.Context` (for example when a
        request scope is first declared inside an async task), in which case
        the context variable is set back explicitly instead of reset with the
        token.
        """
        token = self._token

        if token is None or self._var is None:
            return

        var = self._var
        old = self._old
        self._token = None

        try:
            var.reset(token)
        except ValueError:
            # The token belongs to a different Context. Its changes never
            # affected this Context; putting back the old value is equivalent
            # to resetting and cannot remove someone else's override because
            # this Context's value was not changed by that token.
            var.set(old)


class Config(dict):  # type: ignore[type-arg]
    """Works exactly like a dict but provides ways to fill it from files
    or special dictionaries.  There are two common patterns to populate the
    config.

    Either you can fill the config from a config file::

        app.config.from_pyfile('yourconfig.cfg')

    Or alternatively you can define the configuration options in the
    module that calls :meth:`from_object` or provide an import path to
    a module that should be loaded.  It is also possible to tell it to
    use the same module and with that provide the configuration values
    just before the call::

        DEBUG = True
        SECRET_KEY = 'development key'
        app.config.from_object(__name__)

    In both cases (loading from any Python file or loading from modules),
    only uppercase keys are added to the config.  This makes it possible to use
    lowercase values in the config file for temporary values that are not added
    to the config or to define the config keys in the same file that implements
    the application.

    Probably the most interesting way to load configurations is from an
    environment variable pointing to a file::

        app.config.from_envvar('YOURAPPLICATION_SETTINGS')

    In this case before launching the application you have to set this
    environment variable to the file you want to use.  On Linux and OS X
    use the export statement::

        export YOURAPPLICATION_SETTINGS='/path/to/config/file'

    On windows use `set` instead.

    :param root_path: path to which files are read relative from.  When the
                      config object is created by the application, this is
                      the application's :attr:`~flask.Flask.root_path`.
    :param defaults: an optional dictionary of default values
    """

    def __init__(
        self,
        root_path: str | os.PathLike[str],
        defaults: dict[str, t.Any] | None = None,
    ) -> None:
        super().__init__(defaults or {})
        self.root_path = root_path
        #: Stack of active override scopes, or ``None`` when no overrides
        #: have been declared. Stored in a ContextVar so that concurrent
        #: requests, threads and async tasks never see each other's
        #: overrides.
        self._cv_overrides: contextvars.ContextVar[
            tuple[ConfigOverrideScope, ...] | None
        ] = contextvars.ContextVar(f"flask.config-overrides.{id(self)}")

    # -- request- and scope-local config overrides --------------------------

    @contextmanager
    def override(
        self,
        mapping: cabc.Mapping[str, t.Any] | None = None,
        /,
        *,
        name: str | None = None,
        **values: t.Any,
    ) -> t.Iterator[ConfigOverrideScope]:
        """Open an explicit configuration override scope.

        Within the ``with`` block, reading a key from this config resolves
        through the overrides declared in this scope and any enclosing
        scopes before falling back to the application-level values. When the
        block exits -- for any reason, including an exception -- the scope
        is removed completely. Scopes may be nested; the innermost scope
        that declares a key wins.

        Overrides may be given as a mapping and/or keyword arguments. Further
        values may be declared inside the block with
        :meth:`declare_override` or :meth:`ConfigOverrideScope.declare`.

        .. code-block:: python

            with app.config.override(DEBUG=True):
                assert app.config["DEBUG"] is True

        :param mapping: An optional mapping of keys to override.
        :param name: A name for the scope, shown when inspecting
            :meth:`override_source`. Defaults to ``"override"``.
        :param values: Additional keys to override as keyword arguments.
        """
        scope = ConfigOverrideScope(
            "scope",
            name if name is not None else "override",
            self._prepare_values(mapping, values),
        )
        scope._bind(self._cv_overrides)

        try:
            yield scope
        finally:
            scope.restore()

    # Explicit, descriptive alias.
    override_scope = override

    def declare_override(self, key: str, value: t.Any) -> None:
        """Declare a temporary override for ``key``.

        Inside an explicit :meth:`override` scope, the value is declared in
        the innermost scope. Otherwise it is bound to the current request
        and removed automatically when the request context is popped, no
        matter how the request ends.

        Each key may only be declared once per scope; declare it again in a
        nested scope to shadow it.

        :param key: The config key to override. Must be a string.
        :param value: The temporary value.
        :raise TypeError: if ``key`` is not a string.
        :raise ValueError: if ``key`` is already declared in the target
            scope.
        :raise RuntimeError: if called outside of a request and outside of
            an explicit override scope.
        """
        self._scope_for_declare().declare(key, value)

    def declare_overrides(
        self,
        mapping: cabc.Mapping[str, t.Any] | None = None,
        /,
        **values: t.Any,
    ) -> None:
        """Declare multiple temporary overrides at once.

        Behaves like :meth:`declare_override`, taking a mapping and/or
        keyword arguments. If any declaration is invalid nothing is
        declared.

        :raise TypeError: if ``mapping`` is not a mapping or a key is not a
            string.
        :raise ValueError: if the same key is given more than once or is
            already declared in the target scope.
        :raise RuntimeError: if called outside of a request and outside of
            an explicit override scope.
        """
        scope = self._scope_for_declare()
        prepared = self._prepare_values(mapping, values)

        for key in prepared:
            if key in scope:
                raise ValueError(
                    f"Config override for {key!r} is already declared in the"
                    f" {scope.name!r} scope; remove it or declare the"
                    " override in a nested scope instead."
                )

        scope._values.update(prepared)

    def override_source(self, key: str) -> ConfigOverrideScope | None:
        """Return the scope supplying the effective value of ``key``.

        :return: The innermost active scope that declares ``key``, or
            ``None`` if the value comes from the application config (or
            ``key`` is not present anywhere). The returned scope's
            :attr:`~ConfigOverrideScope.kind` is ``"request"`` for
            request-bound overrides and ``"scope"`` for an explicit
            :meth:`override` block, and :attr:`~ConfigOverrideScope.name`
            identifies the layer.
        """
        stack = self._cv_overrides.get(None)

        if stack is not None:
            for scope in reversed(stack):
                if key in scope:
                    return scope

        return None

    def _scope_for_declare(self) -> ConfigOverrideScope:
        """Find the scope a new declaration belongs in, opening a request
        scope if needed."""
        stack = self._cv_overrides.get(None)

        if stack:
            scope = stack[-1]

            if scope.kind == "scope":
                return scope

            ctx = _cv_app.get(None)

            if ctx is not None and scope.ctx is ctx:
                # Innermost request scope belongs to the current (possibly
                # re-pushed) request context.
                return scope

            # The innermost request scope belongs to an enclosing context.
            # Fall through to open another layer for this context.

        ctx = _cv_app.get(None)

        if ctx is None or not ctx.has_request:
            raise RuntimeError(
                "Cannot declare a config override outside of an override"
                " scope: use 'with app.config.override(...)' or call"
                " 'declare_override' while handling a request."
            )

        scope = ConfigOverrideScope("request", "request", {}, ctx=ctx)
        scope._bind(self._cv_overrides)
        ctx._config_scopes.append(scope)
        return scope

    @staticmethod
    def _prepare_values(
        mapping: cabc.Mapping[str, t.Any] | None, values: dict[str, t.Any]
    ) -> dict[str, t.Any]:
        """Validate and combine the mapping and keyword arguments passed to
        override-declaring APIs."""
        if mapping is not None and not isinstance(mapping, cabc.Mapping):
            raise TypeError(
                "Config overrides must be declared with a mapping and/or"
                " keyword arguments; got a"
                f" {type(mapping).__name__!r} instead."
            )

        prepared: dict[str, t.Any] = dict(mapping) if mapping is not None else {}

        for key, value in values.items():
            if key in prepared:
                raise ValueError(
                    f"Config override for {key!r} was declared more than"
                    " once in the same scope."
                )

            prepared[key] = value

        for key in prepared:
            if not isinstance(key, str):
                raise TypeError(
                    "Config override keys must be strings, got"
                    f" {type(key).__name__!r}."
                )

        return prepared

    def _merged(self, stack: tuple[ConfigOverrideScope, ...]) -> dict[str, t.Any]:
        """Build a snapshot of the application values with all active
        overrides applied (innermost wins).

        Uses the base ``dict`` methods explicitly; calling ``dict.copy`` or
        ``dict(self)`` would resolve through the overridden mapping methods
        and recurse back here.
        """
        merged = {key: dict.__getitem__(self, key) for key in dict.__iter__(self)}

        for scope in stack:
            merged.update(scope._values)

        return merged

    # -- dict protocol with override resolution ----------------------------

    def __getitem__(self, key: str) -> t.Any:
        stack = self._cv_overrides.get(None)

        if stack is not None:
            for scope in reversed(stack):
                values = scope._values

                if key in values:
                    return values[key]

        return dict.__getitem__(self, key)

    def get(self, key: str, default: t.Any = None) -> t.Any:
        stack = self._cv_overrides.get(None)

        if stack is not None:
            for scope in reversed(stack):
                values = scope._values

                if key in values:
                    return values[key]

        return dict.get(self, key, default)

    def __contains__(self, key: object) -> bool:
        stack = self._cv_overrides.get(None)

        if stack is None:
            return dict.__contains__(self, key)

        if dict.__contains__(self, key):
            return True

        return any(key in scope._values for scope in stack)

    def __len__(self) -> int:
        stack = self._cv_overrides.get(None)

        if stack is None:
            return dict.__len__(self)

        return len(self._merged(stack))

    def __iter__(self) -> t.Iterator[str]:
        stack = self._cv_overrides.get(None)

        if stack is None:
            return dict.__iter__(self)

        def _iter() -> t.Iterator[str]:
            seen: set[str] = set()

            for key in dict.__iter__(self):
                seen.add(key)
                yield key

            for scope in stack:
                for key in scope._values:
                    if key not in seen:
                        seen.add(key)
                        yield key

        return _iter()

    def keys(self) -> t.KeysView[str]:  # type: ignore[override]
        stack = self._cv_overrides.get(None)

        if stack is None:
            return dict.keys(self)

        return self._merged(stack).keys()

    def items(self) -> t.ItemsView[str, t.Any]:  # type: ignore[override]
        stack = self._cv_overrides.get(None)

        if stack is None:
            return dict.items(self)

        return self._merged(stack).items()

    def values(self) -> t.ValuesView[t.Any]:  # type: ignore[override]
        stack = self._cv_overrides.get(None)

        if stack is None:
            return dict.values(self)

        return self._merged(stack).values()

    def __eq__(self, other: object) -> bool:
        stack = self._cv_overrides.get(None)

        if stack is None:
            return dict.__eq__(self, other)

        if isinstance(other, cabc.Mapping):
            return self._merged(stack) == other

        return NotImplemented

    def __repr__(self) -> str:
        stack = self._cv_overrides.get(None)

        if stack is None:
            return f"<{type(self).__name__} {dict.__repr__(self)}>"

        return f"<{type(self).__name__} {self._merged(stack)!r}>"

    def from_envvar(self, variable_name: str, silent: bool = False) -> bool:
        """Loads a configuration from an environment variable pointing to
        a configuration file.  This is basically just a shortcut with nicer
        error messages for this line of code::

            app.config.from_pyfile(os.environ['YOURAPPLICATION_SETTINGS'])

        :param variable_name: name of the environment variable
        :param silent: set to ``True`` if you want silent failure for missing
                       files.
        :return: ``True`` if the file was loaded successfully.
        """
        rv = os.environ.get(variable_name)
        if not rv:
            if silent:
                return False
            raise RuntimeError(
                f"The environment variable {variable_name!r} is not set"
                " and as such configuration could not be loaded. Set"
                " this variable and make it point to a configuration"
                " file"
            )
        return self.from_pyfile(rv, silent=silent)

    def from_prefixed_env(
        self, prefix: str = "FLASK", *, loads: t.Callable[[str], t.Any] = json.loads
    ) -> bool:
        """Load any environment variables that start with ``FLASK_``,
        dropping the prefix from the env key for the config key. Values
        are passed through a loading function to attempt to convert them
        to more specific types than strings.

        Keys are loaded in :func:`sorted` order.

        The default loading function attempts to parse values as any
        valid JSON type, including dicts and lists.

        Specific items in nested dicts can be set by separating the
        keys with double underscores (``__``). If an intermediate key
        doesn't exist, it will be initialized to an empty dict.

        :param prefix: Load env vars that start with this prefix,
            separated with an underscore (``_``).
        :param loads: Pass each string value to this function and use
            the returned value as the config value. If any error is
            raised it is ignored and the value remains a string. The
            default is :func:`json.loads`.

        .. versionadded:: 2.1
        """
        prefix = f"{prefix}_"

        for key in sorted(os.environ):
            if not key.startswith(prefix):
                continue

            value = os.environ[key]
            key = key.removeprefix(prefix)

            try:
                value = loads(value)
            except Exception:
                # Keep the value as a string if loading failed.
                pass

            if "__" not in key:
                # A non-nested key, set directly.
                self[key] = value
                continue

            # Traverse nested dictionaries with keys separated by "__".
            current = self
            *parts, tail = key.split("__")

            for part in parts:
                # If an intermediate dict does not exist, create it.
                if part not in current:
                    current[part] = {}

                current = current[part]

            current[tail] = value

        return True

    def from_pyfile(
        self, filename: str | os.PathLike[str], silent: bool = False
    ) -> bool:
        """Updates the values in the config from a Python file.  This function
        behaves as if the file was imported as module with the
        :meth:`from_object` function.

        :param filename: the filename of the config.  This can either be an
                         absolute filename or a filename relative to the
                         root path.
        :param silent: set to ``True`` if you want silent failure for missing
                       files.
        :return: ``True`` if the file was loaded successfully.

        .. versionadded:: 0.7
           `silent` parameter.
        """
        filename = os.path.join(self.root_path, filename)
        d = types.ModuleType("config")
        d.__file__ = filename
        try:
            with open(filename, mode="rb") as config_file:
                exec(compile(config_file.read(), filename, "exec"), d.__dict__)
        except OSError as e:
            if silent and e.errno in (errno.ENOENT, errno.EISDIR, errno.ENOTDIR):
                return False
            e.strerror = f"Unable to load configuration file ({e.strerror})"
            raise
        self.from_object(d)
        return True

    def from_object(self, obj: object | str) -> None:
        """Updates the values from the given object.  An object can be of one
        of the following two types:

        -   a string: in this case the object with that name will be imported
        -   an actual object reference: that object is used directly

        Objects are usually either modules or classes. :meth:`from_object`
        loads only the uppercase attributes of the module/class. A ``dict``
        object will not work with :meth:`from_object` because the keys of a
        ``dict`` are not attributes of the ``dict`` class.

        Example of module-based configuration::

            app.config.from_object('yourapplication.default_config')
            from yourapplication import default_config
            app.config.from_object(default_config)

        Nothing is done to the object before loading. If the object is a
        class and has ``@property`` attributes, it needs to be
        instantiated before being passed to this method.

        You should not use this function to load the actual configuration but
        rather configuration defaults.  The actual config should be loaded
        with :meth:`from_pyfile` and ideally from a location not within the
        package because the package might be installed system wide.

        See :ref:`config-dev-prod` for an example of class-based configuration
        using :meth:`from_object`.

        :param obj: an import name or object
        """
        if isinstance(obj, str):
            obj = import_string(obj)
        for key in dir(obj):
            if key.isupper():
                self[key] = getattr(obj, key)

    def from_file(
        self,
        filename: str | os.PathLike[str],
        load: t.Callable[[t.IO[t.Any]], t.Mapping[str, t.Any]],
        silent: bool = False,
        text: bool = True,
    ) -> bool:
        """Update the values in the config from a file that is loaded
        using the ``load`` parameter. The loaded data is passed to the
        :meth:`from_mapping` method.

        .. code-block:: python

            import json
            app.config.from_file("config.json", load=json.load)

            import tomllib
            app.config.from_file("config.toml", load=tomllib.load, text=False)

        :param filename: The path to the data file. This can be an
            absolute path or relative to the config root path.
        :param load: A callable that takes a file handle and returns a
            mapping of loaded data from the file.
        :type load: ``Callable[[Reader], Mapping]`` where ``Reader``
            implements a ``read`` method.
        :param silent: Ignore the file if it doesn't exist.
        :param text: Open the file in text or binary mode.
        :return: ``True`` if the file was loaded successfully.

        .. versionchanged:: 2.3
            The ``text`` parameter was added.

        .. versionadded:: 2.0
        """
        filename = os.path.join(self.root_path, filename)

        try:
            with open(filename, "r" if text else "rb") as f:
                obj = load(f)
        except OSError as e:
            if silent and e.errno in (errno.ENOENT, errno.EISDIR):
                return False

            e.strerror = f"Unable to load configuration file ({e.strerror})"
            raise

        return self.from_mapping(obj)

    def from_mapping(
        self, mapping: t.Mapping[str, t.Any] | None = None, **kwargs: t.Any
    ) -> bool:
        """Updates the config like :meth:`update` ignoring items with
        non-upper keys.

        :return: Always returns ``True``.

        .. versionadded:: 0.11
        """
        mappings: dict[str, t.Any] = {}
        if mapping is not None:
            mappings.update(mapping)
        mappings.update(kwargs)
        for key, value in mappings.items():
            if key.isupper():
                self[key] = value
        return True

    def get_namespace(
        self, namespace: str, lowercase: bool = True, trim_namespace: bool = True
    ) -> dict[str, t.Any]:
        """Returns a dictionary containing a subset of configuration options
        that match the specified namespace/prefix. Example usage::

            app.config['IMAGE_STORE_TYPE'] = 'fs'
            app.config['IMAGE_STORE_PATH'] = '/var/app/images'
            app.config['IMAGE_STORE_BASE_URL'] = 'http://img.website.com'
            image_store_config = app.config.get_namespace('IMAGE_STORE_')

        The resulting dictionary `image_store_config` would look like::

            {
                'type': 'fs',
                'path': '/var/app/images',
                'base_url': 'http://img.website.com'
            }

        This is often useful when configuration options map directly to
        keyword arguments in functions or class constructors.

        :param namespace: a configuration namespace
        :param lowercase: a flag indicating if the keys of the resulting
                          dictionary should be lowercase
        :param trim_namespace: a flag indicating if the keys of the resulting
                          dictionary should not include the namespace

        .. versionadded:: 0.11
        """
        rv = {}
        for k, v in self.items():
            if not k.startswith(namespace):
                continue
            if trim_namespace:
                key = k[len(namespace) :]
            else:
                key = k
            if lowercase:
                key = key.lower()
            rv[key] = v
        return rv
