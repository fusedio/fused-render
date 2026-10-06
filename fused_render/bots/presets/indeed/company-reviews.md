# Indeed and Glassdoor company reviews
trigger: company reviews, what is it like to work at, glassdoor, employee reviews, reviews of working at, how is the culture at, indeed reviews

1. goto https://www.indeed.com/cmp/<Company>/reviews?fcountry=ALL (if the slug 404s, type the company into the search box at https://www.indeed.com/companies, click its result, then the "Reviews" tab). Cloudflare "Additional Verification Required" → `login`; close sign-in popups.
2. `read` the header: overall stars with review count, the Work wellbeing score, and the category ratings (Work-life balance, Pay and benefits, Job security and advancement, Management, Culture).
3. Narrow if the task names a role or theme: add &fjobtitle=<title> or &ftopic=mgmt, paybenefits, culture, worklifebalance or jobsecadv to the URL. Use the "Sort by" control to pick most recent, then `read` the first page (about 20 reviews): rating, title, role, location, date, pros, cons.
4. Click to page 2 only if page 1 is older than a year. Note the share of 1-2 star reviews and the words that recur.
5. goto https://www.glassdoor.com/Reviews/<Company>-Reviews-E<id>.htm (get the exact URL from a Google search of "<company> glassdoor reviews", or open the company's Glassdoor Overview page and click "Reviews"). `read` the rating, "Recommend to a friend" and "CEO approval" percentages and the 3-5 reviews shown. Glassdoor puts a "Create a free account" wall after a few reviews: if the user has an account use `login`, otherwise stop there and say so.
6. Report: Indeed rating (count) and wellbeing score; three recurring pros and three recurring cons with dates and roles; Glassdoor rating with recommend and CEO figures; where the two sites differ; links to both pages.
