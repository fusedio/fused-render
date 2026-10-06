# Twitch channel snapshot
trigger: twitch channel, twitch streamer, what does this streamer, twitch profile, twitch schedule, when does, latest vod, twitch vods

1. goto https://www.twitch.tv/<channel>/about and `read` the panel: display name, follower count, description, social links, and whether the channel is live right now (the "LIVE" badge and viewer count at the top).
2. goto https://www.twitch.tv/<channel>/schedule and `read` the weekly schedule: day, time, category, title. Note "No schedule" if empty.
3. goto https://www.twitch.tv/<channel>/videos?filter=archives&sort=time and `read` the first 10 VODs: title, category, length, views, date.
4. goto https://www.twitch.tv/<channel>/clips?filter=clips&range=30d and `read` the first 10 clips: title, views, clipper, date.
5. If the task asks what the streamer is like, open the most viewed VOD and `read` its title, category and the chapter list under the player; do not try to watch it.
6. Report: the about line with follower count, live status, the schedule as a table, the latest VODs and top clips as two short lists with links, and the channel link.
