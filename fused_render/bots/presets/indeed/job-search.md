# Indeed job search
trigger: jobs on indeed, indeed search, find jobs, job openings, search for jobs, remote jobs, job listings, new jobs

1. goto https://www.indeed.com/jobs?q=<title>&l=<city, state or zip>&fromage=7&sort=date. Add &sc=0kf%3Aattr(DSQF7)%3B for remote-only, &jt=fulltime (parttime, contract, internship), &radius=<miles>, and fromage=1, 3, 7 or 14 for posting age; leave l= empty for anywhere. If the page says "Additional Verification Required" or "Verify you are human", use `login`; close any "Sign in" or job-alert popup with its X.
2. Apply the task's remaining filters with the chips above the results ("Pay", "Date posted", "Remote", "Job type", "Experience level", "Education") and note which are active.
3. `read` the results list on the left (about 15 cards per page): title, company and its rating, location, pay line, posted age, "Easily apply", "new" or "Urgently hiring" badges. Click "Next Page" and `read` page 2.
4. Pick the 6 best matches for the task's title, pay and requirements. Click each card: the job opens in the right panel (its URL is /viewjob?jk=<id>); `read` the full description for requirements, pay, benefits and the apply button type ("Apply now" = Indeed Apply, "Apply on company site" = external).
5. Report a ranked table: title, company (rating), location or remote, pay, posted, requirements fit, Easily apply, link (https://www.indeed.com/viewjob?jk=<id>); say which filters applied. If more than 8 jobs, `save` as indeed-jobs.md and keep the chat to the top 5.
6. Never click "Apply now". If the user will repeat this search, `offer` a job-watcher app that runs the same URL daily and reports only new postings.
