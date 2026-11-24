# Tasks on the phone

*2026-11-06, D1468. Supersedes the 2026-11-05 door (D1464).*

The Fused Render iOS app's Tasks section is the desktop's own Tasks page
(`frontend/src/shell/Scheduled.tsx`, with `@apps/claude`'s chat in the peek),
mounted by the phone page (`frontend/src/lan/main.tsx`) at `/tasks`; a
`/tasks/<key>` deep link from the native shell becomes the page's own
`?peek=<key>` before it mounts. The routes it reaches are the desktop's —
`/api/tasks`, `/api/tasks/changes`, `/api/claude/agent` and the rest —
forwarded by the LAN listener's allow-list (`fused_render/lan.py` `_DOORS`,
docs/bots.md §14), device-stamped, with the Mac-side verbs (erase, worktree
create/remove, snapshots, terminal, set_mode, app_state) a 404.

The one route the phone keeps of its own is `POST /api/phone/tasks/new-app`
(`fused_render/server/routers/phone_tasks.py`): the Apps grid's "+", which
names (through the AI relay, server-side), scaffolds and builds an app as a
task, then opens that task.

The former `/api/phone/tasks` listing, transcript (`_turns`), card (`_card`)
and verbs, and the phone's own `TasksList`/`TaskView` screens, are gone: a
second wire shape and a hand-copied transcript renderer that lagged the
desktop. The native shell (`ios/`) is unchanged: it opens pages.
