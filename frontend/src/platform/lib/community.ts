// The community marketplace bridge, shared by every surface that talks to
// the marketplace backend (the /apps hub's Showcase tab, the explorer
// preview's Clone button). The backend is native server-side now
// (fused_render/community.py behind POST /api/community); it reports its own
// user-facing failures as {status:"error", message}, surfaced verbatim here.
import { postJson } from "@platform/lib/api";

export async function runCommunity<T extends { status?: string; message?: string }>(
  params: Record<string, unknown>,
): Promise<T> {
  const r = await postJson<T>("/api/community", params);
  if (r?.status === "error") throw new Error(r.message || "community backend failed");
  return r;
}

// Fire-and-forget open marker — ordering metadata only, never blocks the open.
export function touchCommunityApp(slug: string): void {
  void runCommunity({ action: "touch", slug }).catch(() => undefined);
}

// The slug of the community CATALOG CACHE app a path belongs to, or null.
// Apps sit directly under ~/.fused-render/community/repo/<slug>/ (community.py
// CACHE_REPO + _materialize), and slugs are [a-z0-9-] by the same file's
// _require_slug — so a match here is a path inside the read-only preview cache,
// the tree `refresh` resets on every catalog pull. The workspace copy an
// install produces lives elsewhere (Fused/local/<slug>) and never matches.
export function communityCacheSlug(fsPath: string): string | null {
  // Windows callers may carry backslashes; normalize like urlForFsPath does.
  const norm = fsPath.replace(/\\/g, "/");
  const m = norm.match(/\/\.fused-render\/community\/repo\/([a-z0-9][a-z0-9-]{1,63})(?:\/|$)/);
  return m ? m[1] : null;
}
