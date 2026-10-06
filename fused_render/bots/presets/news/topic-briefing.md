# Google News weekly topic briefing
trigger: news this week on, this week in, briefing on, what's new in, roundup of, news roundup, catch me up on

1. goto https://news.google.com/search?q=<topic>+when:7d&hl=en-US&gl=US&ceid=US:en (narrow with "exact phrase" quotes, source:reuters, or -word to exclude). For a standing section (Business, Technology, Science, Health, Sports, World) you may instead click that tab on https://news.google.com/home. Dismiss the consent dialog; captcha → `login`.
2. `read` the first two screens (about 25 results): headline, outlet, age. Merge duplicates: several headlines about one event count as one story.
3. Rank the stories by how many outlets cover them and by relevance to the task; keep the top 6.
4. Open one article per story (prefer AP, Reuters or the specialist outlet), close popups, `read` the first screen. Paywalled: use what loads and say so.
5. Report 6 bullets, newest first: what happened, why it matters for the topic, outlet, date, link; end with one line on the week's theme. If the user asked for more than 10 items or a weekly deliverable, `save` as news-<topic>.md. If this is a recurring ask, `offer` a weekly briefing app.
