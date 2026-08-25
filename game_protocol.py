"""Encrypted Project Sekai protocol transport with no lifecycle dependencies."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

import requests

from config import Config
from utils.constants import base_pjsk_api_url, pjsk_cookie_post_url
from utils.crypto import decrypt, decrypt_msgpack, encrypt, encrypt_msgpack
from utils.deadline import bounded_timeout

type ProtocolResponse = bytes | dict[str, Any] | None


class GameProtocolTransport:
    """Perform encrypted game requests against one region."""

    def __init__(
        self,
        region: str,
        headers: dict[str, Any],
        logger: logging.Logger,
    ) -> None:
        self.region = region
        self.headers = headers
        self.logger = logger

    def init_cookie(self) -> None:
        cookie_url = pjsk_cookie_post_url[self.region]
        self.logger.debug("init_cookie method=POST url=%s", cookie_url)
        try:
            response = requests.post(
                cookie_url,
                timeout=bounded_timeout(Config.REQUEST_TIMEOUT),
                allow_redirects=False,
            )
        except requests.RequestException:
            raise RuntimeError("Cookie initialization request failed") from None

        if 300 <= response.status_code < 400:
            raise RuntimeError(
                f"Cookie initialization redirect refused (HTTP {response.status_code})"
            )
        try:
            response.raise_for_status()
        except requests.RequestException:
            raise RuntimeError(
                f"Cookie initialization failed (HTTP {response.status_code})"
            ) from None

        cookie = response.headers.get("set-cookie") or response.headers.get(
            "Set-Cookie"
        )
        self.logger.debug(
            "init_cookie status=%s set_cookie_present=%s",
            response.status_code,
            cookie is not None,
        )
        if not cookie:
            raise RuntimeError(
                "Cookie initialization failed: response missing Set-Cookie header"
            )
        self.headers["cookie"] = cookie

    @staticmethod
    def encrypt_request_body(method: str, body: str | dict) -> bytes | None:
        if method.lower() not in ("post", "put", "patch"):
            return None
        if isinstance(body, str):
            return encrypt(body.encode())
        if isinstance(body, dict):
            return encrypt_msgpack(body)
        raise ValueError("body type should be str or dict.")

    def send(
        self,
        endpoint: str,
        method: str,
        data: bytes | None,
        request_id: str | None = None,
    ) -> requests.Response:
        self.headers["x-request-id"] = request_id or str(uuid4())
        self.headers["content-type"] = "application/octet-stream"
        req_url = f"{base_pjsk_api_url[self.region]}{endpoint}"
        headers_str = " ".join(f"{k}={v}" for k, v in sorted(self.headers.items()))
        self.logger.debug(
            "request url=%s method=%s headers=%s data_len=%s",
            req_url,
            method.lower(),
            headers_str,
            len(data) if data is not None else None,
        )
        response = requests.request(
            method=method.lower(),
            url=req_url,
            headers=self.headers,
            data=data,
            timeout=bounded_timeout(Config.REQUEST_TIMEOUT),
            allow_redirects=False,
        )
        resp_headers_str = " ".join(
            f"{k}={v}" for k, v in sorted(response.headers.items())
        )
        self.logger.debug(
            "response url=%s method=%s status=%s headers=%s",
            req_url,
            method.lower(),
            response.status_code,
            resp_headers_str,
        )
        if not 300 <= response.status_code < 400 and response.headers.get(
            "x-session-token"
        ):
            self.headers["x-session-token"] = response.headers["x-session-token"]
        return response

    @staticmethod
    def decrypt_response(response: requests.Response) -> ProtocolResponse:
        content_type = response.headers.get("content-type", "")
        if not response.content or "octet-stream" not in content_type:
            return None
        try:
            return decrypt_msgpack(response.content)
        except Exception:
            return decrypt(response.content)
