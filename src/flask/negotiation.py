from __future__ import annotations

import re
import typing as t

from . import typing as ft

if t.TYPE_CHECKING:  # pragma: no cover
    import collections.abc as cabc

    from werkzeug.datastructures import MIMEAccept

#: Attribute set on a view function (or a method of a class-based view)
#: holding the representations it declares. The value is either a
#: :class:`RepresentationMap`, or a ``dict`` mapping HTTP methods to maps.
REPRESENTATIONS_ATTR = "__flask_representations__"

F = t.TypeVar("F", bound=t.Callable[..., t.Any])

# RFC 9110 "token" characters allowed in media type names and parameter names.
_token_re = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def _parse_content_type(value: str) -> tuple[str, str]:
    """Validate a declared content type and return ``(essence, content_type)``.

    The essence is the lowercased ``"type/subtype"`` part used for matching.
    The content type is the normalized full value, including any parameters,
    used for the response ``Content-Type`` header.

    A wildcard or otherwise unparseable value raises :exc:`ValueError`.
    """
    parts = [part.strip() for part in value.split(";")]
    essence = parts[0]
    main, dot, subtype = essence.partition("/")

    if (
        not dot
        or not _token_re.fullmatch(main)
        or not _token_re.fullmatch(subtype)
        or "*" in main
        or "*" in subtype
    ):
        raise ValueError(
            f"Invalid representation content type {value!r}. Expected a"
            " concrete media type such as 'application/json' or"
            " 'text/html', optionally with parameters. Wildcards are not"
            " allowed in declarations."
        )

    normalized_parts = [essence.lower()]

    for param in parts[1:]:
        if "=" not in param:
            raise ValueError(
                f"Invalid content type parameter {param!r} in {value!r}."
                " Parameters must have the form 'name=value'."
            )

        name, _, param_value = param.partition("=")
        name = name.strip()
        param_value = param_value.strip()

        if not _token_re.fullmatch(name):
            raise ValueError(f"Invalid parameter name {name!r} in {value!r}.")

        if len(param_value) >= 2 and param_value[0] == param_value[-1] == '"':
            pass
        elif not _token_re.fullmatch(param_value):
            raise ValueError(f"Invalid parameter value {param_value!r} in {value!r}.")

        normalized_parts.append(f"{name}={param_value}")

    return essence.lower(), "; ".join(normalized_parts)


class Representation:
    """A response representation that a view can offer.

    A representation pairs a ``content_type`` with a ``func`` that produces
    the response for it. ``func`` is called with the view's return value and
    may return anything Flask can convert to a response, such as a string,
    a dict, or a :class:`~flask.Response`.

    The first representation declared is the default, used when the request
    has no ``Accept`` header.

    :param content_type: The media type of the representation, such as
        ``"application/json"``. May include parameters such as
        ``"text/html; charset=utf-8"``. Wildcards are not allowed.
    :param func: The callable that generates the response from the view's
        return value. Required.

    .. versionadded:: 3.2
    """

    func: ft.RepresentationGeneratorCallable

    def __init__(
        self,
        content_type: str,
        func: ft.RepresentationGeneratorCallable,
    ) -> None:
        if not callable(func):
            raise ValueError(
                f"Representation for {content_type!r} must have a callable"
                " generator function."
            )

        if not isinstance(content_type, str):
            raise ValueError(
                "Representation content type must be a string, got"
                f" {type(content_type).__name__!r}."
            )

        self.essence, self.content_type = _parse_content_type(content_type)
        self.func = func

    def __repr__(self) -> str:
        return f"<Representation {self.content_type!r}>"


def _coerce_representation(
    value: Representation | tuple[str, ft.RepresentationGeneratorCallable],
) -> Representation:
    if isinstance(value, Representation):
        return value

    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
        return Representation(value[0], value[1])

    raise ValueError(
        "Representations must be 'Representation' objects or"
        " (content_type, func) tuples."
    )


class RepresentationMap:
    """An ordered collection of :class:`Representation` objects offered by
    one endpoint, blueprint, or application.

    Content types must be unique within a map. The first representation is
    the default.

    .. versionadded:: 3.2
    """

    def __init__(
        self,
        representations: cabc.Iterable[
            Representation | tuple[str, ft.RepresentationGeneratorCallable]
        ] = (),
    ) -> None:
        self._representations: list[Representation] = []
        self._by_essence: dict[str, Representation] = {}

        for representation in representations:
            self.add(_coerce_representation(representation))

    def add(self, representation: Representation) -> None:
        """Add a representation. Duplicate content types raise
        :exc:`ValueError`.
        """
        if representation.essence in self._by_essence:
            raise ValueError(
                f"A representation for content type"
                f" {representation.content_type!r} is already declared."
            )

        self._by_essence[representation.essence] = representation
        self._representations.append(representation)

    @property
    def default(self) -> Representation:
        """The first declared representation, used when no preference is
        given.
        """
        return self._representations[0]

    @property
    def content_types(self) -> list[str]:
        """The declared content types, in declaration order."""
        return [r.content_type for r in self._representations]

    def select(self, accept: MIMEAccept) -> Representation | None:
        """Select the best representation for the request's parsed
        ``Accept`` header.

        Returns the default representation when the header is missing or
        empty, ``None`` when none of the representations are acceptable,
        and the best match otherwise. Quality factors decide the order,
        quality ``0`` excludes a representation, and a wildcard stably
        selects the earliest matching declaration.
        """
        if not accept:
            return self.default

        essence = accept.best_match(
            [representation.essence for representation in self._representations]
        )

        if essence is None:
            return None

        return self._by_essence[essence]

    def __iter__(self) -> cabc.Iterator[Representation]:
        return iter(self._representations)

    def __len__(self) -> int:
        return len(self._representations)

    def __bool__(self) -> bool:
        return bool(self._representations)

    def __repr__(self) -> str:
        types = ", ".join(self.content_types)
        return f"<RepresentationMap {types!r}>"


def representations(
    *declared: Representation | tuple[str, ft.RepresentationGeneratorCallable],
) -> t.Callable[[F], F]:
    """Decorate a view function or class-based view method to declare the
    representations it can produce.

    The view performs any shared work and returns the resource. Each
    representation's generator receives that value and produces the
    response in its format:

    .. code-block:: python

        def as_json(resource):
            return jsonify(resource)

        def as_html(resource):
            return render_template("resource.html", resource=resource)

        @app.route("/resource/<int:id>")
        @representations(
            Representation("application/json", as_json),
            ("text/html", as_html),
        )
        def resource(id):
            return db.get_resource(id)

    The first representation is the default for requests without an
    ``Accept`` header. A return value that is already a response object
    (including streaming and file responses) is passed through unchanged
    without selection.

    :param declared: :class:`Representation` objects or
        ``(content_type, func)`` tuples. At least one is required and
        content types must be unique.

    .. versionadded:: 3.2
    """
    representation_map = RepresentationMap(declared)

    if not representation_map:
        raise ValueError(
            "At least one representation must be declared when using 'representations'."
        )

    def decorator(f: F) -> F:
        setattr(f, REPRESENTATIONS_ATTR, representation_map)
        return f

    return t.cast(t.Callable[[F], F], decorator)
