# Read a Google Doc
trigger: read the google doc, what does the google doc say, google docs tab, read the tab, open my google doc, whats in the google doc, google doc summary

1. Resolve the doc: a URL or id from the task, or a saved name from `docs_list_docs` (case-insensitive; exact name first, then substring). If nothing matches, ask which doc.
2. Call `docs_list_tabs` with `doc`. It returns every tab with id, title, depth and a short preview; nested tabs carry a `parent`.
3. If the task names a tab, call `docs_read_tab` with `doc` and that `tab` (title or id). Otherwise read the first tab, and more tabs only if the task asks about the whole doc; stop after 5 tabs and say which ones you skipped.
4. Do not call any write tool (`docs_create_tab`, `docs_append_content`, `docs_write_tab`, `docs_run_command`) in a reading task.
5. Answer the question from the text: quote the exact lines that matter, name the tab each quote came from, and say if a tab was empty.
6. Report: the doc title and link, the tab list (one line each), the answer with its quotes. More than a screen of text asked for? `save` it as docs-<name>-<tab>.md and keep the chat to the answer.
7. The tools missing from APP TOOLS? Browse the doc at its URL instead (use `login` on a sign-in page) and read tab by tab from the left pane; never type into the doc.
