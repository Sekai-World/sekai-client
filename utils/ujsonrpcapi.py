from jsonrpc.backend.flask import JSONRPCAPI, JSONRPCRequest, JSONRPCInvalidRequestException, JSONRPCResponseManager, Response, DatetimeDecimalEncoder
import ujson as json


class UJSONRPCAPI(JSONRPCAPI):

    def jsonrpc(self):
        request_str = self._get_request_str()
        try:
            jsonrpc_request = JSONRPCRequest.from_json(request_str)
        except (TypeError, ValueError, JSONRPCInvalidRequestException):
            response = JSONRPCResponseManager.handle(request_str,
                                                     self.dispatcher)
        else:
            response = JSONRPCResponseManager.handle_request(
                jsonrpc_request, self.dispatcher)

        if response:
            response.serialize = self._serialize
            response = response.json

        return Response(response, content_type="application/json")

    @staticmethod
    def _serialize(s):
        return json.dumps(s)


api = UJSONRPCAPI()
