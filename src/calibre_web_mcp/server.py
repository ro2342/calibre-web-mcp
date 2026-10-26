"""FastMCP server exposing Calibre-Web shelf and search tools."""

from __future__ import annotations

import os
from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from .client import CalibreWebClient, CalibreWebError

mcp = FastMCP("calibre-web")

_client: CalibreWebClient | None = None


def _get_client() -> CalibreWebClient:
    global _client
    if _client is None:
        url = os.environ.get("CALIBRE_WEB_URL")
        user = os.environ.get("CALIBRE_WEB_USER")
        password = os.environ.get("CALIBRE_WEB_PASS")
        verify_ssl = os.environ.get("CALIBRE_WEB_VERIFY_SSL", "true").lower() != "false"
        if not (url and user and password):
            raise CalibreWebError(
                "missing env: CALIBRE_WEB_URL, CALIBRE_WEB_USER, CALIBRE_WEB_PASS required"
            )
        _client = CalibreWebClient(url, user, password, verify_ssl=verify_ssl)
        _client.login()
    return _client


# ---------- READ tools ----------


@mcp.tool
def list_shelves() -> list[dict]:
    """List all shelves visible to the authenticated user, with public/private flag and shelf id."""
    return [
        {"id": s.id, "name": s.name, "is_public": s.is_public}
        for s in _get_client().list_shelves()
    ]


@mcp.tool
def get_shelf_contents(
    shelf_id: Annotated[int, Field(description="Shelf id from list_shelves")],
) -> dict:
    """Return the list of books in a shelf along with the shelf title and count."""
    return _get_client().get_shelf(shelf_id)


@mcp.tool
def search_books(
    query: Annotated[str, Field(description="Calibre-Web search query (title, author, tag:, series:, etc.)")],
    limit: Annotated[int, Field(ge=1, le=500, description="Max results to return")] = 50,
) -> list[dict]:
    """Search the library using Calibre-Web's own search ranking.

    Supports the standard CW search prefixes: `title:`, `author:`, `tag:`, `series:`, etc.
    """
    return [
        {
            "id": b.id,
            "title": b.title,
            "authors": b.authors,
            "has_cover": b.has_cover,
        }
        for b in _get_client().search_books(query, limit=limit)
    ]


@mcp.tool
def get_book_details(
    book_id: Annotated[int, Field(description="Book id")],
) -> dict | None:
    """Get a single book's metadata — title, formats, cover presence."""
    b = _get_client().get_book(book_id)
    if b is None:
        return None
    return {
        "id": b.id,
        "title": b.title,
        "authors": b.authors,
        "publisher": b.publisher,
        "language": b.language,
        "formats": b.formats,
        "has_cover": b.has_cover,
        "tags": b.tags,
    }


@mcp.tool
def list_books_missing(
    field: Annotated[str, Field(description="Field to audit: 'cover', 'format', or 'tags'")],
    search_query: Annotated[str, Field(description="OPDS search query to scope the audit (empty string is not supported by OPDS; use a broad query like 'a')")] = "a",
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
) -> list[dict]:
    """Return books from a search that are missing the named field.

    `cover` / `format` / `tags` are reliably detectable from OPDS entries.
    """
    if field not in ("cover", "format", "tags"):
        raise ValueError("field must be one of: cover, format, tags")
    client = _get_client()
    candidates = client.search_books(search_query, limit=limit)
    results: list[dict] = []
    for b in candidates:
        missing = (
            (field == "cover" and not b.has_cover)
            or (field == "format" and not b.formats)
            or (field == "tags" and not b.tags)
        )
        if missing:
            results.append({"id": b.id, "title": b.title, "authors": b.authors})
    return results


# ---------- WRITE tools (shelf-only per scope) ----------


@mcp.tool
def create_shelf(
    name: Annotated[str, Field(description="Shelf name")],
    public: Annotated[bool, Field(description="Make the shelf publicly visible")] = False,
) -> dict:
    """Create a new shelf. Returns the new shelf id."""
    sid = _get_client().create_shelf(name, public=public)
    return {"id": sid, "name": name, "is_public": public}


@mcp.tool
def delete_shelf(
    shelf_id: Annotated[int, Field(description="Shelf id to delete")],
) -> dict:
    """Delete a shelf. Does NOT delete books — only the shelf grouping."""
    _get_client().delete_shelf(shelf_id)
    return {"deleted_shelf_id": shelf_id}


@mcp.tool
def add_book_to_shelf(
    shelf_id: Annotated[int, Field(description="Target shelf id")],
    book_id: Annotated[int, Field(description="Book id to add")],
) -> dict:
    """Add a book to a shelf. No-op if already present."""
    _get_client().add_book_to_shelf(shelf_id, book_id)
    return {"shelf_id": shelf_id, "book_id": book_id, "action": "added"}


@mcp.tool
def remove_book_from_shelf(
    shelf_id: Annotated[int, Field(description="Shelf id")],
    book_id: Annotated[int, Field(description="Book id to remove")],
) -> dict:
    """Remove a book from a shelf. No-op if not present."""
    _get_client().remove_book_from_shelf(shelf_id, book_id)
    return {"shelf_id": shelf_id, "book_id": book_id, "action": "removed"}


@mcp.tool
def update_book_metadata(
    book_id: Annotated[int, Field(description="Book id")],
    title: Annotated[str | None, Field(description="New title; omit to keep")] = None,
    authors: Annotated[list[str] | None, Field(description="Full author list; omit to keep")] = None,
    series: Annotated[str | None, Field(description="Series name; empty string clears it; omit to keep")] = None,
    series_index: Annotated[str | None, Field(description="Series number such as 1 or 2.5; omit to keep")] = None,
    tags: Annotated[list[str] | None, Field(description="Full tag list; omit to keep")] = None,
) -> dict:
    """Edit title, authors, series, series index and/or tags of one book, keeping its id.

    Only the fields you pass are changed; everything else on the book is sent back unchanged.
    """
    _get_client().update_book_metadata(
        book_id, title=title, authors=authors, series=series, series_index=series_index, tags=tags
    )
    return {"book_id": book_id, "updated": True}


@mcp.tool
def upload_format(
    book_id: Annotated[int, Field(description="Existing book id to attach the file to")],
    file_path: Annotated[str, Field(description="Local path to an .epub, .kepub or .pdf file")],
) -> dict:
    """Attach a file as a new format of an existing book. Keeps the book id."""
    _get_client().upload_format(book_id, file_path)
    return {"book_id": book_id, "file": file_path, "uploaded": True}


@mcp.tool
def set_read_status(
    book_id: Annotated[int, Field(description="Book id")],
    read: Annotated[bool, Field(description="True to mark as read, False to mark as unread")],
) -> dict:
    """Mark a book as read or unread. No-op if it is already in that state."""
    _get_client().set_read_status(book_id, read)
    return {"book_id": book_id, "is_read": read}


@mcp.tool
def set_shelf_public(
    shelf_id: Annotated[int, Field(description="Shelf id")],
    public: Annotated[bool, Field(description="True to publish, False to make private")],
) -> dict:
    """Toggle a shelf's public-visibility flag."""
    _get_client().set_shelf_public(shelf_id, public)
    return {"shelf_id": shelf_id, "is_public": public}
