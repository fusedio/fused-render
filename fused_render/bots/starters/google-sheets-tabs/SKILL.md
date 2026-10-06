---
name: google-sheets-tabs
description: Read, write and style worksheet tabs of the user's Google Sheets with a saved service-account key — list tabs, read values, append/replace rows, create, rename and delete tabs, list comments, and format cells (colors, bold, borders, merges, column widths, freezing, conditional formatting, header preset, clear formatting).
---

# Google Sheets Tabs

Two Python files beside `index.html`, each with one `main(**params)`. Both
return a JSON dict and never raise: failures come back as
`{"error": "...", "action": "..."}`, so check `error` first.

Shared arguments:

- `doc`: a Google Sheets URL, a bare spreadsheet id, **or the name of a saved
  spreadsheet** (see `list_docs`). Every spreadsheet that is used is saved to
  the library (`.fused/data/sheets.json`).
- `tab`: a worksheet title (case-insensitive; exact, then substring) or its id
  (the `gid` in the URL).
- `range`: an A1 range on that tab: `A1:D10`, `B:C` (columns), `2:5` (rows),
  `C3`, `A2:D` (open-ended), or empty for the whole tab. `'Sheet 1'!A1:B2` also
  works, and then the tab in the range is used when `tab` is empty.

Setup is done once by the user in the page (paste a service-account key).
`sheets.py` `status` reports whether it is done and gives the account `email`.
A 403/404 on a spreadsheet means it is not shared with that email; ask the user
to share it as an Editor.

## sheets.py

Values, tabs, library and comments. `main(action=..., ...)`.

- **Changes:** `save_key`/`disconnect` write or delete `.fused/data/service_account.json`;
  `add_doc`/`rename_doc`/`remove_doc` edit the library; `create_tab`, `append`,
  `write_tab`, `rename_tab` and `delete_tab` change the Google Sheet
  (`delete_tab` permanently removes the tab and its data). Everything else is read-only (any
  Google call also refreshes the library entry's `last_used`).
- **Args:** `action` (default `status`), `doc`, `tab`, `range`, `title`,
  `content`, `key_json`, `raw: bool` (store text exactly, no formula parsing),
  `limit: int` (rows for `read_tab`, default 1000, max 10000),
  `resolved: bool` (include resolved comments).

| action | params | returns |
| --- | --- | --- |
| `status` | — | `{connected, email, docs:[{id,name,title,url,last_used}]}` |
| `save_key` | `key_json` | `{ok, email}` (verifies the key with Google) |
| `disconnect` | — | `{ok}` |
| `list_docs` | — | `{count, docs:[{id,name,title,url,added_at,last_used}]}` |
| `add_doc` | `doc`, `title` (optional friendly name) | `{ok, doc, tabs}` |
| `rename_doc` | `doc`, `title` (new name) | `{ok, doc}` |
| `remove_doc` | `doc` | `{ok, removed}` (library only) |
| `list_tabs` | `doc` | `{doc_id, title, url, tabs:[{id,title,index,type,hidden,rows,cols,frozen_rows,preview,url}]}` |
| `read_tab` | `doc`, `tab`, `range`, `limit` | `{tab, range, rows, cols, truncated, values:[[…]], url}` |
| `create_tab` | `doc`, `title`, `content` | `{ok, tab, written_rows, url}` |
| `append` | `doc`, `tab`, `content` | `{ok, tab, appended_rows, updated_range, url}` |
| `write_tab` | `doc`, `tab`, `content`, `range` | clears the tab (or range), writes: `{ok, tab, written_rows, updated_range, url}` |
| `rename_tab` | `doc`, `tab` (current id or title), `title` (new name) | `{ok, tab, old_title, url}` |
| `delete_tab` | `doc`, `tab` (gid or **exact** title) | `{ok, deleted:{id,title,…}, remaining:[{id,title}], url}` |
| `list_comments` | `doc`, `resolved` | `{doc_id, url, count, comments:[{id,content,author,created,resolved,quoted,anchor,replies}]}` |

`content` is rows as TSV, CSV, a JSON array of arrays, or a JSON array of
objects (keys become the header row). Comments need the Drive API enabled.

`delete_tab` sends one `batchUpdate` `deleteSheet` request and cannot be
undone from here (the sheet's version history can restore it). It only accepts
the gid or the exact title, never a substring. It refuses, with an `error`, to
delete the spreadsheet's only tab or its last visible one. `rename_tab`
refuses a name that another tab already has. Ask the user before you call
either one.

Examples:

- `main(action="append", doc="Team budget", tab="Expenses", content="Coffee,4.50,2026-09-29")`
- `main(action="rename_tab", doc="Team budget", tab="Sheet1", title="Expenses 2026")`
- `main(action="delete_tab", doc="Team budget", tab="Old drafts")`

## styling.py

Cell and sheet formatting. Each call is one `spreadsheets.batchUpdate`
(repeatCell, updateBorders, mergeCells/unmergeCells, updateDimensionProperties,
autoResizeDimensions, updateSheetProperties, addConditionalFormatRule,
setBasicFilter). `main(action=..., doc, tab, range, ...)`.

- **Changes:** every action changes the Google Sheet's formatting or layout
  (never its values). Nothing local changes except the library's `last_used`.
- **Colors** (`bg_color`, `text_color`, `color`): hex `#ff0000` / `#f00`,
  `rgb(255,0,0)`, or a name: black, white, red, green, blue, yellow, orange,
  purple, pink, gray, lightgray, darkgray, cyan, teal, magenta, brown, navy,
  lightblue, lightgreen, lightyellow, lightred, lightorange, lightpurple,
  darkgreen, darkred, darkblue, gold, silver, lime, maroon, olive, indigo,
  violet. `none` removes a background.
- **Flags** `bold`, `italic`, `underline`, `strikethrough` are strings:
  `true`/`false`, empty = leave unchanged.
- **Return shape (all actions):**
  `{ok: true, action, tab: {id, title}, range: "'Tab'!A1:D1", applied: [str], requests: int, url}`
  — `applied` lists what was set in plain words.

| action | args used | does |
| --- | --- | --- |
| `format_range` | `range`, `bg_color`, `text_color`, `bold`, `italic`, `underline`, `strikethrough`, `font_size: int` (pt, 0 = unchanged), `font_family`, `h_align` (left/center/right), `v_align` (top/middle/bottom), `wrap` (wrap/overflow/clip, or true/false), `number_format` | Formats the range; only the given args change. `number_format` is a preset (number, integer, percent, currency, date, time, datetime, scientific, text, automatic) or a Sheets pattern like `#,##0.0` / `dd mmm yyyy`. At least one arg is required. |
| `set_borders` | `range`, `style` (solid, medium, thick, dashed, dotted, double, none; default solid), `color` (default black), `sides` (all [default], outer, inner, horizontal, vertical, or a comma list of top, bottom, left, right, inner_horizontal, inner_vertical) | Draws (or with `none` removes) borders. |
| `merge_cells` | `range` (required), `merge_type` (all [default], columns, rows), `unmerge: bool` | Merges the range, or with `unmerge=true` splits every merge inside it. |
| `resize` | `range` (`A:C`, `B`, `1:5`, `3`; empty = every column), `pixel_size: int`, `auto_fit: bool`, `dimension` (columns/rows; only needed for a cell range like `A1:C3`) | Sets column widths / row heights in pixels, or auto-fits them to the content. One of `pixel_size` or `auto_fit` is required. |
| `freeze` | `rows: int`, `cols: int` (0 unfreezes, -1 = leave as is) | Freezes the top rows / left columns. `range` is ignored. |
| `conditional_format` | `range`, `condition`, `value`, `bg_color`, `text_color`, `bold` | Adds a rule (on top of existing ones). `condition`: greater_than, greater_or_equal, less_than, less_or_equal, equals, not_equals, between / not_between (`value` "10,20"), contains, not_contains, starts_with, ends_with, text_equals, empty, not_empty, is_email, is_url, date_before / date_after / date_equals (`value` a date or today, yesterday, tomorrow, past_week, past_month, past_year), formula (`value` like `=$B2>100`). Also accepts raw API types like `NUMBER_GREATER`. |
| `style_header` | `range` (optional; default row 1 across the tab), `bg_color` (default `#188038`), `text_color` (default white), `basic_filter: bool` | One-call header preset: bold, colored background, white text, vertically centred; freezes through the header's last row; auto-fits the header's columns; optionally adds a basic filter from the header down. |
| `clear_format` | `range` (empty = whole tab) | Removes all cell formatting (colors, fonts, borders, alignment, number formats), keeping values. Merges and conditional rules stay. |

Examples:

- `main(action="format_range", doc="Team budget", tab="Expenses", range="A1:D1", bg_color="#ff0000", text_color="white", bold="true")`
- `main(action="set_borders", doc="Team budget", tab="Expenses", range="A1:D20", style="thin", color="gray", sides="all")`
- `main(action="merge_cells", doc="Team budget", tab="Summary", range="A1:F1")`
- `main(action="resize", doc="Team budget", tab="Expenses", range="A:D", auto_fit=True)`
- `main(action="freeze", doc="Team budget", tab="Expenses", rows=1, cols=1)`
- `main(action="conditional_format", doc="Team budget", tab="Expenses", range="C2:C", condition="greater_than", value="100", bg_color="lightred")`
- `main(action="style_header", doc="Team budget", tab="Expenses")`
- `main(action="clear_format", doc="Team budget", tab="Expenses", range="A1:Z100")`

## MCP tools (mcp.toml)

`sheets_status`, `sheets_set_key`, `sheets_list_docs`, `sheets_add_doc`,
`sheets_rename_doc`, `sheets_remove_doc`, `sheets_list_tabs`,
`sheets_read_tab`, `sheets_create_tab`, `sheets_append_content`,
`sheets_write_tab` and `sheets_list_comments` call `sheets.py`.
`sheets_rename_tab` and `sheets_delete_tab` also call `sheets.py`, and they
are marked `[approval]` because they change the spreadsheet's tabs. The styling
tools call `styling.py` and are marked `[approval]` because they change the
sheet: `sheets_format_range`, `sheets_set_borders`, `sheets_merge_cells`,
`sheets_resize`, `sheets_freeze`, `sheets_conditional_format`,
`sheets_style_header`, `sheets_clear_format`.
