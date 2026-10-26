"""Tests for the shelf-write endpoints (create / delete / add / remove / set_public)."""

from __future__ import annotations

import httpx
import pytest

from calibre_web_mcp.client import CalibreWebError

from .conftest import fixture


def test_create_shelf_extracts_id_from_redirect(mock_cw):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/shelf/create":
            return httpx.Response(200, text=fixture("shelf_edit_form.html"))
        if req.method == "POST" and req.url.path == "/shelf/create":
            body = req.content.decode()
            assert "title=NewShelf" in body
            assert "is_public=on" in body
            return httpx.Response(302, headers={"location": "/shelf/42"})
        return httpx.Response(404)

    c = mock_cw(handler)
    new_id = c.create_shelf("NewShelf", public=True)
    assert new_id == 42


def test_create_shelf_missing_redirect_raises(mock_cw):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, text=fixture("shelf_edit_form.html"))
        return httpx.Response(200, text="form re-rendered with error")

    c = mock_cw(handler)
    with pytest.raises(CalibreWebError):
        c.create_shelf("Bad", public=False)


def test_delete_shelf_posts_with_csrf(mock_cw):
    captured: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/shelf/delete/42":
            captured.append(req.headers.get("X-CSRFToken"))
            return httpx.Response(302, headers={"location": "/"})
        return httpx.Response(404)

    c = mock_cw(handler)
    c.delete_shelf(42)
    assert captured == ["TEST_CSRF"]


def test_set_read_status_toggles_only_when_state_differs(mock_cw):
    toggles: list[str] = []
    state = {"checked": False}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/book/7":
            chk = " checked" if state["checked"] else ""
            return httpx.Response(
                200, text=f'<input type="checkbox" id="have_read_cb" name="have_read_cb" value="on"{chk}>'
            )
        if req.method == "POST" and req.url.path == "/ajax/toggleread/7":
            toggles.append(req.url.path)
            state["checked"] = not state["checked"]
            return httpx.Response(200, json={"result": "success"})
        return httpx.Response(404)

    c = mock_cw(handler)
    c.set_read_status(7, True)
    c.set_read_status(7, True)
    assert toggles == ["/ajax/toggleread/7"]


def test_add_book_to_shelf_sends_post(mock_cw):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/shelf/add/5/100":
            return httpx.Response(302, headers={"location": "/"})
        return httpx.Response(404)

    c = mock_cw(handler)
    c.add_book_to_shelf(5, 100)


def test_remove_book_from_shelf_sends_post(mock_cw):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/shelf/remove/5/100":
            return httpx.Response(204)
        return httpx.Response(404)

    c = mock_cw(handler)
    c.remove_book_from_shelf(5, 100)


def test_set_shelf_public_preserves_title(mock_cw):
    """Regression test: an early version of set_shelf_public used a regex that
    failed to extract the title (because CW puts id="title" between name="..."
    and value="..."), causing the POST to clobber the shelf name with an empty
    string. Guard against that recurrence.
    """

    posted_body: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/shelf/edit/42":
            return httpx.Response(200, text=fixture("shelf_edit_form.html"))
        if req.method == "POST" and req.url.path == "/shelf/edit/42":
            posted_body.append(req.content)
            return httpx.Response(302, headers={"location": "/shelf/42"})
        return httpx.Response(404)

    c = mock_cw(handler)
    c.set_shelf_public(42, public=True)
    body = posted_body[0].decode()
    assert "title=Existing+Shelf+Name" in body
    assert "is_public=on" in body


def test_set_shelf_public_refuses_blank_title(mock_cw):
    """If we somehow can't read the current title, refuse to POST a blank one."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/shelf/edit/42":
            # form without a title field
            return httpx.Response(
                200,
                text='<form method="POST"><input name="csrf_token" value="x"></form>',
            )
        return httpx.Response(500, text="should not reach POST")

    c = mock_cw(handler)
    with pytest.raises(CalibreWebError, match="blank title"):
        c.set_shelf_public(42, True)
