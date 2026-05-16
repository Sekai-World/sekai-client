import logging
import re
import shutil
import sys
import traceback
from os import getenv, path
from time import sleep

import requests
import ujson as json
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from git.repo import Repo
from git.util import Actor
from pytz import timezone

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
from utils.git import check_git_folder
from utils.jsonrpc_client import JSONRPCClient

LOGLEVEL = getenv("LOGLEVEL", "INFO").upper()
logging.basicConfig(level=LOGLEVEL, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

jsonrpc_client = JSONRPCClient(f"http://localhost:{getenv('JSONRPC_PORT', '3939')}/")

version_info = None
is_in_maintenance = False

masterdb_diff_folder_path = path.join(
    path.dirname(__file__), local_git_folder_names["masterDBDiff"]
)
masterdb_diff_repo: Repo | None = None
i18n_diff_folder_path = path.join(
    path.dirname(__file__), local_git_folder_names["i18n"]
)
i18n_diff_repo: Repo | None = None


def day_change_func():
    logger.debug(
        "[bootstrap] Pull %s repo remote changes before making any local changes",
        local_git_folder_names["masterDBDiff"],
    )
    masterdb_diff_repo.remote().pull()

    refresh_version()
    if (
        not check_update_simple_mode
        and not is_in_maintenance
        and update_options["userInfo"]
    ):
        save_info_from_suite_user()

    if commit_master_diff():
        logger.info("Updated and committed master data")
        if update_options["i18n"]:
            if commit_i18n_files():
                logger.info("Updated and committed i18n data")


def try_update_func():
    logger.info("Check update triggered by cron job")

    if check_update_simple_mode:
        try_update_simple_func()
        return

    ver_res = None
    try:
        ver_res = jsonrpc_client.request("check_versions", [version_info])
    except Exception:
        logger.exception(
            "[try_update_func] check_versions failed, re-bootstrap before retry"
        )
        bootstrap()
        ver_res = jsonrpc_client.request("check_versions", [version_info])

    global is_in_maintenance
    if ver_res["maintenance"]:
        logger.warning("PJSK server is in maintenance, skipping...")
        is_in_maintenance = True
        return

    is_in_maintenance = False
    logger.debug(
        "[bootstrap] Pull %s repo remote changes before making any local changes",
        local_git_folder_names["masterDBDiff"],
    )
    masterdb_diff_repo.remote().pull()
    if update_options["i18n"]:
        logger.debug(
            "[bootstrap] Pull %s repo remote changes before making any local changes",
            local_git_folder_names["i18n"],
        )
        i18n_diff_repo.remote().pull()
    if ver_res["new_version"] and update_options["master"]:
        logger.info("Got a new version during checking update")
        refresh_version()

    if update_options["userInfo"]:
        refresh_information()

    if commit_master_diff():
        logger.info("Updated and committed master data")
        if update_options["i18n"]:
            if commit_i18n_files():
                logger.info("Updated and committed i18n data")


def try_update_simple_func():
    global version_info
    global is_in_maintenance

    ver_res = check_versions_simple()
    is_in_maintenance = False

    logger.debug(
        "[bootstrap] Pull %s repo remote changes before making any local changes",
        local_git_folder_names["masterDBDiff"],
    )
    masterdb_diff_repo.remote().pull()
    if update_options["i18n"]:
        logger.debug(
            "[bootstrap] Pull %s repo remote changes before making any local changes",
            local_git_folder_names["i18n"],
        )
        i18n_diff_repo.remote().pull()

    if ver_res["new_version"] and update_options["master"]:
        logger.info("Got a new version during checking update")
        refresh_version()

    if update_options["userInfo"]:
        logger.warning("Simple check-update mode does not support userInfo")

    if commit_master_diff():
        logger.info("Updated and committed master data")
        if update_options["i18n"]:
            if commit_i18n_files():
                logger.info("Updated and committed i18n data")


def _write_i18n_json(filename: str, payload: dict) -> None:
    with open(path.join(i18n_diff_folder_path, "ja", filename), "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _post_strapi_ids(endpoint: str, ids: list[int]) -> None:
    if strapi_base_url and strapi_token:
        requests.post(
            f"{strapi_base_url}/{endpoint}?token={strapi_token}",
            json=ids,
            timeout=60,
        )


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
    _post_strapi_ids("cards/fromDB", [elem["id"] for elem in data if elem["id"] > 500])


def _update_i18n_musics(data: list) -> None:
    _write_i18n_json("music_titles.json", {elem["id"]: elem["title"] for elem in data})
    _post_strapi_ids("musics/fromDB", [elem["id"] for elem in data if elem["id"] > 290])


def _update_i18n_events(data: list) -> None:
    _write_i18n_json("event_name.json", {elem["id"]: elem["name"] for elem in data})
    _post_strapi_ids("events/fromDB", [elem["id"] for elem in data if elem["id"] > 70])


def _update_i18n_virtual_lives(data: list) -> None:
    _write_i18n_json(
        "virtualLive_name.json", {elem["id"]: elem["name"] for elem in data}
    )
    _post_strapi_ids(
        "virtual-lives/fromDB", [elem["id"] for elem in data if elem["id"] > 180]
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
day_change_cron_trigger = CronTrigger(hour="4", minute="0", second="0")
day_change_job = scheduler.add_job(
    day_change_func, day_change_cron_trigger, name="day_change_job"
)
try_update_cron_trigger = CronTrigger(minute="0,30", second="0")
try_update_job = scheduler.add_job(
    try_update_func, try_update_cron_trigger, name="try_update_job"
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


def get_splitted_master_data():
    global pjsk_region
    global version_info

    master_split_paths: list[str] = jsonrpc_client.request("master_split_paths")

    # download every split
    master_data_raw = []
    for split_path in master_split_paths:
        logger.debug(f"[get_splitted_master_data] fetch split {split_path}")
        master_data_raw.append(
            jsonrpc_client.request("call_pjsk_api", [f"/{split_path}"])
        )

    master_data: dict[str, list] = {}
    for idx, split_data_raw in enumerate(master_data_raw):
        logger.debug(
            f"[get_splitted_master_data] merging split {master_split_paths[idx]}"
        )
        master_data |= split_data_raw

    return master_data


def download_nuverse_master_data(cdn_version: int):
    base_url = nuverse_master_data_base_url[pjsk_region]

    if check_update_simple_mode:
        res = requests.get(f"{base_url}/master-data-{cdn_version}.info", timeout=150)
        res.raise_for_status()
        return decrypt_msgpack(res.content)

    return jsonrpc_client.request(
        "request_and_decrypt", [f"{base_url}/master-data-{cdn_version}.info"]
    )


def fetch_simple_version_info():
    if not check_update_versions_url:
        raise RuntimeError(
            "CHECK_UPDATE_VERSIONS_URL is required in simple check-update mode"
        )

    res = requests.get(check_update_versions_url, timeout=150)
    res.raise_for_status()
    return res.json()


def check_versions_simple():
    global version_info

    curr_ver_info = fetch_simple_version_info()
    if version_info is None:
        version_info = curr_ver_info
        return {"maintenance": False, "new_version": True}

    new_version = (
        version_info.get("dataVersion") != curr_ver_info.get("dataVersion")
        or version_info.get("assetVersion") != curr_ver_info.get("assetVersion")
        or version_info.get("appVersion") != curr_ver_info.get("appVersion")
        or version_info.get("cdnVersion") != curr_ver_info.get("cdnVersion")
    )
    version_info = curr_ver_info

    return {"maintenance": False, "new_version": new_version}


def _pull_i18n_repo_before_refresh() -> None:
    if not update_options["i18n"]:
        return
    if not i18n_diff_repo:
        raise RuntimeError(
            f"{local_git_folder_names['i18n']} repository must be existed "
            "to refresh version info."
        )
    i18n_diff_repo.remote().pull()
    logger.debug(
        f"[refresh_version] pulled repository {local_git_folder_names['i18n']}"
    )


def _refresh_version_info_from_source() -> dict:
    logger.info(f"[refresh_version] fetching version info from {pjsk_region} server")
    if check_update_simple_mode:
        return fetch_simple_version_info()

    if pjsk_region in ("jp", "en"):
        if not jsonrpc_client.request("is_login"):
            jsonrpc_client.request("login")
        else:
            logger.debug(
                "[refresh_version] relogin to refresh full version info and "
                "splitted master data list"
            )
            jsonrpc_client.request("relogin")
    return jsonrpc_client.request("version_info")


def _fetch_master_data_by_region() -> dict[str, list]:
    if pjsk_region in ("jp", "en"):
        return get_splitted_master_data()
    return download_nuverse_master_data(version_info["cdnVersion"])


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
    file_data: list,
    current_structures: dict[str, list],
) -> tuple[list, int | None]:
    if not (pjsk_region in ["cn", "tw", "kr"] and key in current_structures):
        return file_data, None

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
    fallback_value: list,
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

    value_ids = {item[id_key] for item in fallback_value}
    merged = [x for x in old_data if x[id_key] not in value_ids] + fallback_value
    merged.sort(key=lambda x: x[id_key])
    return merged


def _write_compact_master_alias_if_needed(key: str, file_data: list) -> None:
    if not (pjsk_region in ["cn", "tw", "kr"] and key.startswith("compact")):
        return
    new_key = key[len("compact") :]
    new_key = new_key[:1].lower() + new_key[1:]
    new_file_path = path.join(masterdb_diff_folder_path, f"{new_key}.json")
    new_file_data = restore_compact_data(file_data)
    with open(new_file_path, "w") as f:
        json.dump(new_file_data, f, ensure_ascii=False, indent=2)


def refresh_version():
    global version_info

    logger.debug("[refresh_version] called")

    _pull_i18n_repo_before_refresh()
    version_info = _refresh_version_info_from_source()
    logger.debug(f"[refresh_version] fetched version info: {version_info}")
    with open(path.join(masterdb_diff_folder_path, "versions.json"), "w") as f:
        json.dump(version_info, f, indent=2)
        f.truncate()

    logger.debug("[refresh_version] fetching master db")
    master_data: dict[str, list] = _fetch_master_data_by_region()
    logger.debug("[refresh_version] write master db to separate json files by keys")
    structures_app_ver = version_info.get("appVersion") or getenv("APP_VER", "")
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
        file_path = path.join(masterdb_diff_folder_path, f"{key}.json")
        file_data = value
        logger.debug(f"[refresh_version] start writing master db {key}.json")
        last_record_idx: int | None = None

        try:
            file_data, last_record_idx = _convert_master_records_for_region(
                key, file_data, current_structures
            )
            id_key = _resolve_master_id_key(key)
            file_data = _merge_existing_file_data(file_path, file_data, id_key, value)
            with open(file_path, "w") as f:
                json.dump(file_data, f, ensure_ascii=False, indent=2)
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

        logger.debug(f"[refresh_version] wrote master db {key}.json")

        if update_options["i18n"]:
            logger.debug(
                f"[refresh_version] write i18n json file for {key}.json if necessary"
            )
            update_i18n_files(key, file_data)

    logger.debug("[refresh_version] finished")


def save_info_from_suite_user():
    suite_user = jsonrpc_client.request("login_user_info")

    logger.debug("[save_info_from_suite_user] write user home banners")
    with open(path.join(masterdb_diff_folder_path, "userHomeBanners.json"), "w") as f:
        json.dump(suite_user["userHomeBanners"], f, ensure_ascii=False, indent=2)

    if pjsk_region == "en":
        refresh_information()
    elif suite_user.get("userInformations", None):
        logger.debug("[save_info_from_suite_user] write user informations")
        with open(
            path.join(masterdb_diff_folder_path, "userInformations.json"), "w"
        ) as f:
            json.dump(suite_user["userInformations"], f, ensure_ascii=False, indent=2)

    logger.debug("[save_info_from_suite_user] finished")
    return suite_user


def refresh_information():
    logger.debug("[refresh_information] get informations")
    res = jsonrpc_client.request("fetch_information")

    logger.debug("[refresh_information] write user informations")
    with open(path.join(masterdb_diff_folder_path, "userInformations.json"), "w") as f:
        json.dump(res["informations"], f, ensure_ascii=False, indent=2)


def commit_master_diff():
    global masterdb_diff_repo
    data_ver = version_info["dataVersion"]
    asset_ver = version_info["assetVersion"]

    if masterdb_diff_repo and masterdb_diff_repo.is_dirty(untracked_files=True):
        try:
            logger.debug(
                "[commit_master_diff] add files to staged in %s",
                local_git_folder_names["masterDBDiff"],
            )

            curr_index = masterdb_diff_repo.index
            curr_index.add("**")

            logger.debug(
                "[commit_master_diff] commit staged changes in %s",
                local_git_folder_names["masterDBDiff"],
            )
            curr_index.commit(
                f"master version {data_ver} asset version {asset_ver}",
                author=Actor("master-db-diff-bot", "anonymous@example.com"),
            )

            logger.debug(
                "[commit_master_diff] push commit to origin in %s",
                local_git_folder_names["masterDBDiff"],
            )
            masterdb_diff_repo.remote().push().raise_if_error()
        except Exception:
            logger.exception(
                "[commit_master_diff] failed to commit/push, re-cloning repository"
            )
            # reset to last commit
            # masterdb_diff_repo.head.reset(commit="HEAD~1",
            #                               index=True,
            #                               working_tree=True)
            # delete current repo folder and clone again
            shutil.rmtree(masterdb_diff_folder_path)
            masterdb_diff_repo = check_git_folder(
                masterdb_diff_folder_path, remote_git_url_base
            )
            return False

        return True

    return False


def commit_i18n_files():
    global i18n_diff_repo
    data_ver = version_info["dataVersion"]

    if i18n_diff_repo and i18n_diff_repo.is_dirty(untracked_files=True):
        try:
            logger.debug(
                "[commit_i18n_files] add files to staged in %s",
                local_git_folder_names["i18n"],
            )

            curr_index = i18n_diff_repo.index
            curr_index.add("**")

            logger.debug(
                "[commit_i18n_files] commit staged changes in %s",
                local_git_folder_names["i18n"],
            )
            curr_index.commit(
                f"i18n update for master version {data_ver}",
                author=Actor("i18n-diff-bot", "anonymous@example.com"),
            )

            logger.debug(
                "[commit_i18n_files] push commit to origin in %s",
                local_git_folder_names["i18n"],
            )
            i18n_diff_repo.remote().push().raise_if_error()
        except Exception:
            logger.exception(
                "[commit_i18n_files] failed to commit/push, re-cloning repository"
            )
            # reset to last commit
            # i18n_diff_repo.head.reset(commit="HEAD~1",
            #                           index=True,
            #                           working_tree=True)
            # delete current repo folder and clone again
            shutil.rmtree(i18n_diff_folder_path)
            i18n_diff_repo = check_git_folder(
                i18n_diff_folder_path, remote_git_url_base
            )
            return False

        return True

    return False


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
    check_version_res = jsonrpc_client.request("check_versions")
    if check_version_res["maintenance"]:
        logger.warning("[bootstrap] Server in maintenance, retry after 10 minutes")
        sleep(10 * 60)
        return False

    logger.debug(
        "[bootstrap] Pull %s repo remote changes before making any local changes",
        local_git_folder_names["masterDBDiff"],
    )
    masterdb_diff_repo.remote().pull()
    if update_options["userInfo"]:
        jsonrpc_client.request("login")
        save_info_from_suite_user()

    global version_info
    version_info = jsonrpc_client.request("version_info")
    if update_options["master"]:
        refresh_version()
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

    if commit_master_diff():
        logger.info("Updated and committed master data")
        if update_options["i18n"]:
            commit_i18n_files()
            logger.info("Updated and committed i18n data")

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

    while True:
        try:
            logger.debug(
                "[bootstrap] Pull %s repo remote changes before making any "
                "local changes",
                local_git_folder_names["masterDBDiff"],
            )
            masterdb_diff_repo.remote().pull()

            if update_options["master"]:
                refresh_version()
            break
        except Exception:
            logging.error(traceback.format_exc())
            logger.error(
                "[bootstrap] Failed to bootstrap simple mode. Retry after 10 minutes."
            )
            sleep(10 * 60)
    logger.info("[bootstrap] Fetched current available version info")

    if commit_master_diff():
        logger.info("Updated and committed master data")
        if update_options["i18n"]:
            commit_i18n_files()
            logger.info("Updated and committed i18n data")

    logger.info(
        "[bootstrap] Finished, will look for new PJSK game data version at "
        "every 0/30 minutes of the hours"
    )
    scheduler.start()


if __name__ == "__main__":
    bootstrap()
