# YouTube video summary
trigger: summarize this video, summarize the video, youtube video, what does this video say, youtube transcript, key points of the video, summarize this youtube, notes from the video

1. goto the video URL from the task. If the task only names it, goto https://www.youtube.com/results?search_query=<title> and open the first result whose title and channel match. Click "Reject all" on a "Before you continue" consent dialog; if "Sign in to confirm you're not a bot" appears, use `login`.
2. `read` the title block: title, channel, view count, upload date, and the length from the player's time display.
3. Click "…more" under the title to expand the description and `read` it: chapters with timestamps, links, sponsors.
4. In the expanded description click "Show transcript". The transcript panel opens on the right of the video; `read` it, scroll the panel and `read` again until the last timestamp is near the video length. Do not play the video.
5. If there is no "Show transcript" button, the video has no captions: say so, and instead `read` the description and the first screen of comments (scroll down once; "Sort by" is "Top comments" by default).
6. Report: a one-paragraph summary; key points as bullets with a rough timestamp each; names, numbers, products and links mentioned; the video link. For a video over 40 minutes, `save` the full notes as youtube-summary.md and keep the chat to 10 bullets.
