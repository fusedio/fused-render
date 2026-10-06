# Google Maps directions and travel time
trigger: how do i get to, directions to, how far is, travel time, how long to drive, transit to, route to, walking distance

1. goto https://www.google.com/maps/dir/?api=1&origin=<start>&destination=<end>&travelmode=driving. Use the task's start; if none is given, `ask` for it (the bot cannot use the user's live location). Consent dialog → dismiss; captcha → `login`.
2. `read` the directions panel: each route option with minutes, distance, "via <road>", traffic note and tolls. If the task gives a time, click the "Leave now" dropdown and set "Depart at" or "Arrive by".
3. Click the transit, walking and cycling mode buttons at the top of the panel in turn and `read` each: duration, and for transit the lines, transfers and next departure.
4. If the task adds stops, click "Add destination" and type each (up to 3) in order; `read` the total time and the per-leg times.
5. Report a table of mode, time, distance, notes (traffic, tolls, transfers); the recommended option for the task's constraints; the first 3 turn-by-turn steps of that option; and the directions link from the address bar.
