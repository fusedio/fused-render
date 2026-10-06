# Gmail unread inbox digest
trigger: my email, my inbox, unread email, gmail digest, what's in my inbox, new mail, catch me up on email, gmail inbox

1. If an APP TOOL for mail is listed in the prompt, use it instead of the browser. Otherwise goto https://mail.google.com/mail/u/0/#inbox ; on a Google sign-in, 2FA or "Verify it's you" page use `login` (never type a password or code yourself).
2. For unread only goto https://mail.google.com/mail/u/0/#search/is%3Aunread+newer_than%3A7d (the search box accepts "is:unread newer_than:7d" with submit too). The Primary / Promotions / Social / Updates tabs exist only in the #inbox view; a search spans all of them, so add category:primary to the query to keep it to the Primary tab.
3. `read` the first page (50 rows): sender, subject, snippet, time, label chips, the paperclip for attachments, the "Important" marker. Bold rows are unread.
4. Open only rows whose snippet does not say what they want (max 6); opening marks them read, and the report must say which. `read` the newest message of the thread; leave older collapsed messages closed.
5. Group them: needs a reply, needs an action (invoice, approval, delivery, calendar), FYI, newsletters and promotions. Never quote verification codes, passwords or card numbers; say "contains a code" instead.
6. Report the groups as bullets newest first: sender, subject, one-line gist, time, thread link (the URL after opening, https://mail.google.com/mail/u/0/#inbox/<id>), and a one-line suggested reply for each "needs a reply". Say how many unread were left beyond the first page. Over 25 rows: `save` gmail-digest.md and keep the chat to the top 10.
