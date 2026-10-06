# Hacker News front page digest
trigger: hacker news, hn digest, what's on hn, top of hacker news, hn front page, hn today, hn headlines

1. goto https://news.ycombinator.com/ and `read` the story table: 30 rows with title, domain, points, comment count and age. Only if the task asks for more than 30, also goto https://news.ycombinator.com/news?p=2 and `read` it.
2. Pick 8 stories: the ones matching the task's topics, else the top 8 by points. Skip job ads (rows with no points) and "Launch HN" unless asked.
3. For each of the 8, open its comments page (the "N comments" link, https://news.ycombinator.com/item?id=<id>), `read` the top of the thread, and note the argument of the top two root comments (the leftmost ones; indented ones are replies). Do not open the article unless its title is unclear; the comments usually summarise it.
4. If the task asks for the articles too, open at most 3 and `read` their first screen; on a paywall or cookie wall, rely on the comments and say so.
5. Report 8 bullets: title, one-line gist, what the comments argue, points and comment count, the HN link and the article link. If the user asked for more than 10 stories, `save` the full list as hn-digest.md and keep the chat answer to the top 5.
