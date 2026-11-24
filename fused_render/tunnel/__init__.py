"""Reaching the phone listener (lan.py) away from the Wi-Fi, over Tailscale.

See docs/REMOTE_ACCESS.md for the design. The listener already binds every
interface, so a tailnet needs nothing run here: the phone dials the
computer's Tailscale name and meets the SAME self-signed certificate it
pinned when it scanned the pairing QR.

- ``tailscale`` — reads the computer's tailnet identity from the CLI.
- ``host``      — the away-access switch and what it tells lan.py.
"""
