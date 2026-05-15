from os import getenv

### BEGIN api_client constansts ###
initial_api_headers = {
    "jp": {
        "x-devicemodel": "iPad13,16",
        "x-app-hash": getenv("APP_HASH", "a4afe2ff-8be2-4e6b-ebbc-75d32802351e"),
        "x-app-version": getenv("APP_VER", "1.10.0"),
        "x-asset-version": getenv("ASSET_VER", "1.7.3.1"),
        "x-data-version": getenv("DATA_VER", "1.7.3.1"),
        "x-if": "0b8db587-9eae-4569-8cd7-8335c8ceba9a",
        "x-install-id": "e5b245d2-e157-4664-b1b4-af881730d9c7",
        "x-kc": "a26272b1-1d9c-4c05-956c-7538b64eb451",
        "x-operatingsystem": "iPadOS 17.4",
        "x-platform": "iOS",
        "x-unity-version": "2020.3.32f1",
        "x-ai": "",
        "x-ma": "",
        "x-ga": "",
        "user-agent": "ProductName/169 CFNetwork/1494.0.7 Darwin/23.4.0",
        "accept": "application/octet-stream",
        "accept-encoding": "gzip, deflate, br",
        "accept-language": "zh-cn",
        "content-type": "application/octet-stream",
    },
    "tw": {
        "x-unity-version": "2019.4.19f1c1",
        "x-devicemodel": "iPad13,16",
        "accept": "application/octet-stream",
        "x-install-id": "9ed9f6fb-4159-4844-842c-26bec8e7289b",
        "accept-language": "zh-cn",
        "accept-encoding": "gzip, deflate, br",
        "content-type": "application/octet-stream",
        # 'X-Request-Id': '8c3aa6b3-a505-4974-afca-b2e911c85434',
        "user-agent": (
            "%E4%B8%96%E7%95%8C%E8%A8%88%E7%95%AB/1258 CFNetwork/1494.0.7 Darwin/23.4.0"
        ),
        "device_id": getenv("SEKAI_TW_DEVICE_ID", "7013473306716718998"),
        "x-app-version": getenv("APP_VER", "3.6.0"),
        "x-platform": "iOS",
        "x-operatingSystem": "iPadOS 17.4",
    },
    "en": {
        "x-devicemodel": "iPad13,16",
        "x-app-hash": getenv("APP_HASH", "b65f9f33-8104-468f-9fa9-3c17379b4569"),
        "x-app-version": getenv("APP_VER", "3.6.0"),
        "x-asset-version": getenv("ASSET_VER", "3.6.1.0"),
        "x-data-version": getenv("DATA_VER", "3.6.1.0"),
        "x-if": "AB5D5811-551F-4590-A671-093BEB2381E2",
        "x-install-id": "5715782a-f05a-4fda-a853-35b1184e4912",
        "x-kc": "0bdf6de1-0a00-43e3-a632-c1af30b9484a",
        "x-operatingsystem": "iPadOS 17.4",
        "x-platform": "iOS",
        "x-unity-version": "2020.3.32f1",
        "x-ai": "",
        "x-ma": "",
        "x-ga": "",
        "user-agent": "ProductName/84 CFNetwork/1494.0.7 Darwin/23.4.0",
        "content-type": "application/octet-stream",
        "accept": "application/octet-stream",
        "accept-encoding": "gzip, deflate, br",
        "accept-language": "zh-cn",
        # "content-type": 'application/octet-stream'
    },
    "kr": {
        "x-unity-version": "2019.4.19f1c1",
        "x-devicemodel": "iPad13,16",
        "content-type": "application/octet-stream",
        "accept": "application/octet-stream",
        "x-asset-version": "",
        "x-install-id": "3e9d5364-1c68-4f53-aae8-2824e08e993f",
        "x-data-version": "",
        "user-agent": (
            "%ED%94%84%EB%A1%9C%EC%84%B8%EC%B9%B4/5011 CFNetwork/1494.0.7 Darwin/23.4.0"
        ),
        "x-app-version": getenv("APP_VER", "3.6.0"),
        # "X-Session-Token": "",
        "x-platform": "iOS",
        "x-operatingsystem": "iPadOS 17.4",
        "device_id": getenv("SEKAI_KR_DEVICE_ID", "7013473306716718593"),
    },
}

base_pjsk_api_url = {
    "jp": "https://production-game-api.sekai.colorfulpalette.org/api",
    "tw": "https://mk-zian-obt-cdn.bytedgame.com/api",
    "en": "https://n-production-game-api.sekai-en.com/api",
    "kr": "https://mkkorea-obt-prod01-cdn.bytedgame.com/api",
    "cn": "https://mkcn-prod-public-60001-1.dailygn.com/api",
}

pjsk_region = getenv("SEKAI_REGION", "jp")

pjsk_cookie_post_url = {"jp": "https://issue.sekai.colorfulpalette.org/api/signature"}
### END api_client constansts ###

### START check_update & event_tracker constants ###
update_options = {
    "master": getenv("ENABLE_SEKAI_UPDATE_MASTER", "false") in ("true", "True", "1"),
    "userInfo": getenv("ENABLE_SEKAI_UPDATE_USER_INFO", "false")
    in ("true", "True", "1"),
    "i18n": getenv("ENABLE_SEKAI_UPDATE_I18N", "false") in ("true", "True", "1"),
}

check_update_simple_mode = getenv("CHECK_UPDATE_SIMPLE_MODE", "false") in (
    "true",
    "True",
    "1",
)
check_update_versions_url = getenv("CHECK_UPDATE_VERSIONS_URL", "")

local_git_folder_names = {
    "i18n": getenv("GIT_FOLDER_SEKAI_I18N", "sekai-i18n"),
    "masterDBDiff": getenv("GIT_FOLDER_SEKAI_MASTER_DB_DIFF", "sekai-master-db-diff"),
}

remote_git_url_base = getenv("REMOTE_GIT_BASE_URL", "https://github.com/Sekai-World")

strapi_base_url = getenv("STRAPI_BASE_URL", "http://localhost:3000")
strapi_token = getenv("STRAPI_TOKEN", "")

sekai_api_key = getenv("SEKAI_API_KEY", "")

app_id_regions = {"jp": "9038", "tw": "18298", "en": "18337", "kr": "20082"}

nuverse_master_data_base_url = {
    "tw": "https://lf16-mkovscdn-sg.bytedgame.com/obj/sf-game-alisg/gdl_app_5245/MasterData/60001",
    "kr": "https://lf19-mkkr.bytedgame.com/obj/sf-game-alisg/gdl_app_292248/MasterData/60001",
    "cn": "https://lf9-mkcncdn-tos.dailygn.com/obj/sf-game-lf/gdl_app_5236/MasterData/60001",
}
### END check_update & event_tracker constants ###
