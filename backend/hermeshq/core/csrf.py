import hmac

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

AUTH_COOKIE_NAME = "hermeshq_token"
CSRF_COOKIE_NAME = "hermeshq_csrf"
CSRF_HEADER_NAME = "x-csrf-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if (
            request.method not in SAFE_METHODS
            and request.cookies.get(AUTH_COOKIE_NAME)
            and not request.headers.get("authorization")
        ):
            cookie_token = request.cookies.get(CSRF_COOKIE_NAME, "")
            header_token = request.headers.get(CSRF_HEADER_NAME, "")
            if not cookie_token or not header_token or not hmac.compare_digest(cookie_token, header_token):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Invalid CSRF token", "status_code": 403, "path": request.url.path},
                )
        return await call_next(request)
