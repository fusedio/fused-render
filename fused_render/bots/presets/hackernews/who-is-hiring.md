# Hacker News Who is hiring
trigger: who is hiring, hn hiring, hn jobs thread, hiring thread, who wants to be hired, freelancer thread, remote jobs on hn

1. goto https://news.ycombinator.com/submitted?id=whoishiring and `read` the list: the newest "Ask HN: Who is hiring? (Month YYYY)", "Ask HN: Who wants to be hired?" and "Ask HN: Freelancer? Seeking freelancer?" threads. Open the one the task wants (default the newest Who is hiring).
2. `read` the thread page. Each root comment is one post, usually "Company | Role | Location | REMOTE or ONSITE | ..."; indented comments are replies, not posts. The thread paginates with a "More" link at the bottom: follow it at most 4 times (about 5 pages).
3. Filter the posts by the task's keywords (role, stack, city, REMOTE, visa, salary), matching the first line and the body. Keep what matches and ignore the rest.
4. For a keyword hunt across months instead, goto https://hn.algolia.com/?query=<keywords>&type=comment&sort=byDate and `read` the first 20 comment hits that belong to whoishiring threads.
5. Report a table: company, role, location or remote, stack, pay if stated, how to apply (email or link), and the post's permalink (the age link such as "3 days ago" on the comment is its item?id link). Always `save` the full table as hn-hiring.csv, show the top 10 in chat, and say how many pages were read and whether more remained.
