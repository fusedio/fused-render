// Community marketplace app entry point. The marketplace is pure html+py
// content (docs/COMMUNITY_MARKETPLACE_SPEC.md): the bundled community.zip is
// mounted read-only at `${mounts_root}/community` (COMMUNITY_MOUNT_NAME in
// shell/mounts) and renders through the same /render + runtime.js engine as
// any user view. The shell renders it at /community (chrome-free); readiness
// is polled via useCommunityMountReady (@platform/lib/hooks).

// The fs path of the marketplace landing page (the bundled index.html), or
// null when the mounts root isn't known yet.
export function communityEntryPath(config: { mounts_root?: string | null }): string | null {
  if (!config.mounts_root) return null;
  return `${config.mounts_root.replace(/\/+$/, "")}/community/index.html`;
}
