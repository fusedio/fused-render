// Sessions app entry point (the Claude Code session inbox). Like learn, the
// experience is pure html+py content: the bundled sessions.zip is mounted
// read-only at `${mounts_root}/sessions` (SESSIONS_MOUNT_NAME in
// shell/mounts/automount.py — always "sessions") and renders through the same
// /render + runtime.js engine as any user view. The shell renders it at
// /sessions (chrome-free); readiness is polled via useSessionsMountReady
// (@platform/lib/hooks). Mutable state (triage, names) is written by the
// bundled .py files to ~/.fused-render/claude-sessions, never to the mount.

// The fs path of the sessions landing page (the bundled inbox.html), or null
// when the mounts root isn't known yet.
export function sessionsEntryPath(config: { mounts_root?: string | null }): string | null {
  if (!config.mounts_root) return null;
  return `${config.mounts_root.replace(/\/+$/, "")}/sessions/inbox.html`;
}
