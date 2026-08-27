import hashlib
import logging
import os
import posixpath
import re
import shutil
import subprocess
import sys
import traceback
from datetime import datetime
from os import getenv, path
from time import monotonic as _monotonic
from time import sleep
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
import ujson as json
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from git.repo import Repo
from git.util import Actor
from pytz import timezone

from logging_config import configure_logging
from response_models import (
    ResponseValidationError,
    validate_information,
    validate_master_data,
    validate_version_info,
)
from utils.array_to_dict import (
    convert_array_to_dict,
    get_structures_for_app_ver,
    resolve_structure_compatibility_version,
    restore_compact_data,
)
from utils.constants import (
    check_update_simple_mode,
    check_update_versions_url,
    local_git_folder_names,
    nuverse_master_data_base_url,
    pjsk_region,
    remote_git_url_base,
    strapi_base_url,
    strapi_token,
    update_options,
)
from utils.crypto import decrypt_msgpack
from utils.git import (
    GitOutcome,
    GitResult,
    check_git_folder,
    prepare_repo_for_update,
    push_current_head,
)
from utils.git_lock import (
    ProcessCycleLock,
    RepoLockUnavailable,
    repo_file_locks,
)
from utils.jsonrpc_client import JSONRPCClient
from utils.strapi_outbox import StrapiOutbox, StrapiOutboxError
from utils.update_transaction import (
    FileEntry,
    JournalError,
    RepoCommitState,
    RepoPushState,
    RepoState,
    TransactionJournal,
    TxnPhase,
    compute_sha256,
    fsync_directory,
    fsync_file,
    fsync_published_file,
    new_transaction_id,
    staging_dir_for,
    validate_journal_roots,
)

LOGLEVEL = getenv("LOGLEVEL", "INFO").upper()
configure_logging(level=LOGLEVEL)
logger = logging.getLogger(__name__)

jsonrpc_client = JSONRPCClient(f"http://localhost:{getenv('JSONRPC_PORT', '3939')}/")

version_info: dict[str, Any] | None = None
is_in_maintenance = False

masterdb_diff_folder_path = path.join(
    path.dirname(__file__), local_git_folder_names["masterDBDiff"]
)
masterdb_diff_repo: Repo | None = None
i18n_diff_folder_path = path.join(
    path.dirname(__file__), local_git_folder_names["i18n"]
)
i18n_diff_repo: Repo | None = None

# --- Phase 4.2 / Phase 2: staging roots, lock state, and the single in-process
# lock ---
#
# During a cycle, all candidate JSON is generated into repository-adjacent
# staging directories on the same filesystem; only after every staged file is
# validated are they published into the formal working trees with ``os.replace``
# (``versions.json`` last). These module-level roots are ``None`` outside a
# cycle, in which case writes go straight to the working trees (legacy behavior)
# and no manifest is recorded.
#
# Phase 2: staging is *journal-owned*. The active staging root for a cycle is
# ``<repo>.staging/<txn_id>/`` (a sub-directory of the legacy ``<repo>.staging``
# parent). The parent ``<repo>.staging`` is the directory that is cleared on a
# clean abort; the journal-owned ``<txn_id>`` sub-directory is what recovery
# inspects after a crash.
_MASTER_STAGING_ROOT: str | None = None
_I18N_STAGING_ROOT: str | None = None
_STAGING_MANIFEST: dict[str, list[str]] | None = None
# The active transaction id for the current cycle (None outside a cycle). The
# journal-owned staging sub-directory is derived from this.
_ACTIVE_TXN_ID: str | None = None

# Candidate version produced by the most recent generate phase of a cycle. It is
# stashed here (not returned alongside the manifest) so the historical
# ``_generate_and_publish`` contract — returning the manifest dict — is preserved
# for standalone/integration callers, while the locked cycle can read the explicit
# candidate for commit construction without indexing the published global
# ``version_info`` (which stays ``None`` on an i18n-only first run).
_CYCLE_CANDIDATE: dict[str, Any] | None = None
_DAILY_DUE_JOURNAL_KEY = "_sekai_daily_due_date"

_PROCESS_LOCK = ProcessCycleLock()

# Repositories are prepared/committed/pushed in this deterministic order.
_REPO_ORDER = ("master", "i18n")

# Default cooperative deadline (seconds) for an ordinary update cycle. A daily
# cycle never uses a deadline (it must always run to completion). Tests may pass
# a smaller value via ``_run_update_cycle``'s ``deadline_seconds`` parameter.
DEFAULT_ORDINARY_DEADLINE_SECONDS = 3600

# Tokyo timezone used for the half-hour scheduler and durable daily-due identity.
_TOKYO_TZ = timezone("Asia/Tokyo")
# Calendar-date daily window starts at 04:00 Asia/Tokyo. Before that hour a
# scheduled callback is never treated as daily, even if yesterday is incomplete.
_DAILY_DUE_HOUR = 4
# Durable, repo-adjacent state for the last fully successful Tokyo daily cycle.
# Kept outside the transaction journal so recovery ambiguity cannot clear it.
_DAILY_DUE_STATE_PATH = path.join(
    path.dirname(path.abspath(__file__)), ".check_update_daily_due.json"
)
_STRAPI_OUTBOX_PATH = path.join(
    path.dirname(path.abspath(__file__)), ".check_update_strapi_outbox.json"
)


class CycleDeadlineExceeded(Exception):
    """Raised cooperatively when an ordinary update cycle's deadline elapses.

    This is a *cooperative* (not forced) deadline: the cycle checks it only at
    safe seams and, on expiry, returns a stable ``deadline_exceeded`` status
    rather than interrupting an in-flight atomic operation. A daily cycle never
    raises this (its deadline is disabled).
    """


class Deadline:
    """Monotonic, finite, non-negative countdown used to cooperatively bound a
    single update cycle.

    The deadline is validated at construction: ``seconds`` must be a finite,
    non-negative real number. A ``None`` deadline is *disabled* (never expires),
    which is what daily cycles use so they are never cooperatively cancelled.

    Checks use a monotonic clock (``time.monotonic``) so wall-clock jumps (NTP,
    suspend/resume) cannot shorten or extend the budget.
    """

    def __init__(self, seconds: float | None) -> None:
        # ``None`` disables the deadline entirely (used by daily cycles).
        if seconds is None:
            self._deadline: float | None = None
            return
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
            raise ValueError("deadline seconds must be a real number or None")
        is_finite = seconds == seconds and seconds not in (float("inf"), float("-inf"))
        if not is_finite:
            raise ValueError("deadline seconds must be finite")
        if seconds < 0:
            raise ValueError("deadline seconds must be non-negative")
        self._deadline = _monotonic() + float(seconds)

    @property
    def enabled(self) -> bool:
        """``True`` when the deadline is active; ``False`` when disabled."""
        return self._deadline is not None

    def expired(self) -> bool:
        """Return ``True`` if the deadline is enabled and has elapsed."""
        if self._deadline is None:
            return False
        return _monotonic() >= self._deadline

    def check(self) -> None:
        """Raise :class:`CycleDeadlineExceeded` if the deadline has elapsed.

        A disabled deadline never raises.
        """
        if self.expired():
            raise CycleDeadlineExceeded("update cycle deadline exceeded")


def _staging_master_root() -> str:
    return _MASTER_STAGING_ROOT or masterdb_diff_folder_path


def _staging_i18n_root() -> str:
    return _I18N_STAGING_ROOT or i18n_diff_folder_path


def _master_staging_parent() -> str:
    """Legacy ``<repo>.staging`` parent directory (cleared on a clean abort)."""
    return masterdb_diff_folder_path + ".staging"


def _i18n_staging_parent() -> str:
    return i18n_diff_folder_path + ".staging"


def _clear_staging_dir(staging_root: str) -> None:
    if path.exists(staging_root):
        shutil.rmtree(staging_root, ignore_errors=True)


def _clear_staging_dir_safe(staging_root: str) -> None:
    """Clear a staging root, logging (not raising) if removal fails.

    Cleanup runs inside a ``finally`` during a publication failure; it must never
    mask the original :class:`PublicationError` with a cleanup exception, so any
    error is logged and swallowed. This only removes the staging directories —
    the already-``os.replace``'d dirty working trees are intentionally left intact.
    """
    try:
        _clear_staging_dir(staging_root)
    except Exception:  # noqa: BLE001 - cleanup must not override publication error
        logger.exception(
            "[publish] failed to clear staging root %s; ignoring cleanup error",
            staging_root,
        )


class PublicationError(Exception):
    """Raised when a staged file's ``os.replace`` into a formal working tree fails.

    Distinct from a generation/validation failure: the staged candidates are
    discarded and the cycle reports ``publication_failed`` (generation and
    validation of the same candidate still count as ``generation_failed``).
    """


class RemoteSnapshotError(JournalError):
    """Raised when the authoritative remote snapshot cannot be captured.

    This is distinct from generation/publication failure: the transaction has
    not yet been published, and no remote mutation is permitted.
    """


class RemoteProbeError(RemoteSnapshotError):
    """Raised when a post-push authoritative probe cannot be completed."""


def _validate_staged_json(file_path: str) -> None:
    """Re-read a staged file to confirm it is valid JSON (parse-only)."""
    with open(file_path, encoding="utf-8") as f:
        json.load(f)


def _write_master_file(relpath: str, data: Any) -> None:
    """Write a master-data file to the active root and record/validate it.

    When master output is disabled (``update_options['master']`` is False) this
    is a no-op: no file is written and nothing is recorded, so a disabled master
    repository never receives a manifest entry and is never committed.

    When a cycle is active (``_STAGING_MANIFEST`` set) the file is written to
    the staging root, validated, and added to the explicit publish manifest so
    the later commit stages only these paths.
    """
    if not update_options["master"]:
        return
    root = _staging_master_root()
    file_path = path.join(root, relpath)
    parent = path.dirname(file_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    fsync_file(file_path)
    if _STAGING_MANIFEST is not None:
        _validate_staged_json(file_path)
        if relpath not in _STAGING_MANIFEST["master"]:
            _STAGING_MANIFEST["master"].append(relpath)


def _write_i18n_file(filename: str, payload: Any) -> None:
    """Write an i18n file (under ``ja/``) to the active root and record/validate.

    When i18n output is disabled (``update_options['i18n']`` is False) this is a
    no-op: no file is written and nothing is recorded, so a disabled i18n
    repository never receives a manifest entry and is never committed.
    """
    if not update_options["i18n"]:
        return
    root = _staging_i18n_root()
    file_path = path.join(root, "ja", filename)
    parent = path.dirname(file_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    fsync_file(file_path)
    if _STAGING_MANIFEST is not None:
        _validate_staged_json(file_path)
        rel = path.join("ja", filename)
        if rel not in _STAGING_MANIFEST["i18n"]:
            _STAGING_MANIFEST["i18n"].append(rel)


def day_change_func():
    """Daily (04:00) full-refresh cycle, run inside the cycle lock."""
    _run_update_cycle(daily=True)


def try_update_func():
    """Ordinary update trigger (called by legacy entry points).

    This is a thin delegate: every decision (maintenance, new-version,
    candidate) and every write/commit runs inside the single locked cycle so
    no ``remote().pull()`` is ever used and no side effects happen outside the
    cycle.
    """
    logger.info("Check update triggered by cron job")
    _run_update_cycle(daily=False)


def try_update_simple_func():
    """Simple-mode update trigger.

    Only maps the simple-mode entry to the unified locked cycle. All candidate
    determination, generation, publication, and commits happen inside the cycle;
    this wrapper never assigns the published ``version_info`` or commits outside
    the cycle.
    """
    logger.info("Check update triggered in simple mode")
    _run_update_cycle(daily=False)


def _write_i18n_json(filename: str, payload: dict) -> None:
    _write_i18n_file(filename, payload)


def _strapi_outbox_path() -> str:
    return _STRAPI_OUTBOX_PATH


def _strapi_outbox() -> StrapiOutbox:
    return StrapiOutbox(_strapi_outbox_path())


def _post_strapi_ids(endpoint: str, ids: list[int]) -> None:
    """Persist a deferred Strapi notification instead of posting in generation.

    The historical helper name is kept for the i18n generation call sites, but
    its side effect is now durable local enqueue only. Delivery is unlocked later
    by the Git publication transaction id after commit/push success.
    """
    try:
        _strapi_outbox().enqueue(
            endpoint,
            ids,
            transaction_id=_ACTIVE_TXN_ID,
        )
    except StrapiOutboxError:
        # Fail closed for malformed local state: do not drop diagnostics or post
        # directly, and do not continue generation as if Strapi state were safely
        # recorded.
        logger.exception("[_post_strapi_ids] failed to persist Strapi outbox record")
        raise


def _mark_strapi_transaction_ready(transaction_id: str | None) -> None:
    if not transaction_id:
        return
    try:
        _strapi_outbox().mark_transaction_ready(transaction_id)
    except Exception as err:  # noqa: BLE001 - readiness must fail closed
        logger.exception("[strapi_outbox] failed to mark transaction ready")
        # This is a durable checkpoint between Git success and journal
        # deletion.  Callers must retain the journal so recovery can retry it.
        if isinstance(err, StrapiOutboxError):
            raise
        raise StrapiOutboxError("failed to persist Strapi readiness") from err


def _drain_ready_strapi_outbox() -> None:
    try:
        result = _strapi_outbox().drain(
            base_url=strapi_base_url,
            token=strapi_token,
            timeout=60,
        )
    except StrapiOutboxError:
        logger.exception("[strapi_outbox] malformed state; delivery disabled")
        return
    if result["sent"] or result["failed"]:
        logger.info(
            "[strapi_outbox] delivery sent=%s failed=%s retained=%s",
            result["sent"],
            result["failed"],
            result["retained"],
        )


I18N_CARD_ID_THRESHOLD = 500
I18N_MUSIC_ID_THRESHOLD = 290
I18N_EVENT_ID_THRESHOLD = 70
I18N_VIRTUAL_LIVE_ID_THRESHOLD = 180


def _update_i18n_cards(data: list) -> None:
    _write_i18n_json("card_prefix.json", {elem["id"]: elem["prefix"] for elem in data})
    _write_i18n_json(
        "card_skill_name.json", {elem["id"]: elem["cardSkillName"] for elem in data}
    )
    _write_i18n_json(
        "card_gacha_phrase.json",
        {
            elem["id"]: elem["gachaPhrase"]
            for elem in data
            if elem["gachaPhrase"] != "-"
        },
    )
    _post_strapi_ids(
        "cards/fromDB",
        [elem["id"] for elem in data if elem["id"] > I18N_CARD_ID_THRESHOLD],
    )


def _update_i18n_musics(data: list) -> None:
    _write_i18n_json("music_titles.json", {elem["id"]: elem["title"] for elem in data})
    _post_strapi_ids(
        "musics/fromDB",
        [elem["id"] for elem in data if elem["id"] > I18N_MUSIC_ID_THRESHOLD],
    )


def _update_i18n_events(data: list) -> None:
    _write_i18n_json("event_name.json", {elem["id"]: elem["name"] for elem in data})
    _post_strapi_ids(
        "events/fromDB",
        [elem["id"] for elem in data if elem["id"] > I18N_EVENT_ID_THRESHOLD],
    )


def _update_i18n_virtual_lives(data: list) -> None:
    _write_i18n_json(
        "virtualLive_name.json", {elem["id"]: elem["name"] for elem in data}
    )
    _post_strapi_ids(
        "virtual-lives/fromDB",
        [elem["id"] for elem in data if elem["id"] > I18N_VIRTUAL_LIVE_ID_THRESHOLD],
    )


def _update_i18n_event_stories(data: list) -> None:
    _write_i18n_json(
        "event_story_episode_title.json",
        {
            f"{episode['eventStoryId']}-{episode['episodeNo']}": episode["title"]
            for elem in data
            for episode in elem["eventStoryEpisodes"]
        },
    )


def _update_i18n_stamps(data: list) -> None:
    _write_i18n_json(
        "stamp_name.json",
        {
            elem["id"]: re.sub(r"^.*:", "", re.sub(r"\[.*\]", "", elem["name"]))
            for elem in data
        },
    )


I18N_SIMPLE_FILE_BUILDERS = {
    "cardEpisodes": (
        "card_episode_title.json",
        lambda data: {elem["title"]: elem["title"] for elem in data},
    ),
    "musicVocals": (
        "music_vocal.json",
        lambda data: {elem["musicVocalType"]: elem["caption"] for elem in data},
    ),
    "gachas": (
        "gacha_name.json",
        lambda data: {elem["id"]: elem["name"] for elem in data},
    ),
    "honors": (
        "honor_name.json",
        lambda data: {elem["name"]: elem["name"] for elem in data},
    ),
    "honorGroups": (
        "honorGroup_name.json",
        lambda data: {elem["id"]: elem["name"] for elem in data},
    ),
    "beginnerMissions": (
        "beginner_mission.json",
        lambda data: {elem["id"]: elem["sentence"] for elem in data},
    ),
    "honorMissions": (
        "honor_mission.json",
        lambda data: {elem["id"]: elem["sentence"] for elem in data},
    ),
    "normalMissions": (
        "normal_mission.json",
        lambda data: {elem["id"]: elem["sentence"] for elem in data},
    ),
    "cheerfulCarnivalSummaries": (
        "cheerful_carnival_themes.json",
        lambda data: {elem["id"]: elem["theme"] for elem in data},
    ),
    "cheerfulCarnivalTeams": (
        "cheerful_carnival_teams.json",
        lambda data: {elem["id"]: elem["teamName"] for elem in data},
    ),
}


I18N_SPECIAL_HANDLERS = {
    "cards": _update_i18n_cards,
    "musics": _update_i18n_musics,
    "events": _update_i18n_events,
    "virtualLives": _update_i18n_virtual_lives,
    "eventStories": _update_i18n_event_stories,
    "stamps": _update_i18n_stamps,
}


scheduler = BlockingScheduler(timezone=_TOKYO_TZ)


def scheduled_update_job() -> None:
    """Single scheduler entry, fired every 30 minutes in Asia/Tokyo.

    Daily semantics are calendar-date based: once the Tokyo clock is at/after
    04:00, any half-hour callback may run a full daily cycle until that Tokyo
    calendar date has completed a successful full publish. Before 04:00 the
    callback is always ordinary. The cycle itself owns the locks, so overlapping
    triggers skip rather than queue stale work.
    """
    now = datetime.now(_TOKYO_TZ)
    daily = _is_daily_run(now)
    logger.info(
        "[scheduled_update_job] triggered at %s (daily=%s)",
        now.strftime("%Y-%m-%d %H:%M %Z"),
        daily,
    )
    _run_update_cycle(daily=daily)


# One half-hour entry under an explicit Asia/Tokyo trigger. After 04:00 Tokyo
# time, late/coalesced callbacks still promote to daily until the date completes.
# max_instances=1 + coalesce ensure a single in-flight run and de-duplicated
# misfires.
scheduled_trigger = CronTrigger(
    minute="0,30",
    second="0",
    timezone=_TOKYO_TZ,
)
scheduler.add_job(
    scheduled_update_job,
    scheduled_trigger,
    name="scheduled_update_job",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=300,
)


def update_i18n_files(key: str, data: list):
    special_handler = I18N_SPECIAL_HANDLERS.get(key)
    if special_handler:
        special_handler(data)
        return

    simple_writer = I18N_SIMPLE_FILE_BUILDERS.get(key)
    if not simple_writer:
        return

    filename, payload_builder = simple_writer
    _write_i18n_json(filename, payload_builder(data))


def _require_dict_response(value: Any, operation: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{operation} returned invalid object data")
    return value


def get_splitted_master_data() -> dict[str, Any]:
    global pjsk_region
    global version_info

    master_split_paths: list[str] = jsonrpc_client.request("master_split_paths")

    # download every split via the scoped, allowlisted RPC
    master_data_raw = []
    for split_path in master_split_paths:
        logger.debug("[get_splitted_master_data] fetch split %s", split_path)
        master_data_raw.append(
            jsonrpc_client.request("fetch_master_split", [split_path])
        )

    master_data: dict[str, Any] = {}
    for idx, split_data_raw in enumerate(master_data_raw):
        logger.debug(
            f"[get_splitted_master_data] merging split {master_split_paths[idx]}"
        )
        master_data |= _require_dict_response(split_data_raw, "fetch master split")

    # Validate the merged master-data object/list shape before generation. A
    # corrupt split (scalar where a list/object is expected, or an i18n record
    # missing its ``id``) fails here with a precise diagnostic rather than
    # during i18n file writing, which would otherwise leave a half-published
    # staging tree.
    try:
        validate_master_data(master_data, source="master-data")
    except ResponseValidationError as error:
        raise RuntimeError(f"Invalid master data response: {error}") from error

    return master_data


def download_nuverse_master_data(cdn_version: int) -> dict[str, Any]:
    base_url = nuverse_master_data_base_url[pjsk_region]

    if check_update_simple_mode:
        res = requests.get(f"{base_url}/master-data-{cdn_version}.info", timeout=150)
        res.raise_for_status()
        raw = decrypt_msgpack(res.content)
    else:
        raw = _require_dict_response(
            jsonrpc_client.request(
                "request_and_decrypt", [f"{base_url}/master-data-{cdn_version}.info"]
            ),
            "download Nuverse master data",
        )

    # Validate the master-data object/list shape before generation.
    try:
        return validate_master_data(raw, source="master-data")
    except ResponseValidationError as error:
        raise RuntimeError(f"Invalid master data response: {error}") from error


def fetch_simple_version_info() -> dict[str, Any]:
    if not check_update_versions_url:
        raise RuntimeError(
            "CHECK_UPDATE_VERSIONS_URL is required in simple check-update mode"
        )

    res = requests.get(check_update_versions_url, timeout=150)
    res.raise_for_status()
    raw = _require_dict_response(res.json(), "fetch simple version info")
    # Simple version info carries the same required version fields as the
    # full ``version_info`` boundary. cn/tw/kr simple feeds also carry a
    # ``cdnVersion`` that downstream consumers require, so enforce it there.
    try:
        return validate_version_info(
            raw, require_cdn_version=pjsk_region in ("cn", "tw", "kr")
        )
    except ResponseValidationError as error:
        raise RuntimeError(f"Invalid simple version info response: {error}") from error


def check_versions_simple() -> dict[str, Any]:
    """Return a candidate version check without advancing the published global.

    In the unified-cycle design the candidate (``candidate_version_info``) and
    the change flag (``new_version``) are returned for the locked cycle to
    consume; this helper never mutates the published ``version_info`` on its own
    (it previously advanced it outside the locked cycle, which is unsafe).
    """
    curr_ver_info = fetch_simple_version_info()
    if version_info is None:
        return {
            "maintenance": False,
            "new_version": True,
            "candidate_version_info": curr_ver_info,
        }

    new_version = (
        version_info.get("dataVersion") != curr_ver_info.get("dataVersion")
        or version_info.get("assetVersion") != curr_ver_info.get("assetVersion")
        or version_info.get("appVersion") != curr_ver_info.get("appVersion")
        or version_info.get("cdnVersion") != curr_ver_info.get("cdnVersion")
    )

    return {
        "maintenance": False,
        "new_version": new_version,
        "candidate_version_info": curr_ver_info,
    }


def _refresh_version_info_from_source() -> dict[str, Any]:
    logger.info("[refresh_version] fetching version info from %s server", pjsk_region)
    if check_update_simple_mode:
        return fetch_simple_version_info()

    if pjsk_region in ("jp", "en"):
        if not jsonrpc_client.request("is_login"):
            jsonrpc_client.request("login")
        else:
            logger.debug(
                "[refresh_version] refresh split master data list without "
                "running full login workflow"
            )
            jsonrpc_client.request("refresh_master_split_paths")
    return _validate_fetched_version_info(
        jsonrpc_client.request("version_info"),
        require_cdn_version=pjsk_region in ("cn", "tw", "kr"),
    )


def _validate_fetched_version_info(
    raw: object, *, require_cdn_version: bool = False
) -> dict[str, Any]:
    """Validate the upstream ``version_info`` boundary before use.

    Raises a clear diagnostic error on malformed/partial version info so a bad
    candidate never advances the published ``version_info`` global. ``cdnVersion``
    is required (and type-checked) for cn/tw/kr version data.
    """
    try:
        return validate_version_info(raw, require_cdn_version=require_cdn_version)
    except ResponseValidationError as error:
        raise RuntimeError(f"Invalid version info response: {error}") from error


def _fetch_master_data_by_region(candidate: dict[str, Any] | None) -> dict[str, Any]:
    if pjsk_region in ("jp", "en"):
        return get_splitted_master_data()
    if candidate is None:
        raise RuntimeError("Refresh version info before fetching master data")
    return download_nuverse_master_data(candidate["cdnVersion"])


# The Python port keeps the broader keyword set used by downstream master-data
# handling so the same merge path covers tips/music/card records as well.
def _resolve_master_id_key(key: str) -> str | None:
    if any(
        x in key
        for x in [
            "event",
            "gacha",
            "virtual",
            "cheerfulCarnival",
            "tips",
            "music",
            "card",
        ]
    ) and (pjsk_region not in ["cn", "tw", "kr"] and key != "eventCards"):
        return "id"
    if pjsk_region in ["cn", "tw", "kr"] and key == "eventCards":
        return "cardId"
    return None


def _convert_master_records_for_region(
    key: str,
    file_data: Any,
    current_structures: dict[str, list],
) -> tuple[Any, int | None]:
    if not (pjsk_region in ["cn", "tw", "kr"] and key in current_structures):
        return file_data, None
    if not isinstance(file_data, list):
        raise RuntimeError(f"Expected list master data for {key}")

    converted_file_data = []
    last_record_idx = None
    for record_idx, file_datum in enumerate(file_data):
        last_record_idx = record_idx
        converted_file_data.append(
            convert_array_to_dict(
                file_datum,
                current_structures[key],
                structure_name=key,
                node_path=f"{key}[{record_idx}]",
            )
        )
    return converted_file_data, last_record_idx


def _merge_existing_file_data(
    file_path: str,
    incoming_data: list,
    id_key: str | None,
) -> list:
    if id_key is None or not path.exists(file_path):
        return incoming_data

    with open(file_path) as f:
        try:
            old_data = json.load(f)
        except json.JSONDecodeError:
            old_data = []

    if not (
        isinstance(old_data, list)
        and len(old_data) > 0
        and old_data[0].get(id_key) is not None
    ):
        return incoming_data

    value_ids = {item[id_key] for item in incoming_data}
    merged = [x for x in old_data if x[id_key] not in value_ids] + incoming_data
    merged.sort(key=lambda x: x[id_key])
    return merged


def _write_compact_master_alias_if_needed(key: str, file_data: Any) -> None:
    if not (pjsk_region in ["cn", "tw", "kr"] and key.startswith("compact")):
        return
    new_key = key[len("compact") :]
    new_key = new_key[:1].lower() + new_key[1:]
    new_file_data = restore_compact_data(file_data)
    _write_master_file(f"{new_key}.json", new_file_data)


def refresh_version(candidate: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fetch master data and generate all master/i18n JSON for a candidate.

    The cycle passes the explicit candidate version (returned by
    ``_refresh_version_info_from_source``) so generation, conversion, and the
    ``versions.json`` write all use the *candidate* — not the published global
    ``version_info``. The global is only advanced by ``_generate_and_publish``
    after every staged file is generated, validated, and published with
    ``os.replace``. A generation/validation failure therefore leaves both the
    global published ``version_info`` and the formal ``versions.json`` unchanged.

    Returns the candidate used for this generation so the caller can advance the
    published global only after publication succeeds. When called without a
    candidate (legacy/standalone path) the source is fetched here and used
    locally; the global is still not advanced inside this function so callers
    remain responsible for publication.
    """
    logger.debug("[refresh_version] called")

    if candidate is None:
        candidate = _refresh_version_info_from_source()
    else:
        # Validate the explicit candidate boundary before writing ``versions.json``
        # or using it for generation; a malformed candidate must fail closed rather
        # than publish a corrupt ``versions.json`` or drive generation from bad data.
        candidate = _validate_fetched_version_info(
            candidate,
            require_cdn_version=pjsk_region in ("cn", "tw", "kr"),
        )
    logger.debug("[refresh_version] using candidate version info: %s", candidate)
    _write_master_file("versions.json", candidate)

    logger.debug("[refresh_version] fetching master db")
    master_data: dict[str, Any] = _fetch_master_data_by_region(candidate)
    logger.debug("[refresh_version] write master db to separate json files by keys")
    structures_app_ver = candidate.get("appVersion") or getenv("APP_VER", "")
    current_structures = get_structures_for_app_ver(structures_app_ver)
    current_structure_version = resolve_structure_compatibility_version(
        structures_app_ver
    )
    if pjsk_region in ["cn", "tw", "kr"]:
        logger.info(
            "[refresh_version] using %s structures for appVersion=%s",
            current_structure_version or "base",
            structures_app_ver or "N/A",
        )

    for key, value in master_data.items():
        file_data = value
        logger.debug("[refresh_version] start writing master db %s.json", key)
        last_record_idx: int | None = None

        try:
            file_data, last_record_idx = _convert_master_records_for_region(
                key, file_data, current_structures
            )
            id_key = _resolve_master_id_key(key)
            working_path = path.join(masterdb_diff_folder_path, f"{key}.json")
            file_data = _merge_existing_file_data(working_path, file_data, id_key)
            _write_master_file(f"{key}.json", file_data)
            _write_compact_master_alias_if_needed(key, file_data)
        except Exception:
            logger.exception(
                (
                    "[refresh_version] failed writing master db key=%s "
                    "appVersion=%s structureVersion=%s last_record_idx=%s"
                ),
                key,
                structures_app_ver or "N/A",
                current_structure_version or "base",
                last_record_idx,
            )
            raise

        logger.debug("[refresh_version] wrote master db %s.json", key)

        if update_options["i18n"]:
            logger.debug(
                f"[refresh_version] write i18n json file for {key}.json if necessary"
            )
            update_i18n_files(key, file_data)

    logger.debug("[refresh_version] finished")
    return candidate


def save_info_from_suite_user():
    suite_user = jsonrpc_client.request("login_user_info")

    logger.debug("[save_info_from_suite_user] write user home banners")
    _write_master_file("userHomeBanners.json", suite_user["userHomeBanners"])

    if pjsk_region == "en":
        refresh_information()
    elif suite_user.get("userInformations", None):
        logger.debug("[save_info_from_suite_user] write user informations")
        _write_master_file("userInformations.json", suite_user["userInformations"])

    logger.debug("[save_info_from_suite_user] finished")
    return suite_user


def refresh_information():
    logger.debug("[refresh_information] get informations")
    res = jsonrpc_client.request("fetch_information")
    # Validate the /information object/list shape before writing so a scalar
    # where a list is expected fails with a clear diagnostic instead of a bare
    # ``KeyError``/``TypeError`` during file write.
    try:
        res = validate_information(res)
    except ResponseValidationError as error:
        raise RuntimeError(f"Invalid information response: {error}") from error

    logger.debug("[refresh_information] write user informations")
    _write_master_file("userInformations.json", res["informations"])


def _commit_diff(
    repo: Repo | None,
    operation: str,
    folder_label: str,
    commit_message: str,
    author: Actor,
    paths: list[str] | None = None,
    version: dict[str, Any] | None = None,
) -> GitResult:
    """Stage (explicit ``paths`` when given) and commit only — no push.

    Contract:
    - ``repo`` is ``None`` or no version is available -> ``FAILED`` (these
      are operational errors, never a benign "nothing to do").
    - repository clean -> ``NOTHING_TO_DO``.
    - stage/commit failure -> ``FAILED`` (``reason="commit_failed"``), with the
      local repo left untouched.

    When ``paths`` is provided (Phase 4.2 cycle), only those explicit paths are
    staged; otherwise the historical broad ``index.add("**")`` is used. The
    original ``master-db-diff-bot`` / ``i18n-diff-bot`` actors are passed by the
    wrapper. The historical boolean contract is preserved via
    :meth:`GitResult.__bool__`.

    ``version`` is the *explicit* candidate/version data for this cycle (passed
    by the locked cycle). When omitted (legacy/standalone callers) the published
    global ``version_info`` is used. This keeps commit construction from ever
    indexing a ``None`` global: an i18n-only first run supplies its candidate
    here even though the published global stays ``None`` (master is disabled and
    never advances it).
    """
    if repo is None:
        logger.error("[%s] repository not initialized", operation)
        return GitResult(
            outcome=GitOutcome.FAILED,
            reason="repo_missing",
            operation=operation,
        )
    # Prefer the explicit candidate; fall back to the published global only for
    # legacy/standalone callers. Never index a None global directly.
    effective_version = version if version is not None else version_info
    if effective_version is None:
        logger.error("[%s] version info not available; cannot build commit", operation)
        return GitResult(
            outcome=GitOutcome.FAILED,
            reason="version_info_missing",
            operation=operation,
        )

    if not repo.is_dirty(untracked_files=True):
        return GitResult(
            outcome=GitOutcome.NOTHING_TO_DO,
            reason="clean",
            operation=operation,
        )

    # Explicit empty manifest: there is nothing for this repository to commit.
    # Do not stage/commit, and crucially do not fall back to a broad add that
    # would sweep unrelated dirty files into the commit. Only a ``None`` paths
    # (legacy/standalone callers) uses the historical broad ``index.add("**")``.
    if paths is not None and len(paths) == 0:
        logger.debug(
            "[%s] explicit empty path list; no staged changes to commit", operation
        )
        return GitResult(
            outcome=GitOutcome.NOTHING_TO_DO,
            reason="no_staged_paths",
            operation=operation,
        )

    try:
        logger.debug("[%s] add files to staged in %s", operation, folder_label)
        if paths is not None and len(paths) > 0:
            repo.index.add(paths)
        else:
            repo.index.add("**")

        logger.debug("[%s] commit staged changes in %s", operation, folder_label)
        repo.index.commit(commit_message, author=author)
    except Exception as err:  # noqa: BLE001 - commit failure is its own contract
        logger.exception("[%s] failed to stage/commit", operation)
        return GitResult(
            outcome=GitOutcome.FAILED,
            reason="commit_failed",
            operation=operation,
            detail=str(err),
        )

    return GitResult(
        outcome=GitOutcome.OK,
        reason="committed",
        operation=operation,
        local_sha=_safe_head_sha(repo),
    )


def _push_diff(
    repo: Repo | None,
    operation: str,
    expected_sha: str | None = None,
    old_remote_sha: str | None = None,
    remote_endpoint_fingerprint: str | None = None,
    remote_state: RepoState | None = None,
) -> GitResult:
    """Push the local HEAD and surface a credential-safe result.

    A push failure or unverified push returns ``PENDING_PUSH``; the local
    commit is *kept* and never deleted, recloned, reset, or force-pushed. The
    warning (when pending) contains only operation, reason, and retained local
    SHA — never the remote URL or exception detail.

    ``expected_sha`` (when provided) is forwarded to ``push_current_head`` as an
    explicit SHA barrier: the push is verified against the exact target commit
    SHA recorded in the journal, so a divergent/force state is rejected rather
    than silently overwritten.
    """
    if repo is None:
        return GitResult(
            outcome=GitOutcome.FAILED,
            reason="repo_missing",
            operation=operation,
        )
    remote_url = None
    if remote_endpoint_fingerprint is not None:
        remote_url, actual_fingerprint = _remote_endpoint(repo, operation)
        if actual_fingerprint != remote_endpoint_fingerprint:
            raise JournalError(f"remote endpoint changed for {operation}")
    push_result = push_current_head(
        repo,
        branch="main",
        require_remote_branch=True,
        expected_sha=expected_sha,
        old_remote_sha=old_remote_sha,
        remote_url=remote_url,
    )
    if expected_sha is not None and remote_state is not None:
        try:
            confirmed_sha = _probe_remote(repo, operation, remote_state)
        except RemoteProbeError:
            return GitResult(
                outcome=GitOutcome.PENDING_PUSH,
                reason="remote_probe_unconfirmed",
                operation=operation,
                local_sha=push_result.local_sha or expected_sha,
            )
        if confirmed_sha == expected_sha:
            return GitResult(
                outcome=GitOutcome.OK,
                reason="remote_probe_confirmed",
                operation=operation,
                local_sha=expected_sha,
                remote_sha=confirmed_sha,
            )
        reason = (
            push_result.reason
            if confirmed_sha == remote_state.remote_base_sha
            else "remote_mismatch"
        )
        return GitResult(
            outcome=GitOutcome.PENDING_PUSH,
            reason=reason,
            operation=operation,
            local_sha=push_result.local_sha or expected_sha,
            remote_sha=confirmed_sha,
        )
    if push_result.outcome is GitOutcome.PENDING_PUSH:
        logger.warning(
            "[%s] push pending (commit retained): reason=%s local_sha=%s",
            operation,
            push_result.reason,
            push_result.local_sha,
        )
    return push_result


def _safe_head_sha(repo: Repo) -> str | None:
    try:
        return repo.head.commit.hexsha
    except Exception:  # noqa: BLE001 - unborn repository
        return None


def _canonical_endpoint(raw: str) -> str:
    """Return a normalized identity used only for endpoint binding."""
    value = raw.strip()
    if "://" not in value and ":" not in value.split("/", 1)[0]:
        return path.realpath(path.abspath(value))
    if "://" not in value:
        user_host, remote_path = value.split(":", 1)
        if "@" in user_host:
            user, host = user_host.rsplit("@", 1)
            user_host = f"{user}@{host.lower()}"
        else:
            user_host = user_host.lower()
        return f"ssh://{user_host}{posixpath.normpath('/' + remote_path)}"
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        raise RemoteSnapshotError("remote endpoint is not canonicalizable")
    if parsed.scheme.lower() == "file":
        return path.realpath(parsed.path)
    host = parsed.hostname
    if not host:
        raise RemoteSnapshotError("remote endpoint host is missing")
    userinfo = ""
    if "@" in parsed.netloc:
        # Keep userinfo in the one-way identity.  It is never persisted or sent
        # as a log value, but rotating HTTPS credentials must invalidate a
        # recovery binding just like changing an SSH user.
        userinfo = parsed.netloc.rsplit("@", 1)[0] + "@"
    netloc = userinfo + host.lower()
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    normalized_path = posixpath.normpath(parsed.path or "/")
    return urlunsplit((parsed.scheme.lower(), netloc, normalized_path, "", ""))


def _remote_endpoint(repo: Repo, key: str) -> tuple[str, str]:
    """Resolve one origin endpoint and reject unsafe fetch/push ambiguity."""
    remote_name = "origin"
    try:
        remote = repo.remote(remote_name)
        fetch_urls = list(remote.urls)
        push_urls = []
        try:
            push_urls = repo.git.config(
                "--get-all", f"remote.{remote_name}.pushurl"
            ).splitlines()
        except Exception:
            push_urls = []
        if len(fetch_urls) != 1 or len(push_urls) > 1:
            raise RemoteSnapshotError(
                f"remote endpoint cardinality invalid for {key!r}"
            )
        fetch_raw = fetch_urls[0]
        fetch_identity = _canonical_endpoint(fetch_raw)
        endpoint = fetch_raw
        if push_urls:
            push_raw = push_urls[0]
            if _canonical_endpoint(push_raw) != fetch_identity:
                raise RemoteSnapshotError(
                    f"remote fetch/push endpoint mismatch for {key!r}"
                )
            endpoint = push_raw
        fingerprint = hashlib.sha256(
            _canonical_endpoint(endpoint).encode("utf-8")
        ).hexdigest()
        return endpoint, fingerprint
    except RemoteSnapshotError:
        raise
    except (OSError, ValueError, subprocess.CalledProcessError) as err:
        raise RemoteSnapshotError(
            f"remote endpoint resolution failed for {key!r}"
        ) from err


def _remote_snapshot(
    repo: Repo, key: str, expected_fingerprint: str | None = None
) -> tuple[str, str, str]:
    """Read exactly one authoritative origin/main ref without updating locals."""
    remote_name = "origin"
    remote_ref = "refs/heads/main"
    remote_url, fingerprint = _remote_endpoint(repo, key)
    if expected_fingerprint is not None and fingerprint != expected_fingerprint:
        raise JournalError(f"remote endpoint changed for {key!r}")
    try:
        result = subprocess.run(
            ["git", "ls-remote", remote_url, remote_ref],
            cwd=repo.working_tree_dir,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as err:
        raise RemoteProbeError(f"remote snapshot failed for {key!r}") from err
    rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != remote_ref:
        raise RemoteProbeError(f"remote snapshot missing or ambiguous for {key!r}")
    remote_sha = rows[0][0]
    if not re.fullmatch(r"[0-9a-f]{40}", remote_sha):
        raise RemoteProbeError(f"remote snapshot SHA invalid for {key!r}")
    return remote_sha, remote_name, remote_ref


def _capture_remote_base(key: str, repo: Repo, state: RepoState) -> None:
    remote_url, fingerprint = _remote_endpoint(repo, key)
    remote_sha, remote_name, remote_ref = _remote_snapshot(repo, key, fingerprint)
    if state.base_sha is None or remote_sha != state.base_sha:
        raise JournalError(f"remote base mismatch for {key!r}")
    state.remote_base_sha = remote_sha
    state.remote_name = remote_name
    state.remote_ref = remote_ref
    state.remote_endpoint_fingerprint = fingerprint


def _probe_remote(repo: Repo, key: str, state: RepoState) -> str:
    remote_sha, _name, _ref = _remote_snapshot(
        repo, key, state.remote_endpoint_fingerprint
    )
    return remote_sha


def _load_bound_journal() -> TransactionJournal | None:
    """Load the sole journal only when it is bound to the live repositories.

    The journal is disk state, while the configured roots and live Git objects
    are process state.  Recovery and the normal journal-backed commit/push paths
    must validate both before doing any mutation.
    """
    if masterdb_diff_repo is None:
        return None
    master_git_dir = path.realpath(masterdb_diff_repo.git_dir)
    expected_journal = path.join(master_git_dir, "sekai-update", "transaction.json")
    journal = TransactionJournal.load(master_git_dir)
    if journal is None:
        return None
    if path.realpath(journal.journal_path) != path.realpath(expected_journal):
        raise JournalError("journal location is not bound to the live master repo")
    if path.realpath(journal.master_git_dir) != master_git_dir:
        raise JournalError("journal master Git directory is not live")

    actual_roots = {
        "master": path.realpath(masterdb_diff_folder_path),
        "i18n": path.realpath(i18n_diff_folder_path),
    }
    validate_journal_roots(journal, actual_roots=actual_roots)
    live_repos = {"master": masterdb_diff_repo, "i18n": i18n_diff_repo}
    for key in journal.enabled_repos:
        repo = live_repos.get(key)
        if repo is None or not repo.working_tree_dir:
            raise JournalError(f"live repo {key!r} is unavailable")
        if path.realpath(repo.working_tree_dir) != actual_roots[key]:
            raise JournalError(
                f"live repo {key!r} working tree does not match configured root"
            )
    return journal


def commit_master_diff(paths: list[str] | None = None) -> GitResult:
    """Stage, commit, and push master data diffs.

    For the Phase 4.2 cycle this is called with an explicit ``paths`` manifest;
    standalone callers (e.g. bootstrap) may omit it and fall back to broad
    staging. The push is non-destructive; a push failure returns ``PENDING_PUSH``
    with the local commit retained.
    """
    global masterdb_diff_repo
    if version_info is None:
        commit_res = _commit_diff(
            masterdb_diff_repo,
            operation="commit_master_diff",
            folder_label=local_git_folder_names["masterDBDiff"],
            commit_message="",
            author=Actor("master-db-diff-bot", "anonymous@example.com"),
            paths=paths,
        )
    else:
        commit_res = _commit_diff(
            masterdb_diff_repo,
            operation="commit_master_diff",
            folder_label=local_git_folder_names["masterDBDiff"],
            commit_message=(
                f"master version {version_info['dataVersion']} "
                f"asset version {version_info['assetVersion']}"
            ),
            author=Actor("master-db-diff-bot", "anonymous@example.com"),
            paths=paths,
        )
    if commit_res.outcome is not GitOutcome.OK:
        return commit_res
    return _push_diff(masterdb_diff_repo, "push_master_diff")


def commit_i18n_files(paths: list[str] | None = None) -> GitResult:
    """Stage, commit, and push i18n data diffs (see ``commit_master_diff``)."""
    global i18n_diff_repo
    if version_info is None:
        commit_res = _commit_diff(
            i18n_diff_repo,
            operation="commit_i18n_files",
            folder_label=local_git_folder_names["i18n"],
            commit_message="",
            author=Actor("i18n-diff-bot", "anonymous@example.com"),
            paths=paths,
        )
    else:
        commit_res = _commit_diff(
            i18n_diff_repo,
            operation="commit_i18n_files",
            folder_label=local_git_folder_names["i18n"],
            commit_message=(
                f"i18n update for master version {version_info['dataVersion']}"
            ),
            author=Actor("i18n-diff-bot", "anonymous@example.com"),
            paths=paths,
        )
    if commit_res.outcome is not GitOutcome.OK:
        return commit_res
    return _push_diff(i18n_diff_repo, "push_i18n_files")


# --------------------------------------------------------------------------- #
# Phase 4.2: single locked cycle with staged generation + file-atomic publish
# --------------------------------------------------------------------------- #


def _tokyo_now(now: datetime | None = None) -> datetime:
    """Normalize ``now`` to an aware Asia/Tokyo datetime."""
    if now is None:
        return datetime.now(_TOKYO_TZ)
    if now.tzinfo is None:
        return _TOKYO_TZ.localize(now)
    return now.astimezone(_TOKYO_TZ)


def _tokyo_calendar_date(now: datetime | None = None) -> str:
    """Return the Asia/Tokyo calendar date identity as ``YYYY-MM-DD``."""
    return _tokyo_now(now).strftime("%Y-%m-%d")


def _daily_due_state_path() -> str:
    """Default durable path for Tokyo daily completion state (repo-adjacent)."""
    return _DAILY_DUE_STATE_PATH


def _read_last_completed_daily_date(state_path: str | None = None) -> str | None:
    """Load the last fully successful Tokyo daily calendar date, if any.

    Malformed/missing state is treated as incomplete so a daily remains due.
    """
    path_name = state_path or _daily_due_state_path()
    try:
        with open(path_name, encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError):
        logger.warning(
            "[daily_due] unreadable state at %s; treating daily as incomplete",
            path_name,
        )
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("last_completed_tokyo_date")
    if not isinstance(value, str) or not value:
        return None
    return value


def _write_last_completed_daily_date(
    tokyo_date: str, state_path: str | None = None
) -> None:
    """Atomically persist the last fully successful Tokyo daily calendar date.

    Writes through a same-directory temp file, fsyncs the file and parent
    directory, then ``os.replace`` so a crash cannot leave a half-written
    completion marker. Completion is only marked after a full daily cycle
    returns ``ok`` / ``recovered``.
    """
    path_name = state_path or _daily_due_state_path()
    parent = path.dirname(path_name) or "."
    os.makedirs(parent, exist_ok=True)
    tmp_name = path_name + f".tmp.{os.getpid()}"
    payload = {
        "last_completed_tokyo_date": tokyo_date,
        "timezone": "Asia/Tokyo",
    }
    try:
        fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path_name)
        fsync_file(path_name)
        fsync_directory(parent)
    finally:
        if path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _mark_daily_completed(
    now: datetime | None = None, *, due_date: str | None = None
) -> None:
    """Record the captured Tokyo daily due date after a full publish."""
    tokyo_date = due_date or _tokyo_calendar_date(now)
    _write_last_completed_daily_date(tokyo_date)
    logger.info("[daily_due] marked Tokyo date %s completed", tokyo_date)


def _is_daily_run(now: datetime | None = None) -> bool:
    """True when a scheduled callback must run daily/full-refresh semantics.

    Identity is the Asia/Tokyo calendar date after the 04:00 boundary, not the
    exact arrival of a 04:00 callback:

    - before 04:00 Tokyo: always ordinary (never daily);
    - at/after 04:00 Tokyo: daily until that calendar date has a durable
      successful full completion marker.

    Maintenance, lock skips, generation/commit/push failure, and recovery
    ambiguity leave the marker uncleared so a later half-hour callback (or a
    process restart) still promotes to daily for the same Tokyo date.
    """
    tokyo_now = _tokyo_now(now)
    if tokyo_now.hour < _DAILY_DUE_HOUR:
        return False
    completed = _read_last_completed_daily_date()
    return completed != _tokyo_calendar_date(tokyo_now)


def _publish_order(relpaths: list[str]) -> list[str]:
    """Order published paths so ``versions.json`` is moved last.

    The other paths keep their input order (stable, deterministic), with
    ``versions.json`` appended last when present.
    """
    ordered = [p for p in relpaths if p != "versions.json"]
    if "versions.json" in relpaths:
        ordered.append("versions.json")
    return ordered


def _publish_staging(
    staging_root: str,
    dest_root: str,
    relpaths: list[str],
    journal: "TransactionJournal | None" = None,
    repo_key: str | None = None,
) -> None:
    """Atomically publish staged files into the working tree via ``os.replace``.

    ``versions.json`` is published last. After each successful replace the durable
    destination checkpoint (the published file's SHA-256) is persisted to the
    journal (when one is active) so recovery can prove the replace completed
    without re-reading the (now-moved) source.

    If any replacement fails, a :class:`PublicationError` is raised (with the
    already-published dirty working tree left intact for diagnosis). The journal +
    journal-owned staging are RETAINED for durable recovery (a crash/ambiguous
    failure must not erase the source staging that recovery needs). Only pre-journal
    generation/deadline failures clean.
    """
    for rel in _publish_order(relpaths):
        src = path.join(staging_root, rel)
        dst = path.join(dest_root, rel)
        dst_dir = path.dirname(dst)
        if dst_dir:
            os.makedirs(dst_dir, exist_ok=True)
            fsync_directory(dst_dir)
        try:
            os.replace(src, dst)
        except OSError as err:  # publish partially; stop, leave staging intact
            logger.error("[publish] replace failed for %s: %s", rel, err)
            raise PublicationError(f"replace failed for {rel}: {err}") from err
        # Persist the durable destination checkpoint before the next file so a
        # crash mid-publication resumes from the last proven destination.
        fsync_published_file(dst, src)
        if journal is not None and repo_key is not None:
            dest = compute_sha256(dst)
            expected = journal.repos[repo_key].files[rel].source_sha256
            if dest != expected:
                raise PublicationError(f"published content mismatch for {rel}")
            journal.update_repo(repo_key, rel=rel, dest_sha256=dest)


def _build_publishing_journal(
    txn_id: str,
    candidate: "dict[str, Any] | None",
    enabled: list[str],
    publish_order: list[str],
    master_staging: str,
    i18n_staging: str,
    master_manifest: list[str],
    i18n_manifest: list[str],
    master_full_manifest: list[str] | None = None,
    i18n_full_manifest: list[str] | None = None,
    daily_due_date: str | None = None,
) -> "TransactionJournal | None":
    """Build + atomically write the ``publishing`` journal BEFORE the first
    formal ``os.replace``. Returns ``None`` when no journal is warranted (no
    master repo, or an all-disabled cycle that publishes nothing).

    The journal records the COMPLETE manifest (including ``versions.json``) and a
    SHA-256 per staged file, so recovery can prove which replaces completed. The
    ``master_manifest`` / ``i18n_manifest`` arguments are the publication-order
    subsets (``versions.json`` excluded, published last); the optional
    ``*_full_manifest`` arguments carry the complete manifest for the journal record.
    """

    def _build_repo_state(
        key: str, staging_root: str, working_root: str, man: list[str]
    ) -> RepoState:
        files = {}
        for rel in man:
            src = path.join(staging_root, rel)
            files[rel] = FileEntry(source_sha256=compute_sha256(src))
        return RepoState(
            manifest=list(man),
            staging_dir=staging_root,
            repo_root=path.realpath(working_root),
            target_commit_sha=None,
            base_sha=_safe_head_sha(
                masterdb_diff_repo if key == "master" else i18n_diff_repo
            ),
            remote_sha=None,
            remote_base_sha=None,
            remote_name="origin",
            remote_ref="refs/heads/main",
            remote_endpoint_fingerprint="0" * 64,
            files=files,
            commit_state=RepoCommitState.PENDING,
            push_state=RepoPushState.PENDING,
        )

    repos_state: dict[str, RepoState] = {}
    if update_options["master"]:
        full = (
            master_full_manifest
            if master_full_manifest is not None
            else master_manifest
        )
        repos_state["master"] = _build_repo_state(
            "master", master_staging, masterdb_diff_folder_path, full
        )
    if update_options["i18n"]:
        full = i18n_full_manifest if i18n_full_manifest is not None else i18n_manifest
        repos_state["i18n"] = _build_repo_state(
            "i18n", i18n_staging, i18n_diff_folder_path, full
        )

    master_git_dir = masterdb_diff_repo.git_dir if masterdb_diff_repo else None
    # A journal is only meaningful when at least one repository is enabled (an
    # all-disabled cycle publishes nothing and must not create a journal that
    # would later fail validation / recovery).
    if master_git_dir is None or not enabled:
        return None
    journal_candidate = dict(candidate) if candidate is not None else {}
    if daily_due_date is not None:
        # ``candidate`` is also written to versions.json, so keep the durable
        # daily identity in the journal copy rather than changing the payload.
        journal_candidate[_DAILY_DUE_JOURNAL_KEY] = daily_due_date
    journal = TransactionJournal(
        master_git_dir=master_git_dir,
        transaction_id=txn_id,
        candidate=journal_candidate,
        enabled_repos=enabled,
        publish_order=publish_order,
        push_order=[key for key in ("i18n", "master") if key in enabled],
        repos=repos_state,
        phase=TxnPhase.PUBLISHING,
    )
    for key in enabled:
        repo = masterdb_diff_repo if key == "master" else i18n_diff_repo
        if repo is None:
            raise JournalError(f"repo {key!r} unavailable for remote snapshot")
        _capture_remote_base(key, repo, repos_state[key])
    journal.write()
    return journal


def _publish_all_staged(
    journal: "TransactionJournal | None",
    manifest: dict[str, list[str]],
    master_staging: str,
    i18n_staging: str,
    master_manifest: list[str],
    i18n_manifest: list[str],
) -> bool:
    """Publish every staged file via ``os.replace`` (no deadline checks inside or
    after — once the first replace begins the cycle runs to completion).

    Returns ``True`` iff the formal ``versions.json`` was published into the
    master working tree (which is what advances the published global). On a
    publication failure this performs a *clean* abort: clears the staging parents
    and drops the journal (an aborted attempt), then re-raises the original
    :class:`PublicationError` so the caller maps it to ``publication_failed``.
    """
    global_published = False
    try:
        # 1) All non-master-version files across both repositories.
        _publish_staging(
            master_staging,
            masterdb_diff_folder_path,
            master_manifest,
            journal=journal,
            repo_key="master",
        )
        # 2) i18n: everything except versions.json.
        _publish_staging(
            i18n_staging,
            i18n_diff_folder_path,
            i18n_manifest,
            journal=journal,
            repo_key="i18n",
        )
        # 3) global versions.json last and alone, from the master staging root,
        #    but only when master is enabled and actually staged it.
        if "versions.json" in manifest["master"]:
            _publish_staging(
                master_staging,
                masterdb_diff_folder_path,
                ["versions.json"],
                journal=journal,
                repo_key="master",
            )
            global_published = True
    except BaseException:
        # A publication failure AFTER the journal was written must RETAIN the
        # journal + journal-owned staging so durable recovery can finish the work
        # (a crash would leave the same state). Only PRE-journal generation /
        # deadline failures clean them. We never erase the source staging here.
        global _ACTIVE_TXN_ID
        _ACTIVE_TXN_ID = None
        raise
    return global_published


def _generate_and_publish(  # noqa: C901
    daily: bool,
    deadline: "Deadline | None" = None,
    daily_due_date: str | None = None,
) -> dict[str, list[str]]:
    """Generate all candidate JSON into journal-owned staging + validate, then
    (if the cooperative deadline has not elapsed) publish via ``os.replace``.

    Returns the explicit publish ``manifest`` (the historical contract, so
    standalone/integration callers are unaffected). The candidate version is
    stashed in the module-level :data:`_CYCLE_CANDIDATE` for the locked cycle to
    read for commit construction.

    Phase 2 durable protocol: staging is journal-owned
    (``<repo>.staging/<txn_id>/``). After every staged file is generated and
    validated, a ``publishing`` journal is written *atomically* (with ``0600`` +
    fsync) BEFORE the first formal ``os.replace``. The journal records, per repo,
    the manifest, a SHA-256 per staged file, the base/remote SHA, and the
    (still-pending) target commit SHA. A clean publication failure clears the
    staging parent and drops the journal (an aborted attempt); a *crash* leaves
    both on disk for recovery (see :func:`_recover_transaction`).

    The candidate version is fetched up front and used locally for every
    generated file. The global published ``version_info`` is *not* advanced until
    every staged file has been generated, validated, and published into both
    working trees.

    Cooperative deadline (Oracle Gate 1): the LAST safe check happens AFTER all
    staging generation + validation but BEFORE the first formal ``os.replace``
    publish. If it fires there, both staging roots are cleared and
    :class:`CycleDeadlineExceeded` is raised *unchanged* (the outer wrapper
    returns ``deadline_exceeded``); the formal working trees stay byte-identical,
    and no commit/push occurs. The deadline is never checked inside publication
    or after it begins, so once the first ``os.replace`` starts the cycle runs
    through commit/push to completion.

    On any generation/validation failure the staging directories are cleared and
    the exception is re-raised without touching the formal working trees. A
    publication (``os.replace``) failure stops before commit/push and leaves the
    dirty working tree intact.
    """
    global _MASTER_STAGING_ROOT, _I18N_STAGING_ROOT, _STAGING_MANIFEST
    global version_info, _CYCLE_CANDIDATE, _ACTIVE_TXN_ID

    _CYCLE_CANDIDATE = None

    # Phase 2: journal-owned staging sub-directory under the legacy parent.
    txn_id = new_transaction_id()
    _ACTIVE_TXN_ID = txn_id
    master_staging = staging_dir_for(masterdb_diff_folder_path, txn_id)
    i18n_staging = staging_dir_for(i18n_diff_folder_path, txn_id)
    # Clear the legacy parent directories (removes any prior txn sub-dir).
    _clear_staging_dir(_master_staging_parent())
    _clear_staging_dir(_i18n_staging_parent())

    manifest: dict[str, list[str]] = {"master": [], "i18n": []}
    # Candidate is local to this cycle; the published global stays untouched
    # until publication of every staged file succeeds. refresh_version returns
    # the candidate it generated with, so the global is advanced only afterward.
    candidate: dict[str, Any] | None = None
    journal: TransactionJournal | None = None
    try:
        _MASTER_STAGING_ROOT = master_staging
        _I18N_STAGING_ROOT = i18n_staging
        _STAGING_MANIFEST = manifest

        candidate = refresh_version()
        if not check_update_simple_mode and update_options["userInfo"]:
            save_info_from_suite_user()
        if update_options["userInfo"] and pjsk_region != "en":
            refresh_information()
    except Exception:
        logger.exception("[cycle] generation/validation failed; discarding staging")
        _STAGING_MANIFEST = None
        _MASTER_STAGING_ROOT = None
        _I18N_STAGING_ROOT = None
        _ACTIVE_TXN_ID = None
        _clear_staging_dir_safe(_master_staging_parent())
        _clear_staging_dir_safe(_i18n_staging_parent())
        raise
    finally:
        _STAGING_MANIFEST = None
        _MASTER_STAGING_ROOT = None
        _I18N_STAGING_ROOT = None

    # FINAL safe seam: AFTER all staging generation + validation, BEFORE the
    # first formal os.replace publish. If the deadline fires here, clear BOTH
    # staging roots and raise CycleDeadlineExceeded unchanged so the outer
    # wrapper returns deadline_exceeded. Formal trees are untouched; no commit or
    # push happens. This is the last place a cooperative cancellation may occur.
    try:
        _check_deadline(deadline)
    except CycleDeadlineExceeded:
        _clear_staging_dir_safe(_master_staging_parent())
        _clear_staging_dir_safe(_i18n_staging_parent())
        _ACTIVE_TXN_ID = None
        raise

    # Stash the explicit candidate for the cycle's commit construction (kept
    # independent of the published global, which stays None on i18n-only runs).
    _CYCLE_CANDIDATE = candidate

    # Phase 2: build + atomically write the ``publishing`` journal BEFORE the
    # first formal os.replace. The journal records the manifest and the SHA-256
    # of every staged file (so recovery can prove which replaces completed).
    enabled = []
    if update_options["master"]:
        enabled.append("master")
    if update_options["i18n"]:
        enabled.append("i18n")
    publish_order = [k for k in _REPO_ORDER if k in enabled]

    # versions.json is published last and alone from the master staging root.
    master_manifest = [p for p in manifest["master"] if p != "versions.json"]
    i18n_manifest = [p for p in manifest["i18n"] if p != "versions.json"]

    journal = _build_publishing_journal(
        txn_id,
        candidate,
        enabled,
        publish_order,
        master_staging,
        i18n_staging,
        master_manifest,
        i18n_manifest,
        master_full_manifest=list(manifest["master"]),
        i18n_full_manifest=list(manifest["i18n"]),
        daily_due_date=daily_due_date if daily else None,
    )

    # Whether the formal ``versions.json`` was actually published into the master
    # working tree. Only a successful master-enabled publication advances the
    # global. master=False (i18n-only) and all-disabled never reach this branch.
    global_published = _publish_all_staged(
        journal,
        manifest,
        master_staging,
        i18n_staging,
        master_manifest,
        i18n_manifest,
    )

    # Only now — after every staged file was generated, validated, and published
    # into the working trees — do we advance the published global, and only when
    # the formal ``versions.json`` was genuinely published by an enabled master.
    # A ``None`` candidate (legacy caller) or a disabled master keeps the prior
    # published value, so the formal ``versions.json`` and the global stay in
    # lock-step.
    if candidate is not None and global_published:
        version_info = candidate
    return manifest


# --------------------------------------------------------------------------- #
# Phase 2: durable transaction recovery
# --------------------------------------------------------------------------- #
#
# Recovery runs FIRST at the top of the locked cycle, before the maintenance /
# new-version gate, the normal prepare, or any generation. If a durable journal
# from a crashed/interrupted cycle exists, recovery completes the interrupted
# work (publication -> commit -> push) using ordered source/destination hashes
# and returns a distinct stable ``recovered`` status without starting fresh work.
#
# The recovery contract (fail-closed):
#   * Malformed / duplicate / invalid-path journal -> raise JournalError; the
#     cycle surfaces ``journal_invalid`` and performs NO generation/reset/force.
#   * Publication recovery: for each repo file, a matching destination SHA proves
#     the replace already completed; otherwise a matching source (staging) SHA
#     can replace; a missing/mismatched source blocks (preserves state).
#   * Commit recovery: no push until ALL enabled repos have the exact target
#     commit SHA. Journal commit states update atomically after each commit.
#   * Push recovery: i18n -> master, exclusively
#     ``push_current_head(... expected_sha=target_sha ...)``; a remote already at
#     the target SHA counts as verified. After all verified, mark COMPLETED,
#     clean journal-owned staging, then delete the journal.


def _recover_publish_file(
    journal: TransactionJournal, repo_key: str, rel: str, st: RepoState
) -> None:
    """Complete a single file's publication using ordered source/dest hashes.

    Raises :class:`JournalError` (fail closed) if the file cannot be proven to
    be in a consistent state (missing source AND non-matching destination).

    After a successful replace the durable destination checkpoint is persisted
    atomically (before the next file) so a crash mid-recovery resumes from the
    last proven destination without re-reading the (now-moved) source.
    """
    src = path.join(st.staging_dir, rel)
    dst = path.join(
        masterdb_diff_folder_path if repo_key == "master" else i18n_diff_folder_path,
        rel,
    )
    dst_dir = path.dirname(dst)
    fe = st.files.get(rel)
    expected_dest = fe.dest_sha256 if fe else None
    expected_src = fe.source_sha256 if fe else None

    current_dest = compute_sha256(dst)
    if expected_dest is not None and current_dest != expected_dest:
        raise JournalError(
            f"recovery blocked: durable destination checkpoint for {repo_key!r}/"
            f"{rel!r} disagrees with destination ({expected_dest} != {current_dest})"
        )
    # The source digest is immutable.  It proves completion even when the
    # checkpoint was not persisted after the atomic replace.
    if expected_src is not None and current_dest == expected_src:
        journal.update_repo(repo_key, rel=rel, dest_sha256=expected_src)
        return
    # 2) Otherwise a matching source (staging) can replace the destination.
    current_src = compute_sha256(src)
    if expected_src is not None and current_src == expected_src:
        if dst_dir:
            os.makedirs(dst_dir, exist_ok=True)
        os.replace(src, dst)
        fsync_published_file(dst, src)
        # Record the now-published destination SHA for later verification, and
        # persist it durably before moving on to the next file.
        dest = compute_sha256(dst)
        if dest != expected_src:
            raise JournalError(
                f"recovery replace produced unexpected content for {repo_key!r}/{rel!r}"
            )
        st.files[rel].dest_sha256 = dest
        journal.update_repo(repo_key, rel=rel, dest_sha256=dest)
        return
    # 3) Missing/mismatched source and non-matching destination -> block.
    raise JournalError(
        f"recovery blocked: file {rel!r} for {repo_key!r} has no consistent "
        f"source/destination (src={current_src}, dst={current_dest})"
    )


def _recover_publish(journal: TransactionJournal) -> None:
    """Complete any unfinished formal ``os.replace`` publications."""
    # Publication is globally ordered: every non-version manifest file from
    # every enabled repository is published before the one global
    # ``master/versions.json`` file.  Do not publish master versions merely
    # because master happens to be first in ``publish_order``.
    for key in journal.publish_order:
        st = journal.repos.get(key)
        if st is None:
            continue
        rels = [p for p in st.manifest if p != "versions.json"]
        for rel in rels:
            _recover_publish_file(journal, key, rel, st)
    master = journal.repos.get("master")
    if master is not None and "versions.json" in master.manifest:
        _recover_publish_file(journal, "master", "versions.json", master)
        _sync_recovered_version_info(journal)
    journal.set_phase(TxnPhase.COMMITTING)


def _sync_recovered_version_info(journal: TransactionJournal) -> None:
    """Advance the in-memory published version after proving master publication.

    Recovery may enter at COMMITTING/PUSHING, so this validation is also called
    when publication was proven by an earlier recovery pass.  The journal
    candidate is the formal ``versions.json`` value for the transaction; it is
    not installed in memory until the destination is proven to contain the
    journal's immutable source/checkpoint hash.
    """
    global version_info

    st = journal.repos.get("master")
    if st is None or "versions.json" not in st.manifest:
        return
    fe = st.files.get("versions.json")
    if fe is None or not fe.source_sha256:
        raise JournalError("recovery blocked: master versions.json has no source hash")
    dst = path.join(masterdb_diff_folder_path, "versions.json")
    current = compute_sha256(dst)
    expected = fe.dest_sha256 or fe.source_sha256
    if current != expected:
        raise JournalError(
            "recovery blocked: master versions.json publication is not proven"
        )
    formal_version: dict[str, Any] | None = None
    try:
        with open(dst, encoding="utf-8") as stream:
            loaded = json.load(stream)
        if isinstance(loaded, dict):
            formal_version = loaded
    except (OSError, ValueError) as err:
        raise JournalError(
            "recovery blocked: published master versions.json is unreadable"
        ) from err
    # The formal file is authoritative once its hash is proven.  Keep any
    # journal-only fields for compatibility with older candidate records.
    version_info = {
        key: value
        for key, value in journal.candidate.items()
        if key != _DAILY_DUE_JOURNAL_KEY
    }
    if formal_version is not None:
        version_info.update(formal_version)


def _recover_commit_repo(
    key: str,
    st: RepoState,
    candidate: dict[str, Any] | None,
    journal: TransactionJournal | None = None,
) -> None:
    """Commit a single repo during recovery if it lacks the exact target SHA.

    Idempotent: if the repo HEAD already equals the recorded target commit SHA,
    the commit is considered done (no duplicate commit). Otherwise a fresh commit
    is created from the published working tree and the target SHA recorded.
    """
    repo = masterdb_diff_repo if key == "master" else i18n_diff_repo
    if repo is None:
        raise JournalError(f"recovery commit: repo {key!r} unavailable")
    _validate_recovery_manifest_state(key, repo, st)
    head_sha = _safe_head_sha(repo)
    _validate_recovery_head(key, st, head_sha)
    target = st.target_commit_sha
    if target is not None:
        _recover_recorded_target(key, repo, st, target, head_sha, journal)
        return
    # No target yet: the base HEAD must match what the journal recorded at
    # creation time, otherwise the repo diverged unexpectedly.
    if not repo.is_dirty(untracked_files=True) and not st.manifest:
        st.commit_state = RepoCommitState.COMMITTED
        st.target_commit_sha = head_sha
        return
    # Commit only the explicit manifest paths (never broad-stage).
    result = _coordinated_commit_repo(key, repo, st, candidate, journal=journal)
    if result.outcome not in (GitOutcome.OK, GitOutcome.NOTHING_TO_DO):
        st.commit_state = RepoCommitState.FAILED
        raise JournalError(f"recovery commit failed for {key!r}: {result.reason}")


def _recover_recorded_target(
    key: str,
    repo: Repo,
    st: RepoState,
    target: str,
    head_sha: str | None,
    journal: TransactionJournal | None,
) -> None:
    """Install or CAS a previously prepared target without rebuilding it."""
    if target != st.base_sha:
        _validate_prepared_target(
            key,
            repo,
            st,
            target,
            journal,
            require_base_index=head_sha != target,
        )
    else:
        _validate_noop_target(key, repo, st, target)
    _validate_target_object(repo, key, target)
    if head_sha == target:
        _validate_existing_target_index(key, repo, st, target)
        _validate_target_worktree(repo, target, key)
        _install_target_index(repo, target, key)
        _validate_recovered_target_state(key, repo, target, st.base_sha)
        st.commit_state = RepoCommitState.COMMITTED
        return
    if head_sha != st.base_sha:
        raise JournalError(
            f"recovery commit blocked for {key!r}: HEAD {head_sha} is neither "
            f"target {target} nor base {st.base_sha}"
        )
    _validate_target_worktree(repo, target, key)
    _update_branch_cas(repo, target, st.base_sha, key)
    if _safe_head_sha(repo) != target:
        raise JournalError(
            f"recovery commit blocked for {key!r}: HEAD did not reach target"
        )
    _install_target_index(repo, target, key)
    _validate_recovered_target_state(key, repo, target, st.base_sha)
    st.commit_state = RepoCommitState.COMMITTED


def _validate_noop_target(key: str, repo: Repo, st: RepoState, target: str) -> None:
    """Prove a recorded no-op still represents the published manifest exactly."""
    _validate_target_object(repo, key, target)
    _validate_target_tree(repo, key, st, target)
    _validate_existing_target_index(key, repo, st, target)
    _validate_target_worktree(repo, target, key)


def _validate_target_object(repo: Repo, key: str, target: str) -> None:
    try:
        object_type = _git_command(repo, "cat-file", "-t", target)
    except (OSError, subprocess.CalledProcessError) as err:
        raise JournalError(
            f"recovery commit blocked for {key!r}: target missing"
        ) from err
    if object_type != "commit":
        raise JournalError(
            f"recovery commit blocked for {key!r}: target is not a commit"
        )


def _validate_recovered_target_state(
    key: str, repo: Repo, target: str, base: str | None
) -> None:
    if target != base:
        _validate_target_worktree_and_index(repo, target, key)


def _validate_recovery_branch(key: str, repo: Repo) -> None:
    try:
        ref = _git_command(repo, "symbolic-ref", "--quiet", "HEAD")
    except (OSError, subprocess.CalledProcessError) as err:
        raise JournalError(
            f"recovery commit blocked for {key!r}: HEAD is detached"
        ) from err
    if ref != "refs/heads/main":
        raise JournalError(
            f"recovery commit blocked for {key!r}: HEAD is {ref}, not main"
        )


def _validate_recovery_branches(journal: TransactionJournal) -> None:
    repos = {"master": masterdb_diff_repo, "i18n": i18n_diff_repo}
    for key in journal.enabled_repos:
        repo = repos.get(key)
        if repo is None:
            raise JournalError(f"recovery commit: repo {key!r} unavailable")
        _validate_recovery_branch(key, repo)


def _validate_completed_journal(journal: TransactionJournal) -> None:
    repos = {"master": masterdb_diff_repo, "i18n": i18n_diff_repo}
    for key in journal.enabled_repos:
        repo = repos.get(key)
        st = journal.repos.get(key)
        if repo is None or st is None or st.target_commit_sha is None:
            raise JournalError(f"completed journal repo {key!r} is incomplete")
        target = st.target_commit_sha
        _validate_target_object(repo, key, target)
        if _safe_head_sha(repo) != target:
            raise JournalError(
                f"completed journal repo {key!r} HEAD does not equal target"
            )
        if target != st.base_sha:
            _validate_target_commit_identity(key, repo, st, target, journal)
        _validate_target_tree(repo, key, st, target)
        _validate_target_worktree_and_index(repo, target, key)
        _validate_recovery_manifest_state(key, repo, st)


def _validate_recovery_head(key: str, st: RepoState, head_sha: str | None) -> None:
    if st.target_commit_sha is not None and head_sha == st.target_commit_sha:
        return
    if (
        st.target_commit_sha is None
        and st.base_sha is not None
        and head_sha != st.base_sha
    ):
        raise JournalError(
            f"recovery commit blocked for {key!r}: base HEAD {head_sha} != "
            f"recorded base {st.base_sha}"
        )


def _validate_recovery_manifest_state(key: str, repo: Repo, st: RepoState) -> None:
    """Fail closed unless publication and all local dirt are manifest-safe."""
    root = masterdb_diff_folder_path if key == "master" else i18n_diff_folder_path
    manifest = set(st.manifest)

    # Every manifest destination must still be exactly the content proven by
    # publication.  A missing checkpoint is tolerated only for old journals
    # when the destination still equals the immutable source hash.
    for rel in st.manifest:
        entry = st.files.get(rel)
        if entry is None or not entry.source_sha256:
            raise JournalError(
                f"recovery commit blocked for {key!r}: missing hash for {rel!r}"
            )
        expected = entry.dest_sha256 or entry.source_sha256
        actual = compute_sha256(path.join(root, rel))
        if actual != expected:
            raise JournalError(
                f"recovery commit blocked for {key!r}: destination {rel!r} "
                f"hash {actual} != expected {expected}"
            )

    # A crash must not turn recovery into a broad stage.  Reject both tracked
    # and untracked worktree/index dirt outside the immutable manifest before
    # any staging or commit operation begins.
    dirty: set[str] = set()
    try:
        for line in _git_command(
            repo, "status", "--porcelain", "--untracked-files=all"
        ).splitlines():
            if line:
                # Porcelain v1 has a two-column status followed by a space.
                dirty.add(line[3:] if len(line) > 3 else line)
    except Exception as err:
        raise JournalError(
            f"recovery commit blocked for {key!r}: cannot inspect worktree: {err}"
        ) from err
    dirty.update(repo.untracked_files)
    outside = sorted(dirty - manifest)
    if outside:
        raise JournalError(
            f"recovery commit blocked for {key!r}: out-of-manifest dirt {outside!r}"
        )


def _index_tree(repo: Repo, index_path: str | None = None) -> str:
    env = {"GIT_INDEX_FILE": index_path} if index_path else None
    return _git_command(repo, "write-tree", env=env)


def _target_parent_shas(repo: Repo, target: str) -> list[str]:
    line = _git_command(repo, "rev-list", "--parents", "-n", "1", target)
    return line.split()[1:]


def _validate_target_tree(repo: Repo, key: str, st: RepoState, target: str) -> None:
    """Prove the target tree is exactly the journal's manifest change."""
    root = masterdb_diff_folder_path if key == "master" else i18n_diff_folder_path
    manifest = set(st.manifest)
    tree_paths = set(
        filter(
            None,
            _git_command(repo, "ls-tree", "-r", "--name-only", target).splitlines(),
        )
    )
    if st.base_sha:
        changed_paths = set(
            filter(
                None,
                _git_command(
                    repo, "diff", "--name-only", st.base_sha, target
                ).splitlines(),
            )
        )
    else:
        changed_paths = tree_paths
    if not changed_paths <= manifest:
        raise JournalError(
            f"recovery commit blocked for {key!r}: target changes outside manifest"
        )
    for rel in st.manifest:
        entry = st.files[rel]
        source = path.join(st.staging_dir, rel)
        if compute_sha256(source) != entry.source_sha256:
            source = path.join(root, rel)
        if compute_sha256(source) != entry.source_sha256:
            raise JournalError(
                f"recovery commit blocked for {key!r}: source mismatch for {rel!r}"
            )
        blob = _git_command(repo, "hash-object", source)
        tree_entry = _git_command(
            repo, "ls-tree", "-r", "--full-tree", target, "--", rel
        )
        fields = tree_entry.split()
        if len(fields) < 4 or fields[1] != "blob" or fields[2] != blob:
            raise JournalError(
                f"recovery commit blocked for {key!r}: target tree mismatch for {rel!r}"
            )


def _validate_prepared_target(
    key: str,
    repo: Repo,
    st: RepoState,
    target: str,
    journal: TransactionJournal | None,
    *,
    require_base_index: bool,
    index_path: str | None = None,
) -> None:
    """Validate an immutable prepared target before accepting or CAS-ing it."""
    _validate_target_commit_identity(key, repo, st, target, journal)
    _validate_target_tree(repo, key, st, target)
    _validate_prepared_index(repo, st, target, require_base_index, index_path)


def _validate_target_commit_identity(
    key: str,
    repo: Repo,
    st: RepoState,
    target: str,
    journal: TransactionJournal | None,
) -> None:
    if journal is None:
        raise JournalError(f"recovery commit blocked for {key!r}: journal missing")
    _validate_target_object(repo, key, target)
    expected_parents = [st.base_sha] if st.base_sha else []
    actual_parents = _target_parent_shas(repo, target)
    if actual_parents != expected_parents:
        raise JournalError(
            f"recovery commit blocked for {key!r}: target parent mismatch "
            f"({actual_parents!r} != {expected_parents!r})"
        )
    _validate_target_trailers(repo, key, target, journal)


def _validate_target_trailers(
    repo: Repo, key: str, target: str, journal: TransactionJournal
) -> None:
    message = _git_command(repo, "show", "-s", "--format=%B", target)
    values: dict[str, list[str]] = {
        "Sekai-Update-Txn": [],
        "Sekai-Update-Repo": [],
    }
    for line in message.splitlines():
        stripped = line.strip()
        if stripped.startswith("Sekai-") and ":" in stripped:
            name = stripped.split(":", 1)[0]
            if name not in values:
                raise JournalError(
                    f"recovery commit blocked for {key!r}: extra transaction trailer"
                )
        for name in values:
            prefix = f"{name}:"
            if stripped.startswith(prefix):
                values[name].append(stripped[len(prefix) :].strip())
    expected = {
        "Sekai-Update-Txn": journal.transaction_id,
        "Sekai-Update-Repo": key,
    }
    valid = all(
        len(values[name]) == 1 and values[name][0] == value
        for name, value in expected.items()
    )
    if not valid:
        raise JournalError(
            f"recovery commit blocked for {key!r}: target trailer mismatch"
        )


def _validate_existing_target_index(
    key: str, repo: Repo, st: RepoState, target: str
) -> None:
    target_tree = _git_command(repo, "rev-parse", f"{target}^{{tree}}")
    base_tree = (
        _git_command(repo, "rev-parse", f"{st.base_sha}^{{tree}}")
        if st.base_sha
        else _git_command(repo, "mktree", stdin="")
    )
    try:
        actual_tree = _index_tree(repo)
    except (OSError, subprocess.CalledProcessError) as err:
        raise JournalError(
            f"recovery commit blocked for {key!r}: existing index unreadable"
        ) from err
    if actual_tree not in {base_tree, target_tree}:
        raise JournalError(
            f"recovery commit blocked for {key!r}: existing index is not base or target"
        )


def _validate_prepared_index(
    repo: Repo,
    st: RepoState,
    target: str,
    require_base_index: bool,
    index_path: str | None,
) -> None:
    if index_path is not None:
        expected_tree = _git_command(repo, "rev-parse", f"{target}^{{tree}}")
    elif require_base_index:
        expected_tree = (
            _git_command(repo, "rev-parse", f"{st.base_sha}^{{tree}}")
            if st.base_sha
            else _git_command(repo, "mktree", stdin="")
        )
    else:
        return
    try:
        actual_tree = _index_tree(repo, index_path)
    except (OSError, subprocess.CalledProcessError) as err:
        raise JournalError(
            f"recovery commit blocked for {st.repo_root!r}: index mismatch"
        ) from err
    if actual_tree != expected_tree:
        raise JournalError(
            f"recovery commit blocked for {st.repo_root!r}: prepared index mismatch"
        )


def _validate_target_worktree_and_index(repo: Repo, target: str, key: str) -> None:
    """Verify the installed target index and worktree after CAS recovery."""
    target_tree = _git_command(repo, "rev-parse", f"{target}^{{tree}}")
    if _index_tree(repo) != target_tree:
        raise JournalError(
            f"recovery commit blocked for {key!r}: target index mismatch"
        )
    _validate_target_worktree(repo, target, key)


def _validate_target_worktree(repo: Repo, target: str, key: str) -> None:
    try:
        _git_command(repo, "diff", "--quiet", target, "--")
    except (OSError, subprocess.CalledProcessError) as err:
        raise JournalError(
            f"recovery commit blocked for {key!r}: target worktree mismatch"
        ) from err


def _temporary_index_path(repo: Repo) -> str:
    return path.join(
        path.dirname(repo.index.path),
        f".{path.basename(repo.index.path)}.sekai-{new_transaction_id()}",
    )


def _install_target_index(repo: Repo, target: str, key: str) -> None:
    """Install and durably flush the exact target commit index idempotently."""
    index_path = _temporary_index_path(repo)
    try:
        _git_command(repo, "read-tree", target, env={"GIT_INDEX_FILE": index_path})
        os.replace(index_path, repo.index.path)
        fsync_file(repo.index.path)
        fsync_directory(path.dirname(repo.index.path))
        fsync_directory(repo.git_dir)
    except (OSError, subprocess.CalledProcessError) as err:
        raise JournalError(
            f"recovery commit blocked for {key!r}: target index install failed"
        ) from err
    finally:
        if os.path.exists(index_path):
            os.remove(index_path)


def _git_command(
    repo: Repo,
    *args: str,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
) -> str:
    command_env = os.environ.copy()
    if env:
        command_env.update(env)
    return subprocess.run(
        ["git", *args],
        cwd=repo.working_tree_dir,
        env=command_env,
        input=stdin,
        text=True,
        check=True,
        capture_output=True,
    ).stdout.strip()


def _update_branch_cas(repo: Repo, target: str, base: str | None, key: str) -> None:
    """Move main only when it still names the transaction's recorded base."""
    _validate_recovery_branch(key, repo)
    expected = base or "0" * 40
    try:
        _git_command(repo, "update-ref", "refs/heads/main", target, expected)
    except subprocess.CalledProcessError as err:
        current = _safe_head_sha(repo)
        if current != target:
            raise JournalError(
                f"commit {key!r} CAS failed: HEAD {current} != target {target}"
            ) from err
    if _safe_head_sha(repo) != target:
        raise JournalError(f"commit {key!r} HEAD verification failed")


def _coordinated_commit_repo(
    key: str,
    repo: Repo,
    st: RepoState,
    candidate: dict[str, Any] | None,
    journal: TransactionJournal | None,
) -> GitResult:
    """Create a deterministic commit object, then install it with a CAS ref move."""
    base = st.base_sha or _safe_head_sha(repo)
    if _safe_head_sha(repo) != base:
        return GitResult(
            GitOutcome.FAILED,
            reason="base_mismatch",
            operation=f"commit_{key}",
        )
    # The state may be an in-memory normal-cycle state whose base was not
    # populated until the commit helper resolved HEAD.  Keep validation and the
    # durable checkpoint bound to the same parent used for commit-tree.
    st.base_sha = base
    index_path = _temporary_index_path(repo)
    try:
        env = {"GIT_INDEX_FILE": index_path}
        if base is None:
            _git_command(repo, "read-tree", "--empty", env=env)
        else:
            _git_command(repo, "read-tree", base, env=env)
        target, result = _prepare_commit_target(
            repo, key, st.manifest, base, candidate, journal, env
        )
        if journal is not None:
            journal.update_repo(
                key,
                base_sha=base,
                target_commit_sha=target,
                commit_state=RepoCommitState.PREPARED,
            )
        if target is None:
            return GitResult(
                GitOutcome.FAILED,
                reason="commit_target_missing",
                operation=f"commit_{key}",
            )
        if target != base:
            _validate_prepared_target(
                key,
                repo,
                st,
                target,
                journal,
                require_base_index=True,
                index_path=index_path,
            )
        _update_branch_cas(repo, target, base, key)
        # Install the exact target index before recording COMMITTED.  A crash
        # after the CAS but before this replace is recovered idempotently from
        # the target object; a COMMITTED checkpoint always has the matching
        # ordinary Git index and a durable fsync behind it.
        os.replace(index_path, repo.index.path)
        fsync_file(repo.index.path)
        fsync_directory(path.dirname(repo.index.path))
        fsync_directory(repo.git_dir)
        if target != base:
            _validate_target_worktree_and_index(repo, target, key)
        if journal is not None:
            journal.update_repo(key, commit_state=RepoCommitState.COMMITTED)
        return result
    except (OSError, subprocess.CalledProcessError) as err:
        return GitResult(
            GitOutcome.FAILED,
            reason="commit_failed",
            operation=f"commit_{key}",
            detail=str(err),
        )
    finally:
        if os.path.exists(index_path):
            os.remove(index_path)


def _prepare_commit_target(
    repo: Repo,
    key: str,
    manifest: list[str],
    base: str | None,
    candidate: dict[str, Any] | None,
    journal: TransactionJournal | None,
    env: dict[str, str],
) -> tuple[str | None, GitResult]:
    if manifest:
        _git_command(repo, "add", "--", *manifest, env=env)
    cached = _git_command(repo, "diff", "--cached", "--name-only", env=env)
    if not set(filter(None, cached.splitlines())) <= set(manifest):
        return None, GitResult(
            GitOutcome.FAILED,
            reason="out_of_manifest_cached_paths",
            operation=f"commit_{key}",
        )
    tree = _git_command(repo, "write-tree", env=env)
    base_tree = _git_command(repo, "rev-parse", f"{base}^{{tree}}") if base else ""
    if tree == base_tree:
        return base, GitResult(
            GitOutcome.NOTHING_TO_DO,
            reason="base_tree_equal",
            operation=f"commit_{key}",
            local_sha=base,
        )
    message = (
        f"{_commit_message_for(key, candidate)}\n\n"
        "Sekai-Update-Txn: "
        f"{journal.transaction_id if journal else 'standalone'}\n"
        f"Sekai-Update-Repo: {key}\n"
    )
    commit_args = ["commit-tree", tree]
    if base:
        commit_args.extend(["-p", base])
    target = _git_command(
        repo,
        *commit_args,
        env={
            **env,
            "GIT_AUTHOR_NAME": (
                "master-db-diff-bot" if key == "master" else "i18n-diff-bot"
            ),
            "GIT_AUTHOR_EMAIL": "anonymous@example.com",
            "GIT_COMMITTER_NAME": (
                "master-db-diff-bot" if key == "master" else "i18n-diff-bot"
            ),
            "GIT_COMMITTER_EMAIL": "anonymous@example.com",
            "GIT_AUTHOR_DATE": _git_command(repo, "show", "-s", "--format=%aI", base)
            if base
            else "1970-01-01T00:00:00+0000",
            "GIT_COMMITTER_DATE": _git_command(repo, "show", "-s", "--format=%aI", base)
            if base
            else "1970-01-01T00:00:00+0000",
        },
        stdin=message,
    )
    _git_command(repo, "cat-file", "-e", f"{target}^{{commit}}")
    return target, GitResult(
        GitOutcome.OK,
        reason="prepared",
        operation=f"commit_{key}",
        local_sha=target,
    )


def _recover_commit(journal: TransactionJournal) -> None:
    """Commit every enabled repo; block push until ALL have exact target SHA."""
    candidate = journal.candidate if journal.candidate else None
    for key in journal.publish_order:
        st = journal.repos.get(key)
        if st is None:
            continue
        _recover_commit_repo(key, st, candidate, journal)
        journal.update_repo(key, commit_state=st.commit_state)
    # Verify ALL enabled repos reached the exact target commit SHA.
    for key in journal.enabled_repos:
        st = journal.repos.get(key)
        if st is None or st.commit_state != RepoCommitState.COMMITTED:
            raise JournalError(f"recovery push blocked: {key!r} not committed")
        head_sha = _safe_head_sha(
            masterdb_diff_repo if key == "master" else i18n_diff_repo
        )
        if head_sha != st.target_commit_sha:
            raise JournalError(
                f"recovery push blocked: {key!r} HEAD {head_sha} != target "
                f"{st.target_commit_sha}"
            )


def _recover_push(journal: TransactionJournal) -> "str | None":
    """Push i18n -> master with explicit expected-SHA verification.

    The journal is advanced to PUSHING *before* the first push so a crash mid-push
    resumes at the push phase. Each repo is pushed exclusively via
    ``push_current_head(expected_sha=...)``; a remote already at the target SHA
    counts as verified (no duplicate push). Every push checkpoint is persisted
    atomically. On ANY ambiguous/failed push the journal is RETAINED (not deleted,
    not fail-closed) so the next cycle retries; the function returns a retryable
    status string instead of raising.
    """
    # Persist PUSHING before the first push so recovery resumes here on crash.
    journal.set_phase(TxnPhase.PUSHING)
    # Push order is i18n -> master (master is published last, pushed last).
    for key in journal.push_order:
        st = journal.repos.get(key)
        if st is None:
            continue
        repo = masterdb_diff_repo if key == "master" else i18n_diff_repo
        if repo is None:
            raise JournalError(f"recovery push: repo {key!r} unavailable")
        target = st.target_commit_sha
        try:
            remote_sha = _probe_remote(repo, key, st)
        except RemoteProbeError:
            return f"push_failed:{key}:remote_probe_unconfirmed"
        if remote_sha == target:
            st.remote_sha = target
            st.push_state = RepoPushState.PUSHED
            journal.update_repo(key, push_state=RepoPushState.PUSHED, remote_sha=target)
            continue
        if remote_sha != st.remote_base_sha:
            return f"remote_mismatch:{key}"
        push_res = _push_diff(
            repo,
            operation=f"push_{key}",
            expected_sha=target,
            old_remote_sha=st.remote_base_sha,
            remote_endpoint_fingerprint=st.remote_endpoint_fingerprint,
            remote_state=st,
        )
        if push_res.outcome is GitOutcome.OK:
            st.push_state = RepoPushState.PUSHED
            st.remote_sha = target
            journal.update_repo(key, push_state=RepoPushState.PUSHED, remote_sha=target)
        else:
            # Ambiguous/failed push: retain the journal for retry. FAILED is not
            # a durable v2 state, so leave the last proven PENDING checkpoint
            # untouched and return a retryable status.
            return f"push_failed:{key}:{push_res.reason}"
    # All verified: mark completed, clean staging, delete journal.
    try:
        _mark_strapi_transaction_ready(journal.transaction_id)
    except StrapiOutboxError:
        # Git is complete, but readiness is not.  Keep the PUSHING journal as
        # the durable retry checkpoint; a later recovery pass will promote the
        # records before it is allowed to delete the journal.
        return "strapi_readiness_failed"
    journal.set_phase(TxnPhase.COMPLETED)
    _clear_staging_dir_safe(_master_staging_parent())
    _clear_staging_dir_safe(_i18n_staging_parent())
    journal.delete()
    return None


def _recover_dispatch(journal: TransactionJournal) -> "str | None":
    """Drive publication -> commit -> push recovery for a loaded journal.

    Returns ``None`` when recovery completed fully (``recovered``), or a
    retryable push-failure status string when a push could not finish (the
    journal is retained for the next cycle).
    """
    if journal.phase in (TxnPhase.PREPARING, TxnPhase.PUBLISHING):
        _recover_publish(journal)
    if journal.phase in (TxnPhase.COMMITTING, TxnPhase.PUSHING):
        # Publication may already be done; ensure commit phase is reached.
        if journal.phase == TxnPhase.COMMITTING:
            _recover_commit(journal)
        else:  # PUSHING: re-verify commits are present before pushing.
            _recover_commit(journal)
        push_status = _recover_push(journal)
        return push_status
    if journal.phase == TxnPhase.PUBLISHING:
        # Handled by the branch above; fall through to commit+push.
        _recover_commit(journal)
        return _recover_push(journal)
    return None


def _recover_transaction() -> tuple[str | None, str | None]:
    """Run durable recovery if a journal exists; return a status or ``None``.

    Returns ``None`` when there is no journal (the cycle proceeds normally). When
    a journal exists, recovery completes the interrupted work and returns the
    stable ``recovered`` status. Any :class:`JournalError` is re-raised so the
    caller can surface a fail-closed ``journal_invalid`` status without doing
    fresh generation/reset/force.
    """
    if masterdb_diff_repo is None:
        return None, None
    journal = _load_bound_journal()
    if journal is None:
        return None, None

    # This must precede phase dispatch and every possible cleanup/mutation.
    validate_journal_roots(
        journal,
        actual_roots={
            "master": masterdb_diff_folder_path,
            "i18n": i18n_diff_folder_path,
        },
    )

    # Re-validate strictly (fail closed on any defect) before any recovery
    # checkpoint cleanup or Git mutation.
    _validate_recovery_branches(journal)
    if journal.phase == TxnPhase.COMPLETED:
        _validate_completed_journal(journal)
        journal.delete()
        return None, None

    # A recovered cycle may start after publication was already proven by the
    # previous process.  Restore the same in-memory formal-version semantics
    # before commit/push recovery, not only on the PUBLISHING path.
    if journal.phase in (TxnPhase.COMMITTING, TxnPhase.PUSHING):
        _sync_recovered_version_info(journal)

    push_status = _recover_dispatch(journal)
    if push_status is not None:
        return push_status, None
    due_date = journal.candidate.get(_DAILY_DUE_JOURNAL_KEY)
    return "recovered", due_date if isinstance(due_date, str) else None


def _commit_message_for(key: str, candidate: dict[str, Any] | None) -> str:
    """Build an explicit, non-crashing commit message for a repository.

    The candidate (or, for legacy/standalone callers, the published global) is
    used explicitly so the message never indexes a ``None`` global. When no
    version data is available at all, a safe static message is returned (no
    ``TypeError``); the caller still stages only the explicit manifest paths, so
    no broad-staging occurs.
    """
    ver = candidate if candidate is not None else version_info
    if ver is None:
        if key == "master":
            return "master data update"
        return "i18n data update"
    if key == "master":
        data_version = ver.get("dataVersion")
        asset_version = ver.get("assetVersion")
        if data_version is None or asset_version is None:
            return "master data update"
        return f"master version {data_version} asset version {asset_version}"
    if ver.get("dataVersion") is None:
        return "i18n data update"
    return f"i18n update for master version {ver['dataVersion']}"


def _commit_enabled_repositories(
    enabled: list[tuple[str, Repo | None]],
    manifest: dict[str, list[str]],
    candidate: dict[str, Any] | None = None,
    journal: TransactionJournal | None = None,
) -> dict[str, GitResult]:
    """Commit every enabled repository (explicit manifest paths) before push.

    ``candidate`` is the explicit version data for this cycle (returned by
    ``_generate_and_publish``). It is threaded into each commit so the message
    and the ``_commit_diff`` version guard use the candidate directly rather than
    the published global ``version_info`` — which stays ``None`` on an i18n-only
    first run (master disabled, never advanced). When ``candidate`` is ``None``
    and the global is also ``None``, the message falls back to a safe static
    string and ``_commit_diff`` reports ``version_info_missing`` without ever
    indexing ``None``.
    """
    commits: dict[str, GitResult] = {}
    for key, repo in enabled:
        relpaths = manifest.get(key, [])
        durable_journal = journal
        if durable_journal is None and masterdb_diff_repo is not None:
            durable_journal = _load_bound_journal()
        state = durable_journal.repos.get(key) if durable_journal else None
        if durable_journal is not None and state is not None:
            if repo is None:
                commits[key] = GitResult(
                    GitOutcome.FAILED,
                    reason="repo_missing",
                    operation=f"commit_{key}",
                )
            else:
                commits[key] = _coordinated_commit_repo(
                    key, repo, state, candidate, durable_journal
                )
            continue
        if key == "master":
            commits[key] = _commit_diff(
                repo,
                operation="commit_master_diff",
                folder_label=local_git_folder_names["masterDBDiff"],
                commit_message=_commit_message_for("master", candidate),
                author=Actor("master-db-diff-bot", "anonymous@example.com"),
                paths=relpaths,
                version=candidate,
            )
        else:
            commits[key] = _commit_diff(
                repo,
                operation="commit_i18n_files",
                folder_label=local_git_folder_names["i18n"],
                commit_message=_commit_message_for("i18n", candidate),
                author=Actor("i18n-diff-bot", "anonymous@example.com"),
                paths=relpaths,
                version=candidate,
            )
    return commits


def _push_enabled_repositories(commits: dict[str, GitResult]) -> str | None:
    """Push committed repositories in deterministic order.

    When a real ``publishing`` journal exists and every enabled repo has reached
    the exact target commit SHA (journal phase ``COMMITTING``), the fresh cycle
    reuses the journal-driven :func:`_recover_push` route instead of the legacy
    push loop: this pushes **i18n -> master** exclusively via
    ``push_current_head(expected_sha=...)`` (an explicit SHA barrier) and, on a
    retryable push failure, retains the journal and returns a ``push_failed:*``
    status. A (test) stub of this function bypasses the delegation entirely, so
    legacy push-order tests remain green.

    Otherwise (no journal, e.g. ``masterdb_diff_repo`` is ``None``) the legacy
    deterministic push loop is used: it stops after the first push failure
    (preserving every local unpushed commit) and returns a
    ``push_failed:<key>:<reason>`` status, or ``None`` on success.
    """
    journal = _load_bound_journal()
    if journal is None or journal.phase != TxnPhase.COMMITTING:
        return "journal_invalid:no_bound_journal"
    return _recover_push(journal)


def _cycle_should_proceed(daily: bool) -> str | None:
    """Honor maintenance / candidate gating while inside the cycle lock.

    Returns a short status string to return early (skipping generation/commit)
    when the cycle must not proceed, or ``None`` to proceed. The published
    ``version_info`` global is never advanced here; the candidate is only
    advanced by ``_generate_and_publish`` after a successful publication.

    Maintenance and the simple-mode candidate are determined here (under the
    held process + repo locks) so no decision is taken outside the locked cycle.
    """
    global is_in_maintenance
    if check_update_simple_mode:
        # Simple mode: only proceed when a new version is detected. The candidate
        # is computed here but never pushed to the published global.
        ver_res = check_versions_simple()
        if not ver_res["new_version"]:
            logger.info("[cycle] simple mode: no new version; skipping")
            return "no_new_version"
        is_in_maintenance = False
        return None

    if daily:
        # Daily / full-refresh run: request the server version/CN info only to
        # honor maintenance. The new-version gate is intentionally bypassed so a
        # daily run always re-fetches and republishes the full data set; the
        # published global is still not advanced when maintenance is active.
        check_version_res = jsonrpc_client.request("check_versions", [version_info])
        if check_version_res["maintenance"]:
            logger.warning("PJSK server is in maintenance, skipping cycle")
            is_in_maintenance = True
            return "maintenance"
        is_in_maintenance = False
        return None

    # Ordinary run: standard mode must respect the new-version gate computed by
    # the server. When the candidate version matches the published global there
    # is nothing to publish, so we skip cleanly.
    check_version_res = jsonrpc_client.request("check_versions", [version_info])
    if check_version_res["maintenance"]:
        logger.warning("PJSK server is in maintenance, skipping cycle")
        is_in_maintenance = True
        return "maintenance"
    is_in_maintenance = False
    if not check_version_res.get("new_version", False):
        logger.info("[cycle] ordinary run: versions match, nothing to publish")
        return "no_new_version"
    return None


def _check_deadline(deadline: "Deadline | None") -> None:
    """Cooperative deadline check at a safe seam.

    A ``None`` deadline is disabled (daily cycles) and never raises. An enabled
    deadline raises :class:`CycleDeadlineExceeded` once it has elapsed.
    """
    if deadline is not None:
        deadline.check()


def _advance_journal_to_committing(
    journal: "TransactionJournal",
    enabled: list[tuple[str, "Repo | None"]],
    commits: dict[str, "GitResult"],
) -> "str | None":
    """Validate commit checkpoints after a commit helper/test double returns.

    The coordinated production commit persists ``PREPARED`` and ``COMMITTED``
    together with the exact target SHA.  This compatibility seam is deliberately
    read-only: it must not move the phase, overwrite an existing target, or
    persist ``FAILED`` for an operational error.
    """
    del enabled, commits
    if any(
        journal.repos.get(key) is None
        or journal.repos[key].commit_state != RepoCommitState.COMMITTED
        or journal.repos[key].target_commit_sha is None
        for key in journal.enabled_repos
    ):
        logger.error(
            "[cycle] a commit did not reach COMMITTED; journal retained for recovery"
        )
        return "commit_failed"
    return None


def _retain_journal_after_push_fail(journal: "TransactionJournal | None") -> None:
    """A push failure AFTER the journal was written must RETAIN the journal (and
    its journal-owned staging) so durable recovery finishes the work on the next
    cycle. Only PRE-journal generation/deadline failures clean them. The local
    commits are already retained by the push helper; we simply leave the journal
    on disk (no delete, no fail-closed)."""
    # Intentionally a no-op: the journal is left intact for recovery.
    return


def _complete_journal_after_push(journal: "TransactionJournal | None") -> None:
    """All pushes succeeded: mark the journal COMPLETED, clean the journal-owned
    staging, and delete the journal. A missing journal (e.g. masterdb_diff_repo
    was None) means there is nothing to clean."""
    if journal is None:
        return
    try:
        journal.set_phase(TxnPhase.COMPLETED)
        _clear_staging_dir_safe(_master_staging_parent())
        _clear_staging_dir_safe(_i18n_staging_parent())
        journal.delete()
    except Exception:  # noqa: BLE001 - cleanup must not mask the ok status
        logger.exception("[journal] post-push cleanup failed; journal retained")


def _prepare_enabled_repositories(
    enabled: list[tuple[str, "Repo | None"]], deadline: "Deadline | None"
) -> "str | None":
    """Prepare every enabled repository, checking the cooperative deadline before
    each prepare.

    Returns a ``not_ready:<key>:<reason>`` status string to return early when a
    repository is not ready, or ``None`` when all prepares succeeded. The deadline
    is checked at the safe seam *before* each prepare (network/disk work) so a
    cooperative cancellation never interrupts an in-flight prepare.
    """
    for key, repo in enabled:
        # Safe seam: before each prepare repo (network/disk work).
        _check_deadline(deadline)
        # Coordinator normal prepare is ALWAYS ``allow_push=False``: the
        # coordinated flow never uses prepare's auto-ahead push. Any ahead state
        # is resolved later by the explicit expected-SHA push workflow only after
        # every enabled repo is clean/valid (see recovery / push section).
        #
        prep = prepare_repo_for_update(repo, branch="main", allow_push=False)
        if prep.outcome != GitOutcome.OK:
            logger.warning(
                "[cycle] repository %s not ready: %s; skipping cycle", key, prep.reason
            )
            return f"not_ready:{key}:{prep.reason}"
    return None


def _recover_first() -> "tuple[str | None, str | None]":
    """Run durable recovery FIRST in the locked cycle. Returns ``"recovered"`` when
    a journal was completed, ``"journal_invalid"`` when it failed closed, or
    ``None`` when there was no journal (the cycle proceeds normally)."""
    try:
        recovered = _recover_transaction()
    except JournalError as err:
        logger.error("[cycle] journal recovery failed closed: %s", err)
        return "journal_invalid", None
    return recovered


def _enabled_repos() -> list[tuple[str, "Repo | None"]]:
    """Build the list of (key, repo) pairs for the currently enabled repos."""
    enabled: list[tuple[str, Repo | None]] = []
    if update_options["master"]:
        enabled.append(("master", masterdb_diff_repo))
    if update_options["i18n"]:
        enabled.append(("i18n", i18n_diff_repo))
    return enabled


def _cycle_lock_paths() -> list[str]:
    """Select locks from current options plus any durable journal repos.

    Configuration can change between a crashed cycle and its next invocation.
    The journal is authoritative for recovery, so a valid journal's enabled
    repository locks are acquired before recovery dispatch even when the
    current ``update_options`` disables one of them.
    """
    lock_files = [masterdb_diff_folder_path + ".lock"]
    if update_options["i18n"]:
        lock_files.append(i18n_diff_folder_path + ".lock")
    if masterdb_diff_repo is not None:
        try:
            journal = _load_bound_journal()
        except JournalError:
            journal = None
        if journal is not None and "i18n" in journal.enabled_repos:
            lock_files.append(i18n_diff_folder_path + ".lock")
    return lock_files


def _invoke_locked_cycle(
    daily: bool,
    deadline: "Deadline | None",
    daily_due_date: str | None,
    daily_context: dict[str, str | None] | None,
) -> str:
    try:
        return _run_update_cycle_locked(
            daily,
            deadline=deadline,
            daily_due_date=daily_due_date,
            daily_context=daily_context,
        )
    except TypeError as err:
        if "daily_due_date" not in str(err) and "daily_context" not in str(err):
            raise
        return _run_update_cycle_locked(daily, deadline=deadline)


def _run_with_authoritative_locks(
    daily: bool,
    deadline: "Deadline | None",
    lock_files: list[str],
    daily_due_date: str | None = None,
    daily_context: dict[str, str | None] | None = None,
) -> str:
    """Acquire master first, then discover and acquire journal repo locks."""
    with repo_file_locks([lock_files[0]], non_blocking=True):
        try:
            authoritative = _load_bound_journal()
        except JournalError:
            # Let the locked cycle body classify malformed or unbound journal
            # state as journal_invalid without starting recovery.
            return _invoke_locked_cycle(daily, deadline, daily_due_date, daily_context)
        all_paths = list(lock_files)
        if authoritative is not None:
            if "i18n" in authoritative.enabled_repos:
                all_paths.append(i18n_diff_folder_path + ".lock")
            if "master" in authoritative.enabled_repos:
                all_paths.append(masterdb_diff_folder_path + ".lock")
        missing = [
            lock_path
            for lock_path in all_paths
            if path.realpath(lock_path) != path.realpath(lock_files[0])
        ]
        if missing:
            with repo_file_locks(missing, non_blocking=True):
                return _invoke_locked_cycle(
                    daily, deadline, daily_due_date, daily_context
                )
        return _invoke_locked_cycle(daily, deadline, daily_due_date, daily_context)


def _generate_and_publish_guarded(
    daily: bool,
    deadline: "Deadline | None",
    daily_due_date: str | None = None,
) -> tuple[dict[str, list[str]], "str | None"]:
    """Call :func:`_generate_and_publish` and map its exceptions to stable cycle
    statuses. Returns ``(manifest, None)`` on success, or ``( {}, status )`` when
    generation/publication failed (``"publication_failed"`` / ``"generation_failed"``).
    A :class:`CycleDeadlineExceeded` is re-raised unchanged so the outer wrapper
    returns ``deadline_exceeded``."""
    try:
        manifest = _generate_and_publish(
            daily, deadline=deadline, daily_due_date=daily_due_date
        )
    except PublicationError:
        return {}, "publication_failed"
    except RemoteSnapshotError:
        # The authoritative remote read happens before the first formal
        # replace. It is neither generation nor publication failure, and no
        # commit/push may be attempted after this status.
        return {}, "remote_snapshot_failed"
    except CycleDeadlineExceeded:
        # The cooperative deadline fired at the final safe seam (after staging
        # generation + validation, before the first formal os.replace). Propagate
        # it unchanged so the outer wrapper returns deadline_exceeded; formal trees
        # are untouched and no commit/push occurs. This must be caught BEFORE the
        # generic Exception handler so it is never mapped to generation_failed.
        raise
    except Exception:
        logger.exception("[cycle] generation/publication guard failed")
        return {}, "generation_failed"
    return manifest, None


def _run_update_cycle_locked(
    daily: bool,
    deadline: "Deadline | None" = None,
    daily_due_date: str | None = None,
    daily_context: dict[str, str | None] | None = None,
) -> str:
    """Run the locked cycle and always retry ready Strapi outbox records.

    The inner body only marks current-transaction Strapi records ready after Git
    success. This finally block therefore can safely run on any status: deferred
    in-flight records stay unsent, while previously-ready records get a recovery
    drain even when this cycle has no new Strapi records.
    """
    try:
        return _run_update_cycle_locked_body(
            daily,
            deadline=deadline,
            daily_due_date=daily_due_date,
            daily_context=daily_context,
        )
    finally:
        _drain_ready_strapi_outbox()


def _run_update_cycle_locked_body(  # noqa: C901
    daily: bool,
    deadline: "Deadline | None" = None,
    daily_due_date: str | None = None,
    daily_context: dict[str, str | None] | None = None,
) -> str:
    """Body of the cycle, executed while all locks are held.

    Returns a short status string for tests/observability.

    ``deadline`` is a cooperative :class:`Deadline` checked only at safe seams
    *before formal publication begins*: after existing maintenance/candidate
    gating, before any repo/network preparation, before each prepare, and
    between prepare and generation. It is never checked inside
    ``_generate_and_publish`` / ``_publish_staging`` or an individual atomic
    ``os.replace``, and — crucially — never after formal publication has started.
    Once ``_generate_and_publish`` has ``os.replace``-published the output, the
    cycle must run through commit (and push) to completion; a deadline that
    elapses only after successful formal publication must NOT turn the cycle into
    ``deadline_exceeded``. When ``daily`` is ``True`` the deadline must be
    disabled (``None``) so a daily cycle is never cooperatively cancelled by it.
    """
    # 0) Phase 2 durable recovery — FIRST, before the maintenance / new-version
    #    gate, the normal prepare, or any generation. If a durable journal from a
    #    crashed/interrupted cycle exists, recovery completes the interrupted work
    #    (publication -> commit -> push) and returns the distinct stable
    #    ``recovered`` status WITHOUT starting fresh work. A malformed/invalid
    #    journal fails closed (``journal_invalid``) and performs no generation,
    #    reset, or force-push.
    recovered, recovered_due_date = _recover_first()
    if recovered is not None:
        if daily_context is not None:
            daily_context["recovered_due_date"] = recovered_due_date
        return recovered

    # 0b) Maintenance / candidate gating (inside the lock, no global mutation).
    #    The deadline is checked only AFTER this gating and BEFORE any repo /
    #    network preparation, so the gate itself is never cooperatively skipped.
    should_not_proceed = _cycle_should_proceed(daily)
    if should_not_proceed is not None:
        return should_not_proceed

    # Reset the cycle-scoped candidate so a stale value from a prior (possibly
    # faked) cycle cannot leak into commit construction. ``_generate_and_publish``
    # re-stashes it on the real path; tests that fake generation must set it
    # explicitly if they need an explicit candidate.
    global _CYCLE_CANDIDATE
    _CYCLE_CANDIDATE = None

    # Safe seam: after gating, before any repo/network preparation work.
    _check_deadline(deadline)

    # 1) Prepare every enabled repository; stop before generation if any is not
    #    ready (never mutates on a blocked repository). The cooperative deadline is
    #    checked at the safe seam before each prepare.
    enabled = _enabled_repos()

    not_ready = _prepare_enabled_repositories(enabled, deadline)
    if not_ready is not None:
        return not_ready

    # Safe seam: between prepare and generation (before the expensive network
    # master-data fetch + staging generation).
    _check_deadline(deadline)

    # 2) Generate (staging) + validate + publish atomically. A generation or
    #    validation failure is "generation_failed"; a publication (os.replace)
    #    failure is "publication_failed" and must not be reported as generation.
    #    The cooperative deadline is NOT checked here or inside publication: once
    #    formal publication begins the cycle must continue through commit/push.
    manifest, gen_status = _generate_and_publish_guarded(
        daily, deadline, daily_due_date=daily_due_date
    )
    if gen_status is not None:
        return gen_status

    # The explicit candidate was stashed by ``_generate_and_publish`` (kept
    # independent of the published global, which stays None on i18n-only runs).
    # Read it back here for commit construction.
    candidate = _CYCLE_CANDIDATE

    # An all-disabled cycle has no transaction to commit or push.
    if not enabled:
        commits = _commit_enabled_repositories(enabled, manifest, candidate)
        _complete_journal_after_push(None)
        return "ok"

    # 3) Load the publication journal and enter COMMITTING before any Git commit
    #    action. A fresh cycle must never commit without the durable publication
    #    record that binds its staged files and base heads.
    try:
        journal = _load_bound_journal() if masterdb_diff_repo is not None else None
    except JournalError as err:
        logger.error("[cycle] commit journal load failed closed: %s", err)
        return "journal_invalid"
    # Empty-manifest generation doubles exercise only the cycle shell and do not
    # create a publication journal. A real generated manifest must have the
    # durable PUBLISHING record and is fail-closed below.
    if journal is None and not any(manifest.values()):
        commits = _commit_enabled_repositories(enabled, manifest, candidate)
        push_status = _push_enabled_repositories(commits)
        return push_status or "ok"
    if journal is None or journal.phase != TxnPhase.PUBLISHING:
        logger.error("[cycle] commit journal missing or not PUBLISHING")
        return "journal_invalid"
    if any(key not in journal.repos for key, _repo in enabled):
        logger.error("[cycle] commit journal is missing an enabled repository")
        return "journal_invalid"
    try:
        journal.set_phase(TxnPhase.COMMITTING)
    except JournalError as err:
        logger.error("[cycle] failed to enter COMMITTING: %s", err)
        return "journal_invalid"

    # Commit all enabled repositories before any push (explicit manifests and
    #    the explicit candidate version). No deadline check here: formal
    #    publication has already started, so the cycle runs to completion.
    try:
        commits = _commit_enabled_repositories(
            enabled, manifest, candidate, journal=journal
        )
    except TypeError as err:
        # A narrow compatibility path for older test doubles which predate the
        # optional journal argument. Production always takes the call above.
        if "journal" not in str(err):
            raise
        commits = _commit_enabled_repositories(enabled, manifest, candidate)

    # If any commit failed, do not push anything (preserve all local commits).
    if any(c.outcome is GitOutcome.FAILED for c in commits.values()):
        logger.error("[cycle] a commit failed; skipping push to avoid partial publish")
        return "commit_failed"

    _commits_real = bool(commits) and all(
        isinstance(c, GitResult) for c in commits.values()
    )
    if _commits_real and journal is not None:
        # Production has already persisted every target and COMMITTED state. A
        # test double may call this read-only validator after doing the same.
        halt = _advance_journal_to_committing(journal, enabled, commits)
        if halt is not None:
            return halt

    # 4) Push in deterministic order; stop after the first failure, preserving
    #    every local unpushed commit. No deadline check here either: publication
    #    has begun and the cycle must finish (a daily cycle is never cancelled,
    #    and an ordinary cycle is not cancelled after successful publication).
    push_status = _push_enabled_repositories(commits)
    if push_status is not None:
        # A push failure AFTER the journal was written must RETAIN the journal
        # (and its staging) so durable recovery finishes the work on the next
        # cycle. Only PRE-journal generation/deadline failures clean them. (Commit
        # failures are different — a commit failure leaves the journal in COMMITTING
        # for recovery.)
        _retain_journal_after_push_fail(journal)
        return push_status

    # Phase 2: all pushes succeeded — mark the journal COMPLETED, clean the
    # journal-owned staging, and delete the journal. (If no journal exists — e.g.
    # masterdb_diff_repo was None — there is nothing to clean.)
    try:
        _mark_strapi_transaction_ready(journal.transaction_id if journal else None)
    except StrapiOutboxError:
        # Do not delete the journal after Git success until local outbox
        # readiness is durably promoted.  External HTTP delivery is still only
        # attempted by the finally-block drain and cannot block Git completion.
        return "strapi_readiness_failed"
    _complete_journal_after_push(journal)
    return "ok"


def _run_update_cycle(  # noqa: C901
    daily: bool,
    deadline: "Deadline | None" = None,
    deadline_seconds: float | None = None,
    daily_due_date: str | None = None,
) -> str:
    """Single update-cycle entry point guarded by the in-process + flock locks.

    Overlapping same-process triggers *skip* (non-blocking in-process lock);
    a held cross-process flock also causes a skip. Locks are released on every
    exit path.

    The cooperative ``deadline`` is created/received once here. When ``daily`` is
    ``True`` the deadline is always disabled (``None``) so a daily cycle is never
    cooperatively cancelled. Otherwise, an explicit ``deadline`` is used, or one
    is built from ``deadline_seconds`` (defaulting to
    :data:`DEFAULT_ORDINARY_DEADLINE_SECONDS`). On
    :class:`CycleDeadlineExceeded` the cycle returns the stable ``deadline_exceeded``
    status; the existing ``finally`` still releases the process/flock locks.
    """
    # A daily cycle must never be cooperatively cancelled by a deadline.
    if daily:
        deadline = None
    elif deadline is None:
        if deadline_seconds is None:
            deadline_seconds = DEFAULT_ORDINARY_DEADLINE_SECONDS
        deadline = Deadline(deadline_seconds)

    acquired = _PROCESS_LOCK.acquire()
    if not acquired:
        logger.info("[cycle] skipped: another update cycle is already running")
        return "skipped:in_process"

    # Capture only after this invocation owns the process lock.  A contended
    # invocation cannot overwrite the active cycle's provenance.
    if daily_due_date is None and daily:
        daily_due_date = _tokyo_calendar_date()
    cycle_context: dict[str, str | None] = {}

    lock_files = _cycle_lock_paths()
    try:
        try:
            status = _run_with_authoritative_locks(
                daily,
                deadline,
                lock_files,
                daily_due_date=daily_due_date,
                daily_context=cycle_context,
            )
        except RepoLockUnavailable as err:
            logger.warning("[cycle] skipped: could not acquire repo locks: %s", err)
            return "skipped:repo_lock"
        except CycleDeadlineExceeded:
            logger.warning("[cycle] skipped: cooperative deadline exceeded")
            return "deadline_exceeded"
        # Clear/mark the Tokyo daily due only after a full daily cycle succeeds.
        # Lock skips, maintenance, generation/commit/push failure, and recovery
        # ambiguity intentionally leave the durable due state intact.
        recovered_daily_due = (
            status == "recovered"
            and cycle_context.get("recovered_due_date") == daily_due_date
        )
        if daily and (status == "ok" or recovered_daily_due):
            try:
                _mark_daily_completed(due_date=daily_due_date)
            except OSError:
                logger.exception(
                    "[daily_due] failed to persist completion; daily remains due"
                )
        return status
    finally:
        _PROCESS_LOCK.release()


def _bootstrap_init_client() -> None:
    if not jsonrpc_client.request("is_init") and not jsonrpc_client.request(
        "init", [pjsk_region]
    ):
        sys.exit(1)
    logger.info("[bootstrap] PJSK client inited")


def _bootstrap_prepare_repositories() -> None:
    global masterdb_diff_repo
    masterdb_diff_repo = check_git_folder(
        masterdb_diff_folder_path, remote_git_url_base
    )
    global i18n_diff_repo
    if update_options["i18n"]:
        i18n_diff_repo = check_git_folder(i18n_diff_folder_path, remote_git_url_base)
    logger.info("[bootstrap] Local git folders checked")


def _bootstrap_try_refresh() -> bool:
    """Check the PJSK server for maintenance before the initial cycle.

    This is a no-side-effect read only: it returns ``False`` to ask bootstrap to
    retry after a delay. All user-info writes and commits happen exclusively
    inside the unified locked cycle (``_run_update_cycle_locked``), never here.
    """
    check_version_res = jsonrpc_client.request("check_versions")
    if check_version_res["maintenance"]:
        logger.warning("[bootstrap] Server in maintenance, retry after 10 minutes")
        sleep(10 * 60)
        return False
    return True


def bootstrap():
    if check_update_simple_mode:
        bootstrap_simple()
        return

    _bootstrap_init_client()
    _bootstrap_prepare_repositories()

    while True:
        try:
            if not _bootstrap_try_refresh():
                continue
            break
        except Exception:
            logging.error(traceback.format_exc())
            logger.error(
                "[bootstrap] Failed to bootstrap, possible reasons: "
                "connection error or account info expired (for tw and kr "
                "servers). Retry after 10 minutes."
            )
            sleep(10 * 60)
    logger.info("[bootstrap] Fetched current available version info")

    # Run the single locked cycle (prepare + generate + publish + commit + push).
    status = _run_update_cycle(daily=True)
    logger.info("[bootstrap] initial cycle status: %s", status)

    logger.info(
        "[bootstrap] Finished, will look for new PJSK game data version at "
        "every 0/30 minutes of the hours"
    )
    scheduler.start()


def bootstrap_simple():
    if pjsk_region not in ("cn", "tw", "kr"):
        raise RuntimeError(
            "Simple check-update mode only supports Nuverse servers: cn, tw, kr"
        )
    if not check_update_versions_url:
        raise RuntimeError(
            "CHECK_UPDATE_VERSIONS_URL is required in simple check-update mode"
        )
    if update_options["userInfo"]:
        logger.warning("Simple check-update mode does not support userInfo")

    global masterdb_diff_repo
    masterdb_diff_repo = check_git_folder(
        masterdb_diff_folder_path, remote_git_url_base
    )
    global i18n_diff_repo
    if update_options["i18n"]:
        i18n_diff_repo = check_git_folder(i18n_diff_folder_path, remote_git_url_base)
    logger.info("[bootstrap] Local git folders checked")

    status = _run_update_cycle(daily=True)
    logger.info("[bootstrap] initial simple-mode cycle status: %s", status)

    logger.info(
        "[bootstrap] Finished, will look for new PJSK game data version at "
        "every 0/30 minutes of the hours"
    )
    scheduler.start()


if __name__ == "__main__":
    bootstrap()
