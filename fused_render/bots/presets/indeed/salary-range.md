# Indeed salary range for a role
trigger: salary for, how much does a, pay range, what do they earn, salary range, average salary, how much do they pay

1. goto https://www.indeed.com/career/<role-with-hyphens>/salaries/<City--ST> (for example /career/software-engineer/salaries/Austin--TX); drop the city part for a national figure. If it 404s, goto https://www.indeed.com/career/salaries and type the role and location into the "What" and "Where" boxes. Cloudflare → `login`.
2. `read` the headline: "The average salary for a <role> is $X per year in <city>", the low to high range, "based on N salaries", and the pay-period toggle (hour, day, year).
3. `read` the "Top companies for <role>" and "Highest paying cities" lists below it.
4. If the task names an employer, goto https://www.indeed.com/cmp/<Company>/salaries and `read` the rows for the role: average, range, sample size. Cross-check on https://www.glassdoor.com/Salary/<Company>-Salaries-E<id>.htm (find the URL via a Google search of "<company> salaries glassdoor") and read the total-pay range shown before the sign-up wall.
5. goto https://www.indeed.com/jobs?q=<role>&l=<city>&fromage=14 and open 2 postings whose cards state pay; note their pay lines as a sanity check.
6. Report: average and range with sample size, top-paying employers, the employer figure if asked, the 2 postings' stated pay, and the links. Flag samples under 50 as thin.
