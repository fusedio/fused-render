# TikTok creator profile and latest videos
trigger: tiktok profile, latest tiktoks from, what did they post on tiktok, tiktok account, tiktok creator, check their tiktok, their tiktok

1. goto https://www.tiktok.com/@<handle> and `wait` for the video grid. A "Verify to continue" puzzle or slider captcha, a sign-in sheet, or a page that is still blank after a second goto means TikTok is challenging the browser: use `login` (the user solves it in the popped-out window), then goto the URL again.
2. Close the "Log in to TikTok" sheet or the "Get app" banner with its X if it covers the page.
3. `read` the header: display name, handle, verified badge, Following / Followers / Likes counts, bio and link.
4. `read` the Videos grid: each tile shows a play count and caption snippet; "Pinned" tiles come first and are not the newest. Note the 12 most recent. The Reposts and Liked tabs are usually private.
5. Open the latest 3 videos (or the ones the task names, max 5): `read` the caption with hashtags, the sound name, date, likes, comments, bookmarks, shares, and the top 5 comments (scroll the comment panel once). The video itself cannot be watched. Escape or the X closes it.
6. Report the profile line, then the videos newest first: date, caption gist, counts, link https://www.tiktok.com/@<handle>/video/<id>. If a public account shows "No content" or "Couldn't load", say TikTok is throttling and `remember` the time it happened. Over 6 videos requested: `save` tiktok-<handle>.md.
