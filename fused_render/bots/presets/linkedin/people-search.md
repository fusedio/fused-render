# LinkedIn people search
trigger: find people on linkedin, linkedin search, search linkedin for, who works at, people at, linkedin people, prospects on linkedin, employees of

1. goto https://www.linkedin.com/search/results/people/?keywords=<query> . If a sign-in page or captcha appears, use `login`. If the page says "commercial use limit reached", stop and report it (free accounts get about 300 people searches a month; it resets on the 1st).
2. Apply only the filters the task names, using the chips under the search bar: "Locations", "Current company", "Connections" (1st, 2nd, 3rd), "Past company", "Industry". Each opens a dropdown: `type` the value, pick it, click "Show results". Set every filter before reading so you search once.
3. `read` the results page: name, headline, location, connection degree, mutual connections. Click "Next" and `read` page 2; stop at 20 people (or the number the task asks for).
4. Open at most the top 3 profiles for detail (current role and tenure, About first lines) only if the task wants more than a list.
5. Never click Connect, Follow or Message.
6. Report a table: name, headline, company, location, degree, profile link; then one line on which filters were applied and how many results LinkedIn reported. If more than 10 rows, `save` as linkedin-people.csv and show the top 10 in chat.
