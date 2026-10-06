# Dark mode, volume, notifications, frontmost app
trigger: dark mode, light mode, turn on dark mode, set the volume, mute, louder, quieter, notify me, send me a notification, what app is in front, which apps are open

1. Reads need no grant: `tool` mac_dark_mode, mac_volume, mac_frontmost give the current state. Report it.
2. Changes: `tool` mac_set_dark_mode with `value` true/false, mac_set_volume with `value` 0-100, mac_notify with `message`, `title`, `subtitle`. These are reversible and need no grant; run them when the task asks and confirm the new state by reading it back.
3. If the task wants this on a schedule or as a one-click thing, offer to build it as an automation app (the "Build an automation app" playbook).
4. If a System Events call returns -1743, this process lacks the System Events Automation grant: tell the user to allow it under System Settings > Privacy & Security > Automation and stop.
5. Report what changed in one line (e.g. "Dark mode on, volume 40, notification shown").
