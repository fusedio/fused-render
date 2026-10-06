# YouTube search on a topic
trigger: search youtube, find videos about, best videos on, youtube search, videos about, youtube videos on, tutorial on youtube, youtube for

1. goto https://www.youtube.com/results?search_query=<query> . Click "Reject all" on a consent dialog if it appears.
2. If the task wants recent videos, click "Filters" and pick an Upload date ("Today", "This week", "This month", "This year"); for long-form pick Duration "Over 20 minutes"; "Prioritize" offers Relevance or Popularity only (there is no newest-first sort any more and sp= URL tricks no longer work).
3. `read` the results: title, channel, length, views, age. Scroll twice to collect about 15; skip "Sponsored" rows and the Shorts shelf unless the task asks for Shorts.
4. Pick the 5 best for the task (relevance, channel size, recency, a length that fits the question). Open each, click "…more" and `read` the description; for the top 2 also click "Show transcript" and `read` the first screen to confirm what the video covers. `back` after each.
5. Report a ranked list of 5: title, channel, length, views, upload date, one line on why it fits, link; then the other 10 as one-liners with links. If the task asks for more than 10 in detail, `save` as youtube-search.md.
