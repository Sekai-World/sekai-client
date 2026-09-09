import logging
from os import environ
from typing import Any

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag

from response_models import validate_version_info
from utils.constants import EN_FALLBACK_VERSION_INFO, JP_FALLBACK_VERSION_INFO
from utils.deadline import bounded_timeout

logger = logging.getLogger(__name__)

EN_CURRENT_VERSION_URL = (
    "https://raw.githubusercontent.com/Team-Haruki/haruki-sekai-en-master/"
    "refs/heads/main/versions/current_version.json"
)
JP_CURRENT_VERSION_URL = (
    "https://raw.githubusercontent.com/Team-Haruki/haruki-sekai-master/"
    "refs/heads/main/versions/current_version.json"
)


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


def _get_authoritative_version_info(
    *, url: str, fallback: dict[str, Any], region: str
) -> dict[str, Any]:
    try:
        logger.debug("get_app_ver_and_hash_%s url=%s", region, url)
        response = requests.get(url, timeout=bounded_timeout(10))
        logger.debug(
            "get_app_ver_and_hash_%s status=%s", region, response.status_code
        )
        response.raise_for_status()
        return validate_version_info(response.json(), require_app_hash=True)
    except Exception as error:  # noqa: BLE001 - all upstream failures use fallback
        logger.warning(
            "get_app_ver_and_hash_%s authoritative fetch failed; "
            "using local fallback error_type=%s",
            region,
            type(error).__name__,
        )
        return dict(fallback)


def get_app_ver_and_hash_jp() -> dict[str, Any]:
    url = environ.get("JP_CURRENT_VERSION_URL") or JP_CURRENT_VERSION_URL
    return _get_authoritative_version_info(
        url=url, fallback=JP_FALLBACK_VERSION_INFO, region="jp"
    )


def get_app_ver_and_hash_en() -> dict[str, Any]:
    url = environ.get("EN_CURRENT_VERSION_URL") or EN_CURRENT_VERSION_URL
    return _get_authoritative_version_info(
        url=url, fallback=EN_FALLBACK_VERSION_INFO, region="en"
    )
