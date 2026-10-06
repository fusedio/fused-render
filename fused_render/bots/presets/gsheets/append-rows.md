# Add rows to a Google Sheet
trigger: add a row to the google sheet, append to the spreadsheet, log it in the google sheet, add rows to google sheets, put it in the spreadsheet, record it in the sheet

1. Resolve the spreadsheet (URL, id or saved name via `sheets_list_docs`) and call `sheets_list_tabs`, then `sheets_read_tab` on the target tab with `range` `1:1` to get the header row, plus the last few rows (`sheets_read_tab` with the whole tab and read the tail) so new rows match the existing columns and date format.
2. Build the rows in header order, one value per column, blanks where the sheet has none. Dates in the format already used in that column; numbers without currency signs unless the column has them.
3. Show the user, in `thought`, the tab and the rows exactly as they will be appended (a small table), then call `sheets_append_content` once with `doc`, `tab` and the rows as `content`. It stops for approval; do not retry while the card is up.
4. Never use `sheets_write_tab` for an "add": it replaces the whole worksheet. Use it only when the task says "replace" or "overwrite", and read the tab first so you can report what it replaced.
5. After the tool returns `ok`, read the tail of the tab again and check the rows landed under the right headers.
6. Report: how many rows went in, on which tab, and the spreadsheet link. If the tool returned an `error`, quote it and stop: a 403 or 404 means the spreadsheet is not shared with the service account's email.
