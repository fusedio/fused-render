"""Self-update machinery shared by the desktop packages.

`common` holds the platform-neutral core (signed-manifest fetch/verify and
the hash-verified download) that both the Windows supervisor updater
(supervisor/_win32/update.py) and the macOS in-app updater (`mac`) build on.
"""
