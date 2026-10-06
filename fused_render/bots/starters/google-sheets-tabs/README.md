# Google Sheets Tabs

A Render App for working with the **worksheet tabs** of your Google Sheets.
It's the companion to Google Docs Tabs and uses the same service-account key.
You can list a spreadsheet's tabs, read a tab as a sortable and filterable
grid, append rows, replace a tab's values (or one range), create a new tab,
and read the spreadsheet's comments.

## Setup (once)

1. Enable the **Google Sheets API** in Google Cloud Console. To see comments,
   also enable the **Google Drive API**.
2. Create a **service account** (no roles needed) and download a **JSON key**.
   If you already set one up for Google Docs Tabs, reuse that key.
3. Paste the key into the app and click **Save key**. The app checks the key
   with Google straight away and shows the account's email in the header.
4. In each spreadsheet you want to use, click **Share** and add that email as
   an **Editor**.

The key and the spreadsheets library are stored in this app's `.fused/data/`.
The only data that leaves your machine is the Google API calls.

## Use

- Paste a Sheets link and click **Load**. The spreadsheet's tabs are listed on
  the left, and the selected tab's values appear as a grid. You can filter
  rows, click a column header to sort, and choose whether the first row is a
  header.
- Every spreadsheet you load is saved to the **library**. Click a name to
  switch to it, ✎ to rename it, or × to forget it. Bots can also refer to a
  spreadsheet by its name.
- **Write to the sheet** has three modes:
  - **Append rows** adds rows after the last row of the selected tab.
  - **Replace values** clears the selected tab (or just the range you give)
    and writes new values.
  - **New tab** creates a worksheet, optionally filled with rows.

  You can paste rows as TSV (copied from a spreadsheet), CSV, or JSON.
  **Edit a copy** loads the current tab into the editor so you can change it
  and write it back.
- **Styling** formats cells in the sheet itself. Pick a tab (by default the
  selected one), type a range (`A1:D10`, `B:B`, `1:1`; empty means the whole
  tab), and set a background color, a text color and bold. Colors can be
  typed as hex or names, or picked with the swatch. **Apply** changes only the
  fields you filled in. **Style header row** makes row 1 bold with a colored
  background and white text, freezes it and auto-fits the columns.
  **Clear formatting** removes all formatting from the range and keeps the
  values. Bots can do more (borders, merges, sizes, freezing, conditional
  rules, number formats); see `SKILL.md`.
- The **Comments** view lists the spreadsheet's comments and their replies.
  Tick **Include resolved** to show resolved ones too.

The URL records the whole view: spreadsheet, tab, filter, sort, mode, draft
and the Styling fields. A copied link reopens the app exactly as you left it. The key is never
put in the URL.

## Files

- `index.html`: the page.
- `sheets.py`: `main(action=…)`. Actions are `status`, `save_key`,
  `disconnect`, `list_docs`, `add_doc`, `rename_doc`, `remove_doc`,
  `list_tabs`, `read_tab`, `create_tab`, `append`, `write_tab`,
  `rename_tab`, `delete_tab` and `list_comments`. `delete_tab` won't delete
  a spreadsheet's only tab or its last visible tab. It also needs the tab's
  exact title or gid.
- `styling.py`: `main(action=…)` for formatting. Actions are `format_range`,
  `set_borders`, `merge_cells`, `resize`, `freeze`, `conditional_format`,
  `style_header` and `clear_format`.
- `mcp.toml`: the same actions published as `sheets_*` MCP tools. The styling
  tools and `sheets_rename_tab` / `sheets_delete_tab` are marked `[approval]`.
- `SKILL.md`: the reference for bots, covering both files. `BOT.md` is the
  older reference for `sheets.py`.
