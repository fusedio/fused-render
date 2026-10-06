# Google Docs Tabs

A Render App that connects to your Google Docs and manages **document tabs**:
tell it "create a tab called Meeting notes and add: …" and it creates the tab
in your doc and fills it in. It can also append to or overwrite an existing tab, list tabs,
read a tab back, and list, add, reply to, resolve or delete comments on the doc.

## Setup (once)

The app acts as a Google **service account**: a robot identity with its own
email. You paste its key once and share docs with that email. No sign-in
pop-ups, no OAuth consent screen.

1. Enable the **Google Docs API** and the **Google Drive API** (used for comments) in Google Cloud Console.
2. IAM & Admin → Service accounts → **Create service account** (no roles needed).
3. Open it → Keys → **Add key → Create new key → JSON**. Paste the file's
   contents into the app and click **Save key**. The app checks the key with
   Google right away and shows the account email in the header.
4. In any Google Doc you want the app to edit: **Share** → add that email as
   an **Editor**.

Everything is stored under this app's `.fused/data/` (the key, the saved documents library).
Nothing leaves your machine except the Docs API calls.

## Use

- Paste a Google Docs link and click **Load**. Tabs appear on the left.
- Every doc you load is saved to a **documents library** shown under the link
  box. Click a name to switch docs, ✎ to give it a friendly name, × to forget
  it. You can also type a saved name instead of a link. Bots can refer to docs
  by these names too (`docs_list_docs`, `docs_add_doc`, `docs_rename_doc`,
  `docs_remove_doc` in `mcp.toml`).
- Type a plain-English instruction and click **Run** (⌘/Ctrl+Enter):
  - `create a tab called Meeting notes and add: # Agenda - roadmap - hiring`
  - `add "Follow up Friday" to the Meeting notes tab`
  - `create a tab called Summary and add a short summary of …` (the AI writes it)
- Or use the form: title + content → **Create tab**, or select a tab → **Append**.

Content supports light markdown: `# `/`## `/`### ` headings, `- ` bullets, `1. ` lists.

Instructions are parsed with `fused.ai` (Claude) when available, otherwise a
built-in phrase matcher handles the common forms. The bot can drive the same
actions through `docs.py`; see `BOT.md`.

## Files

- `index.html` — the page (setup → work).
- `docs.py` — `main(action=…)`: status, save_key, disconnect, list_docs, add_doc, rename_doc, remove_doc, list_tabs, read_tab, create_tab, append,
  write_tab, list_comments, add_comment, reply_comment, resolve_comment, reopen_comment, delete_comment, command.
- `mcp.toml` — the same actions published as `docs_*` MCP tools.
