# Google Calendar draft an event
trigger: create an event, add to my calendar, schedule a meeting, put on my calendar, block time, new event, calendar invite, book a slot

1. Only do this when the task plainly asks to create something; if an APP TOOL for the calendar is listed, use it instead. Collect from the task: title, date, start and end (default 30 minutes), guest emails, location or "Google Meet", and a description. If the date or time is missing, `ask` with 2-4 concrete options.
2. goto the prefilled form: https://calendar.google.com/calendar/render?action=TEMPLATE&text=<title>&dates=YYYYMMDDTHHMMSS/YYYYMMDDTHHMMSS&details=<description>&location=<place>&add=<email1>,<email2> (spaces as +; times without a trailing Z are local time; omit the time parts for an all-day event). `login` if a sign-in page, 2FA or captcha appears.
3. `read` the edit form: title, date and time fields, guests, location, description, and the calendar it will land on. Fix anything the prefill got wrong by clicking the field and typing.
4. If guests are set, click the "Find a time" tab and `read` it to confirm no guest shows a clash at that hour, then click back to "Event details".
5. If the task wants a video call and no Meet link is shown, click "Add Google Meet video conferencing".
6. Stop before "Save". Show the user exactly what will be created: title, date, start and end, guests, location, Meet yes or no, description, and that invitation emails will go out to guests. Click "Save" (and "Send" on the "Send invitation emails?" dialog) only after approval. If the user wants changes, edit the fields and show it again.
7. Report what was created with the time as the page shows it, the guests invited, and the day-view URL https://calendar.google.com/calendar/u/0/r/day/YYYY/M/D. If approval was refused, click "Discard" and report that nothing was saved.
