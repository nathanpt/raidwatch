"""Tests for auth: constant-time token check, HttpOnly cookie login, require_auth.

Mirrors the async + mock style of existing tests. Login + dependency behaviour
is exercised through a minimal FastAPI app over httpx's in-process ASGI
transport (no lifespan → no collector start), with the real Jinja templates for
the login re-render path.
"""

from __future__ import annotations

import hmac
from pathlib import Path

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.templating import Jinja2Templates

from raidwatch import auth
from raidwatch.auth import COOKIE_NAME, handle_login_post, require_auth, verify_token
from raidwatch.config import AppConfig, AuthConfig

# A strong token (≥32 bytes) that passes AuthConfig validation.
TOKEN = "a" * 64
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def _auth_cookie() -> dict[str, str]:
    """A raw Cookie header — avoids httpx's deprecated per-request ``cookies=``."""
    return {"Cookie": f"{COOKIE_NAME}={TOKEN}"}


class TestVerifyToken:
    """Constant-time token comparison (D12/D13)."""

    def test_correct_token_accepted(self) -> None:
        assert verify_token(TOKEN, TOKEN) is True

    def test_wrong_token_rejected(self) -> None:
        assert verify_token("b" * 64, TOKEN) is False

    def test_none_rejected(self) -> None:
        assert verify_token(None, TOKEN) is False

    def test_empty_rejected(self) -> None:
        assert verify_token("", TOKEN) is False

    def test_empty_expected_rejected(self) -> None:
        assert verify_token(TOKEN, "") is False

    def test_no_length_short_circuit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Different-length inputs still reach compare_digest (no early bail-out).

        A length-based short-circuit would leak length info via timing; the
        implementation forwards both sides to ``hmac.compare_digest`` regardless.
        """
        seen: list[tuple[bytes, bytes]] = []
        real = hmac.compare_digest

        def spy(a: bytes, b: bytes) -> bool:
            seen.append((a, b))
            return real(a, b)

        monkeypatch.setattr(auth.hmac, "compare_digest", spy)

        # Lengths differ (4 vs 64) — must still return False AND consult compare_digest.
        assert verify_token("xxxx", TOKEN) is False
        assert len(seen) == 1
        assert seen[0] == (b"xxxx", TOKEN.encode())


@pytest.fixture
def app() -> FastAPI:
    """A minimal app wiring the real login handler + an auth-protected route."""
    application = FastAPI()

    @application.post("/login")
    async def login_post(request: Request) -> object:
        return await handle_login_post(request)

    @application.get("/protected", dependencies=[Depends(require_auth)])
    async def protected() -> dict[str, bool]:
        return {"ok": True}

    application.state.config = AppConfig(auth=AuthConfig(token=TOKEN))
    application.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    return application


@pytest.fixture
async def client(app: FastAPI):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestLogin:
    """POST /login: correct token mints an HttpOnly cookie + redirect."""

    @pytest.mark.asyncio
    async def test_correct_token_sets_httponly_cookie_and_redirects(
        self, client: httpx.AsyncClient
    ) -> None:
        resp = await client.post("/login", data={"token": TOKEN}, follow_redirects=False)
        assert resp.status_code == 303  # See Other → dashboard
        cookie = resp.headers.get("set-cookie", "")
        assert COOKIE_NAME in cookie
        assert "HttpOnly" in cookie
        assert "samesite=lax" in cookie.lower()

    @pytest.mark.asyncio
    async def test_wrong_token_rejected_no_cookie(self, client: httpx.AsyncClient) -> None:
        resp = await client.post("/login", data={"token": "wrong"}, follow_redirects=False)
        assert resp.status_code == 401
        assert COOKIE_NAME not in resp.headers.get("set-cookie", "")


class TestRequireAuth:
    """The require_auth dependency gates protected routes."""

    @pytest.mark.asyncio
    async def test_rejects_unauthenticated(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/protected")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_allows_authenticated(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/protected", headers=_auth_cookie())
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    @pytest.mark.asyncio
    async def test_rejects_invalid_cookie(self, client: httpx.AsyncClient) -> None:
        resp = await client.get(
            "/protected", headers={"Cookie": f"{COOKIE_NAME}=garbage"}
        )
        assert resp.status_code == 401
