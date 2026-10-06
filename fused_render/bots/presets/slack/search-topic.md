# Slack search messages
trigger: search slack, who said, slack messages about, find on slack, what did we decide about, when did we discuss, slack history, look up on slack

1. goto https://app.slack.com/client and `login` if a sign-in page, workspace picker, 2FA or captcha appears. Click the search box at the top (or press Ctrl+G; Cmd+G on Mac).
2. Type 2-4 distinctive words and submit. Add the modifiers the task implies: from:@name, in:#channel, with:@name (DMs and threads with that person), after:YYYY-MM-DD, before:YYYY-MM-DD, during:<month>, has:link, has:pin, is:thread, -word to exclude, and quotes for an exact phrase.
3. On the results page click "Messages" in the left column. Use "Filters" (date, channel, person) or the sort switch between "Most relevant" and "Most recent" when the task says latest. `read` the first 20 results: snippet, channel, person, date.
4. Open the 3 most relevant results (click the result, then "View in channel" or its "N replies" link) and `read` the surrounding messages so quotes have context. If nothing fits, retry once with different words and once without modifiers; stop after 3 queries.
5. The permalink of a message is the href of its timestamp link (…/archives/C…/p…); read it from the element rather than using "Copy link".
6. Report the answer in two or three sentences first, then the evidence: up to 5 quotes with channel, person, date and permalink, and the exact query that found them. If the task wants everything on the topic and there are over 10 hits, `save` them as slack-search.md.
