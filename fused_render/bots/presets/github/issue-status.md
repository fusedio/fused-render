# GitHub issue status and linked fixes
trigger: is this fixed, status of the issue, issue #, what happened with the issue, github issue, has this been fixed, known bug on github, is there a fix for

1. With an issue URL or number, goto https://github.com/<owner>/<repo>/issues/<number>. Without one, goto https://github.com/<owner>/<repo>/issues?q=is%3Aissue+<key words> (public repos need no login; `login` if a private one shows a sign-in page), `read` the first 15 results, and open the best match; pick "Sort: Most commented" for a widespread bug.
2. `read` the issue: title, state (Open, or Closed as completed / not planned / duplicate), author, labels, milestone, assignee, and the first comment in full.
3. Scroll and `read` the comments: for long issues the first 10, the last 10, and any from maintainers (the "Member" or "Contributor" badge). Note the "Development" box in the right sidebar: linked PRs and their state.
4. Open at most 2 linked PRs and `read` the header: merged or not, when, into which branch. If merged, goto https://github.com/<owner>/<repo>/releases and `read` the first screen to find the first release after the merge date, or the one whose notes mention the PR number.
5. If no PR is linked, goto https://github.com/<owner>/<repo>/pulls?q=is%3Apr+<issue number> once and `read` the results.
6. Do not comment, react, close or subscribe. Report: the issue title and link, state and reason, what the bug or request is (one paragraph), the fix status (merged PR link and release version, or open PR link, or no fix yet), workarounds from the comments with the commenter's name, and linked duplicates.
