# What can this Mac automate
trigger: what can you automate, mac status, check my mac, which apps are granted, automation permissions, what macos do i have, can i use shortcuts automations, does this mac support

1. `tool` mac_status: macOS version and name, arch, shortcuts count, applets, `supports` and `note`.
2. `tool` mac_probe for every app: the granted and denied lists.
3. `tool` mac_shortcuts and mac_applets for what already exists.
4. Work out what the version allows: scripting always; Shortcuts automations and Use Model need macOS 26; Apple Intelligence actions need 15.1 on Apple silicon; before 26 a schedule means an applet started by launchd.
5. Report as bullets: version and what it supports, apps granted vs denied with the one-line fix for denied ones (System Settings > Privacy & Security > Automation, or ~/Fused/tools/grant_automation.py from Terminal), the shortcuts and applets found.
6. Offer one concrete automation that fits what is granted, and name the playbook that would build it.
