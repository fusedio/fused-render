# GitHub PRs and issues waiting on me
trigger: my pull requests, prs waiting on me, review requests, my github, assigned issues, github digest, my prs, github to-do

1. goto https://github.com/pulls/review-requested (`login` if a sign-in, 2FA or captcha page appears). `read` the list: repo, number, title, author, age, the check icon (green tick, red x, yellow dot) and review state per row. Pages hold 25 rows; read page 2 only if the task asks for everything.
2. goto https://github.com/pulls (PRs I opened) and `read` the same fields. If the task says everything, also https://github.com/pulls/assigned and https://github.com/pulls/mentioned.
3. goto https://github.com/issues/assigned and `read`: repo, number, title, labels, last activity; then https://github.com/issues/mentioned.
4. Narrow any list by typing in its filter box when the task names a repo or window: repo:owner/name, is:open draft:false, review:changes_requested, updated:>YYYY-MM-DD.
5. Open at most 3 PRs: those with a red x, "Changes requested", or a comment newer than my last activity. On each, `read` the merge box (failing check names) and the last comments at the bottom of the Conversation tab. Do not approve, merge or comment.
6. Report three lists, To review, Mine, Assigned issues, each row as repo#number title (author, age, checks, review state) with the link; blocked or failing rows first. Over 15 rows in total: `save` the full tables as github-queue.md and keep the chat answer to the rows that need action.
