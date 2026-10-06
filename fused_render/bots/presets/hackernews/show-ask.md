# Hacker News Show HN, Ask HN and jobs
trigger: show hn, ask hn, new launches on hn, hn jobs, hn launches, what are people building, hn questions

1. goto the list the task wants: https://news.ycombinator.com/show (Show HN above a points threshold) or https://news.ycombinator.com/shownew (the newest ones), https://news.ycombinator.com/ask or /asknew (Ask HN), https://news.ycombinator.com/jobs (YC company job ads), https://news.ycombinator.com/launches (YC company launches). `read` the first 30 rows: title, domain, points, comments, age.
2. Pick 8 rows: those matching the task's topic, else the top 8 by points.
3. For each pick goto its page (https://news.ycombinator.com/item?id=<id>) and `read` the poster's text and the top 5 root comments. For Show HN also open the project link for at most 3 and `read` the first screen.
4. For jobs, `read` the ad text (role, location, remote or not, how to apply); job posts have no comments.
5. Report 8 bullets: what it is in one line, who posted it, how it was received (points, comment count, the main praise or objection), the HN link and the project link. Over 10 asked for: `save` as hn-show-ask.md and keep the chat answer to the top 5.
