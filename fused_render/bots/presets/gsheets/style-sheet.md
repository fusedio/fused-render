# Style a Google Sheet
trigger: format the google sheet, style the header row, spreadsheet formatting, freeze the header in the sheet, highlight cells in the sheet, bold the header in google sheets, column widths in the sheet

1. Resolve the spreadsheet (URL, id or saved name via `sheets_list_docs`), call `sheets_list_tabs`, then `sheets_read_tab` on the tab so you know the header row and the data extent before formatting anything.
2. Map the request to tools: a tidy header is `sheets_style_header` (bold, fill, frozen first row in one call); colors, bold and alignment on a range is `sheets_format_range`; `sheets_set_borders`, `sheets_merge_cells`, `sheets_resize` (column widths, row heights), `sheets_freeze`, `sheets_conditional_format` (highlight cells by rule), `sheets_clear_format` to undo. Ranges are A1 style on that tab.
3. Keep formatting to the data extent you read: do not style whole columns to row 1000 when the data ends at row 40, except for widths and frozen rows.
4. Write the plan in `thought` first (tab, range, what changes), then call the tools one at a time; each stops for approval. At most 6 formatting calls per task; a bigger makeover asks the user to confirm the list with `ask` first.
5. Never change cell values while styling, and never merge over cells that hold data unless the task says so.
6. Report: what you formatted (tab and range per change) and the spreadsheet link; if a tool returned an `error`, quote it and stop.
