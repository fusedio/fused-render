# Gmail sent emails still waiting for a reply
trigger: waiting on a reply, no reply yet, follow up on, emails i sent, did they reply, who hasn't replied, sent mail, unanswered emails

1. If an APP TOOL for mail is listed, use it first. Otherwise goto https://mail.google.com/mail/u/0/#search/in%3Asent+newer_than%3A14d (narrow with to: or subject: from the task, widen to 30d if asked). On a sign-in page use `login`.
2. `read` the rows: the recipient ("To: name"), subject, snippet, date, and the message count shown after the names (no number means one message).
3. Threads with one message are unanswered: collect them. For threads with 2 or more messages open at most 5 and `read` who sent the last one; if it is the user, the thread is still waiting.
4. Skip automated recipients (noreply, calendar invitations, receipts, support tickets) unless the task asks for them.
5. Report a table sorted oldest first: recipient, subject, sent date, days waiting, link; add a one-line follow-up suggestion for the top 5. Nothing is sent. Over 15 rows: `save` gmail-awaiting-reply.md and keep the chat to the top 8.
