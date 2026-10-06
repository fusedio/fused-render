# Reddit search on a topic
trigger: search reddit, reddit says, what does reddit think, reddit search, reddit opinions, reddit recommendations, reddit recommend, according to reddit

1. goto https://old.reddit.com/search?q=<query>&sort=relevance&t=year (sort=new for recent; t=month to narrow). If a sign-in page appears, use `login`; on a "blocked by network policy" page use https://www.reddit.com/search/?q=<query>&sort=relevance&t=year instead (its tabs are Posts, Communities, Comments, Media, People).
2. `read` the first 25 results: subreddit, title, score, comment count, age.
3. Run one more search restricted to the most relevant subreddit: https://old.reddit.com/r/<sub>/search?q=<query>&restrict_sr=on&sort=top&t=year ; `read` the top 10.
4. Open the 5 most relevant threads (high score and comment count, within the task's time window). On each, append ?sort=top to the URL, `read` the post body and the first 10 top-level comments with scores.
5. Report: the answers or recommendations that recur, each with how many threads mention it and the best supporting link; the strongest dissent with a quote and score; then the 5 thread links with subreddit, score and comment count. More than 10 recommendations: `save` as reddit-search.md.
