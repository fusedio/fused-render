# X search on a topic
trigger: search x, search twitter, what are people saying about, x search, tweets about, posts about, on x about, twitter search

1. goto https://x.com/search?q=<query>&src=typed_query&f=live for newest posts (f=top for the most engaged; the page's tabs are "Top", "Latest", "People", "Media", "Lists"). Search needs login: if a sign-in page or captcha appears, use `login`.
2. Narrow with operators in the query when the task implies them: from:handle, since:YYYY-MM-DD until:YYYY-MM-DD, min_faves:50, filter:links, -filter:replies, lang:en; quotes for exact phrases. The form at https://x.com/search-advanced builds the same query.
3. On "Latest", scroll 3 times reading @handle, text, time, engagement. Then click "Top" and scroll 2 times. Note verified or official accounts and news outlets.
4. Open the 3 most engaged on-topic posts and `read` the first screen of replies for pushback; `back` after each.
5. Report: a two-line overview; the 2 to 4 main positions with 2 representative posts each (@handle, short quote, time, link); anything that reads as news rather than opinion; the search URL. If the user wants the full list, `save` as x-search.md.
