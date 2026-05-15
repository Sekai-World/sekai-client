"""
Flask route decorators for API authentication and authorization.

Provides decorators to protect API endpoints with token-based authentication.
API tokens are read at request time (not import time) to allow configuration
reloading without process restart.
"""

from os import getenv
from hmac import compare_digest
from functools import wraps
from typing import Callable, Any
from flask import request, abort


def require_apikey(view_function: Callable[..., Any]) -> Callable[..., Any]:
    """
    Decorator to enforce API key authentication on Flask routes.
    
    Reads API_TOKEN from environment at request time (not import time).
    Uses constant-time comparison to prevent timing attacks.
    Implements fail-closed semantics: returns 500 if API_TOKEN not configured,
    returns 401 if token doesn't match.
    
    Args:
        view_function: Flask view function to decorate
        
    Returns:
        Decorated function that checks API token before calling view
        
    Example:
        >>> @app.route('/protected', methods=['POST'])
        >>> @require_apikey
        >>> def protected_endpoint():
        ...     return {'status': 'success'}
    """
    @wraps(view_function)
    def decorated_function(*args: Any, **kwargs: Any) -> Any:
        api_token = getenv('API_TOKEN', '')
        if not api_token:
            # Fail closed when server is misconfigured instead of
            # allowing empty-token access.
            abort(500, description='API_TOKEN is not configured')

        request_token = request.headers.get('x-api-token', '')
        if request_token and compare_digest(request_token, api_token):
            return view_function(*args, **kwargs)
        else:
            abort(401)
    
    return decorated_function
