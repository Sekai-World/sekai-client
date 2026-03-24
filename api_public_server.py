from os import getenv
from threading import Lock
from flask import Flask, jsonify, request, json
from werkzeug.exceptions import BadRequest
from werkzeug.middleware.proxy_fix import ProxyFix

from utils.jsonrpc_client import JSONRPCClient
from utils.decorators import require_apikey

app = Flask(__name__)
app.wsgi_app = ProxyFix(
    app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1
)

client_map = {
    "jp": JSONRPCClient(f'http://localhost:{getenv("JP_PORT", "39390")}/'),
    "tw": JSONRPCClient(f'http://localhost:{getenv("TW_PORT", "39391")}/'),
    "en": JSONRPCClient(f'http://localhost:{getenv("EN_PORT", "39392")}/'),
    "kr": JSONRPCClient(f'http://localhost:{getenv("KR_PORT", "39393")}/'),
    "cn": JSONRPCClient(f'http://localhost:{getenv("CN_PORT", "39394")}/'),
}
bootstrap_lock = Lock()
bootstrapped = False


@app.errorhandler(BadRequest)
def handle_bad_request(e):
    response = e.get_response()
    response.data = json.dumps({"status": "error", "message": e.description})
    response.content_type = "application/json"
    return response


def get_regional_client(region):
    client = client_map.get(region, None)

    if not client:
        raise BadRequest("No such region.")
    if not is_regional_client_inited(region):
        try:
            init_regional_client(region)
        except:
            raise BadRequest(f"Failed to init {region} client")

    return client


def init_regional_client(region):
    if not is_regional_client_inited(region):
        client_map[region].request("init", [region])

    client_map[region].request("check_versions", [])
    client_map[region].request("login", [])
    

def is_regional_client_inited(region):
    return client_map[region].request("is_init", [])


def bootstrap():
    global bootstrapped
    if bootstrapped:
        return

    with bootstrap_lock:
        if bootstrapped:
            return

        for region in client_map:
            try:
                init_regional_client(region)
            except:
                print(f"skip {region} region for bootstrap", flush=True)

        bootstrapped = True


@app.before_request
def ensure_bootstrap():
    bootstrap()


@app.route('/health', methods=['GET'])
def health():
    is_healthy = all([(not not client_map.get(region)
                       and is_regional_client_inited(region))
                      for region in client_map])

    return jsonify({"status": "success" if is_healthy else "error"
                    }), 200 if is_healthy else 500


@app.route('/<region>/refresh', methods=['POST'])
@require_apikey
def refresh_regional_client(region):
    client = get_regional_client(region)
    client.request("relogin")

    return jsonify({"status": "success"})


@app.route('/<region>/user/<user_id>/profile')
@require_apikey
def fetch_user_profile_by_user_id(region, user_id):
    client = get_regional_client(region)
    user_profile = client.request("fetch_user_profile", [user_id])

    return jsonify({"status": "success", "data": user_profile})


@app.route('/<region>/user/<target_user_id>/event/<event_id>/ranking')
@require_apikey
def fetch_event_ranking_by_user_id(region, target_user_id, event_id):
    client = get_regional_client(region)
    user_profile = client.request("fetch_user_event_ranking",
                                  [target_user_id, event_id])

    return jsonify({"status": "success", "data": user_profile})
