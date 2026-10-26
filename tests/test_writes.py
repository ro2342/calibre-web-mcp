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


def _edit_form_handler(post_log: list, fixture_html: str | None = None):
    html = fixture_html if fixture_html is not None else fixture("book_edit_form.html")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/admin/book/7":
            return httpx.Response(200, text=html)
        if req.method == "POST" and req.url.path == "/admin/book/7":
            post_log.append(req)
            return httpx.Response(302, headers={"location": "/book/7"})
        return httpx.Response(404)

    return handler


def test_update_book_metadata_changes_only_passed_fields(mock_cw):
    from urllib.parse import parse_qs

    posts: list[httpx.Request] = []
    c = mock_cw(_edit_form_handler(posts))
    c.update_book_metadata(7, title="New Title", tags=["x", "y"])

    assert len(posts) == 1
    body = parse_qs(posts[0].content.decode(), keep_blank_values=True)
    assert body["title"] == ["New Title"]
    assert body["tags"] == ["x,y"]
    assert body["authors"] == ["Ada Lovelace & Alan Turing"]
    assert body["series"] == ["Saga"]
    assert body["series_index"] == ["1.0"]
    assert body["publisher"] == ["Pub House"]
    assert body["comments"] == ["Some <b>comment</b>"]
    assert body["rating"] == ["8"]
    assert body["csrf_token"] == ["FORM_CSRF"]


def test_update_book_metadata_refuses_when_expected_field_missing(mock_cw):
    posts: list[httpx.Request] = []
    html = fixture("book_edit_form.html").replace('name="tags"', 'name="tagz"')
    c = mock_cw(_edit_form_handler(posts, html))
    with pytest.raises(CalibreWebError, match="tags"):
        c.update_book_metadata(7, title="Nope")
    assert posts == []


def test_update_book_metadata_rejects_bad_input_before_any_request(mock_cw):
    posts: list[httpx.Request] = []
    c = mock_cw(_edit_form_handler(posts))
    with pytest.raises(ValueError):
        c.update_book_metadata(7, series_index="abc")
    with pytest.raises(ValueError):
        c.update_book_metadata(7, tags=["a,b"])
    with pytest.raises(ValueError):
        c.update_book_metadata(7, authors=["A & B"])
    assert posts == []


def test_upload_format_posts_file_to_upload_route(mock_cw, tmp_path):
    uploads: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/upload":
            uploads.append(req)
            return httpx.Response(302, headers={"location": "/admin/book/7"})
        return httpx.Response(404)

    f = tmp_path / "Book - Someone.epub"
    f.write_bytes(b"PK\x03\x04fake")
    c = mock_cw(handler)
    c.upload_format(7, f)

    assert len(uploads) == 1
    raw = uploads[0].content
    assert b'name="btn-upload-format"; filename="Book - Someone.epub"' in raw
    assert b'name="book_id"' in raw
    assert b"\r\n\r\n7\r\n" in raw


def test_upload_format_rejects_unknown_extension_and_missing_file(mock_cw, tmp_path):
    posts: list[httpx.Request] = []
    c = mock_cw(_edit_form_handler(posts))
    bad = tmp_path / "notes.txt"
    bad.write_text("x")
    with pytest.raises(ValueError):
        c.upload_format(7, bad)
    with pytest.raises(FileNotFoundError):
        c.upload_format(7, tmp_path / "missing.epub")
    assert posts == []


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
