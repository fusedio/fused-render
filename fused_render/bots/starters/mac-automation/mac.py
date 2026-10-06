"""Mac automation from a bot: stock Apple apps and system settings through
`osascript` (AppleScript or JavaScript for Automation), Shortcuts through the
`shortcuts` CLI, and automation *apps* (double-clickable applets) through
`osacompile`. It is the shell half of the `mac-automation` skill, packaged so a
bot that has no shell can still use it: every rule the skill learned the hard
way is enforced here (return-not-log, timeouts around `shortcuts run`, -1743 is a
missing Automation grant and not a code bug, arguments passed as argv and never
interpolated into the script).

main(action=..., ...) returns a JSON dict and never raises; failures come back as
{"error": "...", "action": "..."}. An osascript failure also carries `rc`,
`stderr` and, when the stderr matches a known macOS error, `hint` saying what
to do (grant Automation in System Settings, fix a JXA collection call, ...).

Applets are written to ~/Fused/automations/<name>.app (override with the
FUSED_MAC_AUTOMATIONS env var). An applet is an ordinary macOS app: it gets its
own Automation prompt the first time it runs, so it keeps working from the
Dock, Finder, a launchd job or a Shortcut where a shell script would not.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess

TIMEOUT_S = 55  # the page's runner stops at 60 s
SHORTCUT_TIMEOUT_S = 30  # a shortcut that shows UI never returns on its own
LIMIT_MAX = 200
APPLET_NAME_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,60}$")
AUTOMATABLE = ("Reminders", "Calendar", "Notes", "Mail", "Messages", "Finder", "Safari", "Music", "System Events")

# stderr fragments → what they mean for the user (skill rules 4, 5, 8 and the permission table).
HINTS = (
    ("-1743", "macOS has not granted this process Automation access to that app. Nothing is wrong with the "
              "script: ask the user to allow it under System Settings > Privacy & Security > Automation, or to "
              "run ~/Fused/tools/grant_automation.py once from Terminal. Do not retry or rewrite the script."),
    ("-1728", "The object or property does not exist in this app's scripting dictionary on this macOS "
              "(Messages has chats, not services; use `chat id`). Check with `sdef`."),
    ("-2700", "JXA TypeError: a collection was called as a function or vice versa (use `coll().length`, "
              "not `coll.length()`; `whose({_and: [...]})` needs two or more clauses)."),
    ("1002", "UI scripting needs Accessibility access: System Settings > Privacy & Security > Accessibility."),
    ("-1712", "The app did not answer in time (AppleEvent timed out). It may be showing a dialog; ask the user to look."),
    ("-600", "The app is not running and could not be launched."),
)


def _automations_dir():
    return os.path.expanduser(os.environ.get("FUSED_MAC_AUTOMATIONS") or "~/Fused/automations")


def _hint(stderr):
    for key, text in HINTS:
        if key in (stderr or ""):
            return text
    return ""


def _run(cmd, stdin=None, timeout=TIMEOUT_S):
    """subprocess.run with text I/O; returns (rc, stdout, stderr); rc 124 on timeout, like gtimeout."""
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        return 124, out, err + f"\ntimed out after {timeout}s"
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: command not found"
    return p.returncode, p.stdout, p.stderr


def _osascript(script, args=(), language="applescript", timeout=TIMEOUT_S):
    """Run `script` with argv `args` (never interpolated). AppleScript needs `on run argv`,
    JXA needs `function run(argv)` to see them; a script without that still runs."""
    cmd = ["osascript"]
    if language == "jxa":
        cmd += ["-l", "JavaScript"]
    cmd += ["-", *[str(a) for a in args]]
    rc, out, err = _run(cmd, stdin=script, timeout=timeout)
    out, err = out.rstrip("\n"), err.strip()
    res = {"ok": rc == 0, "rc": rc, "stdout": out, "stderr": err, "language": language}
    if rc == 0 and out:
        try:
            res["json"] = json.loads(out)  # JXA scripts that `return JSON.stringify(...)`
        except ValueError:
            pass
    if rc != 0:
        res["error"] = (err.splitlines() or [f"osascript exit {rc}"])[-1]
        h = _hint(err)
        if h:
            res["hint"] = h
    return res


def _probe(app):
    """One harmless read against `app` to see whether this process may drive it (skill Step 0b)."""
    probes = {
        "Reminders": 'tell application "Reminders" to get name of default list',
        "Calendar": 'tell application "Calendar" to get name of first calendar',
        "Notes": 'tell application "Notes" to get name of first folder',
        "Mail": 'tell application "Mail" to get name',
        "Messages": 'tell application "Messages" to get name',
        "Finder": 'tell application "Finder" to get name of startup disk',
        "Safari": 'tell application "Safari" to get name',
        "Music": 'tell application "Music" to get name',
        "System Events": 'tell application "System Events" to tell appearance preferences to get dark mode',
    }
    script = probes.get(app) or f'tell application "{app}" to get name'
    r = _osascript(script, timeout=20)
    return {"app": app, "granted": r["ok"], "error": r.get("error", ""), "hint": r.get("hint", "")}


def _version():
    rc, out, _ = _run(["sw_vers", "-productVersion"], timeout=10)
    v = out.strip() if rc == 0 else platform.mac_ver()[0]
    major = int(v.split(".")[0]) if v.split(".")[0].isdigit() else 0
    name = {14: "Sonoma", 15: "Sequoia", 26: "Tahoe"}.get(major, "")
    return v, major, name


def _shortcuts_list():
    rc, out, err = _run(["shortcuts", "list", "--show-identifiers"], timeout=20)
    if rc != 0:
        return None, (err.strip() or f"shortcuts exit {rc}")
    items = []
    for line in out.splitlines():
        m = re.match(r"^(.*)\s\(([0-9A-Fa-f-]{36})\)\s*$", line.strip())
        if m:
            items.append({"name": m.group(1).strip(), "id": m.group(2)})
        elif line.strip():
            items.append({"name": line.strip(), "id": ""})
    return items, ""


def _applets():
    d = _automations_dir()
    out = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for n in names:
        p = os.path.join(d, n)
        if n.endswith(".app") and os.path.isdir(p):
            src = os.path.join(p, "Contents", "Resources", "Scripts", "main.scpt")
            try:
                mtime = os.path.getmtime(p)
            except OSError:
                mtime = 0
            out.append({"name": n[:-4], "path": p, "modified": mtime, "has_script": os.path.isfile(src)})
    return out


def main(action="status", script="", language="applescript", args=None, app="", name="", input="",
         output_type="public.plain-text", title="", message="", subtitle="", value=None, limit=20,
         days=7, reminder_list="", calendar="", body="", due="", overwrite=False):
    """Dispatch one action; see SKILL.md for the table."""
    act = (action or "status").strip().lower()
    language = "jxa" if str(language).lower() in ("jxa", "javascript", "js") else "applescript"
    argv = [str(a) for a in (args or [])] if isinstance(args, (list, tuple)) else ([str(args)] if args not in (None, "") else [])
    limit = max(1, min(int(limit or 20), LIMIT_MAX))
    try:
        if platform.system() != "Darwin":
            return {"error": "Mac automation only runs on macOS", "action": act}

        if act == "status":
            v, major, vname = _version()
            sc, sc_err = _shortcuts_list()
            se = _probe("System Events")
            return {"ok": True, "macos": v, "macos_name": vname, "arch": platform.machine(),
                    "osascript": bool(shutil.which("osascript")), "shortcuts_cli": sc is not None,
                    "shortcuts": len(sc or []), "shortcuts_error": sc_err,
                    "system_events": se["granted"], "applets": len(_applets()), "automations_dir": _automations_dir(),
                    "supports": {"scripting": True, "shortcuts_cli": sc is not None,
                                 "shortcuts_automations": major >= 26, "use_model": major >= 26,
                                 "apple_intelligence_actions": major >= 15 and platform.machine() == "arm64"},
                    "note": ("Scripting stock apps works. Shortcuts automations and Use Model need macOS 26; "
                             "Apple Intelligence actions need 15.1+ on Apple silicon." if major < 26 else
                             "Full support: scripting, Shortcuts automations, Use Model.")}

        if act == "probe":
            apps = [app] if app else [a for a in AUTOMATABLE if a != "System Events"]
            res = [_probe(a) for a in apps]
            return {"apps": res, "granted": [r["app"] for r in res if r["granted"]],
                    "denied": [r["app"] for r in res if not r["granted"]]}

        if act in ("run", "applescript", "jxa"):
            if act == "jxa":
                language = "jxa"
            if not script.strip():
                return {"error": "script is required", "action": act}
            return _osascript(script, argv, language)

        if act == "compile":
            if not script.strip():
                return {"error": "script is required", "action": act}
            cmd = ["osacompile", "-o", "/dev/null"] + (["-l", "JavaScript"] if language == "jxa" else [])
            rc, out, err = _run(cmd, stdin=script, timeout=30)
            return {"ok": rc == 0, "rc": rc, "stderr": err.strip(), "language": language,
                    **({"error": err.strip().splitlines()[-1] if err.strip() else f"osacompile exit {rc}"} if rc else {})}

        if act == "shortcuts":
            items, err = _shortcuts_list()
            if items is None:
                return {"error": err, "action": act}
            if name:
                q = name.lower()
                items = [s for s in items if q in s["name"].lower()]
            return {"count": len(items), "shortcuts": items[:limit]}

        if act == "shortcut":
            if not name:
                return {"error": "name (or UUID) of the shortcut is required", "action": act}
            cmd = ["shortcuts", "run", name]
            if input:
                cmd += ["-i", "-"]
            cmd += ["-o", "-", "--output-type", output_type or "public.plain-text"]
            rc, out, err = _run(cmd, stdin=input if input else None, timeout=SHORTCUT_TIMEOUT_S)
            res = {"ok": rc == 0, "rc": rc, "output": out.rstrip("\n"), "stderr": err.strip(), "name": name}
            if rc == 124:
                res["error"] = (f"the shortcut did not finish in {SHORTCUT_TIMEOUT_S}s: it probably shows a result, asks "
                                "for input or waits on a permission prompt. For CLI use it must end in Stop and Output.")
            elif rc != 0:
                res["error"] = (err.strip().splitlines() or [f"shortcuts exit {rc}"])[-1]
            return res

        if act == "notify":
            if not (message or title):
                return {"error": "message is required", "action": act}
            r = _osascript('on run argv\ndisplay notification (item 1 of argv) with title (item 2 of argv) subtitle (item 3 of argv)\nend run',
                           [message or "", title or "FusedBot", subtitle or ""])
            return {"ok": r["ok"], **({"error": r["error"]} if not r["ok"] else {})}

        if act == "dark_mode":
            if value is None or value == "":
                r = _osascript('tell application "System Events" to tell appearance preferences to get dark mode')
                return {"dark": r["stdout"] == "true", **({"error": r["error"]} if not r["ok"] else {})}
            want = str(value).lower() in ("1", "true", "on", "yes", "dark")
            r = _osascript(f'tell application "System Events" to tell appearance preferences to set dark mode to {"true" if want else "false"}')
            return {"ok": r["ok"], "dark": want, **({"error": r["error"]} if not r["ok"] else {})}

        if act == "volume":
            if value is None or value == "":
                r = _osascript("get volume settings")
                parts = dict(kv.strip().split(":", 1) for kv in r["stdout"].split(",") if ":" in kv) if r["ok"] else {}
                return {"output": int(parts.get("output volume", 0) or 0), "muted": parts.get("output muted") == "true",
                        "raw": r["stdout"], **({"error": r["error"]} if not r["ok"] else {})}
            lvl = max(0, min(100, int(float(value))))
            r = _osascript(f"set volume output volume {lvl}")
            return {"ok": r["ok"], "output": lvl, **({"error": r["error"]} if not r["ok"] else {})}

        if act == "frontmost":
            r = _osascript('tell application "System Events" to get name of first application process whose frontmost is true')
            r2 = _osascript('tell application "System Events" to get name of every process whose background only is false')
            return {"frontmost": r["stdout"], "apps": [s.strip() for s in r2["stdout"].split(",") if s.strip()],
                    **({"error": r["error"]} if not r["ok"] else {})}

        if act == "reminders":
            js = r"""
function run(argv) {
  const R = Application('Reminders'), limit = parseInt(argv[1] || '20', 10);
  const lst = argv[0] ? R.lists.byName(argv[0]) : R.defaultList();
  const open = lst.reminders.whose({completed: false})();
  return JSON.stringify({list: lst.name(), lists: R.lists.name(), open: open.length,
    reminders: open.slice(0, limit).map(r => ({id: r.id(), name: r.name(), body: r.body() || '',
      due: r.dueDate() ? r.dueDate().toISOString() : null}))});
}"""
            r = _osascript(js, [reminder_list or "", limit], "jxa")
            return r.get("json") or {"error": r.get("error", "no result"), "hint": r.get("hint", ""), "action": act}

        if act == "add_reminder":
            if not name:
                return {"error": "name is required", "action": act}
            js = r"""
function run(argv) {
  const R = Application('Reminders');
  const lst = argv[0] ? R.lists.byName(argv[0]) : R.defaultList();
  const props = {name: argv[1]}; if (argv[2]) props.body = argv[2];
  if (argv[3]) { const d = new Date(argv[3]); if (!isNaN(d)) props.dueDate = d; }
  lst.reminders.push(R.Reminder(props));
  const made = lst.reminders.whose({name: argv[1]})();
  const m = made[made.length - 1];
  return JSON.stringify({ok: true, id: m.id(), name: m.name(), list: lst.name(), due: m.dueDate() ? m.dueDate().toISOString() : null});
}"""
            r = _osascript(js, [reminder_list or "", name, body or "", due or ""], "jxa")
            return r.get("json") or {"error": r.get("error", "no result"), "hint": r.get("hint", ""), "action": act}

        if act == "calendar":
            js = r"""
function run(argv) {
  const C = Application('Calendar'), days = parseInt(argv[0] || '7', 10), only = argv[1] || '', limit = parseInt(argv[2] || '20', 10);
  const now = new Date(), end = new Date(Date.now() + days * 86400e3), out = [];
  let latest = 0;
  for (const cal of C.calendars()) {
    if (only && cal.name() !== only) continue;
    const evs = cal.events.whose({_and: [{startDate: {_greaterThan: now}}, {startDate: {_lessThan: end}}]})();
    evs.forEach(e => out.push({calendar: cal.name(), summary: e.summary(), start: e.startDate().toISOString(),
      end: e.endDate() ? e.endDate().toISOString() : null, location: e.location() || '', recurring: !!e.recurrence()}));
  }
  out.sort((a, b) => a.start < b.start ? -1 : 1);
  return JSON.stringify({days: days, calendars: C.calendars.name(), count: out.length, events: out.slice(0, limit),
    note: 'Calendar.app only knows what it has synced; recurring events appear once with recurring=true, not as instances.'});
}"""
            r = _osascript(js, [days, calendar or "", limit], "jxa")
            return r.get("json") or {"error": r.get("error", "no result"), "hint": r.get("hint", ""), "action": act}

        if act == "applets":
            return {"dir": _automations_dir(), "applets": _applets()}

        if act == "create_applet":
            if not name or not APPLET_NAME_RX.match(name):
                return {"error": "name is required: letters, digits, spaces, dots, dashes (max 60)", "action": act}
            if not script.strip():
                return {"error": "script is required", "action": act}
            d = _automations_dir()
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, f"{name}.app")
            if os.path.exists(path):
                if not overwrite:
                    return {"error": f"{path} exists; pass overwrite=true to replace it", "action": act, "path": path}
                shutil.rmtree(path)
            cmd = ["osacompile", "-o", path] + (["-l", "JavaScript"] if language == "jxa" else [])
            rc, out, err = _run(cmd, stdin=script, timeout=60)
            if rc != 0:
                return {"error": (err.strip().splitlines() or [f"osacompile exit {rc}"])[-1], "stderr": err.strip(),
                        "action": act, "language": language}
            with open(os.path.join(path, "Contents", "Resources", "source." + ("js" if language == "jxa" else "applescript")),
                      "w", encoding="utf-8") as f:
                f.write(script)
            return {"ok": True, "name": name, "path": path, "language": language,
                    "run": f'open "{path}"',
                    "note": "The applet is a real app: the first run asks the user to allow it to control each app it "
                            "scripts. Launch it with Finder, the Dock, a Shortcut (Open App) or a launchd job."}

        if act == "run_applet":
            if not name:
                return {"error": "name is required", "action": act}
            path = name if name.endswith(".app") and os.path.isabs(name) else os.path.join(_automations_dir(), f"{name}.app")
            if not os.path.isdir(path):
                return {"error": f"no applet at {path}", "action": act}
            rc, out, err = _run(["open", "-W", path], timeout=TIMEOUT_S)
            return {"ok": rc == 0, "rc": rc, "path": path, "stderr": err.strip(),
                    **({"error": (err.strip().splitlines() or [f"open exit {rc}"])[-1]} if rc else {})}

        if act == "delete_applet":
            if not name:
                return {"error": "name is required", "action": act}
            path = os.path.join(_automations_dir(), f"{os.path.basename(name).removesuffix('.app')}.app")
            if not os.path.isdir(path):
                return {"error": f"no applet at {path}", "action": act}
            shutil.rmtree(path)
            return {"ok": True, "deleted": path}

        return {"error": f"unknown action {action!r}", "action": act}
    except Exception as e:  # noqa: BLE001 — the contract is "never raises"
        return {"error": f"{type(e).__name__}: {e}", "action": act}


if __name__ == "__main__":
    import sys
    print(json.dumps(main(**(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {})), indent=1, default=str))
