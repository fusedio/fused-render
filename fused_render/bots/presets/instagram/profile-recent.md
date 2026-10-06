# Instagram profile and latest posts
trigger: instagram profile, instagram account, latest posts on instagram, what did they post on instagram, check their instagram, instagram bio, their instagram

1. goto https://www.instagram.com/<handle>/ and `wait` for the post grid. On a sign-in page, 2FA or "Suspicious login attempt" page use `login`. If the page stays blank, goto the same URL once more; still blank means Instagram is throttling: report that and stop.
2. Dismiss the "Turn on notifications" or "Save your login info" dialog with its Not Now button.
3. `read` the header: name, verified badge, bio, external link, and the posts / followers / following counts. "This account is private" with no grid: report the header only and stop.
4. `read` the grid: the alt text of the first 9 tiles ("Photo by X on <date>. May be an image of ...") gives date and content; a pin icon marks pinned posts, which are not the newest. The Reels tab (https://www.instagram.com/<handle>/reels/) lists reels with play counts.
5. Open the latest 3 tiles (or the ones the task names, max 6): `read` the date, caption with hashtags, like count (or "liked by X and others"), comment count and the top 3 comments. Press Escape to close each post before opening the next.
6. Report one profile line (counts, bio, link in bio), then the posts newest first as bullets: date, caption gist, likes/comments, link https://www.instagram.com/p/<code>/. Say if like counts are hidden. If more than 6 posts were asked for, `save` the full list as instagram-<handle>.md and keep the chat to the latest 3.
