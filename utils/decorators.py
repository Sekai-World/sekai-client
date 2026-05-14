from os import getenv
from hmac import compare_digest
from functools import wraps
from flask import request, abort


API_TOKEN = getenv('API_TOKEN', '')

# The actual decorator function
def require_apikey(view_function):
    @wraps(view_function)
    # the new, post-decoration function. Note *args and **kwargs here.
    def decorated_function(*args, **kwargs):
        if not API_TOKEN:
            # Fail closed when server is misconfigured instead of allowing empty-token access.
            abort(500, description='API_TOKEN is not configured')

        request_token = request.headers.get('x-api-token', '')
        if request_token and compare_digest(request_token, API_TOKEN):
            return view_function(*args, **kwargs)
        else:
            abort(401)
    return decorated_function
