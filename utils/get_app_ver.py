import logging
from os import environ
from typing import Any

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag

from utils.deadline import bounded_timeout

logger = logging.getLogger(__name__)


def get_app_ver_qooapp(appid: str) -> str:
    url = f"https://apps.qoo-app.com/en/app/{appid}"
    logger.debug("get_app_ver_qooapp url=%s", url)

    r = requests.get(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:108.0) "
                "Gecko/20100101 Firefox/108.0"
            )
        },
        timeout=bounded_timeout(10),
    )
    logger.debug("get_app_ver_qooapp status=%s", r.status_code)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "lxml")
    app_info_tree = soup.find(class_="app-info android")
    if not isinstance(app_info_tree, Tag):
        raise RuntimeError("Could not find QooApp version details")

    rows = app_info_tree.find_all(class_="row")
    if len(rows) < 2:
        raise RuntimeError("Could not find QooApp version row")
    version_element = rows[1].find("var")
    if not isinstance(version_element, Tag):
        raise RuntimeError("Could not find QooApp version value")
    var_text = version_element.get_text(strip=True)
    environ["APP_VER"] = var_text

    return var_text


def get_app_ver_and_hash_jp() -> dict[str, Any]:
    url = "https://raw.githubusercontent.com/Team-Haruki/haruki-sekai-master/refs/heads/main/versions/current_version.json"
    logger.debug("get_app_ver_and_hash_jp url=%s", url)

    try:
        r = requests.get(url, timeout=bounded_timeout(10))
        logger.debug("get_app_ver_and_hash_jp status=%s", r.status_code)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as err:
        logger.warning("Primary JP version endpoint failed: %s", err)
        # try to get from github repo

        url = "https://sekai-world.github.io/sekai-master-db-diff/versions.json"
        logger.debug("get_app_ver_and_hash_jp fallback url=%s", url)
        r = requests.get(url, timeout=bounded_timeout(10))
        logger.debug("get_app_ver_and_hash_jp fallback status=%s", r.status_code)
        r.raise_for_status()
        data = r.json()

    if not isinstance(data, dict):
        raise RuntimeError("JP version endpoint returned invalid JSON data")
    return data


def get_app_ver_and_hash_en() -> dict[str, Any]:
    # url = 'https://storage.sekai.best/sekai-best-assets/app-en-app-hash.json'
    url = "https://raw.githubusercontent.com/Team-Haruki/haruki-sekai-en-master/refs/heads/main/versions/current_version.json"
    logger.debug("get_app_ver_and_hash_en url=%s", url)

    try:
        r = requests.get(url, timeout=bounded_timeout(10))
        logger.debug("get_app_ver_and_hash_en status=%s", r.status_code)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as err:
        logger.warning("Primary EN version endpoint failed: %s", err)
        # try to get from github repo

        url = "https://sekai-world.github.io/sekai-master-db-en-diff/versions.json"
        logger.debug("get_app_ver_and_hash_en fallback url=%s", url)
        r = requests.get(url, timeout=bounded_timeout(10))
        logger.debug("get_app_ver_and_hash_en fallback status=%s", r.status_code)
        r.raise_for_status()
        data = r.json()

    if not isinstance(data, dict):
        raise RuntimeError("EN version endpoint returned invalid JSON data")
    return data
