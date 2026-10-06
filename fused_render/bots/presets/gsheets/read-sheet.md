# Read a Google Sheet
trigger: read the google sheet, whats in the spreadsheet, google sheets tab, read the worksheet, open my google sheet, spreadsheet values, totals in the sheet

1. Resolve the spreadsheet: a URL or id from the task, or a saved name from `sheets_list_docs` (case-insensitive; exact name first, then substring). If nothing matches, ask which one.
2. Call `sheets_list_tabs` with `doc`: every worksheet with its id, title, row and column counts.
3. Call `sheets_read_tab` with `doc`, the `tab` the task names (title or gid; default the first), and a `range` when only part matters (`A1:D50`, `B:C`, `2:20`). Leave `range` empty for the whole tab; keep `limit` at the default so a huge sheet does not flood the step.
4. Do not call any write or styling tool in a reading task (`sheets_append_content`, `sheets_write_tab`, `sheets_create_tab`, `sheets_format_range` and the rest).
5. Work the numbers yourself from the returned rows: totals, counts by a column, min and max, blanks. Treat the first row as the header unless it plainly is data.
6. Report: the spreadsheet title and link, the tabs (one line each), and the answer with the exact cells it rests on (tab!A1 style). More than 30 rows asked for? `save` them as sheets-<name>-<tab>.csv and keep the chat to the summary.
7. The tools missing from APP TOOLS? Browse the sheet at its URL instead (use `login` on a sign-in page), pick the tab at the bottom, and read without typing into any cell.
