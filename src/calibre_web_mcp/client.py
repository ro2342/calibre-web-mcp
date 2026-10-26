"""Calibre-Web client.

Reads use the OPDS Atom feed with HTTP Basic auth (stable, well-defined schema).
Writes use Calibre-Web's HTML routes with session-cookie + CSRF (less stable, but
the only way to mutate shelves since OPDS is read-only).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import httpx


OPDS_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/terms/",
    "dcterms": "http://purl.org/dc/terms/",
}

EDIT_FORM_REQUIRED_FIELDS = (
    "csrf_token",
    "title",
    "authors",
    "tags",
    "series",
    "series_index",
    "publisher",
    "comments",
    "languages",
    "rating",
    "pubdate",
)

UPLOAD_FORMAT_MIME = {
    ".epub": "application/epub+zip",
    ".kepub": "application/epub+zip",
    ".pdf": "application/pdf",
}


class _EditFormParser(HTMLParser):
    """Collects (name, value) pairs from a Calibre-Web edit form, in page order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: list[tuple[str, str]] = []
        self._textarea: str | None = None
        self._textarea_text: list[str] = []
        self._select: str | None = None
        self._select_selected: str | None = None
        self._select_first: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        name = a.get("name")
        if tag == "input" and name:
            itype = a.get("type", "text").lower()
            if itype in ("file", "submit", "button", "image", "reset"):
                return
            if itype in ("checkbox", "radio"):
                if any(k == "checked" for k, _ in attrs):
                    self.fields.append((name, a.get("value") or "on"))
                return
            self.fields.append((name, a.get("value", "")))
        elif tag == "textarea" and name:
            self._textarea = name
            self._textarea_text = []
        elif tag == "select" and name:
            self._select = name
            self._select_selected = None
            self._select_first = None
        elif tag == "option" and self._select is not None:
            value = a.get("value", "")
            if self._select_first is None:
                self._select_first = value
            if any(k == "selected" for k, _ in attrs) and self._select_selected is None:
                self._select_selected = value

    def handle_endtag(self, tag: str) -> None:
        if tag == "textarea" and self._textarea is not None:
            self.fields.append((self._textarea, "".join(self._textarea_text)))
            self._textarea = None
        elif tag == "select" and self._select is not None:
            chosen = self._select_selected if self._select_selected is not None else self._select_first
            if chosen is not None:
                self.fields.append((self._select, chosen))
            self._select = None

    def handle_data(self, data: str) -> None:
        if self._textarea is not None:
            self._textarea_text.append(data)


def _form_payload(fields: list[tuple[str, str]], overrides: dict[str, str]) -> dict[str, Any]:
    """Build the POST body from the page's fields, replacing only the keys in `overrides`."""
    payload: dict[str, Any] = {}
    applied: set[str] = set()
    for name, value in fields:
        if name in overrides and name not in applied:
            value = overrides[name]
            applied.add(name)
        if name in payload:
            prev = payload[name]
            payload[name] = (prev if isinstance(prev, list) else [prev]) + [value]
        else:
            payload[name] = value
    return payload


class CalibreWebError(Exception):
    pass


class AuthError(CalibreWebError):
    pass


@dataclass
class Shelf:
    id: int
    name: str
    is_public: bool


@dataclass
class Book:
    id: int
    title: str
    authors: list[str] = field(default_factory=list)
    publisher: str | None = None
    language: str | None = None
    formats: list[str] = field(default_factory=list)
    has_cover: bool = False
    summary: str | None = None
    tags: list[str] = field(default_factory=list)


class CalibreWebClient:
    """Calibre-Web HTTP client. Reads via OPDS (Basic auth); writes via session login."""

    def __init__(self, base_url: str, username: str, password: str, verify_ssl: bool = True):
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._basic = httpx.BasicAuth(username, password)
        self._client = httpx.Client(
            base_url=self.base_url,
            verify=verify_ssl,
            follow_redirects=False,
            timeout=30.0,
        )
        self._session_authed = False
        self._csrf: str | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CalibreWebClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ---------- session auth (for write routes) ----------

    def _csrf_token_from_login(self) -> str:
        r = self._client.get("/login")
        r.raise_for_status()
        m = re.search(r'name="csrf_token"\s+value="([^"]+)"', r.text)
        if not m:
            raise AuthError("CSRF token not found on /login")
        return m.group(1)

    def login(self) -> None:
        """Establish a session cookie so HTML write routes are accepted."""
        token = self._csrf_token_from_login()
        r = self._client.post(
            "/login",
            data={
                "username": self._username,
                "password": self._password,
                "csrf_token": token,
                "next": "/",
                "submit": "",
                "remember_me": "on",
            },
        )
        if r.status_code == 302 and "/login" not in r.headers.get("location", ""):
            self._session_authed = True
            return
        raise AuthError(f"Login failed (HTTP {r.status_code}): check CALIBRE_WEB_USER and CALIBRE_WEB_PASS")

    def _refresh_csrf(self) -> str:
        """Grab a fresh CSRF token. /me always has a form; / does not."""
        for probe in ("/me", "/shelf/create"):
            r = self._client.get(probe)
            if r.status_code == 302 and "/login" in r.headers.get("location", ""):
                self._session_authed = False
                self.login()
                r = self._client.get(probe)
            if r.status_code == 200:
                m = re.search(r'name="csrf_token"\s+value="([^"]+)"', r.text)
                if m:
                    self._csrf = m.group(1)
                    return self._csrf
        raise CalibreWebError("CSRF token not found on any probed page")

    def _session_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not self._session_authed:
            self.login()
        # For mutating verbs, inject CSRF header
        if method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
            if self._csrf is None:
                self._refresh_csrf()
            headers = dict(kwargs.pop("headers", {}) or {})
            headers.setdefault("X-CSRFToken", self._csrf or "")
            kwargs["headers"] = headers
        r = self._client.request(method, path, **kwargs)
        # session expired → relogin and retry once
        if r.status_code == 302 and "/login" in r.headers.get("location", ""):
            self._session_authed = False
            self.login()
            r = self._client.request(method, path, **kwargs)
        # CSRF rejected → refresh and retry once
        if r.status_code in (400, 403) and method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
            self._refresh_csrf()
            headers = dict(kwargs.get("headers") or {})
            headers["X-CSRFToken"] = self._csrf or ""
            kwargs["headers"] = headers
            r = self._client.request(method, path, **kwargs)
        return r

    def _csrf_for(self, path: str) -> str:
        r = self._session_request("GET", path)
        r.raise_for_status()
        m = re.search(r'name="csrf_token"\s+value="([^"]+)"', r.text)
        if not m:
            raise CalibreWebError(f"CSRF token not found on {path}")
        return m.group(1)

    # ---------- OPDS reads (Basic auth) ----------

    def _opds(self, path: str) -> ET.Element:
        r = self._client.get(path, auth=self._basic)
        if r.status_code == 401:
            raise AuthError("OPDS rejected Basic auth")
        r.raise_for_status()
        return ET.fromstring(r.text)

    @staticmethod
    def _atom_text(entry: ET.Element, tag: str) -> str | None:
        el = entry.find(f"atom:{tag}", OPDS_NS)
        if el is not None and el.text:
            return el.text.strip()
        return None

    def list_shelves(self) -> list[Shelf]:
        """Return all shelves the user can see. Calibre-Web's OPDS appends ' (Public)' to public shelf names."""
        feed = self._opds("/opds/shelfindex")
        shelves: list[Shelf] = []
        for entry in feed.findall("atom:entry", OPDS_NS):
            id_text = self._atom_text(entry, "id") or ""
            title = self._atom_text(entry, "title") or ""
            m = re.search(r"/opds/shelf/(\d+)", id_text)
            if not m:
                continue
            shelf_id = int(m.group(1))
            is_public = title.endswith(" (Public)")
            name = title[: -len(" (Public)")] if is_public else title
            shelves.append(Shelf(id=shelf_id, name=name, is_public=is_public))
        return shelves

    def get_shelf(self, shelf_id: int) -> dict[str, Any]:
        feed = self._opds(f"/opds/shelf/{shelf_id}")
        books = [self._entry_to_book(e) for e in feed.findall("atom:entry", OPDS_NS)]
        return {"id": shelf_id, "books": [_book_to_dict(b) for b in books], "count": len(books)}

    def search_books(self, query: str, limit: int = 50) -> list[Book]:
        """Search books via OPDS.

        WARNING: Calibre-Web's `/opds/search/<term>` returns ALL matching books in
        a single Atom feed with no server-side pagination. For very broad terms
        against a large library this can be multi-MB and multi-second. Pass a
        specific query and consider the timeout you set on the client.
        """
        if not query or not query.strip():
            raise ValueError("search_books: query must be non-empty")
        safe = httpx.QueryParams({"q": query})["q"]
        feed = self._opds(f"/opds/search/{safe}")
        out: list[Book] = []
        for entry in feed.findall("atom:entry", OPDS_NS):
            if len(out) >= limit:
                break
            out.append(self._entry_to_book(entry))
        return out

    def get_book(self, book_id: int) -> Book | None:
        # No single-book OPDS endpoint; search by id-equivalent. Easier: fetch from /opds and filter.
        # CW exposes book detail at /opds/new — not direct-by-id. Workaround: hit HTML /book/<id> for richness.
        r = self._session_request("GET", f"/book/{book_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        # minimal extraction; full enrichment is via search_books which uses OPDS
        title_m = re.search(r"<title>([^<]+)\s+\|", r.text)
        has_cover_m = re.search(rf'/cover/{book_id}\b', r.text)
        formats = sorted(set(re.findall(rf'/download/{book_id}/([a-z0-9]+)/', r.text)))
        return Book(
            id=book_id,
            title=(title_m.group(1).strip() if title_m else f"book#{book_id}"),
            has_cover=bool(has_cover_m),
            formats=formats,
        )

    @staticmethod
    def _entry_to_book(entry: ET.Element) -> Book:
        title = CalibreWebClient._atom_text(entry, "title") or ""
        authors = [
            (a.findtext("atom:name", default="", namespaces=OPDS_NS) or "").strip()
            for a in entry.findall("atom:author", OPDS_NS)
        ]
        authors = [a for a in authors if a]
        summary = CalibreWebClient._atom_text(entry, "summary")
        # publisher: <publisher><name>...</name></publisher>
        pub_el = entry.find("atom:publisher", OPDS_NS)
        publisher = None
        if pub_el is not None:
            pn = pub_el.find("atom:name", OPDS_NS)
            if pn is not None and pn.text:
                publisher = pn.text.strip()
        language = entry.findtext("dcterms:language", default=None, namespaces=OPDS_NS)
        # extract book id + formats from links
        formats: list[str] = []
        has_cover = False
        book_id: int | None = None
        for link in entry.findall("atom:link", OPDS_NS):
            href = link.get("href", "")
            if m := re.match(r"/opds/download/(\d+)/([a-z0-9]+)", href):
                book_id = int(m.group(1))
                formats.append(m.group(2))
            elif re.match(r"/opds/cover/\d+", href):
                has_cover = True
                if book_id is None:
                    m2 = re.match(r"/opds/cover/(\d+)", href)
                    if m2:
                        book_id = int(m2.group(1))
        tags = [c.get("label") or c.get("term") or "" for c in entry.findall("atom:category", OPDS_NS)]
        tags = [t for t in tags if t]
        return Book(
            id=book_id or 0,
            title=title,
            authors=authors,
            publisher=publisher,
            language=language,
            formats=sorted(set(formats)),
            has_cover=has_cover,
            summary=summary,
            tags=tags,
        )

    # ---------- writes (HTML session routes) ----------

    def create_shelf(self, name: str, public: bool = False) -> int:
        token = self._csrf_for("/shelf/create")
        data = {"title": name, "csrf_token": token}
        if public:
            data["is_public"] = "on"
        r = self._session_request("POST", "/shelf/create", data=data)
        loc = r.headers.get("location", "")
        m = re.search(r"/shelf/(\d+)$", loc)
        if not m:
            raise CalibreWebError(
                f"create_shelf got unexpected response: HTTP {r.status_code}, location={loc!r}"
            )
        return int(m.group(1))

    def delete_shelf(self, shelf_id: int) -> None:
        r = self._session_request("POST", f"/shelf/delete/{shelf_id}")
        if r.status_code not in (200, 204, 302):
            raise CalibreWebError(f"delete_shelf failed: HTTP {r.status_code}")

    def add_book_to_shelf(self, shelf_id: int, book_id: int) -> None:
        r = self._session_request("POST", f"/shelf/add/{shelf_id}/{book_id}")
        if r.status_code not in (200, 204, 302):
            raise CalibreWebError(f"add_book_to_shelf failed: HTTP {r.status_code}")

    def remove_book_from_shelf(self, shelf_id: int, book_id: int) -> None:
        r = self._session_request("POST", f"/shelf/remove/{shelf_id}/{book_id}")
        if r.status_code not in (200, 204, 302):
            raise CalibreWebError(f"remove_book_from_shelf failed: HTTP {r.status_code}")

    def set_read_status(self, book_id: int, read: bool) -> None:
        r = self._session_request("GET", f"/book/{book_id}")
        r.raise_for_status()
        m = re.search(r'<input[^>]*id="have_read_cb"[^>]*>', r.text)
        if not m:
            raise CalibreWebError(f"read checkbox not found on /book/{book_id}")
        currently_read = " checked" in m.group(0)
        if currently_read == read:
            return
        r = self._session_request("POST", f"/ajax/toggleread/{book_id}")
        if r.status_code not in (200, 204, 302):
            raise CalibreWebError(f"set_read_status failed: HTTP {r.status_code}")

    def _read_book_edit_form(self, book_id: int) -> list[tuple[str, str]]:
        r = self._session_request("GET", f"/admin/book/{book_id}")
        r.raise_for_status()
        parser = _EditFormParser()
        parser.feed(r.text)
        names = {name for name, _ in parser.fields}
        missing = [f for f in EDIT_FORM_REQUIRED_FIELDS if f not in names]
        if missing:
            raise CalibreWebError(
                f"edit form for book {book_id} is missing expected fields: {', '.join(missing)}; refusing to edit"
            )
        return parser.fields

    def update_book_metadata(
        self,
        book_id: int,
        *,
        title: str | None = None,
        authors: list[str] | None = None,
        series: str | None = None,
        series_index: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        overrides: dict[str, str] = {}
        if title is not None:
            if not title.strip():
                raise ValueError("title must not be empty")
            overrides["title"] = title.strip()
        if authors is not None:
            if not authors or any(not a.strip() or "&" in a for a in authors):
                raise ValueError("authors must be non-empty names without '&'")
            overrides["authors"] = " & ".join(a.strip() for a in authors)
        if series is not None:
            overrides["series"] = series.strip()
        if series_index is not None:
            if not re.fullmatch(r"\d+(\.\d+)?", series_index.strip()):
                raise ValueError("series_index must be a number such as 1 or 2.5")
            overrides["series_index"] = series_index.strip()
        if tags is not None:
            if any("," in t for t in tags):
                raise ValueError("tags must not contain commas")
            overrides["tags"] = ",".join(t.strip() for t in tags if t.strip())
        if not overrides:
            return
        fields = self._read_book_edit_form(book_id)
        r = self._session_request("POST", f"/admin/book/{book_id}", data=_form_payload(fields, overrides))
        if r.status_code != 302:
            raise CalibreWebError(f"update_book_metadata failed: HTTP {r.status_code}")

    def upload_format(self, book_id: int, file_path: str | Path) -> None:
        path = Path(file_path)
        ext = path.suffix.lower()
        if ext not in UPLOAD_FORMAT_MIME:
            raise ValueError(f"unsupported format {ext!r}; allowed: {', '.join(sorted(UPLOAD_FORMAT_MIME))}")
        if not path.is_file():
            raise FileNotFoundError(f"file not found: {path}")
        fields = self._read_book_edit_form(book_id)
        payload = _form_payload(fields, {})
        with path.open("rb") as fh:
            r = self._session_request(
                "POST",
                f"/admin/book/{book_id}",
                data=payload,
                files=[("btn-upload-format", (path.name, fh, UPLOAD_FORMAT_MIME[ext]))],
            )
        if r.status_code != 302:
            raise CalibreWebError(f"upload_format failed: HTTP {r.status_code}")

    def set_shelf_public(self, shelf_id: int, public: bool) -> None:
        r = self._session_request("GET", f"/shelf/edit/{shelf_id}")
        r.raise_for_status()
        tok_m = re.search(r'name="csrf_token"[^>]*?value="([^"]+)"', r.text)
        title_m = re.search(r'name="title"[^>]*?value="([^"]*)"', r.text)
        if not tok_m:
            raise CalibreWebError("CSRF token missing on /shelf/edit page")
        if not title_m or not title_m.group(1):
            raise CalibreWebError(
                f"could not read current shelf title from /shelf/edit/{shelf_id}; refusing to POST blank title"
            )
        data = {"title": title_m.group(1), "csrf_token": tok_m.group(1)}
        if public:
            data["is_public"] = "on"
        r = self._session_request("POST", f"/shelf/edit/{shelf_id}", data=data)
        if r.status_code not in (200, 302):
            raise CalibreWebError(f"set_shelf_public failed: HTTP {r.status_code}")


def _book_to_dict(b: Book) -> dict[str, Any]:
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
