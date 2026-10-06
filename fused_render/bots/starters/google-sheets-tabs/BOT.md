# Google Sheets Tabs

Run `sheets.py` with `main(action=..., ...)`. Every action returns a JSON dict;
failures come back as `{"error": "..."}` (never an exception) so check that
key first. `doc` accepts a full Google Sheets URL, a bare spreadsheet id, **or
the name of a saved spreadsheet**. `tab` is a worksheet id (the `gid` in the
URL) or its title (case-insensitive, exact first, then substring).

The app keeps a **saved spreadsheets library** (`.fused/data/sheets.json`):
every spreadsheet that is used gets saved automatically under its title, and
the user can rename entries in the page. When the user says "the budget
sheet", call `list_docs` (or `status`) to see the names, then pass that name
as `doc`.

Requires a one-time setup by the user in the page: paste a Google
service-account key JSON (the same key as the Google Docs Tabs app works).
`status` tells you whether that is done and gives the service account's
`email`. A 403/404 on a spreadsheet means it is not shared with that email;
tell the user to share it as an Editor.

| action | params | returns |
| --- | --- | --- |
| `status` | — | `{connected, email, docs:[{id,name,title,url,last_used}]}` |
| `save_key` | `key_json` | `{ok, email}` (verifies the key with Google) |
| `list_docs` | — | `{count, docs:[{id,name,title,url,added_at,last_used}]}` |
| `add_doc` | `doc` (URL or id), `title` (friendly name, optional) | `{ok, doc, tabs}` (verifies the sheet opens) |
| `rename_doc` | `doc` (name/id/URL), `title` (new name) | `{ok, doc}` |
| `remove_doc` | `doc` (name/id/URL) | `{ok, removed}` (library only; the Google Sheet is untouched) |
| `list_tabs` | `doc` | `{doc_id, title, url, tabs:[{id,title,index,type,hidden,rows,cols,preview,url}]}` |
| `read_tab` | `doc`, `tab`, `range` (optional A1, e.g. `A1:D50`), `limit` (rows, default 1000) | `{tab, range, rows, cols, truncated, values:[[…]]}` |
| `create_tab` | `doc`, `title`, `content` (optional rows) | `{ok, tab:{id,title,…}, written_rows, url}` |
| `append` | `doc`, `tab`, `content` (rows) | `{ok, tab, appended_rows, updated_range, url}` |
| `write_tab` | `doc`, `tab`, `content` (rows), `range` (optional) | clears the tab (or range) and writes: `{ok, tab, written_rows, updated_range, url}` |
| `rename_tab` | `doc`, `tab`, `title` (new name) | `{ok, tab, old_title, url}` |
| `delete_tab` | `doc`, `tab` (gid or exact title) | `{ok, deleted, remaining:[{id,title}], url}`. Permanent. Refuses the only or last visible tab. Ask the user first. |
| `list_comments` | `doc`, `resolved` (bool, include resolved) | `{count, comments:[{id,content,author,created,resolved,quoted,anchor,replies:[…]}]}` |

`content` is rows in any of these forms:

- TSV, e.g. copied from a spreadsheet (used when the text has a tab character)
- CSV (`Name,Amount\nCoffee,4.50`)
- a JSON array of arrays (`[["Name","Amount"],["Coffee",4.5]]`)
- a JSON array of objects (the keys become a header row)

Values are entered like typing into Sheets (numbers, dates and `=formulas` are
parsed). Pass `raw=true` to store everything as plain text.

Comments go through the Google Drive API, so the service account's Cloud
project must have the **Drive API** enabled as well as the Sheets API. The
same actions are published as MCP tools in `mcp.toml` (one `sheets_*` tool per
action).

Example: `main(action="append", doc="Team budget", tab="Expenses", content="Coffee,4.50,2026-09-29")`.

Formatting (colors, bold, borders, merges, column widths, freezing, conditional
formatting, header preset, clear formatting) lives in `styling.py`; see
`SKILL.md` for its actions and args.
