// Learn app entry point. The learn experience is pure html+py content: the
// bundled learn.zip is mounted read-only at `${mounts_root}/learn` (D123,
// LEARN_MOUNT_NAME in shell/mounts.py — always "learn") and renders through
// the same /render + runtime.js engine as any user view. The shell only needs
// this doorway; readiness is polled via useLearnMountReady (@platform/lib/hooks).
import { statPath } from "@platform/lib/api";
import { currentUrl, navigate } from "@platform/lib/router";

// Open the learn experience: prefer the bundled index.html as the landing
// page when it exists; fall back to the mount folder otherwise (older
// learn.zip builds). The stat can be slow (mount-backed read); if the user
// navigated elsewhere while it was in flight, don't yank them back.
export async function openLearn(config: { mounts_root?: string | null }): Promise<void> {
  if (!config.mounts_root) return;
  const root = `${config.mounts_root.replace(/\/+$/, "")}/learn`;
  const before = currentUrl();
  let dest = root;
  let destIsDir = true;
  try {
    const st = await statPath(`${root}/index.html`);
    if (!st.is_dir) {
      dest = `${root}/index.html`;
      destIsDir = false;
    }
  } catch {
    // stat 404s (or the mount is briefly not attached) — open the folder.
  }
  if (currentUrl() === before) navigate(dest, { isDir: destIsDir });
}
