# calibre-web-mcp

[![tests](https://github.com/acato/calibre-web-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/acato/calibre-web-mcp/actions/workflows/test.yml)

An MCP (Model Context Protocol) server for **[Calibre-Web](https://github.com/janeczku/calibre-web)** — the web frontend for Calibre libraries.

Unlike existing Calibre MCP servers (which target the Calibre Content Server, `calibredb` CLI, or `metadata.db` directly), this one talks to **Calibre-Web's own routes** so it can manage **shelves** — a Calibre-Web concept that doesn't exist in plain Calibre.

> **Status:** beta. 11 tools across shelves/books/search, 18 unit + integration tests passing, API stable, in production against a multi-thousand-book Calibre-Web instance. HTML-parsing selectors are best-effort against Calibre-Web's stock theme; please [open an issue](../../issues) if anything breaks on your install.

## What it gives you

**Reads**
- `list_shelves` — your shelves + public ones, with public/private flag and id
- `get_shelf_contents` — books in a shelf
- `search_books` — full Calibre-Web search (`title:`, `author:`, `tag:`, `series:`, etc.)
- `get_book_details` — title, authors, tags, formats, presence of cover/description
- `list_books_missing` — audit which books are missing a cover, description, or format

**Writes (shelves and read status)**
- `create_shelf` — with optional `public=true`
- `delete_shelf` — removes the shelf, not the books
- `add_book_to_shelf` / `remove_book_from_shelf`
- `set_shelf_public` — toggle visibility
- `set_read_status` — mark a book as read or unread (no-op if already in that state)

This server intentionally does **not** write metadata (use `calibredb` for that — it's lock-aware) and does **not** touch admin/user settings.

## Install

Requires Python 3.11+. Install directly from GitHub:

```bash
uvx --from git+https://github.com/acato/calibre-web-mcp calibre-web-mcp
```

> Not yet on PyPI — `uvx calibre-web-mcp` / `pip install calibre-web-mcp` will work once published.

### Windows: avoid Microsoft Store Python

If `uv` picks Microsoft Store Python (path under `\WindowsApps\PythonSoftwareFoundation...`) when creating the venv, the MCP runs fine from a terminal but fails to launch from GUI hosts like the Claude desktop app, IDE extensions, or scheduled tasks. You will see:

```
Unable to create process using "...\WindowsApps\PythonSoftwareFoundation.Python.3.12_...\python.exe"
```

The Store-Python app-execution alias requires an interactive user context that GUI-spawned children do not get. Pin `uv` to a non-Store interpreter — uv's managed Python is easiest:

```powershell
uv python install 3.12
uv venv --python 3.12 --python-preference only-managed --clear
uv sync
```

Verify: `Get-Content .venv\pyvenv.cfg` — the `home =` line should point under `AppData\Roaming\uv\python\...`, **not** `\WindowsApps\`. A python.org installer or `winget install Python.Python.3.12` also works.

## Configure

Three environment variables:

| Variable | Required | Example |
|---|---|---|
| `CALIBRE_WEB_URL` | yes | `http://10.0.0.5:8083` |
| `CALIBRE_WEB_USER` | yes | `admin` |
| `CALIBRE_WEB_PASS` | yes | (your password) |
| `CALIBRE_WEB_VERIFY_SSL` | no | `false` to skip TLS verification (default `true`) |

The MCP logs in with CSRF + session cookie just like a browser; it does not need an app password.

## Use with Claude Code

```bash
claude mcp add --scope user calibre-web -- \
  uvx --from git+https://github.com/acato/calibre-web-mcp calibre-web-mcp
```

Then edit `~/.claude.json` to add env vars under the server entry (the `claude mcp add -e` flag is currently buggy for multi-env-var setups — see Claude Code issue tracker):

```json
"calibre-web": {
  "type": "stdio",
  "command": "uvx",
  "args": ["--from", "git+https://github.com/acato/calibre-web-mcp", "calibre-web-mcp"],
  "env": {
    "CALIBRE_WEB_URL": "http://10.0.0.5:8083",
    "CALIBRE_WEB_USER": "admin",
    "CALIBRE_WEB_PASS": "..."
  }
}
```

## Use with Claude Desktop

In `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "calibre-web": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/acato/calibre-web-mcp", "calibre-web-mcp"],
      "env": {
        "CALIBRE_WEB_URL": "http://10.0.0.5:8083",
        "CALIBRE_WEB_USER": "admin",
        "CALIBRE_WEB_PASS": "..."
      }
    }
  }
}
```

## Why a separate Calibre-Web MCP

Calibre and Calibre-Web are different:

- **Calibre** is the desktop application; `calibredb` is its CLI; metadata lives in `metadata.db`. Existing MCP servers (e.g. `sandraschi/calibremcp`, `FaceDeer/calibre_full_mcp_server`) target one of these surfaces.
- **Calibre-Web** is a separate Flask app by [janeczku](https://github.com/janeczku/calibre-web) that **adds shelves, public-shelf URLs, per-user state, and OPDS on top of a Calibre library**. None of these concepts exist in `metadata.db`. They live in Calibre-Web's own `app.db`.

If you only need search/metadata, the Calibre-targeted MCPs are fine — and safer, because they don't fight Calibre-Web for the library lock. This server picks up where they stop: the **Calibre-Web–specific** features.

## Concurrent-access safety

`calibredb` (and any MCP that uses Calibre's internal API) takes a write-lock on the library and **will conflict with a running Calibre-Web container**. This server avoids that problem by going through Calibre-Web's own routes — the same routes the web UI uses — so locking is handled by Calibre-Web itself.

For *metadata* writes, still use `calibredb`. (Calibre-Web's metadata-edit endpoints are gated, version-fragile, and not in scope here.)

## Known gaps / version compatibility

- **Reads use OPDS**, which has a stable schema across recent Calibre-Web versions. Writes use the HTML routes (`/shelf/create`, `/shelf/edit/<id>`, etc.) — if your build has reworked these, the write tools will need updating.
- **Broad search queries are slow.** Calibre-Web's `/opds/search/<term>` returns *every* matching book in a single Atom feed with no server-side pagination; a query like `"a"` against a 4000-book library is multi-MB and can wedge the container if your timeout is short. Use specific terms and adjust `httpx` timeouts if needed. An empty query is rejected client-side.
- `set_shelf_public` re-reads the current title via the edit form to avoid clobbering. Bug fixed in v0.1.0a0 where the regex didn't tolerate `id="title"` between `name="title"` and `value="..."`.
- **2FA isn't supported.** Calibre-Web doesn't ship 2FA in stock; if your reverse proxy enforces it, use an API/token-aware proxy instead.

## Development

```bash
git clone https://github.com/acato/calibre-web-mcp
cd calibre-web-mcp
uv sync --dev
uv run pytest                   # unit tests (no network)
CALIBRE_WEB_TEST_URL=http://your-cw:8083 \
CALIBRE_WEB_TEST_USER=admin \
CALIBRE_WEB_TEST_PASS=... \
  uv run pytest                 # incl. integration tests against a live CW
```

Integration tests create `pytest-cw-mcp-DELETEME-*` shelves and clean them up; they do not modify existing shelves or books.

## License

[Apache License 2.0](LICENSE).
