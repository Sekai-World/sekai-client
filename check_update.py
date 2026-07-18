import logging
import os
import re
import shutil
import sys
import traceback
from datetime import datetime
from os import getenv, path
from time import monotonic as _monotonic
from time import sleep
from typing import Any

import requests
import ujson as json
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from git.repo import Repo
from git.util import Actor
from pytz import timezone

from logging_config import configure_logging
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

# --- Phase 4.2: staging roots, lock state, and the single in-process lock ---
#
# During a cycle, all candidate JSON is generated into repository-adjacent
# staging directories on the same filesystem; only after every staged file is
# validated are they published into the formal working trees with ``os.replace``
# (``versions.json`` last). These module-level roots are ``None`` outside a
# cycle, in which case writes go straight to the working trees (legacy behavior)
# and no manifest is recorded.
_MASTER_STAGING_ROOT: str | None = None
_I18N_STAGING_ROOT: str | None = None
_STAGING_MANIFEST: dict[str, list[str]] | None = None

_PROCESS_LOCK = ProcessCycleLock()

# Repositories are prepared/committed/pushed in this deterministic order.
_REPO_ORDER = ("master", "i18n")

# Default cooperative deadline (seconds) for an ordinary update cycle. A daily
# cycle never uses a deadline (it must always run to completion). Tests may pass
# a smaller value via ``_run_update_cycle``'s ``deadline_seconds`` parameter.
DEFAULT_ORDINARY_DEADLINE_SECONDS = 3600


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
        os.makedirs(parent, exist_ok=True)  # noqa: F821 - os imported below
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
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


def _post_strapi_ids(endpoint: str, ids: list[int]) -> None:
    # The token is sent via the Authorization header (never in the URL query).
    # X-Strapi-Token is also accepted by Strapi; both carry the secret in
    # a header so it is not logged in access logs. Legacy query-string token
    # fallback is intentionally removed.
    #
    # A Strapi failure must NOT block the master-data update pipeline
    # (it is best-effort auxiliary publishing). We therefore surface the
    # HTTP error via a redacted exception log and continue.
    if strapi_base_url and strapi_token:
        try:
            requests.post(
                f"{strapi_base_url}/{endpoint}",
                json=ids,
                headers={
                    "Authorization": f"Bearer {strapi_token}",
                    "X-Strapi-Token": strapi_token,
                },
                timeout=60,
            ).raise_for_status()
        except requests.RequestException as err:
            logger.exception(
                "[_post_strapi_ids] failed to publish %s ids: %s",
                endpoint,
                err,
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


scheduler = BlockingScheduler(timezone=timezone("Asia/Tokyo"))


def scheduled_update_job() -> None:
    """Single scheduler entry, fired every 30 minutes.

    At 04:00 (hour == 4, minute == 0) it runs exactly one cycle with
    daily/full-refresh semantics; at 04:30 and every other half-hour it runs an
    ordinary update. The cycle itself owns the locks, so overlapping triggers
    skip rather than queue stale work.
    """
    now = datetime.now(timezone("Asia/Tokyo"))
    daily = _is_daily_run(now)
    logger.info(
        "[scheduled_update_job] triggered at %s (daily=%s)",
        now.strftime("%H:%M"),
        daily,
    )
    _run_update_cycle(daily=daily)


# One half-hour entry. At 04:00 it is the unique daily/full-refresh cycle;
# at 04:30 (and other half-hours) it is ordinary. max_instances=1 + coalesce
# ensure a single in-flight run and de-duplicated misfires.
scheduled_trigger = CronTrigger(minute="0,30", second="0")
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

    return master_data


def download_nuverse_master_data(cdn_version: int) -> dict[str, Any]:
    base_url = nuverse_master_data_base_url[pjsk_region]

    if check_update_simple_mode:
        res = requests.get(f"{base_url}/master-data-{cdn_version}.info", timeout=150)
        res.raise_for_status()
        return decrypt_msgpack(res.content)

    return _require_dict_response(
        jsonrpc_client.request(
            "request_and_decrypt", [f"{base_url}/master-data-{cdn_version}.info"]
        ),
        "download Nuverse master data",
    )


def fetch_simple_version_info() -> dict[str, Any]:
    if not check_update_versions_url:
        raise RuntimeError(
            "CHECK_UPDATE_VERSIONS_URL is required in simple check-update mode"
        )

    res = requests.get(check_update_versions_url, timeout=150)
    res.raise_for_status()
    return _require_dict_response(res.json(), "fetch simple version info")


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
    return _require_dict_response(
        jsonrpc_client.request("version_info"), "fetch version info"
    )


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

    logger.debug("[refresh_information] write user informations")
    _write_master_file("userInformations.json", res["informations"])


def _commit_diff(
    repo: Repo | None,
    operation: str,
    folder_label: str,
    commit_message: str,
    author: Actor,
    paths: list[str] | None = None,
) -> GitResult:
    """Stage (explicit ``paths`` when given) and commit only — no push.

    Contract:
    - ``repo`` is ``None`` or ``version_info`` is missing -> ``FAILED`` (these
      are operational errors, never a benign "nothing to do").
    - repository clean -> ``NOTHING_TO_DO``.
    - stage/commit failure -> ``FAILED`` (``reason="commit_failed"``), with the
      local repo left untouched.

    When ``paths`` is provided (Phase 4.2 cycle), only those explicit paths are
    staged; otherwise the historical broad ``index.add("**")`` is used. The
    original ``master-db-diff-bot`` / ``i18n-diff-bot`` actors are passed by the
    wrapper. The historical boolean contract is preserved via
    :meth:`GitResult.__bool__`.
    """
    if repo is None:
        logger.error("[%s] repository not initialized", operation)
        return GitResult(
            outcome=GitOutcome.FAILED,
            reason="repo_missing",
            operation=operation,
        )
    if version_info is None:
        logger.error("[%s] version_info not loaded; cannot build commit", operation)
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


def _push_diff(repo: Repo | None, operation: str) -> GitResult:
    """Push the local HEAD and surface a credential-safe result.

    A push failure or unverified push returns ``PENDING_PUSH``; the local
    commit is *kept* and never deleted, recloned, reset, or force-pushed. The
    warning (when pending) contains only operation, reason, and retained local
    SHA — never the remote URL or exception detail.
    """
    if repo is None:
        return GitResult(
            outcome=GitOutcome.FAILED,
            reason="repo_missing",
            operation=operation,
        )
    push_result = push_current_head(repo, branch="main", require_remote_branch=True)
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


def _is_daily_run(now: datetime) -> bool:
    """True only at the unique 04:00 daily/full-refresh boundary."""
    return now.hour == 4 and now.minute == 0


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
    staging_root: str, dest_root: str, relpaths: list[str]
) -> None:
    """Atomically publish staged files into the working tree via ``os.replace``.

    ``versions.json`` is published last. If any replacement fails, a
    :class:`PublicationError` is raised (with the already-published dirty working
    tree left intact for diagnosis) and the remaining staging is cleaned so no
    half-published content lingers. No commit/push happens here.
    """
    for rel in _publish_order(relpaths):
        src = path.join(staging_root, rel)
        dst = path.join(dest_root, rel)
        dst_dir = path.dirname(dst)
        if dst_dir:
            os.makedirs(dst_dir, exist_ok=True)
        try:
            os.replace(src, dst)
        except OSError as err:  # publish partially; stop, clean the rest
            logger.error("[publish] replace failed for %s: %s", rel, err)
            _clear_staging_dir(staging_root)
            raise PublicationError(f"replace failed for {rel}: {err}") from err


def _generate_and_publish(daily: bool) -> dict[str, list[str]]:
    """Generate all candidate JSON into staging, validate, then publish.

    The candidate version is fetched up front (or taken from a passed-in
    argument) and used locally for every generated file and commit message.
    The global published ``version_info`` is *not* advanced until every staged
    file has been generated, validated, and published (``os.replace``) into both
    working trees. On any generation/validation failure the staging directories
    are cleared and the exception is re-raised *without* touching the formal
    working trees, so both working trees stay byte-identical and the published
    version unchanged. A publication (``os.replace``) failure stops before
    commit/push and leaves the dirty working tree intact; the global published
    version is likewise left unchanged.

    The global ``version_info`` is advanced *only* when master output is enabled
    and the formal ``versions.json`` was actually published (``os.replace`` into
    the master working tree succeeded). When master is disabled (``i18n=True``
    only) or both repositories are disabled, no ``versions.json`` is ever staged
    or published, so the global published version is never advanced and the
    formal ``versions.json`` stays consistent with it.
    """
    global _MASTER_STAGING_ROOT, _I18N_STAGING_ROOT, _STAGING_MANIFEST
    global version_info

    master_staging = masterdb_diff_folder_path + ".staging"
    i18n_staging = i18n_diff_folder_path + ".staging"
    _clear_staging_dir(master_staging)
    _clear_staging_dir(i18n_staging)

    manifest: dict[str, list[str]] = {"master": [], "i18n": []}
    # Candidate is local to this cycle; the published global stays untouched
    # until publication of every staged file succeeds. refresh_version returns
    # the candidate it generated with, so the global is advanced only afterward.
    candidate: dict[str, Any] | None = None
    try:
        _MASTER_STAGING_ROOT = master_staging
        _I18N_STAGING_ROOT = i18n_staging
        _STAGING_MANIFEST = manifest

        candidate = refresh_version()
        if not check_update_simple_mode and update_options["userInfo"]:
            save_info_from_suite_user()
        if update_options["userInfo"]:
            refresh_information()
    except Exception:
        logger.exception("[cycle] generation/validation failed; discarding staging")
        _STAGING_MANIFEST = None
        _MASTER_STAGING_ROOT = None
        _I18N_STAGING_ROOT = None
        _clear_staging_dir_safe(master_staging)
        _clear_staging_dir_safe(i18n_staging)
        raise
    finally:
        _STAGING_MANIFEST = None
        _MASTER_STAGING_ROOT = None
        _I18N_STAGING_ROOT = None

    # Publish in deterministic repo order: master (all except versions.json)
    # first, i18n second, and the formal ``versions.json`` *last* and on its own —
    # sourced from the master staging root. Keeping the master staging directory
    # alive until after ``versions.json`` is published guarantees an i18n replace
    # failure leaves the formal ``versions.json`` (and thus the published global)
    # untouched. The manifest path ``versions.json`` in either repo staging denotes
    # the global version file, published into the master working tree.
    master_manifest = [p for p in manifest["master"] if p != "versions.json"]
    i18n_manifest = [p for p in manifest["i18n"] if p != "versions.json"]

    # Whether the formal ``versions.json`` was actually published into the master
    # working tree. Only a successful master-enabled publication advances the
    # global. master=False (i18n-only) and all-disabled never reach this branch.
    global_published = False

    # Outer try/finally guarantees both staging roots are cleared after the
    # publication phase, *without* masking a PublicationError: ``raise`` inside
    # the ``except`` preserves the original exception/backtrace, and the cleanup
    # only removes the staging directories (any already-``os.replace``'d dirty
    # working trees are intentionally left intact for diagnosis).
    try:
        # 1) master: everything except versions.json.
        _publish_staging(master_staging, masterdb_diff_folder_path, master_manifest)
        # 2) i18n: everything except versions.json.
        _publish_staging(i18n_staging, i18n_diff_folder_path, i18n_manifest)
        # 3) global versions.json last and alone, from the master staging root,
        #    but only when master is enabled and actually staged it.
        if "versions.json" in manifest["master"]:
            _publish_staging(
                master_staging, masterdb_diff_folder_path, ["versions.json"]
            )
            global_published = True
    except BaseException:
        # A publication failure mid-step-1 would otherwise leave the i18n staging
        # root behind. Clean BOTH roots here, then re-raise the original error so
        # a cleanup failure cannot override the original PublicationError.
        _clear_staging_dir_safe(master_staging)
        _clear_staging_dir_safe(i18n_staging)
        raise
    finally:
        # Best-effort clearance of both staging roots on the success path too;
        # safe logging ensures a stray cleanup error cannot mask prior work.
        _clear_staging_dir_safe(master_staging)
        _clear_staging_dir_safe(i18n_staging)

    # Only now — after every staged file was generated, validated, and published
    # into the working trees — do we advance the published global, and only when
    # the formal ``versions.json`` was genuinely published by an enabled master.
    # A ``None`` candidate (legacy caller) or a disabled master keeps the prior
    # published value, so the formal ``versions.json`` and the global stay in
    # lock-step.
    if candidate is not None and global_published:
        version_info = candidate
    return manifest


def _commit_enabled_repositories(
    enabled: list[tuple[str, Repo | None]], manifest: dict[str, list[str]]
) -> dict[str, GitResult]:
    """Commit every enabled repository (explicit manifest paths) before push."""
    commits: dict[str, GitResult] = {}
    for key, repo in enabled:
        relpaths = manifest.get(key, [])
        if key == "master":
            commits[key] = _commit_diff(
                repo,
                operation="commit_master_diff",
                folder_label=local_git_folder_names["masterDBDiff"],
                commit_message=(
                    f"master version {version_info['dataVersion']} "
                    f"asset version {version_info['assetVersion']}"
                ),
                author=Actor("master-db-diff-bot", "anonymous@example.com"),
                paths=relpaths,
            )
        else:
            commits[key] = _commit_diff(
                repo,
                operation="commit_i18n_files",
                folder_label=local_git_folder_names["i18n"],
                commit_message=(
                    f"i18n update for master version {version_info['dataVersion']}"
                ),
                author=Actor("i18n-diff-bot", "anonymous@example.com"),
                paths=relpaths,
            )
    return commits


def _push_enabled_repositories(commits: dict[str, GitResult]) -> str | None:
    """Push committed repositories in deterministic order.

    Stops after the first push failure (preserving every local unpushed commit)
    and returns a ``push_failed:<key>:<reason>`` status, or ``None`` on success.
    """
    for key in _REPO_ORDER:
        commit_res = commits.get(key)
        if commit_res is None or commit_res.outcome != GitOutcome.OK:
            continue
        repo = masterdb_diff_repo if key == "master" else i18n_diff_repo
        push_res = _push_diff(repo, f"push_{key}")
        if push_res.outcome != GitOutcome.OK:
            logger.warning(
                "[cycle] push failed for %s (%s); not pushing remaining repos",
                key,
                push_res.reason,
            )
            return f"push_failed:{key}:{push_res.reason}"
    return None


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


def _run_update_cycle_locked(daily: bool, deadline: "Deadline | None" = None) -> str:
    """Body of the cycle, executed while all locks are held.

    Returns a short status string for tests/observability.

    ``deadline`` is a cooperative :class:`Deadline` checked only at safe seams
    (after existing maintenance/candidate gating, before any repo/network
    preparation, before each prepare, between prepare and generation, before
    commit, before push). It is never checked inside ``_publish_staging`` or an
    individual atomic ``os.replace``. When ``daily`` is ``True`` the deadline
    must be disabled (``None``) so a daily cycle is never cooperatively
    cancelled by it.
    """
    # 0) Maintenance / candidate gating (inside the lock, no global mutation).
    #    The deadline is checked only AFTER this gating and BEFORE any repo /
    #    network preparation, so the gate itself is never cooperatively skipped.
    should_not_proceed = _cycle_should_proceed(daily)
    if should_not_proceed is not None:
        return should_not_proceed

    # Safe seam: after gating, before any repo/network preparation work.
    _check_deadline(deadline)

    # 1) Prepare every enabled repository; stop before generation if any is not
    #    ready (never mutates on a blocked repository).
    enabled = []
    if update_options["master"]:
        enabled.append(("master", masterdb_diff_repo))
    if update_options["i18n"]:
        enabled.append(("i18n", i18n_diff_repo))

    for key, repo in enabled:
        # Safe seam: before each prepare repo (network/disk work).
        _check_deadline(deadline)
        prep = prepare_repo_for_update(repo, branch="main")
        if prep.outcome != GitOutcome.OK:
            logger.warning(
                "[cycle] repository %s not ready: %s; skipping cycle", key, prep.reason
            )
            return f"not_ready:{key}:{prep.reason}"

    # Safe seam: between prepare and generation (before the expensive network
    # master-data fetch + staging generation).
    _check_deadline(deadline)

    # 2) Generate (staging) + validate + publish atomically. A generation or
    #    validation failure is "generation_failed"; a publication (os.replace)
    #    failure is "publication_failed" and must not be reported as generation.
    try:
        manifest = _generate_and_publish(daily)
    except PublicationError:
        return "publication_failed"
    except Exception:
        return "generation_failed"

    # 3) Commit all enabled repositories before any push (explicit manifests).
    #    Safe seam: before commit.
    _check_deadline(deadline)
    commits = _commit_enabled_repositories(enabled, manifest)

    # If any commit failed, do not push anything (preserve all local commits).
    if any(c.outcome is GitOutcome.FAILED for c in commits.values()):
        logger.error("[cycle] a commit failed; skipping push to avoid partial publish")
        return "commit_failed"

    # 4) Push in deterministic order; stop after the first failure, preserving
    #    every local unpushed commit. Safe seam: before push.
    _check_deadline(deadline)
    push_status = _push_enabled_repositories(commits)
    if push_status is not None:
        return push_status

    return "ok"


def _run_update_cycle(
    daily: bool,
    deadline: "Deadline | None" = None,
    deadline_seconds: float | None = None,
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

    lock_files = [masterdb_diff_folder_path + ".lock"]
    if update_options["i18n"]:
        lock_files.append(i18n_diff_folder_path + ".lock")
    try:
        try:
            with repo_file_locks(lock_files, non_blocking=True):
                return _run_update_cycle_locked(daily, deadline=deadline)
        except RepoLockUnavailable as err:
            logger.warning("[cycle] skipped: could not acquire repo locks: %s", err)
            return "skipped:repo_lock"
        except CycleDeadlineExceeded:
            logger.warning("[cycle] skipped: cooperative deadline exceeded")
            return "deadline_exceeded"
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
