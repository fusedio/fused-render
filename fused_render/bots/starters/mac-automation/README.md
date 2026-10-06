# Mac Automation

Drive the stock Apple apps and system settings on this Mac from a fused-render
page, and let Browser Bots do the same. The app wraps `osascript` (AppleScript and
JavaScript for Automation), the `shortcuts` CLI and `osacompile`, so there is
nothing to install: every account and app the Mac already has is reachable, and
a finished script can be turned into an **automation app** (a double-clickable
applet under `~/Fused/automations`) that runs from the Dock, Finder, a Shortcut
or a launchd schedule with its own Automation permission.

**Page.** A status strip (macOS version, what it supports, which apps are
granted), a script runner with an AppleScript/JXA switch and argv box, the list
of Shortcuts with their UUIDs and a run button, and the applets built so far
with Build / Run / Delete.

**Bots.** `mcp.toml` exposes reads that run at once (`mac_status`, `mac_probe`,
`mac_shortcuts`, `mac_applets`, `mac_frontmost`, `mac_reminders`, `mac_calendar`,
`mac_dark_mode`, `mac_volume`) and actions that pause for your approval
(`mac_run_script`, `mac_run_shortcut`, `mac_notify`, `mac_set_dark_mode`,
`mac_set_volume`, `mac_add_reminder`, `mac_create_applet`, `mac_run_applet`,
`mac_delete_applet`). `SKILL.md` documents `mac.py` for the `py` action and
carries the tested snippets and the rules from the `mac-automation` skill.

**Permissions.** Automation (Apple Events) is granted per *host process*, not
per user. The first script against an app makes macOS ask "FusedRender wants to
control X" once; a `-1743` error means the grant is missing and the tool says so
in `hint` instead of guessing. `~/Fused/tools/grant_automation.py`, run from
Terminal, grants or audits every app at once. Notifications, volume, dark mode
and `shortcuts list/run` need no grant. Applets get their own prompt on first
launch, which is why scheduled automations should be applets rather than
scripts.

**Shortcuts.** Listed and run only; macOS has no CLI to create them. A shortcut
that ends in Show Result or asks for input blocks forever, so runs are capped at
30 s and the result says what to change (end in Stop and Output).
