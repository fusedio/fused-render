# GitHub notifications inbox
trigger: github notifications, unread github, github inbox, what's new on github, github mentions, github alerts, catch me up on github

1. goto https://github.com/notifications?query=is%3Aunread (`login` if a sign-in, 2FA or captcha page appears). If the task wants everything recent rather than unread, goto https://github.com/notifications.
2. `read` the inbox: each row has repo, a type icon (issue, PR, discussion, release, workflow run), title, the reason (review requested, mention, assign, author, subscribed, CI activity) and age. Rows group by repo when "Group by: Repository" is set. Read at most 50 rows (two screens).
3. Narrow by editing the query when the task asks: reason:review-requested, reason:mention, reason:assign, repo:owner/name, is:done, is:saved. The sidebar filters (Assigned, Participating, Mentioned, Review requested) do the same.
4. Open at most 5 rows whose reason is review requested, mention or assign, or whose check failed: click the row, `read` the newest comments or the failing job summary, then go back. Opening a row marks only that row read; never click Done, Unsubscribe, Save, "Mark as read" or "Mark all as read".
5. Report grouped by reason in this order: review requested, mentioned or assigned, CI failures, releases, everything else; each row repo#number title, who, age, link, and say which 5 were opened (and so marked read). Over 20 rows: `save` as github-notifications.md and keep the chat answer to the first three groups.
