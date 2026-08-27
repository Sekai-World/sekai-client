"""Explicit response models and boundary validation for upstream APIs.

These helpers turn malformed, partial, or type-incompatible upstream responses
into clear, diagnostic errors (``ResponseValidationError``). They return the
validated mapping unchanged so existing call sites keep working with the plain
``dict`` shape, and they raise *before* any module-level state is overwritten so
a bad response never destroys the last-known-good client state.

Validators are deliberately narrow: they assert presence and types of the
fields each consumer actually depends on rather than enforcing a full schema.
No new dependencies are introduced.
"""

from __future__ import annotations

from typing import Any

# Tables whose records are consumed by the i18n special handlers and therefore
# must carry an integer ``id``. Used by ``validate_master_data`` to give a
# precise diagnostic for the records that drive public JSON output.
_I18N_ID_TABLES = frozenset(
    {
        "cards",
        "musics",
        "events",
        "virtualLives",
        "eventStories",
        "stamps",
    }
)


class ResponseValidationError(ValueError):
    """Raised when an upstream response fails boundary validation.

    Carries the originating ``source`` (endpoint or logical name), the offending
    ``field`` path when known, and what was ``expected`` vs. ``got`` so logs and
    tests can pinpoint the schema break without dumping the whole payload.
    """

    def __init__(
        self,
        message: str,
        *,
        source: str = "",
        field: str = "",
        expected: str = "",
        got: Any = None,
    ) -> None:
        super().__init__(message)
        self.source = source
        self.field = field
        self.expected = expected
        self.got = got

    @classmethod
    def missing_field(cls, source: str, field: str) -> ResponseValidationError:
        return cls(
            f"{source}: missing required field {field!r}",
            source=source,
            field=field,
            expected="present",
            got=None,
        )

    @classmethod
    def invalid_type(
        cls, source: str, field: str, expected: str, got: Any
    ) -> ResponseValidationError:
        return cls(
            f"{source}: field {field!r} expected {expected}, got {type(got).__name__}",
            source=source,
            field=field,
            expected=expected,
            got=got,
        )


def _require_dict(value: Any, source: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ResponseValidationError(
            f"{source}: expected an object response, got {type(value).__name__}",
            source=source,
            expected="object",
            got=value,
        )
    return value


def _require_str(value: Any, source: str, field: str) -> str:
    if not isinstance(value, str):
        raise ResponseValidationError.invalid_type(source, field, "str", value)
    return value


def _require_int(value: Any, source: str, field: str) -> int:
    # ``bool`` is a subclass of ``int``; reject it explicitly.
    if not isinstance(value, int) or isinstance(value, bool):
        raise ResponseValidationError.invalid_type(source, field, "int", value)
    return value


def _require_positive_int(value: Any, source: str, field: str) -> int:
    """Require an ``int`` strictly greater than zero (rejects ``bool``/<= 0)."""
    result = _require_int(value, source, field)
    if result <= 0:
        raise ResponseValidationError.invalid_type(source, field, "int>0", result)
    return result


def _require_list(value: Any, source: str, field: str) -> list:
    if not isinstance(value, list):
        raise ResponseValidationError.invalid_type(source, field, "list", value)
    return value


def _is_str_or_int(value: Any) -> bool:
    """True only for a real ``str`` or ``int`` — ``bool`` is rejected."""
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def _validate_optional_region(
    container: dict[str, Any],
    source: str,
    expected_region: str | None,
    *,
    path_prefix: str = "",
) -> None:
    """Validate an optional upstream region marker on a response container.

    Accepts ``region`` or ``regionCode`` when present, each must be a non-empty
    string. When ``expected_region`` is supplied, a present value is compared
    case-insensitively; a missing region field is never required.
    """
    for key in ("region", "regionCode"):
        if key not in container:
            continue
        value = container[key]
        field = f"{path_prefix}.{key}" if path_prefix else key
        if not isinstance(value, str) or value == "":
            raise ResponseValidationError.invalid_type(
                source, field, "non-empty str", value
            )
        if expected_region is not None and value.lower() != expected_region.lower():
            raise ResponseValidationError(
                f"{source}: field {field!r} value {value!r} does not match "
                f"expected region {expected_region!r}",
                source=source,
                field=field,
                expected=expected_region,
                got=value,
            )


def validate_auth_response(
    data: object, *, require_cdn_version: bool = False
) -> dict[str, Any]:
    """Validate a ``/user/auth`` (or suite auth) login response.

    Required for every region: ``sessionToken`` (non-empty str),
    ``appVersion``/``dataVersion``/``assetVersion`` (str) and
    ``multiPlayVersion`` (str or int). ``cdnVersion`` is required only when
    *require_cdn_version* is set (cn/tw/kr login path). ``assetHash`` and
    ``suiteMasterSplitPath`` are optional and type-checked when present.
    """
    src = "auth/login"
    d = _require_dict(data, src)

    session_token = d.get("sessionToken")
    if "sessionToken" not in d:
        raise ResponseValidationError.missing_field(src, "sessionToken")
    if not isinstance(session_token, str) or not session_token:
        raise ResponseValidationError.invalid_type(
            src, "sessionToken", "non-empty str", session_token
        )

    for field in ("appVersion", "dataVersion", "assetVersion"):
        if field not in d:
            raise ResponseValidationError.missing_field(src, field)
        _require_str(d[field], src, field)

    if "multiPlayVersion" not in d:
        raise ResponseValidationError.missing_field(src, "multiPlayVersion")
    if not isinstance(d["multiPlayVersion"], (str, int)) or isinstance(
        d["multiPlayVersion"], bool
    ):
        raise ResponseValidationError.invalid_type(
            src, "multiPlayVersion", "str|int", d["multiPlayVersion"]
        )

    if require_cdn_version:
        _require_present_str_or_int(d, src, "cdnVersion")

    if "assetHash" in d and not _is_str_or_int(d["assetHash"]):
        raise ResponseValidationError.invalid_type(
            src, "assetHash", "str|int", d["assetHash"]
        )

    if "suiteMasterSplitPath" in d:
        _validate_split_paths(d["suiteMasterSplitPath"], src)

    return d


def _validate_split_paths(paths: Any, source: str) -> None:
    if not isinstance(paths, (list, tuple)):
        raise ResponseValidationError.invalid_type(
            source, "suiteMasterSplitPath", "list", paths
        )
    if not all(isinstance(p, str) for p in paths):
        raise ResponseValidationError.invalid_type(
            source, "suiteMasterSplitPath", "list[str]", paths
        )


def _require_present_str_or_int(d: dict[str, Any], source: str, field: str) -> None:
    """Require *field* to be present with a ``str``/``int`` (non-bool) value."""
    if field not in d:
        raise ResponseValidationError.missing_field(source, field)
    if not _is_str_or_int(d[field]):
        raise ResponseValidationError.invalid_type(source, field, "str|int", d[field])


def validate_version_info(
    data: object, *, require_cdn_version: bool = False
) -> dict[str, Any]:
    """Validate the client ``version_info`` mapping.

    Every consumer reads ``appVersion``/``dataVersion``/``assetVersion`` (str).
    ``cdnVersion`` is required (and must be ``str``/``int``, never ``bool`` or
    ``float``) when *require_cdn_version* is set (cn/tw/kr version data). The
    other optional fields (``appVersionStatus``, ``multiPlayVersion``,
    ``cdnVersion``/``appHash``/``assetHash``) are type-checked when present so a
    corrupt value surfaces clearly instead of raising a bare
    ``KeyError``/``TypeError`` later.
    """
    src = "version_info"
    d = _require_dict(data, src)
    for field in ("appVersion", "dataVersion", "assetVersion"):
        if field not in d:
            raise ResponseValidationError.missing_field(src, field)
        _require_str(d[field], src, field)

    if "appVersionStatus" in d:
        _require_str(d["appVersionStatus"], src, "appVersionStatus")
    if "multiPlayVersion" in d and not _is_str_or_int(d["multiPlayVersion"]):
        raise ResponseValidationError.invalid_type(
            src, "multiPlayVersion", "str|int", d["multiPlayVersion"]
        )
    if require_cdn_version:
        _require_present_str_or_int(d, src, "cdnVersion")
    for field in ("cdnVersion", "appHash", "assetHash"):
        if field in d and not _is_str_or_int(d[field]):
            raise ResponseValidationError.invalid_type(src, field, "str|int", d[field])

    return d


def validate_system_data(data: object) -> dict[str, Any]:
    """Validate the ``/system`` version/system response.

    Required: ``maintenanceStatus`` (str) and ``appVersions`` (list of version
    descriptors, each with ``appVersion``/``dataVersion``/``assetVersion`` /
    ``appVersionStatus``).
    """
    src = "system-data"
    d = _require_dict(data, src)
    if "maintenanceStatus" not in d:
        raise ResponseValidationError.missing_field(src, "maintenanceStatus")
    _require_str(d["maintenanceStatus"], src, "maintenanceStatus")

    if "appVersions" not in d:
        raise ResponseValidationError.missing_field(src, "appVersions")
    app_versions = _require_list(d["appVersions"], src, "appVersions")
    for i, version in enumerate(app_versions):
        if not isinstance(version, dict):
            raise ResponseValidationError.invalid_type(
                src, f"appVersions[{i}]", "object", version
            )
        for field in (
            "appVersion",
            "dataVersion",
            "assetVersion",
            "appVersionStatus",
        ):
            if field not in version:
                raise ResponseValidationError.missing_field(
                    src, f"appVersions[{i}].{field}"
                )
            _require_str(version[field], src, f"appVersions[{i}].{field}")
        if "cdnVersion" in version and not _is_str_or_int(version["cdnVersion"]):
            raise ResponseValidationError.invalid_type(
                src,
                f"appVersions[{i}].cdnVersion",
                "str|int",
                version["cdnVersion"],
            )

    return d


def validate_current_event_response(
    payload: object, *, expected_region: str | None = None
) -> dict[str, Any] | None:
    """Validate the Strapi current-event payload and return its ``eventJson``.

    Required: ``eventJson`` present, an object or ``null``. When present, the
    event must carry identity (``id`` int > 0, ``eventType`` str) and timing
    (``startAt``/``closedAt``/``rankingAnnounceAt``/``aggregateAt`` int). Returns
    the validated event dict (or ``None`` when there is no ongoing event) so the
    caller can assign it only after validation succeeds.

    An optional upstream region marker (``region``/``regionCode``, at the
    top-level payload or inside ``eventJson``) is validated as a non-empty string
    when present. When ``expected_region`` is supplied by the caller, a present
    region value is compared against it; a missing region field is never required.
    """
    src = "current-event"
    d = _require_dict(payload, src)
    if "eventJson" not in d:
        raise ResponseValidationError.missing_field(src, "eventJson")

    event = d["eventJson"]
    if event is None:
        return None
    if not isinstance(event, dict):
        raise ResponseValidationError.invalid_type(
            src, "eventJson", "object|null", event
        )

    if "id" not in event:
        raise ResponseValidationError.missing_field(src, "eventJson.id")
    _require_positive_int(event["id"], src, "eventJson.id")
    if "eventType" not in event:
        raise ResponseValidationError.missing_field(src, "eventJson.eventType")
    _require_str(event["eventType"], src, "eventJson.eventType")

    # Optional upstream region marker (never required).
    _validate_optional_region(d, src, expected_region)
    _validate_optional_region(event, src, expected_region, path_prefix="eventJson")

    for field in ("startAt", "closedAt", "rankingAnnounceAt", "aggregateAt"):
        if field not in event:
            raise ResponseValidationError.missing_field(src, f"eventJson.{field}")
        _require_int(event[field], src, f"eventJson.{field}")

    # Reject impossible / non-monotonic event timing. The consumed ordering is
    # startAt <= aggregateAt <= rankingAnnounceAt <= closedAt, and every
    # timestamp must be non-negative (epoch millis).
    start_at = event["startAt"]
    aggregate_at = event["aggregateAt"]
    ranking_announce_at = event["rankingAnnounceAt"]
    closed_at = event["closedAt"]
    if start_at < 0 or aggregate_at < 0 or ranking_announce_at < 0 or closed_at < 0:
        raise ResponseValidationError(
            f"{src}: eventJson timing fields must be non-negative",
            source=src,
            field="eventJson.timing",
            expected="non-negative",
            got=(start_at, aggregate_at, ranking_announce_at, closed_at),
        )
    if not (start_at <= aggregate_at <= ranking_announce_at <= closed_at):
        raise ResponseValidationError(
            f"{src}: eventJson timing must be monotonic "
            f"(startAt <= aggregateAt <= rankingAnnounceAt <= closedAt)",
            source=src,
            field="eventJson.timing",
            expected="startAt<=aggregateAt<=rankingAnnounceAt<=closedAt",
            got=(start_at, aggregate_at, ranking_announce_at, closed_at),
        )

    return event


def _validate_ranking_entries(entries: list, source: str) -> None:
    """Validate a list of ranking rows (``rankings``/``borderRankings``)."""
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ResponseValidationError.invalid_type(
                source, f"[{i}]", "object", entry
            )
        if "rank" not in entry:
            raise ResponseValidationError.missing_field(source, f"[{i}].rank")
        rank = entry["rank"]
        if not isinstance(rank, int) or isinstance(rank, bool) or rank < 1:
            raise ResponseValidationError.invalid_type(
                source, f"[{i}].rank", "int>=1", rank
            )
        if "score" not in entry:
            raise ResponseValidationError.missing_field(source, f"[{i}].score")
        score = entry["score"]
        if not isinstance(score, int) or isinstance(score, bool) or score < 0:
            raise ResponseValidationError.invalid_type(
                source, f"[{i}].score", "int>=0", score
            )


def validate_event_ranking_snapshot(snapshot: object) -> dict[str, Any]:
    """Validate the combined event ranking snapshot (``first100`` + ``border``).

    ``first100`` is always required and must carry ``isEventAggregate`` (bool)
    and ``rankings`` (list of rows with ``rank``/``score``). ``border`` is
    required (non-null object with ``borderRankings``) unless ``first100`` is an
    aggregate event (``isEventAggregate`` is True), since ``track_event_scores``
    unconditionally consumes ``snapshot['border']``; a missing/null border for a
    non-aggregate first100 is rejected. Rows are checked for ranking identity
    (``rank``) and value ranges (``score`` >= 0).
    """
    src = "event-ranking-snapshot"
    d = _require_dict(snapshot, src)

    first100 = d.get("first100")
    if not isinstance(first100, dict):
        raise ResponseValidationError.invalid_type(src, "first100", "object", first100)
    if "isEventAggregate" not in first100:
        raise ResponseValidationError.missing_field(src, "first100.isEventAggregate")
    if not isinstance(first100["isEventAggregate"], bool):
        raise ResponseValidationError.invalid_type(
            src, "first100.isEventAggregate", "bool", first100["isEventAggregate"]
        )
    rankings = first100.get("rankings")
    if not isinstance(rankings, list):
        raise ResponseValidationError.invalid_type(
            src, "first100.rankings", "list", rankings
        )
    _validate_ranking_entries(rankings, f"{src}.first100.rankings")

    border = d.get("border")
    # ``track_event_scores`` unconditionally consumes ``snapshot['border']``, so a
    # missing/null border is only acceptable for aggregate (event-final) rankings
    # where no border line exists. For non-aggregate first100 the border must be
    # present and carry ``borderRankings``.
    if border is None:
        if not first100["isEventAggregate"]:
            raise ResponseValidationError.missing_field(src, "border")
        return d
    if not isinstance(border, dict):
        raise ResponseValidationError.invalid_type(src, "border", "object|null", border)
    border_rankings = border.get("borderRankings")
    if not isinstance(border_rankings, list):
        raise ResponseValidationError.invalid_type(
            src, "border.borderRankings", "list", border_rankings
        )
    _validate_ranking_entries(border_rankings, f"{src}.border.borderRankings")

    return d


def validate_information(data: object) -> dict[str, Any]:
    """Validate the ``/information`` response object/list shape.

    Required to be an object. Known list fields (``informations``,
    ``userHomeBanners``, ``userInformations``) are type-checked when present so a
    scalar where a list is expected fails clearly instead of crashing downstream.
    """
    src = "information"
    d = _require_dict(data, src)
    for field in ("informations", "userHomeBanners", "userInformations"):
        if field in d and not isinstance(d[field], list):
            raise ResponseValidationError.invalid_type(src, field, "list", d[field])
    return d


def validate_master_data(
    data: object, *, source: str = "master-data"
) -> dict[str, Any]:
    """Validate master-data object/list shape.

    Required: a top-level object whose values are lists of objects. The tables
    consumed by the i18n special handlers (``cards``/``musics``/``events``/
    ``virtualLives``/``eventStories``/``stamps``) additionally require each record
    to carry an integer ``id`` so the i18n writers fail with a precise diagnostic
    rather than a bare ``KeyError``.
    """
    d = _require_dict(data, source)
    for table, records in d.items():
        if not isinstance(records, list):
            raise ResponseValidationError.invalid_type(source, table, "list", records)
        if table in _I18N_ID_TABLES:
            for i, record in enumerate(records):
                if not isinstance(record, dict):
                    raise ResponseValidationError.invalid_type(
                        source, f"{table}[{i}]", "object", record
                    )
                rid = record.get("id")
                if not isinstance(rid, int) or isinstance(rid, bool):
                    raise ResponseValidationError.invalid_type(
                        source, f"{table}[{i}].id", "int", rid
                    )
        else:
            for i, record in enumerate(records):
                if not isinstance(record, dict):
                    raise ResponseValidationError.invalid_type(
                        source, f"{table}[{i}]", "object", record
                    )
    return d
