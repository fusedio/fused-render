# Instagram hashtag or keyword search
trigger: instagram hashtag, instagram search, posts tagged, search instagram, what's trending on instagram, instagram posts about, hashtag on instagram

1. For a hashtag goto https://www.instagram.com/explore/tags/<tag>/ (no # in the URL); for a keyword goto https://www.instagram.com/explore/search/keyword/?q=<query>. Both need a logged-in session: on a sign-in page or challenge use `login`, then goto again.
2. `wait` for the grid. Hashtag pages no longer have a Recent tab; the grid is algorithmic, so take the date from each post, not from its position. A keyword search shows the tabs For you, Accounts, Tags and Places: stay on For you for posts, use Accounts when the task wants people.
3. `read` the header (a hashtag page shows the post count) and the alt text of the first 12 tiles for account, date and image description.
4. Open 6 tiles that match the task (otherwise the first 6): `read` account, date, caption, hashtags, likes, comment count and 2-3 top comments. Escape closes the post.
5. Scroll at most twice for more tiles. If "Try again later" or an empty grid appears, stop opening posts, `remember` that Instagram throttled a search today, and report what you have.
6. Report: the hashtag post count, accounts that appear more than once, the post types seen (product, tutorial, meme, ad), then 6 bullets with account, date, caption gist, counts and link. If the task wants more than 10, `save` the list as instagram-<tag>.md and keep the chat to 6.
