// The community marketplace bridge, shared by every surface that talks to
// the marketplace backend (the /apps hub's Showcase tab, the explorer
// preview's Clone button). The backend is native server-side now
// (fused_render/community.py behind POST /api/community); it reports its own
// user-facing failures as {status:"error", message}, surfaced verbatim here.
import { postJson } from "@platform/lib/api";

// The workspace tag dir the showcase clone lands in (community.py SHOWCASE_DIR).
export const SHOWCASE_TAG = "showcase";

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

