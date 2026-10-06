# GitHub search for repos, issues and code
trigger: search github, find a repo, github issues about, is there an issue for, who else hit this error, find on github, github search, library for

1. Pick the type. Repos: goto https://github.com/search?q=<words>&type=repositories&s=stars&o=desc. Issues and PRs: https://github.com/search?q=<words>&type=issues, adding is:open, repo:owner/name, label:bug, created:>YYYY-MM-DD or sort:updated-desc as needed. Code: https://github.com/search?q=<words>&type=code needs a signed-in account; `login` if the page says so. Put exact error text in quotes after stripping paths, line numbers and hex IDs.
2. `read` the first 15 results: for repos the name, description, stars, language and last update; for issues the title, repo, state, comment count and age.
3. If results are poor, try one reworded query (fewer words, or a repo: qualifier) and one with another type; stop after 3 queries.
4. Open the 3 most relevant: for a repo `read` the About box (stars, license, last commit) and the README's first screen; for an issue `read` the first comment and the last 5, noting whether a maintainer answered or a fix was linked.
5. Do not star, watch, fork or comment. Report the best matches as bullets: name or title, one-line gist, the signal (stars and last update, or state and whether a fix exists) and the link, then the query URLs used. Over 10 results: `save` as github-search.md and keep the chat answer to the top 5.
