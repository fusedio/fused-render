---
name: apple-notes
description: Read and search the user's Apple Notes on this Mac through Notes.app itself (no iCloud web, no network, no Shortcuts) — folders, title or full-text search, read a note's plain text, notes edited recently — create a new note, and append lines to an existing note.
approve: [notes.py]
---

# Apple Notes

One Python file beside `index.html`, `notes.py`, with one `main(**params)`.
It drives Notes.app with JavaScript for Automation. FusedRender itself cannot get
macOS's "control Notes" prompt (its Info.plist lacks NSAppleEventsUsageDescription),
so `notes.py` builds a tiny helper applet, `.fused/data/NotesBridge.app`, and runs
the script inside it; macOS asks once "NotesBridge wants to control Notes" and the
user must click OK (results carry `"backend": "bridge"`). If that is refused,
reads fall back to the Notes database (read-only, `"backend": "sqlite"`) and
`create`/`append` return an `error` explaining how to allow it in System Settings
> Privacy & Security > Automation; do not retry until the user has. A call that
times out is usually that prompt waiting for an answer.

It returns a JSON dict and never raises: failures come back as
`{"error": "...", "action": "..."}`, so check `error` first. Note ids look like
`x-coredata://<uuid>/ICNote/p<n>` on every backend. Password-protected notes
return an empty body until the user unlocks them in Notes and refuse `append`.
Note bodies come back as plain text (checklists as lines, tables flattened).

## notes.py

`main(action=..., query="", folder="", id="", title="", body="", text="", days=7, limit=20, in_body=False)`

- **Changes:** `create` writes a new note; `append` adds lines to the end of an existing note (attachments are kept). Both save at once in Notes, with no undo from here. Everything else is read-only.
- **Args:** `action` (default `status`); `query` for `search`; `folder` to narrow `search`/`recent` or to place a `create`; `id` or `title` for `read` and `append`; `title` and `body` (plain text, newlines kept) for `create`; `text` (plain text, each line becomes a paragraph) for `append`; `days` for `recent`; `limit` 1-100; `in_body: bool` makes `search` also match note text (slower on big libraries).
- **append by title:** an exact title match, else a title fragment exactly one note contains (Recently Deleted is skipped). Several matches return `{error, candidates:[…]}` and change nothing: call again with the chosen `id`.

| action | params | returns |
| --- | --- | --- |
| `status` | — | `{ok, app, version, accounts:[…], notes, folders, backend}` |
| `folders` | — | `{folders:[{name, account, notes, id}]}` |
| `search` | `query`, `folder`, `in_body`, `limit` | `{query, count, notes:[{id, title, folder, modified, created, snippet}]}` newest first |
| `read` | `id` or `title` | `{note:{id, title, folder, modified, created, body, shared, password_protected}}` |
| `recent` | `days`, `folder`, `limit` | `{days, count, notes:[{id, title, folder, modified, created, snippet}]}` newest first |
| `create` | `title`, `body`, `folder` | `{ok, note:{id, title, folder, modified, created, snippet}}` |
| `append` | `id` or `title`, `text` | `{ok, appended, note:{id, title, folder, modified, created, snippet}}` |

Examples: `{"action":"search","query":"packing list"}` · `{"action":"read","id":"x-coredata://…/ICNote/p911"}` ·
`{"action":"recent","days":3,"limit":10}` · `{"action":"create","title":"Groceries","body":"milk\neggs","folder":"Notes"}` ·
`{"action":"append","title":"Groceries","text":"bread"}`.
