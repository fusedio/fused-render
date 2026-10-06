---
name: mac-automation
description: Drive stock Apple apps and system settings on this Mac (Reminders, Calendar, Notes, Mail, Messages, Finder, Safari, Music, dark mode, volume, notifications) with AppleScript or JXA through osascript, run Shortcuts, and build automation apps (applets) the user can double-click or schedule.
approve: [mac.py]
---

# Mac automation

One Python file beside `index.html`, `mac.py`, with one `main(**params)`. It is the
shell half of the `mac-automation` skill: `osascript` for the apps that have a
scripting dictionary, the `shortcuts` CLI for what only a Shortcut can do, and
`osacompile` to turn a finished script into an **automation app** (an applet under
`~/Fused/automations/<name>.app`). It returns a JSON dict and never raises: failures
come back as `{"error": "...", "action": "..."}` and, for osascript, a `hint` naming
the known macOS cause. Check `error` first.

## Decide

- App data (Reminders, Calendar, Notes, Mail, Messages, Finder, Safari, Music) → JXA
  (`language: "jxa"`) that ends in `return JSON.stringify(...)`; the parsed value comes
  back in `json`.
- System toggles (dark mode, volume, notification, frontmost app) → the ready-made
  actions, or an AppleScript one-liner.
- Focus modes, Health, Home, anything without a dictionary → a shortcut the user built
  in Shortcuts.app, run with `shortcut` (30 s timeout; it must end in Stop and Output).
- Something the user wants to run again by hand, from the Dock, or on a schedule →
  `create_applet`. An applet is a real app with its own Automation permission, so it
  keeps working where a shell script would not (launchd jobs have no TCC identity).

## Rules that prevent the known failures

1. **Step 0: `status`, then `probe`** before scripting an app for the first time. A
   denied app means this process has no Automation grant: tell the user to allow it
   (System Settings > Privacy & Security > Automation, or `~/Fused/tools/grant_automation.py`
   from Terminal) and stop. Do not rewrite the script, retry, or go through another app.
2. **Return, don't log.** In JXA only the last expression / `return` reaches stdout.
3. **Pass user text as `args`**, read with `on run argv` / `function run(argv)`. Never
   paste it into the script body.
4. **`-1743`** is a missing grant, **`-1728`** a property the dictionary lacks on this
   macOS (Messages: use `chat id`, not services), **`-2700`** a JXA collection called
   wrong (`coll().length`, not `coll.length()`; `whose({_and:[...]})` needs 2+ clauses).
5. **Shortcuts cannot be created from here**; only listed and run. Prefer the UUID.
6. **Calendar.app only knows what it has synced**; recurring events come once with
   `recurring: true`. An empty week is not proof of a free week.
7. **`compile` before any script that sends, deletes or changes** data; then show the
   user what it will do and run it only after approval.
8. **Version gates** (from `status.supports`): Shortcuts automations and Use Model need
   macOS 26; Apple Intelligence actions need 15.1+ on Apple silicon. Say so plainly.

## mac.py

`main(action=..., script="", language="applescript", args=[], app="", name="", input="", output_type="public.plain-text", title="", message="", subtitle="", value=None, limit=20, days=7, reminder_list="", calendar="", body="", due="", overwrite=False)`

- **Changes:** `run` (whatever the script does), `shortcut`, `notify`, `dark_mode`/`volume` with a `value`, `add_reminder`, `create_applet`, `run_applet`, `delete_applet`. Everything else is read-only.

| action | params | returns |
| --- | --- | --- |
| `status` | — | `{ok, macos, macos_name, arch, osascript, shortcuts_cli, shortcuts, system_events, applets, automations_dir, supports:{…}, note}` |
| `probe` | `app` (optional) | `{apps:[{app, granted, error, hint}], granted:[…], denied:[…]}` |
| `run` | `script`, `language`, `args` | `{ok, rc, stdout, stderr, json?, error?, hint?}` |
| `compile` | `script`, `language` | `{ok, rc, stderr, error?}` |
| `shortcuts` | `name` (filter), `limit` | `{count, shortcuts:[{name, id}]}` |
| `shortcut` | `name` or UUID, `input`, `output_type` | `{ok, rc, output, stderr, error?}` (rc 124 = timed out on UI) |
| `notify` | `message`, `title`, `subtitle` | `{ok}` |
| `dark_mode` | `value` (optional) | `{dark}` / `{ok, dark}` |
| `volume` | `value` 0-100 (optional) | `{output, muted}` / `{ok, output}` |
| `frontmost` | — | `{frontmost, apps:[…]}` |
| `reminders` | `reminder_list`, `limit` | `{list, lists, open, reminders:[{id, name, body, due}]}` |
| `add_reminder` | `name`, `body`, `due` (ISO), `reminder_list` | `{ok, id, name, list, due}` |
| `calendar` | `days`, `calendar`, `limit` | `{days, calendars, count, events:[{calendar, summary, start, end, location, recurring}], note}` |
| `applets` | — | `{dir, applets:[{name, path, modified, has_script}]}` |
| `create_applet` | `name`, `script`, `language`, `overwrite` | `{ok, name, path, language, run, note}` |
| `run_applet` | `name` or path | `{ok, rc, path, stderr, error?}` |
| `delete_applet` | `name` | `{ok, deleted}` |

## Tested snippets (macOS 14.3)

JXA, `language: "jxa"`:

```javascript
// Reminders: create with a due date, then confirm
function run(argv) {
  const R = Application('Reminders'), list = R.defaultList();
  list.reminders.push(R.Reminder({name: argv[0], body: 'notes here', dueDate: new Date(Date.now()+3600e3)}));
  const made = list.reminders.whose({name: argv[0]})()[0];
  return JSON.stringify({id: made.id(), name: made.name(), list: list.name()});
}
```
```javascript
// Reminders: complete, then delete, by name
function run(argv) {
  const R = Application('Reminders'), hits = R.defaultList().reminders.whose({name: argv[0]})();
  hits.forEach(x => { x.completed = true; });   // complete
  hits.forEach(x => R.delete(x));               // delete
  return JSON.stringify({removed: hits.length});
}
```
```javascript
// Calendar: create an event tomorrow in the first writable calendar
function run(argv) {
  const C = Application('Calendar'), cal = C.calendars.whose({writable: true})()[0];
  const start = new Date(Date.now()+86400e3), end = new Date(start.getTime()+1800e3);
  cal.events.push(C.Event({summary: argv[0], startDate: start, endDate: end, location: argv[1] || ''}));
  const ev = cal.events.whose({summary: argv[0]})()[0];
  return JSON.stringify({uid: ev.uid(), start: ev.startDate().toISOString(), cal: cal.name()});
}
```
```javascript
// Finder: selection, desktop items, trash count
const F = Application('Finder');
JSON.stringify({selection: F.selection().map(i => decodeURIComponent(i.url()).replace('file://','')),
  desktop: F.desktop.items.name(), trashCount: F.trash.items().length});
```

AppleScript, `language: "applescript"`:

```applescript
tell application "Notes" to make new note at folder "Notes" with properties {name:"Title", body:"<h1>Title</h1><p>Body is HTML</p>"}
```
```applescript
tell application "Messages" to get id of every chat          -- iMessage;-;+1555…
```
```applescript
on run argv  -- send an iMessage: args = ["iMessage;-;+15555550100", "hello"]
  tell application "Messages" to send (item 2 of argv) to chat id (item 1 of argv)
end run
```
```applescript
tell application "Finder" to get POSIX path of (target of front Finder window as alias)
```
```applescript
tell application "Mail"
  set m to make new outgoing message with properties {subject:"Subj", content:"Body", visible:false}
  tell m to make new to recipient with properties {address:"a@b.c"}
  send m
end tell
```
```applescript
tell application "Shortcuts Events" to run shortcut "Log Water" with input "250"
```

Applet example (`create_applet`, name "Focus Lights", language applescript): a script
that sets dark mode, volume and posts a notification. Explain to the user that the
first double-click asks them to allow the applet to control System Events, and that
they can add it to Login Items or call it from a Shortcut's "Open App" action.

Examples: `{"action":"probe"}` · `{"action":"run","language":"jxa","script":"...","args":["Buy milk"]}` ·
`{"action":"shortcut","name":"6F1C…","input":"250"}` · `{"action":"create_applet","name":"Morning","script":"..."}`.
