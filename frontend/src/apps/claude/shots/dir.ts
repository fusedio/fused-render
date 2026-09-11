// WHERE A SHOT GOES, AND WHAT IT IS CALLED (T:9012-9040, T:10041, T:11375).
//
// Our own 0700 temp dir, asked for once per agent dir and cached — NOT the
// user's project, a screenshot is not their file. Fetched lazily rather than at
// boot so a chat that never attaches never makes the directory, and a failure
// clears the cache so the next attach tries again instead of degrading forever
// (T:9019).
import { runAgent } from "../protocol/agent";

/** Resolved dirs, kept synchronously readable: `readDirs` has to know which
 *  attachment paths are ALREADY covered by the spawn line's standing Read rule
 *  and it runs inside the send with nothing to await on, so the answer is
 *  remembered the moment it lands (T:9018 `shotDirSeen`). */
const seen = new Map<string, string>();
const pending = new Map<string, Promise<string>>();

/** The shots dir for one agent dir. One in-flight promise per dir; a rejection
 *  is not cached (T:9019-9036). */
export function shotsDir(agentDir: string): Promise<string> {
  const cached = seen.get(agentDir);
  if (cached) return Promise.resolve(cached);
  const running = pending.get(agentDir);
  if (running) return running;
  const p = runAgent(agentDir, "shots_dir", {}, { key: null })
    .then((r) => {
      // The union is `{dir}` | `{error}`; a handler that answered neither is the
      // same failure as one that answered `error` (T:9022).
      const dir = "dir" in r ? r.dir : "";
      if (!dir) throw new Error(("error" in r && r.error) || "no screenshot directory");
      seen.set(agentDir, dir);
      return dir;
    })
    .catch((err: unknown) => {
      pending.delete(agentDir);
      throw err;
    });
  pending.set(agentDir, p);
  return p;
}

/** What `shotsDir` has already answered, without awaiting. `""` until then,
 *  which is a safe default: the worst it costs is one redundant Read rule
 *  (T:9018). */
export function shotsDirSeen(agentDir: string): string {
  return seen.get(agentDir) || "";
}

/** Test-only: the cache is a page-lifetime memo in production. */
export function resetShotsDirForTests(): void {
  seen.clear();
  pending.clear();
}

/** Always "/", never a guessed platform separator: agent.py hands the directory
 *  back through `_wire_path` (forward slashes on every platform) and spells the
 *  double-slash Read allow-rule from the SAME normalisation, which the CLI
 *  matches as TEXT — so a backslash join would sit outside its own
 *  pre-approval and card every shot (T:9039). */
export function shotJoin(dir: string, name: string): string {
  return dir.replace(/[\\/]+$/, "") + "/" + name;
}

/** The filename prefix every file one capture writes shares: a 14-digit UTC
 *  timestamp plus 8 hex of uuid, so two captures in the same second cannot
 *  collide (T:10041). */
export function shotStamp(): string {
  return (
    new Date().toISOString().replace(/[^0-9]/g, "").slice(0, 14) +
    "-" +
    crypto.randomUUID().slice(0, 8)
  );
}

/** Name suffixes: the camera's whole-pane shot (T:10096) and the send-time
 *  badged overview (T:10180). */
export const SHOT_SUFFIX_VIEW = "-view";
export const SHOT_SUFFIX_OVERVIEW = "-overview";

/** MIME → extension (T:11375). A clipboard image arrives as `image/png` with NO
 *  filename at all, so the type is the only evidence there is. */
export const SHOT_MIME_EXT: Record<string, string> = {
  "image/png": ".png",
  "image/jpeg": ".jpg",
  "image/webp": ".webp",
  "image/gif": ".gif",
  "image/bmp": ".bmp",
  "image/avif": ".avif",
};

/** The extension a pasted blob has EARNED, from its MIME type — the name has to
 *  follow the content or the agent's `Read` gets bytes its extension lied about
 *  (T:11375). Falls back to the name's own extension, then `.png`. */
export function shotFileExt(type: string | undefined, name: string | undefined): string {
  const known = SHOT_MIME_EXT[type || ""];
  if (known) return known;
  const n = name || "";
  const dot = n.lastIndexOf(".");
  return dot > 0 ? n.slice(dot).toLowerCase() : ".png";
}

/** The last segment of a path, either separator (T:11651). */
export function shotBase(path: string): string {
  const parts = String(path || "").split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : String(path || "");
}

/** The directory part, in the forward-slash spelling `_wire_path` hands the page
 *  — the Read rule is matched as TEXT, so the two have to agree (T:11658). */
export function shotDirOf(path: string): string {
  const norm = String(path || "").replace(/\\/g, "/");
  const cut = norm.lastIndexOf("/");
  return cut > 0 ? norm.slice(0, cut) : norm;
}
