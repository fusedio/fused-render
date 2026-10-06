# GitHub PR summary
trigger: summarize the pr, summarize this pull request, what does this pr do, review summary for, pr #, review this pr, look at the pull request, github pr

1. goto the PR (https://github.com/<owner>/<repo>/pull/<number>); `login` if a sign-in, 2FA or captcha page appears. `read` the header and description: title, author, base and head branches, labels, reviewers with their state, the merge box (checks passing or failing, conflicts, "Changes requested" or "Approved") and the size line (commits, files, additions, deletions).
2. goto https://github.com/<owner>/<repo>/pull/<number>/files. `read` the file tree on the left, then the diff of the 5 largest or most central files; click "Load diff" on collapsed files that matter. Never tick "Viewed". Diffs over about 1,500 lines: read the file list and the first 3 files only, and say so.
3. Back on the Conversation tab, scroll to the bottom and `read` the review comments and threads; note which are marked Resolved and which are open, and who is waiting on whom.
4. If a check is failing, goto https://github.com/<owner>/<repo>/pull/<number>/checks and `read` the failing job's summary (the first error lines).
5. If the description links an issue ("Fixes #N" or the Development box), open it and `read` its first comment so the report says what problem the PR solves.
6. If the task asks to comment, approve or request changes: type the text in the review box, stop before "Submit review" or "Comment", show the user the exact text and the verdict, and click only after approval.
7. Report: what the PR changes and why (3 lines), files touched grouped by area, risky parts (migrations, deleted code, config, public API changes), what reviewers asked for and what is still open, check status and mergeability, then the PR link and the /files link.
