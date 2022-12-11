import requests

from jsonrpcclient.requests import request_uuid
from jsonrpcclient.responses import parse, Ok


class JSONRPCClient:

    def __init__(self, url="http://localhost:3939/") -> None:
        self.url = url

    def request(self, func_name: str, params: tuple | dict | None = None):
        r = requests.post(self.url, json=request_uuid(func_name, params))
        parsed = parse(r.json())

        if isinstance(parsed, Ok):
            return parsed.result
        else:
            raise RuntimeError(parsed.message)

    @property
    def url(self) -> dict:
        return self._url

    @url.setter
    def url(self, data: str):
        self._url = data