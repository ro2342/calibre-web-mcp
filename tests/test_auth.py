"""Session login, CSRF token discovery, and 401-refresh tests."""

from __future__ import annotations

import httpx
import pytest

from calibre_web_mcp.client import AuthError, CalibreWebClient

from .conftest import fixture


def test_login_success_sets_session_authed():
    """Server returns the login page, then 302 to / with a session cookie set."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/login":
            return httpx.Response(200, text=fixture("login_page.html"))
        if req.method == "POST" and req.url.path == "/login":
            # Verify the client sent the CSRF token from the form
            body = req.content.decode()
            assert "csrf_token=LOGIN_CSRF_TOKEN_VALUE" in body
            assert "username=alice" in body
            return httpx.Response(
                302,
                headers={"location": "/", "set-cookie": "session=ok; Path=/"},
            )
        return httpx.Response(404)

    c = CalibreWebClient("http://cw.test", "alice", "pw")
    c._client.close()
    c._client = httpx.Client(
        base_url="http://cw.test",
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    c.login()
    assert c._session_authed is True


def test_login_failure_with_session_cookie_still_raises():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/login":
            return httpx.Response(200, text=fixture("login_page.html"))
        if req.method == "POST" and req.url.path == "/login":
            return httpx.Response(200, text="bad credentials", headers={"set-cookie": "session=anon; Path=/"})
        return httpx.Response(404)

    c = CalibreWebClient("http://cw.test", "alice", "wrong")
    c._client.close()
    c._client = httpx.Client(
        base_url="http://cw.test",
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    with pytest.raises(AuthError):
        c.login()


def test_login_failure_raises_autherror():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/login":
            return httpx.Response(200, text=fixture("login_page.html"))
        if req.method == "POST" and req.url.path == "/login":
            # bad creds redirect back to /login
            return httpx.Response(302, headers={"location": "/login"})
        return httpx.Response(404)

    c = CalibreWebClient("http://cw.test", "alice", "wrong")
    c._client.close()
    c._client = httpx.Client(
        base_url="http://cw.test",
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    with pytest.raises(AuthError):
        c.login()


def test_login_missing_csrf_raises():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/login":
            return httpx.Response(200, text="<html><body>no form here</body></html>")
        return httpx.Response(404)

    c = CalibreWebClient("http://cw.test", "alice", "pw")
    c._client.close()
    c._client = httpx.Client(
        base_url="http://cw.test",
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    with pytest.raises(AuthError):
        c.login()


def test_csrf_refresh_uses_me_page(mock_cw):
    """When _csrf is missing and a POST is attempted, the client should fetch a fresh token from /me."""

    posted_with_csrf: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/me":
            return httpx.Response(200, text=fixture("me_page.html"))
        if req.method == "POST" and req.url.path == "/shelf/add/5/100":
            posted_with_csrf.append(req.headers.get("X-CSRFToken"))
            return httpx.Response(302, headers={"location": "/"})
        return httpx.Response(404)

    c = mock_cw(handler, pre_authed=True)
    c._csrf = None  # force refresh
    c.add_book_to_shelf(5, 100)
    assert posted_with_csrf == ["ME_PAGE_CSRF_TOKEN"]


def test_session_expiry_triggers_relogin(mock_cw):
    """If a POST redirects to /login, client should relogin and retry once."""
    state = {"logged_in_count": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/login":
            return httpx.Response(200, text=fixture("login_page.html"))
        if req.method == "POST" and req.url.path == "/login":
            state["logged_in_count"] += 1
            return httpx.Response(302, headers={"location": "/", "set-cookie": "session=ok; Path=/"})
        if req.method == "POST" and req.url.path == "/shelf/add/5/100":
            # First call: session expired. Second call: success.
            if state["logged_in_count"] == 0:
                return httpx.Response(302, headers={"location": "/login"})
            return httpx.Response(302, headers={"location": "/"})
        return httpx.Response(404)

    c = mock_cw(handler, pre_authed=True)
    c.add_book_to_shelf(5, 100)
    assert state["logged_in_count"] == 1
