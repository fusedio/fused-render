# Hacker News search on a topic
trigger: search hn, hacker news about, hn search, has hn discussed, hn threads on, what does hn think about, hn opinions on

1. goto https://hn.algolia.com/?query=<words>&sort=byPopularity&type=story&dateRange=all&prefix=false&page=0 with 1-3 words (an exact product name in quotes). For "recent" set dateRange=pastWeek, pastMonth or pastYear, or sort=byDate for newest first. For everything from one site, goto https://news.ycombinator.com/from?site=<domain> instead.
2. `read` the results: each row has title, points, author, age, comment count, and the HN thread link. Read the first 20 (one page). If they are off-topic, reword once; stop after 3 queries.
3. Pick the 3 threads with the most comments that fit the task and goto each https://news.ycombinator.com/item?id=<id>; `read` the top 10 root comments (leftmost; indented ones are replies). Skip threads under 10 comments unless nothing better exists.
4. If the task asks what the articles said, open at most 2 article links and `read` the first screen.
5. Report: the threads as a table (title, date, points, comments, HN link, article link), then the main positions people took with one short quote and the commenter's name each, and the thread dates so the user knows how old the view is. Over 10 threads: `save` as hn-search.md and keep the chat answer to the top 5.
