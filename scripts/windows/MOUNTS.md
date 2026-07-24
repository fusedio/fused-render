# Remote mounts on Windows

Fused Render can mount remote storage (S3, Google Drive, …) so it appears as an
ordinary local folder under `~/.fused-render/mounts/<name>`. On Windows this is
served by **[WinFsp](https://winfsp.dev/rel/)** — the same user-mode filesystem
driver rclone itself uses for its Windows `mount` command.

## Install WinFsp

WinFsp is **not bundled** with the installer. Download and install it once from:

> https://winfsp.dev/rel/

It is a kernel-mode driver, so installing it needs administrator rights and (on
some systems) a reboot. Once installed, creating a mount from the Mounts page
just works — no further setup.

Why it isn't bundled: WinFsp ships under GPLv3-with-a-linking-exception, and this
project's own license is still being decided, so we point at the official
installer for now (exactly as rclone does) rather than redistribute the MSI.
Bundling it into the installer may be revisited after that licensing decision.
See `DECISIONS.md` (D132) for the full rationale.

## If a mount fails

- **"Windows mounts require WinFsp…"** — WinFsp isn't installed (or the app
  can't find it). Install it from the link above and retry.
- **A mount won't disconnect** — a WinFsp mount is backed by the background
  `rclone` daemon and clears when that process exits; quitting Fused Render (or
  disconnecting the mount) removes it. There is no `umount`-style force on
  Windows.
