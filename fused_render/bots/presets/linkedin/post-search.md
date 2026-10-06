# LinkedIn posts on a topic
trigger: linkedin posts about, what are people saying on linkedin, linkedin discussion, posts on linkedin, linkedin content search, who is posting about, linkedin takes on

1. goto https://www.linkedin.com/search/results/content/?keywords=<query>&datePosted=%22past-week%22 (past-24h or past-month to match the task; add &sortBy=%22date_posted%22 for newest first). If the parameters are ignored, use the "Date posted" and "Sort by" chips under the search bar. If a sign-in page or captcha appears, use `login`.
2. `read` the results: author, headline, first lines, reactions, comments, age. Scroll 3 times to collect about 15 posts. Skip "Promoted".
3. Open the 5 posts with the most reactions that match the task (click the post text), click "…more", `read` the full post, then `read` the first screen of comments.
4. Never react, comment or repost.
5. Report: a two-line overview of the positions people take; then 5 bullets (author and headline, gist, reactions and comments, link); then up to 5 one-line honourable mentions with links; then the date filter you used. More than 10 posts requested: `save` as linkedin-posts.md.
