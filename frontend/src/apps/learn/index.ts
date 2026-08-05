// Learn app entry point. The learn experience is pure html+py content: the
// bundled learn.zip is mounted read-only at `${mounts_root}/learn` (D123,
// LEARN_MOUNT_NAME in shell/mounts.py — always "learn") and renders through
// the same /render + runtime.js engine as any user view. The shell renders it
// at /learn (chrome-free — no breadcrumb, no preview header); readiness is
// polled via useLearnMountReady (@platform/lib/hooks).

// The fs path of the learn landing page (the bundled index.html), or null when
// the mounts root isn't known yet.
export function learnEntryPath(config: { mounts_root?: string | null }): string | null {
  if (!config.mounts_root) return null;
  return `${config.mounts_root.replace(/\/+$/, "")}/learn/index.html`;
}
