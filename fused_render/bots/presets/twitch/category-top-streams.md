# Twitch top streams in a category or game
trigger: top streams for, who is streaming <game>, twitch category, twitch directory, most watched on twitch, biggest streams right now, twitch top channels

1. goto https://www.twitch.tv/directory/category/<slug> (lowercase, hyphens, e.g. just-chatting, league-of-legends). If unsure of the slug, goto https://www.twitch.tv/search?term=<game> and click the category card. Dismiss the mature-content prompt.
2. Make sure the sort is "Viewers (High to Low)" (the Sort dropdown above the grid); apply a language or tag filter only if the task gives one.
3. `read` the first 20 stream cards: channel, title, viewers, tags. Scroll once if the task wants more.
4. For an overall top list, goto https://www.twitch.tv/directory and `read` the "Categories we think you'll like" or "Browse" grid (https://www.twitch.tv/directory/all for live channels sorted by viewers).
5. Open the top 3 channels' about pages only if the task asks who they are; `read` follower count and description.
6. Report a ranked table: channel, title, viewers, tags, link; then the category's total viewers from the page header. If the user tracks a game regularly, `offer` a tracker app that logs top streams per hour.
