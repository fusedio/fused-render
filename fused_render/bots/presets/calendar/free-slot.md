# Google Calendar find a free slot
trigger: free slot, when am i free, find a time, open slot, availability, free time, when are we both free, time to meet with

1. Work out the window from the task: the days (default the next 5 working days), the hours (default 9:00 to 18:00 local) and the meeting length (default 30 minutes). If the length or week is missing, `ask` with 2-4 concrete options instead of guessing.
2. goto https://calendar.google.com/calendar/u/0/r/week for that window (`login` if a sign-in page, 2FA or captcha appears; press `n` to move a week forward). `read` the grid and list every busy block in the window, including all-day and out-of-office items.
3. If the task names other people: in the left panel find the "Search for people" box under the mini month, type the name or email and click the match. Their calendar is layered onto the grid (busy blocks only unless they share details). Add at most 3 people; note anyone who shows no events or "no access", since that is not the same as free.
4. `read` the grid again and list every gap of at least the meeting length inside the hours that is free for everyone. Skip gaps of under 15 minutes between meetings.
5. Remove the added people with the X beside each name under "Search for people" so the view is back to normal. Nothing is created or sent.
6. Report the three best slots as a table: day, start, end, who is free, and why it works (for example "first thing before the 10:00 standup"). More than 5 slots asked for: `save` the full list as free-slots.md. If no slot fits, say so and give the closest options just outside the hours. End with the week-view URL.
