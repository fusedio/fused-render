# Update UX decisions

Append-only.

## Status chip label carries no version

The update row's title is "Update available" (also `updateLabel()`); the chip
used to read "Update available — v0.6.x" and always truncated the version. The
version stays in the notification `detail` ("v0.6.x is ready to download.") and
in the chip's `title` tooltip (RepoUpdatesDock.tsx), which only applies when the
update row is the sole thing waiting. "Update ready" and the in-flight restart
card never named a version in the title.

## "Automatically download updates" is server-side, default off

Pref `auto_download_updates` in prefs.json (`GET/PUT /api/prefs`, response
`update.auto_download`). The download is started by the update manager, not the
page, so it works with no window open: `UpdateManager.maybe_auto_install()` runs
after the five-minute forced check and after `POST /api/update/check`. It acts
only from `available` (an `error` is the user's to retry, so a broken artifact is
not re-downloaded every tick) and reuses `install(expected_version=...)`, which
already refuses for a check-only manager. Restart is never triggered from it.
The toggle sits in the Settings "Updates" section, shown only when the build has
an updater.

## Restart blocks the window with an overlay

`RestartOverlay` (platform/ui) renders the shared `Modal` chassis in its `busy`
posture from the moment `requestRestart()` flips the flow store out of `ready`,
in every window (the store is shared across windows). It stays up through
quitting / restarting / reconnecting / back; the existing probe loop in
`ServerStatusBanner` reloads the page once the new version answers. On the
120 s give-up it turns into an error with Dismiss and Try again (dismissal is
per press). Mounted once in the top document, `!IS_EMBED`, beside `UpdateNotifier`.

## Server-side slowness not touched

The wait is teardown (`app.quit_teardown`: bounded 2 s server drain, capture
finalise, DuckDB close, mount detach, rcd reap) plus a cold start of the new
bundle. Nothing in it is a needless fixed sleep that is cheap to cut; the
mount-detach ladder is the long pole and is not safe to shorten here.
