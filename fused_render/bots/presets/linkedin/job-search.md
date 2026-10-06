# LinkedIn job search
trigger: linkedin jobs, jobs on linkedin, job search on linkedin, openings on linkedin, linkedin job postings, remote jobs, find jobs, hiring for

1. goto https://www.linkedin.com/jobs/search/?keywords=<role>&location=<place> and add filters as URL parameters: f_TPR=r86400 (past 24 h) or f_TPR=r604800 (past week); f_WRA=1 remote, 2 on-site, 3 hybrid; f_E=2 entry, 3 associate, 4 mid-senior, 5 director; f_WT=1 full-time, 3 contract. If a sign-in page or captcha appears, use `login`.
2. `wait` for the results list and `read` it: title, company, location, posted date, "Easy Apply" or "Apply", pay if shown. Scroll the left list 3 times and read up to 25 cards.
3. Pick the 5 best matches for the task. Click each card: the job opens in the right panel; click "See more" under "About the job" and `read` the description: requirements, pay, seniority, workplace type, applicant count. The job link is https://www.linkedin.com/jobs/view/<id>/ (the id is in the card's href).
4. Never click Easy Apply, Apply, Save or Set alert.
5. Report a table of the 5 (title, company, location, remote/hybrid/on-site, pay, posted, Easy Apply or external, link) with a two-line fit note each, then the other cards as one-line bullets with links. If the task wants more than 10 jobs in detail, `save` the full list as linkedin-jobs.csv and keep the chat to the top 5.
