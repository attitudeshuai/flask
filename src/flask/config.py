from __future__ import annotations

import copy
import errno
import json
import os
import types
import typing as t
from dataclasses import dataclass

from werkzeug.utils import import_string

if t.TYPE_CHECKING:
    import typing_extensions as te

    from .sansio.app import App


T = t.TypeVar("T")


@dataclass(frozen=True)
class ConfigSource:
    """Describes how a config key's current value was set.

    Instances are returned by :meth:`Config.get_source`. Only the
    attributes relevant to :attr:`method` are populated, the others are
    ``None``.

    :param method: how the value was loaded: ``"default"`` (passed to
        the constructor), ``"direct"`` (direct item assignment),
        ``"pyfile"``, ``"envvar"``, ``"object"``, ``"file"``,
        ``"mapping"`` or ``"prefixed_env"``.
    :param path: resolved path of the file that was loaded, for the
        ``"pyfile"``, ``"envvar"`` and ``"file"`` methods.
    :param name: name of the object for ``"object"``, or the name of
        the environment variable for ``"envvar"`` and
        ``"prefixed_env"``.
    :param prefix: prefix used by ``"prefixed_env"``.
    """

    method: str
    path: str | None = None
    name: str | None = None
    prefix: str | None = None


_DEFAULT_SOURCE = ConfigSource("default")
_DIRECT_SOURCE = ConfigSource("direct")
_MAPPING_SOURCE = ConfigSource("mapping")
_missing = object()


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

    In addition to behaving like a regular dict, the source of the
    current value of each key is tracked and can be inspected with
    :meth:`get_source`.

    :param root_path: path to which files are read relative from.  When the
                      config object is created by the application, this is
                      the application's :attr:`~flask.Flask.root_path`.
    :param defaults: an optional dictionary of default values
    """

    #: Source recorded for each key, keyed by config key.
    _sources: dict[str, ConfigSource]

    def __init__(
        self,
        root_path: str | os.PathLike[str],
        defaults: dict[str, t.Any] | None = None,
    ) -> None:
        # ``dict.__init__`` does not dispatch to the overridden
        # ``__setitem__``, so the sources have to be recorded explicitly.
        object.__setattr__(self, "_sources", {})
        super().__init__(defaults or {})

        if defaults is not None:
            for key in defaults:
                self._sources[key] = _DEFAULT_SOURCE

        self.root_path = root_path

    def _set_source(self, key: str, source: ConfigSource) -> None:
        """Record ``source`` for ``key``, creating the tracking dict when
        necessary (for example when an unpickled config is repopulated
        before its state is restored)."""
        try:
            sources = self._sources
        except AttributeError:
            sources = {}
            object.__setattr__(self, "_sources", sources)

        sources[key] = source

    def _set(self, key: str, value: t.Any, source: ConfigSource) -> None:
        """Set ``key`` and record ``source`` for it. Item assignment still
        goes through ``__setitem__`` so subclasses overriding it keep
        working, then the source is corrected afterwards."""
        self[key] = value
        self._set_source(key, source)

    def get_source(self, key: str) -> ConfigSource | None:
        """Return the source of the current value of ``key``.

        The source describes which loader most recently set the key and
        the context it was loaded from (file path, object or environment
        variable name, environment variable prefix).

        Keys passed as ``defaults`` when the config was created have the
        ``"default"`` source. Keys set by direct assignment or regular
        dict mutation methods such as :meth:`update` have the ``"direct"``
        source.

        :return: The :class:`ConfigSource`, or ``None`` if the key does
            not exist or has no recorded source.
        """
        return self._sources.get(key)

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
        filename = os.path.join(self.root_path, rv)
        source = ConfigSource("envvar", path=filename, name=variable_name)
        return self._load_pyfile(rv, silent=silent, source=source)

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
        doesn't exist, it will be initialized to an empty dict. Only
        the final (leaf) key of a nested variable is considered set by
        the environment variable; intermediate keys are not attributed.

        :param prefix: Load env vars that start with this prefix,
            separated with an underscore (``_``).
        :param loads: Pass each string value to this function and use
            the returned value as the config value. If any error is
            raised it is ignored and the value remains a string. The
            default is :func:`json.loads`.

        .. versionadded:: 2.1
        """
        env_prefix = f"{prefix}_"

        for env_key in sorted(os.environ):
            if not env_key.startswith(env_prefix):
                continue

            value = os.environ[env_key]
            key = env_key.removeprefix(env_prefix)

            try:
                value = loads(value)
            except Exception:
                # Keep the value as a string if loading failed.
                pass

            source = ConfigSource("prefixed_env", prefix=prefix, name=env_key)

            if "__" not in key:
                # A non-nested key, set directly.
                self._set(key, value, source)
                continue

            # Traverse nested dictionaries with keys separated by "__".
            current = self
            *parts, tail = key.split("__")

            for part in parts:
                # If an intermediate dict does not exist, create it. It
                # is not attributed to the environment variable: only
                # the leaf is.
                if part not in current:
                    if current is self:
                        self[part] = {}
                        self._sources.pop(part, None)
                    else:
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
        path = os.path.join(self.root_path, filename)
        source = ConfigSource("pyfile", path=path)
        return self._load_pyfile(filename, silent=silent, source=source)

    def _load_pyfile(
        self,
        filename: str | os.PathLike[str],
        silent: bool,
        source: ConfigSource,
    ) -> bool:
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
        self._load_object(d, source)
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

        name = getattr(obj, "__name__", None) or obj.__class__.__name__
        self._load_object(obj, ConfigSource("object", name=name))

    def _load_object(self, obj: object, source: ConfigSource) -> None:
        for key in dir(obj):
            if key.isupper():
                self._set(key, getattr(obj, key), source)

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

        return self._load_mapping(obj, ConfigSource("file", path=filename))

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
        return self._load_mapping(mappings, _MAPPING_SOURCE)

    def _load_mapping(
        self, mappings: t.Mapping[str, t.Any], source: ConfigSource
    ) -> bool:
        for key, value in mappings.items():
            if key.isupper():
                self._set(key, value, source)
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

    # Overrides of mutating dict methods so the tracked sources stay in
    # sync. These all behave exactly like the dict equivalents.

    def __setitem__(self, key: str, value: t.Any) -> None:
        super().__setitem__(key, value)
        self._set_source(key, _DIRECT_SOURCE)

    def __delitem__(self, key: str) -> None:
        super().__delitem__(key)
        self._sources.pop(key, None)

    def pop(self, key: str, default: t.Any = _missing) -> t.Any:
        try:
            value = super().pop(key)
        except KeyError:
            if default is _missing:
                raise
            return default

        self._sources.pop(key, None)
        return value

    def popitem(self) -> tuple[str, t.Any]:
        key, value = super().popitem()
        self._sources.pop(key, None)
        return key, value

    def clear(self) -> None:
        super().clear()
        self._sources.clear()

    def setdefault(self, key: str, default: t.Any = None) -> t.Any:
        if key not in self:
            self[key] = default
        return self[key]

    def update(self, *args: t.Any, **kwargs: t.Any) -> None:
        super().update(*args, **kwargs)

        try:
            sources = self._sources
        except AttributeError:
            return

        if args:
            mapping = args[0]

            if hasattr(mapping, "keys"):
                keys: t.Iterable[t.Any] = mapping.keys()
            else:
                keys = (key for key, _ in mapping)

            for key in keys:
                sources[key] = _DIRECT_SOURCE

        for key in kwargs:
            sources[key] = _DIRECT_SOURCE

    def __deepcopy__(self, memo: dict[int, t.Any]) -> te.Self:
        # The default reconstruction of a dict subclass re-adds the items
        # through ``__setitem__`` after restoring the state, which would
        # turn every source into ``"direct"``. Restore the items without
        # going through ``__setitem__`` so the copied sources are kept.
        other = self.__class__.__new__(self.__class__)
        memo[id(self)] = other
        dict.update(other, copy.deepcopy(dict(self), memo))
        other.__dict__.update(copy.deepcopy(self.__dict__, memo))
        return other

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {dict.__repr__(self)}>"
