// The Add-mount form's pure decisions, extracted from the component so they can
// be tested without a renderer (the same split lib/oauth.ts and lib/uploads.ts
// use). Everything here answers one question about a pasted link or an existing
// set of remotes; nothing here touches React or the network.
import type { RemoteKind, RemoteProvider } from "../../lib/api";

// A storage location pasted as a URL, reduced to the rclone-relative form the
// Path field wants: a provider ("s3" | "gcs") and a `bucket/prefix` string (the
// key path an rclone S3/GCS remote is addressed by). null when the input isn't a
// recognized storage link, so the caller leaves the manual fields untouched.
export type ParsedLink = { provider: "s3" | "gcs"; path: string };

// Strip leading slashes and trailing whitespace; rclone paths are relative to
// the remote and never start with "/".
const stripLead = (p: string) => p.replace(/^\/+/, "").replace(/\s+$/, "");
const joinPath = (bucket: string, rest: string) => {
  const r = stripLead(rest);
  return r ? `${bucket}/${r}` : bucket;
};

export function parseStorageUrl(raw: string): ParsedLink | null {
  const s = raw.trim();
  if (!s) return null;

  // Scheme URIs: s3://bucket/prefix, gs://bucket/prefix (gcs:// tolerated too).
  let m = /^s3:\/\/(.+)$/i.exec(s);
  if (m) return { provider: "s3", path: stripLead(m[1]) };
  m = /^gc?s:\/\/(.+)$/i.exec(s);
  if (m) return { provider: "gcs", path: stripLead(m[1]) };

  let u: URL;
  try {
    u = new URL(s);
  } catch {
    return null;
  }
  if (u.protocol !== "http:" && u.protocol !== "https:") return null;
  const host = u.hostname.toLowerCase();
  const segs = u.pathname.split("/").filter(Boolean).map((x) => {
    try {
      return decodeURIComponent(x);
    } catch {
      return x;
    }
  });
  const qsPrefix = u.searchParams.get("prefix") ?? "";

  // AWS S3 console link shapes: the bucket view …/s3/buckets/<bucket>?prefix=a/b/
  // and the object view …/s3/object/<bucket>[/<key>]?prefix=<key>. Require one of
  // those markers so an unrelated AWS console page (ec2, iam, …) isn't mistaken
  // for a bucket and doesn't auto-fill a bogus path from its last URL segment.
  if (host.endsWith("console.aws.amazon.com")) {
    const bi = segs.indexOf("buckets");
    const oi = segs.indexOf("object");
    const bucket = bi >= 0 ? segs[bi + 1] : oi >= 0 ? segs[oi + 1] : "";
    if (!bucket) return null;
    // The object view may carry the key in the path after the bucket; both
    // shapes may carry it in ?prefix=.
    const inPath = oi >= 0 ? segs.slice(oi + 2).join("/") : "";
    return { provider: "s3", path: joinPath(bucket, qsPrefix || inPath) };
  }
  // GCP console: …/storage/browser/<bucket>/<prefix> — likewise require the
  // "browser/<bucket>" marker; other cloud-console pages are not storage links.
  if (host.endsWith("console.cloud.google.com")) {
    const bi = segs.indexOf("browser");
    if (bi < 0) return null;
    const rest = segs.slice(bi + 1);
    // The single-object view inserts a "_details" marker before the bucket
    // (…/browser/_details/<bucket>/<key>). Reading it as the bucket produced a
    // path no remote could ever serve.
    if (rest[0] === "_details") rest.shift();
    if (!rest.length) return null;
    // The console hangs matrix parameters on the BUCKET segment
    // ("my-bucket;tab=objects"). That is UI state, not part of the name.
    // Stripped from the bucket only: ";" is legal in an object name, so doing
    // this to every segment would corrupt real keys.
    rest[0] = rest[0].split(";")[0];
    if (!rest[0]) return null;
    const base = rest.join("/");
    // ?prefix= appears on the BUCKET view, where it carries the whole key path;
    // a deeper URL already encodes that path in its segments, so appending
    // there would duplicate it.
    return {
      provider: "gcs",
      path: rest.length === 1 && qsPrefix ? joinPath(base, qsPrefix) : base,
    };
  }
  // GCS path-style data hosts.
  if (host === "storage.googleapis.com" || host === "storage.cloud.google.com") {
    return segs.length ? { provider: "gcs", path: segs.join("/") } : null;
  }
  // GCS virtual-hosted: <bucket>.storage.googleapis.com/<prefix>
  if (host.endsWith(".storage.googleapis.com")) {
    const bucket = host.slice(0, -".storage.googleapis.com".length);
    return { provider: "gcs", path: joinPath(bucket, segs.join("/")) };
  }
  if (host.endsWith(".amazonaws.com")) {
    // Path-style: s3.amazonaws.com/<bucket>/… or s3.<region>.amazonaws.com/<bucket>/…
    if (host === "s3.amazonaws.com" || /^s3[.-]/.test(host)) {
      return segs.length ? { provider: "s3", path: segs.join("/") } : null;
    }
    // Virtual-hosted: <bucket>.s3.<region>.amazonaws.com/<prefix> (also s3-<region>).
    const vm = /^(.+?)\.s3[.-]/.exec(host);
    if (vm) return { provider: "s3", path: joinPath(vm[1], segs.join("/")) };
  }
  return null;
}

// A trailing segment with a short extension (e.g. "TCI.tif", "part-0001.parquet")
// — but NOT one whose extension names a directory this app browses as a folder
// (.zarr, .gdb): those are prefixes, not objects, so a link ending in (or under)
// one must keep the directory in the path. Used to tell a link-to-a-file from a
// link-to-a-prefix.
const FILE_EXT = /\.([A-Za-z0-9]{1,8})$/;
const DIR_EXTS = new Set(["zarr", "gdb"]);
function looksLikeFile(seg: string): boolean {
  const m = FILE_EXT.exec(seg);
  return !!m && !DIR_EXTS.has(m[1].toLowerCase());
}

// The path to actually mount for a pasted link. Pasting a deep link to a single
// FILE — e.g. s3://sentinel-cogs/sentinel-s2-l2a-cogs/32/T/QR/2025/8/…/TCI.tif —
// should not mount that one scene folder (let alone the file); the useful mount
// is the dataset root, bucket + first prefix segment
// (sentinel-cogs/sentinel-s2-l2a-cogs), which you then browse. A link to a
// PREFIX (no file tail — a bucket root, a trailing-slash prefix, a .zarr/.gdb
// directory, a console ?prefix=) is kept verbatim, since navigating there was
// deliberate. Either way the Path field stays editable, so this is only the
// starting suggestion.
export function mountRootForLink(path: string): string {
  const segs = path.split("/").filter(Boolean);
  if (!segs.length || !looksLikeFile(segs[segs.length - 1])) return path;
  const [bucket, ...key] = segs;
  // key.length > 1 ⇒ there's a prefix directory before the file — keep it (even
  // a dotted one like "data.zarr", which is a directory, not the object). A lone
  // key segment IS the file (sits directly under the bucket) ⇒ just the bucket.
  return key.length > 1 ? `${bucket}/${key[0]}` : bucket;
}

// add_mount() strips the name and rejects it empty or containing / \ : or a
// leading dot; mirror that when deriving so the auto-filled value always passes
// server validation (or is empty, which disables the Add button).
export const folderSafe = (s: string) => s.trim().replace(/[/\\:]/g, "").replace(/^\.+/, "");

// A segment that names nothing on its own: a year, a day, a partition number —
// "2026", "2026-08-04", "01", "0000_0". Anchored on a digit so "v2" and "32N"
// stay meaningful names.
const OPAQUE_SEG = /^[0-9][0-9\-_.]*$/;

// The folder name to suggest for a mount path. The last segment, which is
// usually the dataset — except when that segment is a bare date or number, in
// which case it is meaningless on its own ("2026") AND collides with the next
// dataset's identical tail, so it is qualified by its parent
// ("telemetry-2026"). A single-segment path is the bucket, which names itself.
export function suggestMountName(path: string): string {
  const segs = path.split("/").map((s) => s.trim()).filter(Boolean);
  const last = segs[segs.length - 1] ?? "";
  const parent = segs.length > 1 ? segs[segs.length - 2] : "";
  const raw = OPAQUE_SEG.test(last) && parent ? `${parent}-${last}` : last;
  return folderSafe(raw);
}

// Everything the Remote picker can offer, as one shape: a remote the user has
// (value = its verbatim rclone spec) and a suggestion that becomes one on submit
// (value = "suggest:<id>") differ only in `creates`.
export interface RemoteChoice {
  value: string;
  label: string;
  kind: RemoteKind;
  provider: RemoteProvider;
  creates: boolean;
}

// How much a remote can actually read, most-capable first. This used to be the
// other way round — public first — on the theory that a pasted link is usually
// open data and an unsigned read works even with no credentials. Live testing
// killed it: pasting a link to a PRIVATE bucket preselected "public datasets
// (no credentials)" while the user's own credentialed AWS remote sat in the
// same list, and the mount failed with an access error on a remote that could
// never have served it. A credentialed remote reads open data too, so ranking
// it first is strictly better; the anonymous one is the fallback for a machine
// that has no credentials at all.
const KIND_RANK: Record<RemoteKind, number> = { other: 0, detected: 1, public: 2 };

// Whether a setup flow's handoff should move the Remote picker now.
//
// Two separate hazards, which is why `applied` exists at all. The remote is
// routinely ABSENT when the preselect arrives (finishSetup sets it as the modal
// closes, before getMounts() lands), so this must keep saying "not yet" rather
// than firing once and giving up. And the mount list re-reads itself on a timer
// and on window focus, so once applied it must never fire AGAIN — that would
// throw away a Remote the user has since picked by hand.
export function shouldApplyPreselect(
  pending: string | null,
  applied: string | null,
  remoteNames: string[],
): boolean {
  return !!pending && pending !== applied && remoteNames.includes(pending);
}

// The <option> value to select for a pasted link's provider. Cost first: a
// remote that already EXISTS beats any suggestion, because picking a suggestion
// commits the user to creating a remote they never asked for as a side effect
// of pasting a link. Within each of those two tiers, KIND_RANK. undefined when
// nothing matches — the link still fills Path/Name and the user picks.
export function pickRemote(
  choices: RemoteChoice[],
  provider: "s3" | "gcs",
): string | undefined {
  return choices
    .filter((c) => c.provider === provider)
    .sort(
      (a, b) => Number(a.creates) - Number(b.creates) || KIND_RANK[a.kind] - KIND_RANK[b.kind],
    )[0]?.value;
}
