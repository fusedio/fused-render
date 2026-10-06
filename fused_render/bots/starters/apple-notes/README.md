# Apple Notes

Browse and search your Apple Notes from a fused-render page, and let Browser Bots
read them, create notes and add lines to existing ones. The app talks to Notes.app
on this Mac through macOS automation (JavaScript for Automation), so there is no
iCloud sign-in, no network and no Shortcuts: every account and folder Notes shows
is available, including On My Mac.

**Page.** Folders on the left with note counts; a search box (title match, or
"in text" for full-text); Recent lists what changed in the last days; click a
note to read its plain text. The folder, search text, "in text" toggle and open
note all live in the URL. Nothing on the page edits a note.

**Bots.** `mcp.toml` exposes `notes_status`, `notes_folders`, `notes_search`,
`notes_read`, `notes_recent` (read, run at once), `notes_create` and
`notes_append` (both pause for your approval). `SKILL.md` documents `notes.py`
for the `py` action.

**How it reaches Notes.** macOS only shows the "control Notes" prompt to apps
whose Info.plist carries `NSAppleEventsUsageDescription`, and FusedRender's does
not, so its own automation is refused silently. `notes.py` therefore builds a
small helper applet, `.fused/data/NotesBridge.app` (osacompile, ad-hoc signed,
no Dock icon), and runs each request inside it. The first time, macOS asks
"NotesBridge wants to control Notes": click OK. Deleting that folder means a new
prompt. If you refuse, reads fall back to the Notes database
(`~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite`, read-only,
needs Full Disk Access) and creating or appending explains how to allow
NotesBridge under System Settings > Privacy & Security > Automation. The status
line on the page says which path is in use.

**Appending** adds plain-text lines at the end of a note and keeps its
attachments. A title must match one note (exactly, or as the only note containing
it); otherwise nothing changes and the matching ids come back. Password-protected
notes come back with an empty body, and refuse appends, until unlocked in Notes.
