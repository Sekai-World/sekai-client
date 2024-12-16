import logging
import shutil
import sys
import ujson as json
import requests
import re
import traceback

from os import path, getenv
from time import sleep
from pytz import timezone
from git.repo import Repo
from git.util import Actor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from utils.crypto import decrypt_msgpack
from utils.jsonrpc_client import JSONRPCClient
from utils.constants import remote_git_url_base, local_git_folder_names, strapi_base_url, strapi_token, update_options, pjsk_region, nuverse_master_data_base_url
from utils.git import check_git_folder
from utils.array_to_dict import structures, convert_array_to_dict

LOGLEVEL = getenv('LOGLEVEL', 'INFO').upper()
logging.basicConfig(level=LOGLEVEL, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

jsonrpc_client = JSONRPCClient(
    f'http://localhost:{getenv("JSONRPC_PORT", "3939")}/')

version_info = None
is_in_maintenance = False

masterdb_diff_folder_path = path.join(path.dirname(__file__),
                                      local_git_folder_names["masterDBDiff"])
masterdb_diff_repo: Repo | None = None
i18n_diff_folder_path = path.join(path.dirname(__file__),
                                  local_git_folder_names["i18n"])
i18n_diff_repo: Repo | None = None


def day_change_func():
    logger.debug(
        f'[bootstrap] Pull {local_git_folder_names["masterDBDiff"]} repo remote changes before making any local changes'
    )
    masterdb_diff_repo.remote().pull()

    refresh_version()
    if not is_in_maintenance and update_options["userInfo"]:
        save_info_from_suite_user()


def try_update_func():
    logger.info("Check update triggered by cron job")

    ver_res = None
    try:
        ver_res = jsonrpc_client.request("check_versions", [version_info])
    except:
        bootstrap()
        ver_res = jsonrpc_client.request("check_versions", [version_info])

    global is_in_maintenance
    if ver_res["maintenance"]:
        logger.warning("PJSK server is in maintenance, skipping...")
        is_in_maintenance = True
        return

    is_in_maintenance = False
    logger.debug(
        f'[bootstrap] Pull {local_git_folder_names["masterDBDiff"]} repo remote changes before making any local changes'
    )
    masterdb_diff_repo.remote().pull()
    if update_options["i18n"]:
        logger.debug(
            f'[bootstrap] Pull {local_git_folder_names["i18n"]} repo remote changes before making any local changes'
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


scheduler = BlockingScheduler(timezone=timezone('Asia/Tokyo'))
day_change_cron_trigger = CronTrigger(hour='4', minute='0', second='0')
day_change_job = scheduler.add_job(day_change_func,
                                   day_change_cron_trigger,
                                   name="day_change_job")
try_update_cron_trigger = CronTrigger(minute='0,30', second='0')
try_update_job = scheduler.add_job(try_update_func,
                                   try_update_cron_trigger,
                                   name="try_update_job")


def update_i18n_files(key: str, data: list):
    if key == "cards":
        with open(path.join(i18n_diff_folder_path, "ja", "card_prefix.json"),
                  'w') as f:
            json.dump({elem["id"]: elem["prefix"]
                       for elem in data},
                      f,
                      ensure_ascii=False,
                      indent=2)
        with open(
                path.join(i18n_diff_folder_path, "ja", "card_skill_name.json"),
                'w') as f:
            json.dump({elem["id"]: elem["cardSkillName"]
                       for elem in data},
                      f,
                      ensure_ascii=False,
                      indent=2)
        with open(
                path.join(i18n_diff_folder_path, "ja",
                          "card_gacha_phrase.json"), 'w') as f:
            json.dump(
                {
                    elem["id"]: elem["gachaPhrase"]
                    for elem in data if elem["gachaPhrase"] != "-"
                },
                f,
                ensure_ascii=False,
                indent=2)

        if strapi_base_url and strapi_token:
            requests.post(
                f'{strapi_base_url}/cards/fromDB?token={strapi_token}',
                json=[elem["id"] for elem in data if elem["id"] > 500])

    elif key == "cardEpisodes":
        with open(
                path.join(i18n_diff_folder_path, "ja",
                          "card_episode_title.json"), 'w') as f:
            json.dump({elem["title"]: elem["title"]
                       for elem in data},
                      f,
                      ensure_ascii=False,
                      indent=2)

    elif key == "musics":
        with open(path.join(i18n_diff_folder_path, "ja", "music_titles.json"),
                  'w') as f:
            json.dump({elem["id"]: elem["title"]
                       for elem in data},
                      f,
                      ensure_ascii=False,
                      indent=2)

        if strapi_base_url and strapi_token:
            requests.post(
                f'{strapi_base_url}/musics/fromDB?token={strapi_token}',
                json=[elem["id"] for elem in data if elem["id"] > 290])

    elif key == "musicVocals":
        with open(path.join(i18n_diff_folder_path, "ja", "music_vocal.json"),
                  'w') as f:
            json.dump(
                {elem["musicVocalType"]: elem["caption"]
                 for elem in data},
                f,
                ensure_ascii=False,
                indent=2)

    elif key == "stamps":
        with open(path.join(i18n_diff_folder_path, "ja", "stamp_name.json"),
                  'w') as f:
            json.dump(
                {
                    elem["id"]: re.sub(r"^.*:", "",
                                       re.sub(r"\[.*\]", "", elem["name"]))
                    for elem in data
                },
                f,
                ensure_ascii=False,
                indent=2)

    elif key == "gachas":
        with open(path.join(i18n_diff_folder_path, "ja", "gacha_name.json"),
                  'w') as f:
            json.dump({elem["id"]: elem["name"]
                       for elem in data},
                      f,
                      ensure_ascii=False,
                      indent=2)

    elif key == "events":
        with open(path.join(i18n_diff_folder_path, "ja", "event_name.json"),
                  'w') as f:
            json.dump({elem["id"]: elem["name"]
                       for elem in data},
                      f,
                      ensure_ascii=False,
                      indent=2)

        if strapi_base_url and strapi_token:
            requests.post(
                f'{strapi_base_url}/events/fromDB?token={strapi_token}',
                json=[elem["id"] for elem in data if elem["id"] > 70])

    elif key == "eventStories":
        with open(
                path.join(i18n_diff_folder_path, "ja",
                          "event_story_episode_title.json"), 'w') as f:
            json.dump(
                {
                    f'{episode["eventStoryId"]}-{episode["episodeNo"]}':
                    episode["title"]
                    for elem in data for episode in elem["eventStoryEpisodes"]
                },
                f,
                ensure_ascii=False,
                indent=2)

    elif key == "honors":
        with open(path.join(i18n_diff_folder_path, "ja", "honor_name.json"),
                  'w') as f:
            json.dump({elem["name"]: elem["name"]
                       for elem in data},
                      f,
                      ensure_ascii=False,
                      indent=2)

    elif key == "honorGroups":
        with open(
                path.join(i18n_diff_folder_path, "ja", "honorGroup_name.json"),
                'w') as f:
            json.dump({elem["id"]: elem["name"]
                       for elem in data},
                      f,
                      ensure_ascii=False,
                      indent=2)

    elif key == "virtualLives":
        with open(
                path.join(i18n_diff_folder_path, "ja",
                          "virtualLive_name.json"), 'w') as f:
            json.dump({elem["id"]: elem["name"]
                       for elem in data},
                      f,
                      ensure_ascii=False,
                      indent=2)

        if strapi_base_url and strapi_token:
            requests.post(
                f'{strapi_base_url}/virtual-lives/fromDB?token={strapi_token}',
                json=[elem["id"] for elem in data if elem["id"] > 180])

    elif key == "beginnerMissions":
        with open(
                path.join(i18n_diff_folder_path, "ja",
                          "beginner_mission.json"), 'w') as f:
            json.dump({elem["id"]: elem["sentence"]
                       for elem in data},
                      f,
                      ensure_ascii=False,
                      indent=2)

    elif key == "honorMissions":
        with open(path.join(i18n_diff_folder_path, "ja", "honor_mission.json"),
                  'w') as f:
            json.dump({elem["id"]: elem["sentence"]
                       for elem in data},
                      f,
                      ensure_ascii=False,
                      indent=2)

    elif key == "normalMissions":
        with open(
                path.join(i18n_diff_folder_path, "ja", "normal_mission.json"),
                'w') as f:
            json.dump({elem["id"]: elem["sentence"]
                       for elem in data},
                      f,
                      ensure_ascii=False,
                      indent=2)

    elif key == "cheerfulCarnivalSummaries":
        with open(
                path.join(i18n_diff_folder_path, "ja",
                          "cheerful_carnival_themes.json"), 'w') as f:
            json.dump({elem["id"]: elem["theme"]
                       for elem in data},
                      f,
                      ensure_ascii=False,
                      indent=2)

    elif key == "cheerfulCarnivalTeams":
        with open(
                path.join(i18n_diff_folder_path, "ja",
                          "cheerful_carnival_teams.json"), 'w') as f:
            json.dump({elem["id"]: elem["teamName"]
                       for elem in data},
                      f,
                      ensure_ascii=False,
                      indent=2)


def get_splitted_master_data():
    global pjsk_region
    global version_info

    master_split_paths: list[str] = jsonrpc_client.request(
        "master_split_paths")

    # download every split
    master_data_raw = []
    for split_path in master_split_paths:
        logger.debug(f'[get_splitted_master_data] fetch split {split_path}')
        master_data_raw.append(
            jsonrpc_client.request("call_pjsk_api", [f'/{split_path}']))

    master_data: dict[str, list] = {}
    for idx, split_data_raw in enumerate(master_data_raw):
        logger.debug(
            f'[get_splitted_master_data] merging split {master_split_paths[idx]}'
        )
        master_data |= split_data_raw

    return master_data


def download_nuverse_master_data(cdn_version: int):
    base_url = nuverse_master_data_base_url[pjsk_region]
    
    r = requests.get(f'{base_url}/master-data-{cdn_version}.info')
    r.raise_for_status()
    
    return decrypt_msgpack(r.content)


def refresh_version():
    logger.debug("[refresh_version] called")

    if update_options["i18n"]:
        if not i18n_diff_repo:
            raise RuntimeError(
                f'{local_git_folder_names["i18n"]} repository must be existed to refresh version info.'
            )

        i18n_diff_repo.remote().pull()
        logger.debug(
            f'[refresh_version] pulled repository {local_git_folder_names["i18n"]}'
        )

    logger.debug('[refresh_version] write version info to json file')
    if pjsk_region == "jp":
        if not jsonrpc_client.request("is_login"):
            jsonrpc_client.request("login")
        else:
            logger.debug(
                '[refresh_version] relogin to refresh full version info and splitted master data list'
            )
            jsonrpc_client.request("relogin")
    global version_info
    version_info = jsonrpc_client.request("version_info")
    with open(path.join(masterdb_diff_folder_path, "versions.json"), 'w') as f:
        json.dump(version_info, f, indent=2)
        f.truncate()

    logger.debug('[refresh_version] fetching master db')
    if pjsk_region in ["jp"]:
        # ask apiclient to relogin and refresh the splitted master data list
        # logger.debug('[refresh_version] relogin to refresh splitted master data list')
        # jsonrpc_client.request("relogin")
        master_data: dict[str, list] = get_splitted_master_data()
    elif pjsk_region in ["en"]:
        master_data: dict[str,
                          list] = jsonrpc_client.request("fetch_master_data")
    else:
        master_data: dict[str,
                          list] = download_nuverse_master_data(version_info["cdnVersion"])
    logger.debug(
        '[refresh_version] write master db to separate json files by keys')
    for key, value in master_data.items():
        file_path = path.join(masterdb_diff_folder_path, f'{key}.json')
        file_data = value
        logger.debug(f'[refresh_version] start writing master db {key}.json')

        try:
            id_key = None
            if any(x in key for x in [
                    "event", "gacha", "virtual", "cheerfulCarnival", "tips",
                    "music", "card"
            ]) and (pjsk_region not in ["cn", "tw", "kr"] and key != "eventCards"):
                id_key = "id"
            elif (pjsk_region in ["cn", "tw", "kr"] and key == "eventCards"):
                id_key = "cardId"
            if pjsk_region in ["cn", "tw", "kr"] and key in structures:
                file_data = [convert_array_to_dict(file_datum, structures[key]) for file_datum in file_data]
            if id_key is not None and path.exists(file_path):
                with open(file_path, 'r') as f:
                    try:
                        old_data = json.load(f)
                    except json.JSONDecodeError:
                        old_data = []  # 或者根据需要处理异常

                if isinstance(old_data,
                              list) and len(old_data) > 0 and old_data[0].get(
                                  id_key) is not None:
                    # save ids in a set for faster lookup
                    value_ids = {item[id_key] for item in value}
                    # use list comprehension to filter out old data with same ids
                    file_data = [
                        x for x in old_data if x[id_key] not in value_ids
                    ] + value
                    # sort file_name accroding to id_key again
                    file_data.sort(key=lambda x: x[id_key])
            with open(file_path, 'w') as f:
                json.dump(file_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            # logger.debug(json.dumps(value))
            raise e

        logger.debug(f'[refresh_version] wrote master db {key}.json')

        if update_options["i18n"]:
            logger.debug(
                f'[refresh_version] write i18n json file for {key}.json if necessary'
            )
            update_i18n_files(key, file_data)

    logger.debug("[refresh_version] finished")


def save_info_from_suite_user():
    suite_user = jsonrpc_client.request("login_user_info")

    logger.debug("[save_info_from_suite_user] write user home banners")
    with open(path.join(masterdb_diff_folder_path, "userHomeBanners.json"),
              'w') as f:
        json.dump(suite_user["userHomeBanners"],
                  f,
                  ensure_ascii=False,
                  indent=2)

    if pjsk_region == "en":
        refresh_information()
    elif suite_user.get("userInformations", None):
        logger.debug("[save_info_from_suite_user] write user informations")
        with open(
                path.join(masterdb_diff_folder_path, "userInformations.json"),
                'w') as f:
            json.dump(suite_user["userInformations"],
                      f,
                      ensure_ascii=False,
                      indent=2)

    logger.debug("[save_info_from_suite_user] finished")
    return suite_user


def refresh_information():
    logger.debug("[refresh_information] get informations")
    res = jsonrpc_client.request("fetch_information")

    logger.debug("[refresh_information] write user informations")
    with open(path.join(masterdb_diff_folder_path, "userInformations.json"),
              'w') as f:
        json.dump(res["informations"], f, ensure_ascii=False, indent=2)


def commit_master_diff():
    global masterdb_diff_repo
    data_ver = version_info["dataVersion"]
    asset_ver = version_info["assetVersion"]

    if masterdb_diff_repo and masterdb_diff_repo.is_dirty(
            untracked_files=True):
        try:
            logger.debug(
                f'[commit_master_diff] add files to staged in {local_git_folder_names["masterDBDiff"]}'
            )

            curr_index = masterdb_diff_repo.index
            curr_index.add('**')

            logger.debug(
                f'[commit_master_diff] commit staged changes in {local_git_folder_names["masterDBDiff"]}'
            )
            curr_index.commit(
                f'master version {data_ver} asset version {asset_ver}',
                author=Actor("master-db-diff-bot", "anonymous@example.com"))

            logger.debug(
                f'[commit_master_diff] push commit to origin in {local_git_folder_names["masterDBDiff"]}'
            )
            masterdb_diff_repo.remote().push().raise_if_error()
        except:
            # reset to last commit
            # masterdb_diff_repo.head.reset(commit="HEAD~1",
            #                               index=True,
            #                               working_tree=True)
            # delete current repo folder and clone again
            shutil.rmtree(masterdb_diff_folder_path)
            masterdb_diff_repo = check_git_folder(masterdb_diff_folder_path,
                                                  remote_git_url_base)
            return False

        return True

    return False


def commit_i18n_files():
    global i18n_diff_repo
    data_ver = version_info["dataVersion"]

    if i18n_diff_repo and i18n_diff_repo.is_dirty(untracked_files=True):
        try:
            logger.debug(
                f'[commit_i18n_files] add files to staged in {local_git_folder_names["i18n"]}'
            )

            curr_index = i18n_diff_repo.index
            curr_index.add('**')

            logger.debug(
                f'[commit_i18n_files] commit staged changes in {local_git_folder_names["i18n"]}'
            )
            curr_index.commit(f'i18n update for master version {data_ver}',
                              author=Actor("i18n-diff-bot",
                                           "anonymous@example.com"))

            logger.debug(
                f'[commit_i18n_files] push commit to origin in {local_git_folder_names["i18n"]}'
            )
            i18n_diff_repo.remote().push().raise_if_error()
        except:
            # reset to last commit
            # i18n_diff_repo.head.reset(commit="HEAD~1",
            #                           index=True,
            #                           working_tree=True)
            # delete current repo folder and clone again
            shutil.rmtree(i18n_diff_folder_path)
            i18n_diff_repo = check_git_folder(i18n_diff_folder_path,
                                              remote_git_url_base)
            return False

        return True

    return False


def bootstrap():
    if not jsonrpc_client.request("is_init") and not jsonrpc_client.request(
            "init", [pjsk_region]):
        sys.exit(1)
    logger.info("[bootstrap] PJSK client inited")

    global masterdb_diff_repo
    masterdb_diff_repo = check_git_folder(masterdb_diff_folder_path,
                                          remote_git_url_base)
    global i18n_diff_repo
    if update_options["i18n"]:
        i18n_diff_repo = check_git_folder(i18n_diff_folder_path,
                                          remote_git_url_base)
    logger.info("[bootstrap] Local git folders checked")

    try:
        check_version_res = jsonrpc_client.request("check_versions")
        if check_version_res["maintenance"]:
            logger.warning(
                "[bootstrap] Server in maintenance, retry after 10 minutes")
            sleep(10 * 60)
            bootstrap()
            return

        logger.debug(
            f'[bootstrap] Pull {local_git_folder_names["masterDBDiff"]} repo remote changes before making any local changes'
        )
        masterdb_diff_repo.remote().pull()
        if update_options["userInfo"]:
            jsonrpc_client.request("login")
            save_info_from_suite_user()

        global version_info
        version_info = jsonrpc_client.request("version_info")

        if update_options["master"]:
            refresh_version()
    except Exception as e:
        logging.error(traceback.format_exc())
        logger.error(
            "[bootstrap] Failed to bootstrap, possible reasons: connection error or account info expired (for tw and kr servers). Retry after 10 minutes."
        )
        sleep(10 * 60)
        bootstrap()
        return
    logger.info("[bootstrap] Fetched current available version info")

    if commit_master_diff():
        logger.info("Updated and committed master data")
        if update_options["i18n"]:
            commit_i18n_files()
            logger.info("Updated and committed i18n data")

    logger.info(
        "[bootstrap] Finished, will look for new PJSK game data version at every 0/30 minutes of the hours"
    )
    scheduler.start()


if __name__ == "__main__":
    bootstrap()
