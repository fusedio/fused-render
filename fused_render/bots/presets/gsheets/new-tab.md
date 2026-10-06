# New worksheet tab in a Google Sheet
trigger: new tab in the google sheet, create a worksheet, add a sheet tab, new worksheet in the spreadsheet, rename the sheet tab, delete the sheet tab

1. Resolve the spreadsheet (URL, id or saved name via `sheets_list_docs`) and call `sheets_list_tabs` so you do not create a title that already exists.
2. To create: `sheets_create_tab` with `doc`, `title`, and `content` when the task gives the rows (header row first). To rename: `sheets_rename_tab` with the current `tab` and the new `title`. To delete: `sheets_delete_tab`, only when the task names that tab and after `sheets_read_tab` shows what it holds (say the row count in `thought`).
3. If the new tab should mirror an existing one (same columns), read that tab's header row with `sheets_read_tab` and `range` `1:1` and pass it as the first content row.
4. Show the user, in `thought`, the tab name and the first rows, then call the tool once; it stops for approval. Do not retry while the card is up.
5. After `ok`, call `sheets_list_tabs` again and confirm the tab is there (or gone), then, for a new tab with content, read it back once.
6. Report: the tab name, its gid and the spreadsheet link; if the tool returned an `error`, quote it and stop.
