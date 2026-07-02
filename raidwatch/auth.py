"""Authentication: login form, constant-time token check, HttpOnly cookie (D12/D24/D33).

Flow (D24): ``GET /login`` renders a single-password form → ``POST`` validates
the token in constant time → sets an ``HttpOnly``/``SameSite=Lax`` ~90-day
cookie → redirect to ``/``. The token is generated at install (D13) and is
**never logged** — redacted via a logging filter (D33).

SSE auth (D12): ``EventSource`` cannot send ``Authorization`` headers, so the
cookie (sent automatically) is the auth mechanism for ``/api/stream``.

Wiring: ``main.py`` stores the config on ``app.state.config`` and the Jinja
templates on ``app.state.templates`` at startup; this module reads from
``request.app.state``, avoiding circular imports.
"""

from __future__ import annotations

import hmac
import logging
import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.security import APIKeyCookie
from pydantic import BaseModel

logger = logging.getLogger(__name__)

COOKIE_NAME = "raidwatch_token"
COOKIE_MAX_AGE_SECONDS = 90 * 24 * 3600  # ~90 days (D24)

# Cookie-based auth dependency for SSE + all protected routes.
cookie_security = APIKeyCookie(name=COOKIE_NAME, auto_error=False)


# --------------------------------------------------------------------------- #
# Token validation                                                            #
# --------------------------------------------------------------------------- #
def verify_token(provided: str | None, expected: str) -> bool:
    """Constant-time comparison of the provided token against the expected one.

    Both sides are encoded to bytes; ``hmac.compare_digest`` is timing-safe.
    Returns False if either side is empty/None.
    """
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())


def generate_token(num_bytes: int = 32) -> str:
    """Generate a cryptographically-secure random token (≥32 bytes; D13).

    Used by the install script. Returns a hex string (64 chars for 32 bytes).
    """
    return secrets.token_hex(num_bytes)


# --------------------------------------------------------------------------- #
# Auth dependency                                                             #
# --------------------------------------------------------------------------- #
async def require_auth(
    request: Request,
    token: str | None = Depends(cookie_security),
) -> str:
    """FastAPI dependency: require a valid cookie token on protected routes.

    Raises 401 if invalid. The expected token comes from ``app.state.config``.
    """
    config = request.app.state.config
    if verify_token(token, config.auth.token):
        return token  # type: ignore[return-value]

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"Location": "/login"},
    )


# --------------------------------------------------------------------------- #
# Login form model + route handler                                             #
# --------------------------------------------------------------------------- #
class LoginForm(BaseModel):
    token: str


async def handle_login_post(request: Request) -> RedirectResponse | object:
    """Process the login form: validate token, mint cookie, redirect to /.

    Returns a RedirectResponse on success, or re-renders the login template
    with an error on failure.
    """
    config = request.app.state.config
    templates = request.app.state.templates
    form = await request.form()
    provided = str(form.get("token", ""))

    if verify_token(provided, config.auth.token):
        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(
            key=COOKIE_NAME,
            value=provided,
            max_age=COOKIE_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
            secure=False,  # cleartext HTTP in v1 (D13); TLS deferred to v1.x
        )
        client = request.client.host if request.client else "?"
        logger.info("Successful login from %s", client)
        return response

    client = request.client.host if request.client else "?"
    logger.warning("Failed login attempt from %s", client)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": "Invalid token. Please try again."},
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


# --------------------------------------------------------------------------- #
# Token redaction filter (D33)                                                #
# --------------------------------------------------------------------------- #
class TokenRedactionFilter(logging.Filter):
    """Redact the auth token from all log records (D33 — token never logged)."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        if self._token and self._token in record.getMessage():
            record.msg = record.getMessage().replace(self._token, "[REDACTED]")
            record.args = ()
        return True
