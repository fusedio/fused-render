// The pages Claude PUBLISHED from this target's working directory —
// `./artifacts.py` (T:18475-18683, inventory 05 §E).
//
// Two reads, and they answer different questions. `list` is the landing page's:
// every page published from this folder across every past session, newest
// first, which is what the "Artifacts" list draws. `live` is the transcript's:
// the pages THIS session has published, which is what the chip strip above the
// composer draws.
//
// NOTHING HERE THROWS AT THE UI. An unreachable index is not news on a landing
// page whose subject is the composer above it, so a failed read is an absent
// section — `[]` — and a console warning, exactly as in T.
import { runArtifacts } from "./agent";

/** One row of `{action:"list"}` (artifacts.py:43-47). Every field is optional
 *  because the join that fills them is best-effort: a publish that let the
 *  HTML's own `<title>` name it echoes back no title, and a republish routinely
 *  omits the favicon. */
export interface Artifact {
  remote_url: string;
  file_path?: string;
  title?: string;
  description?: string;
  favicon?: string;
  session_id?: string;
  cwd?: string;
  created_at?: number;
  updated_at?: number;
  /** `true` = the local source is really there, `false` = KNOWN gone (dropped
   *  by `_list` before it reaches here), `null`/absent = a mount-backed path
   *  the server refuses to stat, where the hosted page is the one door that
   *  cannot hang (T:18521-18526). */
  exists?: boolean | null;
}

/** The title the page published under, falling back to the local file's
 *  basename: an untitled row is worse than a filename (T:18495). */
export function artLabel(a: Artifact): string {
  return (
    a.title ||
    String(a.file_path || "")
      .split(/[\\/]/)
      .pop() ||
    a.remote_url
  );
}

/** The shell route for the LOCAL source, mirroring `router.ts urlForFsPath`:
 *  only a DRIVE-LETTER path has its backslashes rewritten, because a backslash
 *  is a legal POSIX filename char and must round-trip (T:18485-18491). */
export function artOpenHref(path: string): string {
  const norm = /^[A-Za-z]:[\\/]/.test(path)
    ? String(path).replace(/\\/g, "/")
    : String(path);
  const segs = norm
    .replace(/^\/+/, "")
    .split("/")
    .filter(Boolean)
    .map(encodeURIComponent);
  return "/explorer/view/" + segs.join("/");
}

/** Which door a row's own press opens. `exists === true` and a path means the
 *  local file — the thing this window can show; otherwise the hosted page
 *  (T:18519-18531). Either way a row is always pressable. */
export function artLocalPath(a: Artifact): string | null {
  return a.exists === true && a.file_path ? a.file_path : null;
}

function readRows(out: { artifacts?: unknown[] }): Artifact[] {
  return (out.artifacts || []).filter(
    (a): a is Artifact =>
      !!a &&
      typeof a === "object" &&
      typeof (a as Artifact).remote_url === "string",
  );
}

/** T:18547 `loadArtifacts` — the landing page's read, made on every path ONTO
 *  the landing page (boot and Back), not once per page life: a turn that just
 *  published a page must be able to leave it behind here. */
export async function loadArtifacts(
  agentDir: string,
  file: string | null,
): Promise<Artifact[]> {
  try {
    const out = await runArtifacts(
      agentDir,
      { action: "list", file: file ?? "" },
      { key: null },
    );
    if (out.error) throw new Error(out.error);
    return readRows(out);
  } catch (err) {
    console.warn(
      "artifacts list failed:",
      err instanceof Error ? err.message : String(err),
    );
    return [];
  }
}

/** T:18604 `pollArtifacts` — one transcript read, deliberately dumb: the whole
 *  returned list is re-absorbed every time and the caller's Map dedupes by url,
 *  because a publish that arrives twice must be idempotent anyway (a redeploy
 *  reports the same url again). */
export async function pollArtifacts(
  agentDir: string,
  file: string | null,
  sessionId: string,
): Promise<Artifact[]> {
  if (!sessionId) return [];
  try {
    const out = await runArtifacts(
      agentDir,
      { action: "live", session_id: sessionId, file: file ?? "" },
      { key: null },
    );
    return readRows(out);
  } catch (err) {
    // A poll for decoration must never interrupt the poll for the reply.
    console.warn(
      "artifacts poll failed:",
      err instanceof Error ? err.message : String(err),
    );
    return [];
  }
}

/** Change detection by CONTENT, not by count: the favicon/title join can land a
 *  poll or two after the frame-link, so an already-seen url can gain metadata
 *  without the set growing (T:18627-18634). */
export function artSignature(rows: Iterable<Artifact>): string {
  return Array.from(
    rows,
    (a) => `${a.remote_url} ${a.title || ""} ${a.favicon || ""}`,
  ).join(" | ");
}
