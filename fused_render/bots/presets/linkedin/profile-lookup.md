# LinkedIn profile lookup
trigger: linkedin profile, look up on linkedin, check their linkedin, find on linkedin, background of, linkedin of, who is this person on linkedin

1. If the task gives a profile URL, goto it. Else goto https://www.linkedin.com/search/results/people/?keywords=<name> and pick the row whose headline or location matches the company or place in the task; if several match, note the top 3 for the report and open the best one. If a sign-in page, 2FA or captcha appears, use `login`.
2. `read` the top card: name, headline, location, current company, connection degree, follower count. Profile views are visible to the other person; the user accepts that.
3. Scroll to About and click "…see more" if present; `read` it.
4. `read` the Experience section (last 3 roles with dates; click "Show all experiences" if fewer than 3 are visible) and the Education section.
5. Scroll to Activity and click "Show all posts" (the URL ends in /recent-activity/all/); `read` the first screen: the 3 most recent posts or comments with dates.
6. Report: a two-line summary, then bullets for headline, location, current role, last 3 roles with dates, education, recent activity (3 items with gists), the profile URL. If several people share the name, list the 3 candidates with headlines and links and say which you chose and why.
