# Google Doc comments
trigger: google doc comments, comments on the google doc, open comments in the doc, resolve the comments in the google doc, reply to the doc comment, comment on the google doc

1. Resolve the doc (URL, id or saved name via `docs_list_docs`) and call `docs_list_comments` with `doc`; pass `resolved: true` only when the task asks about resolved ones too.
2. For each comment note the author, date, the quoted text it is anchored to, the replies, and whether it is resolved. Group them by tab or section when the doc is long.
3. If the task is a review, read the tab around the busiest comments with `docs_read_tab` so your summary says what each thread is actually about.
4. Writes are separate tools and each stops for approval: `docs_add_comment` (new thread), `docs_reply_comment` (answer a thread), `docs_resolve_comment` / `docs_reopen_comment`, `docs_delete_comment`. Call one only when the task says so in plain words, and write the exact text you will post in `thought` first.
5. Never resolve or delete a comment someone else opened unless the task names it; bulk "resolve all" asks the user to confirm the count first with `ask`.
6. Report: open comments as a short list (author, the quoted text, the ask), who is waiting on whom, and the doc link. More than 20 comments? `save` the full list as docs-<name>-comments.md and keep the chat to the open ones.
