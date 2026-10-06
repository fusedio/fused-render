# Gmail search for an email or thread
trigger: find the email, search my email, email from, email about, did i get an email, gmail search, look up the email, the email where

1. If an APP TOOL for mail is listed, use it first. Otherwise build the query from the task with operators: from:, to:, subject:, "exact phrase", after:YYYY/MM/DD, before:, newer_than:7d, older_than:1m, has:attachment, filename:pdf, label:, category:updates, is:unread, in:anywhere (includes Spam and Trash), -word to exclude, OR. goto https://mail.google.com/mail/u/0/#search/<query with spaces as + and colons as %3A> (e.g. #search/from%3Aacme+newer_than%3A30d), or `type` it in the search box with submit. On a sign-in page use `login`.
2. `read` the result rows: sender, subject, snippet, date, label chips, message count after the names. No results: broaden once (drop subject:, widen the dates) and then try in:anywhere once.
3. Open the 3 best matches. In a thread with several collapsed messages click "Expand all" (icon top right of the thread) and `read` the thread; the "..." button (Show trimmed content) at the end of a message hides quoted text, click it only when the quote is needed.
4. Note each thread's URL as its link (https://mail.google.com/mail/u/0/#search/.../<threadid> or #inbox/<id>), the date of each message and who said what.
5. Report: the matching messages (sender, date, subject), the exact passage that answers the task quoted, and each link; say which query you used. Never quote codes, passwords or card numbers; say "contains a verification code" instead.
