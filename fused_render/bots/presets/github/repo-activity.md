# GitHub repo activity and releases
trigger: what changed in the repo, repo activity, recent commits, github activity this week, latest release, release notes, changelog, what shipped

1. goto https://github.com/<owner>/<repo>/pulse (public repos need no login; `login` if a private one shows a sign-in page). Click the "Period" dropdown at the top right and pick 24 hours, 3 days, 1 week or 1 month to match the task (default 1 week).
2. `read` the Pulse page: the overview counts, then "Merged pull requests", "Proposed pull requests", "Closed issues", "New issues" and "Unresolved conversations", each a list of number, title, author and age.
3. goto https://github.com/<owner>/<repo>/commits (default branch; add /<branch> for another) and `read` the first 25 commits with author and date, stopping at the period start.
4. goto https://github.com/<owner>/<repo>/releases and `read` the first screen. If a release falls in the period, click it and `read` the notes. For "what changed between v1 and v2", goto https://github.com/<owner>/<repo>/compare/<v1>...<v2> and `read` the commit list and the files-changed count.
5. Open at most 3 merged PRs, the largest or the ones the task names, and `read` their descriptions.
6. Do not star, watch or comment. Report: shipped (merged PRs and releases with version and date), in progress (open PRs), notable issues opened or closed, who did the work (top authors by count), each with links. Over 20 items: `save` the full lists as repo-activity.md and keep the chat answer to the highlights.
