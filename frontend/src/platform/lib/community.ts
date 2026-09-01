// The community marketplace bridge, shared by every surface that talks to
// the marketplace backend (the /apps hub's Showcase tab). The backend is
// native server-side now (fused_render/community.py behind POST
// /api/community); it reports its own user-facing failures as
// {status:"error", message}, surfaced verbatim here.
import { postJson } from "@platform/lib/api";

export async function runCommunity<T extends { status?: string; message?: string }>(
  params: Record<string, unknown>,
): Promise<T> {
  const r = await postJson<T>("/api/community", params);
  if (r?.status === "error") throw new Error(r.message || "community backend failed");
  return r;
}
