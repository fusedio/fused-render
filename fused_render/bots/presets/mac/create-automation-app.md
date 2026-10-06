# Build an automation app (applet) for the Mac
trigger: create an automation app, make an applet, automation app, build an automation, double-click to run, run it every morning, schedule on my mac, turn this into an app, save this script as an app

1. `tool` mac_status and read `supports`: on macOS 14 or 15 there are no Shortcuts automations, so a scheduled automation is an applet started by launchd; on 26 a Shortcuts automation can open the applet. Tell the user which applies.
2. Gather what the automation should do, in order: which apps it touches, what it reads or changes, what it should show at the end (a notification is the usual sign of success). Ask one short question if the goal is unclear; otherwise propose the steps in plain words.
3. `tool` mac_probe for every app the script touches. For a denied app, explain the grant and stop here: the applet will ask for its own grant on first run, but you cannot test the script without one.
4. Write the script. AppleScript for system toggles and single-app one-liners; JXA (`function run(argv)`, `return JSON.stringify(...)`) for app data. Keep names and text as literals the user approved; nothing typed by the user goes into the body unquoted. End with `display notification` so the applet reports itself.
5. `tool` mac_compile_check with the script. Fix syntax until it passes.
6. `tool` mac_run_script once with the same script to prove it does what it says (skip only when running it would send or delete something the user has not approved yet). Show the user the result and the full script.
7. After approval `tool` mac_create_applet with a short `name` (letters, digits, spaces) and the script; `overwrite=true` only when the user asked to replace it. Report the path, that the first double-click asks macOS to allow the applet to control each app, and how to launch it: Finder/Dock, Login Items, a Shortcut's "Open App" action, or a launchd job (`launchctl` plist calling `open <path>`) for a schedule.
8. `tool` mac_applets to confirm the applet is listed; report the list.
