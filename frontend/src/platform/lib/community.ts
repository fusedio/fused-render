// The community.py bridge, shared by every surface that talks to the
// marketplace backend (the /apps hub's community tab, the explorer preview's
// Clone button). The marketplace is html+py content: there is no dedicated
// REST surface, and community.py deliberately can't be imported by the server
// — actions go through POST /api/run against the mounted script.
import { getConfig, postJson } from "@platform/lib/api";

// The mounted backend's path never changes within a session; getConfig() is
// awaited once and the result reused by every action call.
let pyPathPromise: Promise<string> | null = null;
function communityPy(): Promise<string> {
  if (!pyPathPromise) {
    pyPathPromise = getConfig().then((config) => {
      if (!config.mounts_root) throw new Error("community content is not available yet");
      return `${config.mounts_root.replace(/\/+$/, "")}/community/community.py`;
    });
    // A failed config fetch must not poison every later call.
    pyPathPromise.catch(() => (pyPathPromise = null));
  }
  return pyPathPromise;
}

// /api/run's wire shape ({ok, result, error}); community.py additionally
// reports its own user-facing failures as {status:"error", message}.
interface RunEnvelope<T> {
  ok: boolean;
  result: T;
  error?: { message?: string };
}

export async function runCommunity<T extends { status?: string; message?: string }>(
  params: Record<string, unknown>,
): Promise<T> {
  const py = await communityPy();
  const r = await postJson<RunEnvelope<T>>("/api/run", { py, params });
  if (!r.ok) throw new Error(r.error?.message || "community backend failed");
  if (r.result?.status === "error") throw new Error(r.result.message || "community backend failed");
  return r.result;
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
