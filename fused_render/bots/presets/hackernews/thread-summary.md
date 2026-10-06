# Hacker News thread summary
trigger: summarize the hn thread, hn comments on, what did hn say, summarize this hacker news, item?id, hn discussion of

1. goto the thread (https://news.ycombinator.com/item?id=<id>). If the task gives a title instead of a link, goto https://hn.algolia.com/?query=<title>&type=story first and click the matching result. `read` the header: title, article link, points, poster, age, comment count.
2. Open the article link in a new tab and `read` its first two screens (on a paywall, say so and rely on the comments), then switch back to the thread tab.
3. `read` the comments from top to bottom. Root comments are the leftmost; indented ones are replies. Collapsed branches show "[N more]"; click to expand at most 3 of the largest. Threads over about 200 comments end with a "More" link; follow it at most twice.
4. Pick the 5 longest root threads; for each note the claim, the strongest reply, and whether anyone speaks first-hand (the author, an employee, or a user of the thing).
5. Report: what the article claims (2 lines), the consensus if there is one, the strongest objections, first-hand accounts, each with the commenter's name and a short quote, then the HN link and the article link. Over 300 comments: `save` the full notes as hn-thread.md and keep the chat answer to five short paragraphs.
