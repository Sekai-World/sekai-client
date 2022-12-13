import logging
import requests

from os import getenv
from uuid import uuid4
from copy import deepcopy
from time import sleep

from utils.constants import initial_api_headers, base_pjsk_api_url, pjsk_cookie_post_url, pjsk_region
from utils.crypto import encrypt, decrypt, encrypt_msgpack, decrypt_msgpack

LOGLEVEL = getenv('LOGLEVEL', 'INFO').upper()
logging.basicConfig(level=LOGLEVEL, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)


class APIClient:

    def __init__(self, region=pjsk_region, logger=logger) -> None:
        self.logger = logger
        self.region = pjsk_region
        self.headers = deepcopy(initial_api_headers[region])

        self.rate_limited = False

    @property
    def account_info(self) -> dict:
        return self._account_info or {}

    @account_info.setter
    def account_info(self, data: dict):
        self._account_info = data

    @property
    def version_info(self) -> dict:
        return self._version_info

    @version_info.setter
    def version_info(self, data: dict):
        self._version_info = data

    @property
    def user_info(self) -> dict:
        return self._user_info

    @user_info.setter
    def user_info(self, data: dict):
        self._user_info = data

    @property
    def region(self) -> str:
        return self._region

    @region.setter
    def region(self, data: str):
        self._region = data

        self.headers = deepcopy(initial_api_headers[data])

    def init_cookie(self):
        r = requests.post(pjsk_cookie_post_url[self.region])
        self.headers["cookie"] = r.headers["set-cookie"]

    def call_pjsk_api(self,
                      endpoint: str,
                      method="get",
                      body: str | dict = "",
                      retry_after_error=True) -> bytes | dict:
        if self.rate_limited:
            raise RuntimeError("Cooling down for rate limit...")

        r = None
        data = None
        res_data = None
        if method.lower() in ("post", "put", "patch"):
            if type(body) == str:
                data = encrypt(body)
            elif type(body) == dict:
                data = encrypt_msgpack(body)
            else:
                raise ValueError("body type should be str or dict.")

        try:
            self.headers["x-request-id"] = str(uuid4())
            self.headers["content-type"] = "application/octet-stream"
            self.logger.debug(
                f"request url={base_pjsk_api_url[self.region]}{endpoint}, method={method.lower()}, headers={self.headers.items()}"
            )
            r = requests.request(
                method=method.lower(),
                url=f"{base_pjsk_api_url[self.region]}{endpoint}",
                headers=self.headers,
                data=data)
            self.logger.debug(
                f"response url={base_pjsk_api_url[self.region]}{endpoint}, method={method.lower()}, headers={self.headers.items()}, status={r.status_code}"
            )
            if r.headers.get("x-session-token", None):
                self.headers["x-session-token"] = r.headers["x-session-token"]
            if r.headers["content-type"] == "application/octet-stream" and len(
                    r.content):
                try:
                    res_data = decrypt_msgpack(r.content)
                except:
                    res_data = decrypt(r.content)

            r.raise_for_status()
        except requests.HTTPError as err:
            self.logger.error(
                f"Request PJSK api error, endpoint={endpoint}, method={method}, body={body}, status={r.status_code}"
            )

            if r.status_code == 403 and r.headers["content-type"] == "text/xml":
                self.logger.warn(
                    f"{self.region} server cookie expired, refreshing...")
                self.init_cookie()
                if retry_after_error:
                    return self.call_pjsk_api(endpoint, method, body)
            elif r.status_code == 429:
                self.logger.warn(
                    f"{self.region} server hits rate limit, sleep for 60s")
                self.rate_limited = True
                sleep(60.0)
                self.rate_limited = False
                if retry_after_error:
                    return self.call_pjsk_api(endpoint, method, body)
            elif r.status_code == 426:
                self.logger.warn(
                    f"{self.region} server should update version info")
                self.check_versions()
                self.login()
                if retry_after_error:
                    return self.call_pjsk_api(endpoint, method, body)
            elif r.status_code == 403:
                if res_data["errorCode"] == "session_error":
                    self.login()
                    if retry_after_error:
                        return self.call_pjsk_api(endpoint, method, body)

            raise RuntimeError(r, res_data)

        return res_data

    def check_versions(self, input_ver_info=None):
        res = {"maintenance": False, "new_version": False}

        if self.region in ("tw", "kr"):
            return res

        system_data = self.fetch_system_data()
        if self.region in ("jp"):
            res["maintenance"] = system_data[
                "maintenanceStatus"] == "maintenance_in"

        all_ver_infos = system_data["appVersions"]
        curr_app_ver = self.headers["x-app-version"]

        curr_ver_info = None
        curr_ver_infos = list(
            filter(
                lambda ver_info: ver_info["appVersion"] == curr_app_ver and
                ver_info["appVersionStatus"] == "available",
                all_ver_infos))[:1]

        if not len(curr_ver_infos):
            curr_ver_infos = list(
                filter(
                    lambda ver_info: ver_info["appVersionStatus"] ==
                    "available", all_ver_infos))[:1]

            if not len(curr_ver_infos):
                curr_ver_infos = list(
                    filter(
                        lambda ver_info: ver_info["appVersionStatus"] ==
                        "maintenance", all_ver_infos))[:1]
                if not len(curr_ver_infos):
                    raise RuntimeError(
                        f"{self.region} server failed to fetch valid version info"
                    )
                else:
                    curr_ver_info = curr_ver_infos[0]
                    res["maintenance"] = True
            else:
                curr_ver_info = curr_ver_infos[0]
                res["new_version"] = True
        else:
            curr_ver_info = curr_ver_infos[0]
            res["new_version"] = self.headers[
                "x-data-version"] != curr_ver_info[
                    "dataVersion"] or self.headers[
                        "x-asset-version"] != curr_ver_info[
                            "assetVersion"] or self.headers[
                                "x-app-version"] != curr_ver_info["appVersion"]

        if res["new_version"]:
            self.headers["x-data-version"] = curr_ver_info["dataVersion"]
            self.headers["x-asset-version"] = curr_ver_info["assetVersion"]
            self.headers["x-app-version"] = curr_ver_info["appVersion"]

            self.logger.info(
                f'{self.region} server fetched a new available version: appVersion={curr_ver_info["appVersion"]}, dataVersion={curr_ver_info["dataVersion"]}, assetVersion={curr_ver_info["assetVersion"]}'
            )

        self.version_info = curr_ver_info

        if input_ver_info:
            res["maintenance"] = self.version_info[
                "appVersionStatus"] == "maintenance"
            res["new_version"] = input_ver_info["dataVersion"] != curr_ver_info[
                "dataVersion"] or input_ver_info[
                    "assetVersion"] != curr_ver_info[
                        "assetVersion"] or input_ver_info[
                            "appVersion"] != curr_ver_info["appVersion"]

        return res

    def register_new_account(self) -> dict:
        return self.call_pjsk_api(
            "/user", "post", {
                "platform": "iOS",
                "deviceModel": "iPad6,11",
                "operatingSystem": "iOS 13.5",
            })

    def login(self) -> dict:
        self.logger.info("simulate login process")

        self.logger.debug("do auth")
        self.headers.pop('x-session-token', None)
        if self.region in ("jp", "en"):
            user_id = self.account_info["userId"]
            credential = self.account_info["credential"]

            auth_data = self.call_pjsk_api(
                f"/user/{user_id}/auth?refreshUpdatedResources=False", "put",
                {"credential": credential})
        elif self.region in ("tw", "kr"):
            access_token = self.account_info["loginInfo"]["accessToken"]

            auth_data = self.call_pjsk_api("/user/auth", "post", {
                "userID": 0,
                "accessToken": access_token
            })

        session_token = auth_data["sessionToken"]
        app_ver = auth_data["appVersion"]
        data_ver = auth_data["dataVersion"]
        asset_ver = auth_data["assetVersion"]
        # asset_hash = auth_data["assetHash"]
        # app_hash = auth_data["appHash"]
        multi_play_ver = auth_data["multiPlayVersion"]

        self.headers["x-session-token"] = session_token
        self.headers["x-app-version"] = app_ver
        self.headers["x-data-version"] = data_ver
        self.headers["x-asset-version"] = asset_ver

        self.logger.info(
            f'login appVersion={app_ver} dataVersion={data_ver} assetVersion={asset_ver}'
        )

        if self.region in ("tw", "kr"):
            self.version_info = {
                "systemProfile": "production",
                "appVersion": app_ver,
                "multiPlayVersion": multi_play_ver,
                "dataVersion": data_ver,
                "assetVersion": asset_ver,
                "appHash": "",
                "assetHash": "",
                'appVersionStatus': "available",
            }

        self.logger.debug("get suite user")
        user_id = self.account_info["userId"]
        user_info = self.fetch_suite_user()

        self.logger.debug("check and skip tutorial")
        user_tutorial = user_info["userTutorial"]
        if user_tutorial["tutorialStatus"] == "start":
            self.logger.warn("tutorial is at start, set username first")
            self.call_pjsk_api(f'/user/{user_id}/tutorial', 'patch',
                               {"tutorialStatus": "opening_1"})
            self.call_pjsk_api(f'/user/{user_id}', 'patch', {
                "userGamedata": {
                    "name": "\u30bb\u30ab\u30a4\u306e\u4f4f\u4eba"
                }
            })
            user_tutorial["tutorialStatus"] = "opening_1"
        if user_tutorial["tutorialStatus"] != "end":
            self.logger.debug("roll tutorial")
            steps = [
                "opening_1",
                "gameplay",
                "opening_2",
                "unit_select",
                "idol_opening",
                "summary",
                "end",
            ]
            for status in steps[steps.index(user_tutorial["tutorialStatus"]) +
                                1:]:
                self.call_pjsk_api(f'/user/{user_id}/tutorial', 'patch',
                                   {"tutorialStatus": status})

        self.logger.debug("refresh home login_bonus")
        self.call_pjsk_api(f'/user/{user_id}/home/refresh', 'put',
                           {"refreshableTypes": ["login_bonus"]})

        self.user_info = user_info
        return user_info

    def fetch_suite_user(self, update_user_info: bool = False) -> dict:
        user_id = self.account_info["userId"]
        res = self.call_pjsk_api(f'/suite/user/{user_id}')

        if update_user_info:
            self.user_info = res

        return res

    def fetch_user_profile(self, user_id: str) -> dict:
        return self.call_pjsk_api(f'/user/{user_id}/profile')

    def fetch_user_event_ranking(self, target_user_id: str,
                                 event_id: str) -> dict:
        user_id = self.account_info["userId"]
        return self.call_pjsk_api(
            f'/user/{user_id}/event/{event_id}/ranking?targetUserId={target_user_id}'
        )

    def fetch_information(self):
        return self.call_pjsk_api('/information')

    def fetch_system_data(self):
        return self.call_pjsk_api('/system')
