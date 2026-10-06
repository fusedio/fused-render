# Gmail find or download attachments
trigger: attachments, find the attachment, the pdf they sent, the file from, download the invoice, email attachment, gmail attachment, the receipt from

1. If an APP TOOL for mail is listed, use it first. Otherwise goto https://mail.google.com/mail/u/0/#search/has%3Aattachment+<terms> combining has:attachment with from:, subject:, filename:pdf (or filename:xlsx, filename:csv, filename:zip) and newer_than:30d. On a sign-in page use `login`.
2. `read` the rows: a paperclip row shows attachment chips with file names under the snippet. Pick the 3 best matches.
3. Open the thread. Attachments sit as chips at the bottom of each message with file name, type icon and size; `read` the chip names. Hover a chip to show its icons: Download (down arrow) and Add to Drive; clicking the chip opens a preview (Escape closes it). Large files arrive as "Google Drive" links instead of chips.
4. If the task wants the file, click Download on the chip (or the download icon top right of the preview); the file lands in this task's Inbox folder. Download at most 5 files; otherwise download nothing. If the preview shows text (a PDF invoice, a doc), `read` it there to answer questions about its contents.
5. Report: for each message the sender, date, subject, attachment names and sizes, what was downloaded (file names) and the thread link. Say that downloaded files are in the task's Inbox folder.
