# Add to a Google Doc tab
trigger: add to the google doc, append to the google doc, create a tab in the google doc, new tab in google docs, put the notes in the google doc, write it to the google doc, meeting notes tab

1. Resolve the doc (URL, id or saved name via `docs_list_docs`) and call `docs_list_tabs` so you know which tabs exist before changing anything.
2. Work out the target: an existing tab the task names (append to it with `docs_append_content`), or a tab to create (`docs_create_tab` with `title`, `content`, and `parent` when it should nest under another tab). Never pick `docs_write_tab`: it replaces the whole tab, so use it only when the task says "replace" or "overwrite".
3. Draft the content first. Light markdown works: `# ` / `## ` headings, `- ` bullets, `1. ` lists. Gather anything the task refers to (an earlier answer, a page you read, a file under FILES) before drafting.
4. Show the user, in `thought`, the tab name and the content you are about to add, then call the write tool once. It stops for approval; do not retry while the card is up.
5. After the tool returns `ok`, call `docs_read_tab` on that tab and check your text landed where expected.
6. Report: the tab name, how many lines went in, and the doc link. If the tool returned an `error`, quote it and stop: a 403 or 404 means the doc is not shared with the service account's email.
