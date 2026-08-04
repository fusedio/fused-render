// Mounts page — the /view/_mounts sentinel, entered from the sidebar
// footer. Remote storage (S3-compatible and anything else rclone speaks)
// mounted as local folders under ~/.fused-render/mounts; everything
// downstream — previews, readers, tile servers — sees ordinary local paths.
// Backend: shell/mounts.py (rclone rcd). Credentials live in rclone's
// own config, never here. Section layout and per-action busy/error state
// follow views/Preferences.tsx.
import { useEffect, useRef, useState } from "react";
import {
  cancelRemoteOAuth,
  createDetectedRemote,
  createMount,
  createRemote,
  deleteMount,
  getMounts,
  getRemoteOAuthStatus,
  reconnectMount,
  restartRclone,
  startRemoteOAuth,
} from "../lib/api";
import type {
  Mount,
  MountsResult,
  MountUploads,
  RcloneRemote,
  RemoteOAuthStatus,
  RemoteSuggestion,
} from "../lib/api";
import { OAUTH_PROVIDERS, oauthCancelOutcome, oauthTick } from "../lib/oauth";
import type { OAuthDecision, OAuthProvider, OAuthProviderKey } from "../lib/oauth";
import {
  clearGoogleClient,
  googleConsoleUrls,
  loadGoogleClient,
  parseGoogleClientJson,
  saveGoogleClient,
} from "../lib/google-client";
import type { GoogleOAuthClient } from "../lib/google-client";
import { useRefreshOnReturn } from "../lib/hooks";
import { navigate } from "../lib/router";
import { hasDrainingUploads, uploadNotice } from "../lib/uploads";
import { Modal } from "../components/modal/Modal";
import { ErrorBanner } from "../components/ErrorBanner";
import { Field, Select, TextInput } from "../components/field/fields";
import { ProviderIcon } from "../components/ProviderIcons";

// Files written to this mount that haven't reached the remote yet (D207).
// Worth its own line because a mount caches writes locally and uploads them
// afterwards: the user already saw the save succeed, so a rejection at the
// remote (quota, permissions, a revoked token) is otherwise completely
// invisible.
//
// Three states, and conflating any two of them is the bug this shape exists to
// prevent — see lib/uploads.ts, which owns the decision and is tested there.
function UploadQueue({ uploads }: { uploads?: MountUploads | null }) {
  const notice = uploadNotice(uploads);
  switch (notice.kind) {
    case "none":
      return null;
    case "unknown":
      return (
        <div className="mount-hint warn" title={notice.reason}>
          Upload status unavailable — saved files may not have reached the remote.
        </div>
      );
    case "failed":
      return (
        <div
          className="mount-hint warn"
          title="These files were saved on your computer but the remote rejected the upload — rclone keeps retrying with a growing delay."
        >
          {notice.failed} {notice.failed === 1 ? "file has" : "files have"} not reached the
          remote
          {notice.names.length > 0 && <> — {notice.names.join(", ")}</>}
          {notice.truncated && <> …</>}. Saved locally; still retrying.
        </div>
      );
    case "pending":
      return (
        <div className="mount-hint" title="Saved on your computer and uploading to the remote.">
          Uploading {notice.pending} {notice.pending === 1 ? "file" : "files"}…
        </div>
      );
  }
}

function MountRow({
  conn,
  onChanged,
}: {
  conn: Mount;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  // "disconnected": a mount is (or was) there but its rclone daemon no longer
  // serves it — listings show stale/empty data and a plain unmount fails.
  // "stale": the 2026-07-16 split-brain — rclone still lists the mount but the
  // kernel dropped it (e.g. the macOS "Server connections interrupted" dialog's
  // Disconnect). Both are unhealthy and both recover the same way: Reconnect
  // force-clears the dead mountpoint and mounts fresh.
  const dotLabel = {
    mounted: "Mounted",
    disconnected: "Disconnected — remote data is not flowing",
    stale: "Disconnected — the mount dropped; reconnect to restore it",
    unmounted: "Not mounted",
  }[conn.state];
  // Both broken states show the same "disconnected" badge and Reconnect remedy;
  // "stale" is a distinct backend state (for logs/diagnosis) but the same fix.
  const broken = conn.state === "disconnected" || conn.state === "stale";

  return (
    <div className="mount-card">
      <div className="mount-card-main">
        <span className={`mount-dot ${conn.state}`} role="img" aria-label={dotLabel} title={dotLabel} />
        <div className="mount-card-info">
          <div style={{ fontWeight: 600 }}>
            {conn.name}
            {conn.read_only && (
              <span className="mount-hint" title="This remote rejects writes — files open read-only">
                {" "}
                — read-only
              </span>
            )}
            {broken && (
              <span
                className="mount-hint warn"
                title="The mount stopped responding — remote data is not flowing. Use Reconnect to restore it."
              >
                {" "}
                — disconnected
              </span>
            )}
          </div>
          <div className="deploy-muted mount-remote" title={conn.mountpoint}>
            {conn.remote}
          </div>
          <UploadQueue uploads={conn.uploads} />
        </div>
        <div className="mount-card-actions">
          {conn.state === "mounted" ? (
            <button type="button" disabled={busy} onClick={() => navigate(conn.mountpoint, { isDir: true })}>
              Open
            </button>
          ) : (
            // "disconnected", "stale" and "unmounted" all recover the same way: there is
            // no unmount action (mounts automount and stay up), so Reconnect is
            // the single "something's wrong" repair — it force-clears any dead
            // mountpoint and mounts fresh (reconnect_mount also handles the
            // never-mounted case, where it just attaches).
            <button
              type="button"
              disabled={busy}
              onClick={() => act(() => reconnectMount(conn.id))}
            >
              {busy ? "Reconnecting…" : "Reconnect"}
            </button>
          )}
        </div>
        {!conn.builtin && (
          <button
            type="button"
            className="mount-delete"
            disabled={busy}
            title="Delete mount"
            aria-label="Delete mount"
            onClick={() => act(() => deleteMount(conn.id))}
          >
            ✕
          </button>
        )}
      </div>
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </div>
  );
}

// A storage location pasted as a URL, reduced to the rclone-relative form the
// Path field wants: a provider ("s3" | "gcs") and a `bucket/prefix` string (the
// key path an rclone S3/GCS remote is addressed by). null when the input isn't a
// recognized storage link, so the caller leaves the manual fields untouched.
type ParsedLink = { provider: "s3" | "gcs"; path: string };

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
    return rest.length ? { provider: "gcs", path: rest.join("/") } : null;
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

function AddMount({
  remotes,
  suggested,
  onChanged,
}: {
  remotes: RcloneRemote[];
  suggested: RemoteSuggestion[];
  onChanged: () => void;
}) {
  const [name, setName] = useState("");
  const [remote, setRemote] = useState("");
  const [subpath, setSubpath] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Until the user edits Name themselves, it follows the last path segment
  // (the "slug tracks title" pattern) — the mount name and its bucket/prefix
  // are usually the same, so typing the path twice is pure friction.
  const [nameTouched, setNameTouched] = useState(false);

  // add_mount() strips the name and rejects it empty or containing / \ : or a
  // leading dot; mirror that when deriving so the auto-filled value always
  // passes server validation (or is empty, which disables the button below).
  const folderSafe = (s: string) => s.trim().replace(/[/\\:]/g, "").replace(/^\.+/, "");

  const onPathChange = (v: string) => {
    setSubpath(v);
    if (!nameTouched) {
      // Last non-blank segment: trim first so a trailing "/" or a whitespace
      // tail ("bucket/  ") derives the real segment, never a spaces-only name.
      const seg = v.split("/").map((s) => s.trim()).filter(Boolean).pop() ?? "";
      setName(folderSafe(seg));
    }
  };

  // A pasted S3/GCS link (see parseStorageUrl) that auto-fills the fields below.
  const [link, setLink] = useState("");

  // Classify an available remote/suggestion so a pasted link can pick a matching
  // one: which cloud, and whether it's a public (no-credentials) remote. Names +
  // labels are the only client-side signal (e.g. "aws:" + "AWS S3 — default
  // profile", or "aws-open:" + "… public buckets (no credentials)").
  const classify = (nameRaw: string, labelRaw: string) => {
    const n = nameRaw.toLowerCase();
    const l = labelRaw.toLowerCase();
    const provider =
      n.startsWith("gcs") || l.includes("google cloud")
        ? "gcs"
        : n.startsWith("aws") || l.includes("s3")
          ? "s3"
          : "other";
    const isPublic =
      n.includes("open") ||
      l.includes("public") ||
      l.includes("no credentials") ||
      l.includes("anon");
    return { provider, isPublic };
  };

  // The <option> value (a raw remote spec or "suggest:<id>") to select for a
  // pasted link's provider: prefer a PUBLIC (anonymous) remote over a
  // credentialed one — pasted links are usually to open/public data, and an
  // anonymous request works even when creds are absent or expired; the user can
  // switch to their own remote for a private bucket. undefined when nothing
  // matches — the link still fills Path/Name and the user picks.
  const pickRemote = (provider: "s3" | "gcs"): string | undefined => {
    const candidates = [
      ...remotes.map((r) => ({ value: r.name, ...classify(r.name, r.label) })),
      ...suggested.map((s) => ({
        value: `suggest:${s.id}`,
        ...classify(s.remote_name, s.label),
        isPublic: s.kind === "public",
      })),
    ].filter((c) => c.provider === provider);
    return (candidates.find((c) => c.isPublic) ?? candidates[0])?.value;
  };

  const parsedLink = parseStorageUrl(link);

  const applyLink = (raw: string) => {
    setLink(raw);
    const parsed = parseStorageUrl(raw);
    if (!parsed) return;
    const rv = pickRemote(parsed.provider);
    if (rv) setRemote(rv);
    const rooted = mountRootForLink(parsed.path);
    setSubpath(rooted);
    // Name from the MOUNTED root's last segment (the dataset/collection), not a
    // deep scene or file name — and keep it tracking Path edits (no hand-typed
    // name yet).
    const seg = rooted.split("/").map((s) => s.trim()).filter(Boolean).pop() ?? "";
    setName(folderSafe(seg));
    setNameTouched(false);
  };

  // The rclone spec the Add button will mount, previewed live so it matches
  // what the mounted card then shows. A "suggest:<id>" selection resolves to
  // its real remote name at submit; use the suggestion's name for the preview.
  const resolvedBase = remote.startsWith("suggest:")
    ? `${suggested.find((s) => `suggest:${s.id}` === remote)?.remote_name ?? ""}:`
    : remote;
  const spec = resolvedBase && resolvedBase !== ":" ? resolvedBase + subpath : "";

  // Whether the typed Name is one add_mount() will accept — non-empty after
  // trimming, and no / \ : or leading dot. Gating the button and the preview
  // on this keeps the preview from ever describing a folder the server rejects
  // (auto-derived names are already folderSafe; this catches manual edits).
  const trimmedName = name.trim();
  const nameValid = trimmedName !== "" && !/[/\\:]/.test(trimmedName) && !trimmedName.startsWith(".");

  const add = async () => {
    setBusy(true);
    setError(null);
    try {
      // A "suggest:<id>" selection is a detected credential source, not an
      // existing remote — materialize it into a keyless remote first, then
      // mount against the real name it returns.
      let base = remote;
      if (remote.startsWith("suggest:")) {
        base = (await createDetectedRemote(remote.slice("suggest:".length))).name;
      }
      await createMount(name, base + subpath);
      setName("");
      setSubpath("");
      setRemote("");
      setLink("");
      setNameTouched(false);
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="prefs-section">
      <h2>Add mount</h2>
      <p className="deploy-muted">
        Surface a remote as a local folder. Pick a remote you created, one under{" "}
        <b>Detected credentials</b> (from your AWS / gcloud config — no keys stored), or{" "}
        <b>Public datasets</b> for anonymous access to open data (no credentials needed).
      </p>
      <div className="mount-paste">
        <Field label="Paste a link">
          <TextInput
            placeholder="s3://bucket/prefix, gs://bucket/prefix, or an S3/GCS console URL"
            value={link}
            onChange={(e) => applyLink(e.target.value)}
          />
        </Field>
        {link.trim() &&
          (parsedLink ? (
            <p className="deploy-muted mount-paste-hint">
              Recognized {parsedLink.provider.toUpperCase()} link — filled the fields below
              {pickRemote(parsedLink.provider) ? "" : "; pick a remote"}.
              {mountRootForLink(parsedLink.path) !== parsedLink.path
                ? " Trimmed to the dataset root — edit Path to mount deeper."
                : " Review, then mount."}
            </p>
          ) : (
            <p className="deploy-muted mount-paste-hint warn">
              Not a recognized S3/GCS link — fill the fields below manually.
            </p>
          ))}
      </div>
      <form
        className="mount-form-row"
        onSubmit={(e) => {
          e.preventDefault();
          if (!busy && nameValid && remote) void add();
        }}
      >
        <Field label="Name" required>
          <TextInput
            placeholder="e.g. sensor-data"
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              setNameTouched(true);
            }}
          />
        </Field>
        <Field label="Remote" required>
          <Select value={remote} onChange={(e) => setRemote(e.target.value)}>
            <option value="">— remote —</option>
            {remotes.length > 0 && (
              <optgroup label="Remotes">
                {remotes.map((r) => (
                  // value is the raw rclone spec (add() and the live preview
                  // mount against r.name); only the shown text is the label.
                  <option key={r.name} value={r.name}>
                    {r.label}
                  </option>
                ))}
              </optgroup>
            )}
            {suggested.some((s) => s.kind === "public") && (
              <optgroup label="Public datasets (no credentials)">
                {suggested
                  .filter((s) => s.kind === "public")
                  .map((s) => (
                    <option key={s.id} value={`suggest:${s.id}`}>
                      {s.label}
                    </option>
                  ))}
              </optgroup>
            )}
            {suggested.some((s) => s.kind === "detected") && (
              <optgroup label="Detected credentials">
                {suggested
                  .filter((s) => s.kind === "detected")
                  .map((s) => (
                    <option key={s.id} value={`suggest:${s.id}`}>
                      {s.label}
                    </option>
                  ))}
              </optgroup>
            )}
          </Select>
        </Field>
        <Field label="Path">
          <TextInput
            placeholder="bucket/prefix"
            style={{ minWidth: 200 }}
            value={subpath}
            onChange={(e) => onPathChange(e.target.value)}
          />
        </Field>
        {/* Blank caption reserves the label row's height so the button
            aligns with the input boxes, not the labels above them. */}
        <Field label={" "}>
          <button type="submit" className="btn btn-primary" disabled={busy || !nameValid || !remote}>
            {busy ? "Mounting…" : "Add & mount"}
          </button>
        </Field>
      </form>
      {spec && (
        <p className="deploy-muted mount-spec">
          Mounts <code>{spec}</code>
          {nameValid ? (
            <>
              {" "}
              as folder <code>{trimmedName}</code>
            </>
          ) : trimmedName ? (
            <span className="warn">
              {" "}
              — name can’t contain / \ : or start with “.”
            </span>
          ) : (
            <>
              {" "}
              as folder <code>…</code>
            </>
          )}
        </p>
      )}
      <p className="deploy-muted" style={{ fontSize: "0.8em", margin: 0 }}>
        Tip: mount a specific <b>bucket/prefix</b>, not a whole bucket — narrow mounts browse and
        search much faster.
      </p>
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </section>
  );
}

function AddRemote({
  onChanged,
  onBusyChange,
}: {
  onChanged: () => void;
  onBusyChange?: (busy: boolean) => void;
}) {
  const [name, setName] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [region, setRegion] = useState("");
  const [accessKey, setAccessKey] = useState("");
  const [secretKey, setSecretKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = !busy && !!name && !!accessKey && !!secretKey;

  const add = async () => {
    setBusy(true);
    onBusyChange?.(true);
    setError(null);
    try {
      await createRemote(name, {
        access_key_id: accessKey,
        secret_access_key: secretKey,
        endpoint,
        region,
      });
      setName("");
      setAccessKey("");
      setSecretKey("");
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
      onBusyChange?.(false);
    }
  };

  return (
    <div className="prefs-section">
      <p className="deploy-muted" style={{ marginTop: 0 }}>
        For S3-compatible storage that needs a custom endpoint — Cloudflare R2, Backblaze B2,
        Wasabi, MinIO, and the like. Keys are written straight into rclone's own config;
        fused-render never stores them. For plain AWS S3 pick <b>AWS S3</b> instead, and for
        Google Drive, Dropbox or Box pick those — they have no keys to paste.
      </p>
      <form
        className="mount-form-row"
        onSubmit={(e) => {
          e.preventDefault();
          if (canSubmit) void add();
        }}
      >
        <Field label="Remote name" required>
          <TextInput placeholder="e.g. r2" value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Endpoint">
          <TextInput
            placeholder="blank for AWS S3"
            style={{ minWidth: 240 }}
            value={endpoint}
            onChange={(e) => setEndpoint(e.target.value)}
          />
        </Field>
        <Field label="Region">
          <TextInput
            placeholder="optional"
            value={region}
            onChange={(e) => setRegion(e.target.value)}
          />
        </Field>
        <Field label="Access key ID" required>
          <TextInput value={accessKey} onChange={(e) => setAccessKey(e.target.value)} />
        </Field>
        <Field label="Secret access key" required>
          <TextInput
            type="password"
            value={secretKey}
            onChange={(e) => setSecretKey(e.target.value)}
          />
        </Field>
        {/* Blank caption reserves the label row's height so the button aligns
            with the inputs, not the captions above them. */}
        <Field label={" "}>
          <button type="submit" className="btn btn-primary" disabled={!canSubmit}>
            {busy ? "Creating…" : "Create remote"}
          </button>
        </Field>
      </form>
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </div>
  );
}

// How often the mount list re-reads itself while an upload is in flight.
// Deliberately slow: GET /api/mounts probes every mount, so this only runs
// when there is something to watch drain.
const UPLOAD_POLL_MS = 8000;

// Poll cadence for a browser sign-in. Faster than the account flow's 2s: the
// user is sitting on a modal watching for the browser round-trip to land.
const OAUTH_POLL_MS = 1500;

// The Google Cloud console setup, as a stepper that does the navigating.
//
// This step exists only because Google is retiring rclone's built-in shared
// client ID (charging for requests made with it begins later in 2026, after 90
// days' notice), which makes bring-your-own-client MANDATORY for Drive. It is
// by far the most error-prone thing we ask of a user, so every step is a button
// that opens the exact console page rather than a sentence describing where to
// click, and the DOWNLOADED FILE is the primary input — typing a client secret
// off a screen is the step people get wrong.
function GoogleClientSetup({
  client,
  onChange,
  saved,
  onForget,
}: {
  client: GoogleOAuthClient;
  onChange: (c: GoogleOAuthClient) => void;
  // A client remembered from a previous sign-in: entered once per MACHINE, not
  // once per remote. Collapsed to a one-line summary so the common case (a
  // second Drive account) is straight to consent.
  saved: boolean;
  onForget: () => void;
}) {
  const [project, setProject] = useState("");
  const [expanded, setExpanded] = useState(!saved);
  const [fileError, setFileError] = useState<string | null>(null);
  const urls = googleConsoleUrls(project);

  const open = (url: string) => window.open(url, "_blank", "noopener,noreferrer");

  // A dropped/picked client_secret_*.json. Validated rather than trusted: the
  // usual wrong file (a service-account key) is valid JSON from the same
  // console and even carries a client_id, so silently half-filling the form
  // would fail much later at Google with an opaque error.
  const takeFile = async (file: File | null | undefined) => {
    if (!file) return;
    setFileError(null);
    let text: string;
    try {
      text = await file.text();
    } catch {
      setFileError(`Could not read ${file.name}.`);
      return;
    }
    const parsed = parseGoogleClientJson(text);
    if (!parsed) {
      setFileError(
        `${file.name} isn’t an OAuth client JSON — it has no client_id/client_secret pair. ` +
          `Download the file from the client you created under “OAuth clients”.`
      );
      return;
    }
    onChange(parsed);
  };

  if (saved && !expanded) {
    return (
      <div className="mount-callout">
        <div className="mount-callout-title">Using your Google API client</div>
        <div className="mount-callout-body">
          <code>{client.clientId}</code> — remembered on this machine, so you only set
          this up once.
        </div>
        <button type="button" className="mount-link" onClick={() => setExpanded(true)}>
          Use a different client
        </button>
      </div>
    );
  }

  return (
    <div className="mount-setup">
      <p className="deploy-muted" style={{ marginTop: 0 }}>
        Google Drive needs <b>your own</b> Google API client. rclone’s shared one is being
        retired, so there is no way around this — but it is a one-time setup, remembered
        on this machine afterwards.
      </p>
      <ol className="mount-steps">
        <li>
          <div className="mount-step-body">
            <b>Create or pick a Google Cloud project.</b> Any project works; a free one is
            fine.
            <div className="mount-step-actions">
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => open(urls.createProject)}
              >
                Open project setup ↗
              </button>
            </div>
            <Field label="Project ID (optional — links below jump straight to it)">
              <TextInput
                placeholder="e.g. my-drive-mount"
                value={project}
                onChange={(e) => setProject(e.target.value)}
              />
            </Field>
          </div>
        </li>
        <li>
          <div className="mount-step-body">
            <b>Enable the Google Drive API</b> for that project, then press Enable.
            <div className="mount-step-actions">
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => open(urls.enableApi)}
              >
                Open Drive API ↗
              </button>
            </div>
          </div>
        </li>
        <li>
          <div className="mount-step-body">
            <b>Configure the consent screen</b> — pick <b>External</b>, fill in the
            required name/email, and set publishing status to <b>In production</b>.
            <div className="mount-step-actions">
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => open(urls.consentScreen)}
              >
                Open consent screen ↗
              </button>
            </div>
            {/* The two things people get wrong here, and both are costly.
                "Testing" LOOKS like the cautious choice and silently breaks the
                mount a week later — Google drops refresh tokens issued by a
                Testing-mode client after 7 days. And the scary "unverified app"
                warning stops people mid-flow even though verification is simply
                not required at this scale. */}
            <p className="mount-paste-hint">
              Do <b>not</b> leave it in “Testing” — Google expires those sign-ins after 7
              days and the mount stops working. Google’s “unverified app” warning is
              expected: under the Personal Use exemption, verification isn’t required
              below 100 users, so click through <i>Advanced → Go to … (unsafe)</i> when you
              sign in.
            </p>
          </div>
        </li>
        <li>
          <div className="mount-step-body">
            <b>Create an OAuth client</b> of type <b>Desktop app</b>, then download its
            JSON.
            <div className="mount-step-actions">
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => open(urls.createClient)}
              >
                Open client setup ↗
              </button>
            </div>
            {/* Desktop app is not a preference: rclone authorize serves a
                loopback redirect, which is exactly what a Desktop client
                permits and a Web client does not without a registered URI. */}
            <p className="mount-paste-hint">
              It must be <b>Desktop app</b> — the sign-in comes back to a local address,
              which only that client type allows.
            </p>
          </div>
        </li>
      </ol>

      <div
        className="mount-drop"
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => {
          e.preventDefault();
          void takeFile(e.dataTransfer.files?.[0]);
        }}
      >
        <b>Drop the downloaded <code>client_secret_….json</code> here</b>
        <span className="deploy-muted">or</span>
        <input
          type="file"
          accept=".json,application/json"
          onChange={(e) => void takeFile(e.target.files?.[0])}
        />
      </div>
      {fileError && <ErrorBanner>{fileError}</ErrorBanner>}

      {/* The fallback, deliberately below the file path: it works, but it is
          the step people mistype. */}
      <details className="mount-manual">
        <summary>Or paste the client ID and secret</summary>
        <div className="mount-form-row">
          <Field label="Client ID" required>
            <TextInput
              style={{ minWidth: 280 }}
              placeholder="….apps.googleusercontent.com"
              value={client.clientId}
              onChange={(e) => onChange({ ...client, clientId: e.target.value.trim() })}
            />
          </Field>
          <Field label="Client secret" required>
            <TextInput
              type="password"
              style={{ minWidth: 200 }}
              value={client.clientSecret}
              onChange={(e) =>
                onChange({ ...client, clientSecret: e.target.value.trim() })
              }
            />
          </Field>
        </div>
      </details>

      {client.clientId && client.clientSecret && (
        <p className="mount-paste-hint">
          Client <code>{client.clientId}</code> ready.{" "}
          {saved && (
            <button
              type="button"
              className="mount-link"
              onClick={() => {
                onForget();
                setExpanded(true);
              }}
            >
              Forget the saved client
            </button>
          )}
        </p>
      )}
    </div>
  );
}

// A browser sign-in for any OAuth provider (D205, D209). The server spawns
// `rclone authorize "<backend>"`, which runs its own loopback callback server
// and opens the SYSTEM browser itself — so unlike the Fused login
// (lib/account.ts) there is no URL for us to window.open, and the client's whole
// job is start → poll → report. Completion is polled because there is no push
// channel; `in_flight` dropping without `ok` is the failure case, and it covers
// the abandoned browser tab.
//
// ONE component for all three providers rather than three copies: the poll, its
// bounds, and the cancel reconciliation are the subtle parts, and three copies
// of them would be three places for the next fix to miss. What varies is the
// label, the default remote name, and whether a client id/secret is collected.
function OAuthSignIn({
  provider,
  remotes,
  onConnected,
  onBusyChange,
}: {
  provider: OAuthProvider;
  remotes: RcloneRemote[];
  onConnected: () => void;
  onBusyChange?: (busy: boolean) => void;
}) {
  // A free default so the common case is one click. rclone's config/create
  // overwrites a same-named remote, so reusing an existing name takes an
  // explicit opt-in below — and the server enforces that independently, since
  // this list is a snapshot from when the dialog opened.
  const taken = new Set(remotes.map((r) => r.name.replace(/:$/, "")));
  const firstFree = () => {
    const base = provider.defaultRemoteName;
    if (!taken.has(base)) return base;
    for (let i = 2; ; i++) if (!taken.has(`${base}-${i}`)) return `${base}-${i}`;
  };

  // Pre-filled from the per-machine store, which is what makes the Google Cloud
  // trip a one-time cost rather than a per-remote one. Non-Drive providers
  // never touch this — they have no client to supply.
  const [savedClient, setSavedClient] = useState<GoogleOAuthClient | null>(() =>
    provider.needsClient ? loadGoogleClient() : null
  );
  const [client, setClient] = useState<GoogleOAuthClient>(
    () => savedClient ?? { clientId: "", clientSecret: "" }
  );

  const [name, setName] = useState(firstFree);
  const [replace, setReplace] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<number | null>(null);
  // Bounds on the poll, so a status endpoint that stops answering ends the
  // wait instead of leaving the modal on "Waiting…" forever (the server's own
  // timeout can't rescue us — reading it needs the fetch that is failing).
  const failures = useRef(0);
  const startedAt = useRef(0);

  const stopPolling = () => {
    if (timer.current !== null) {
      window.clearInterval(timer.current);
      timer.current = null;
    }
  };
  useEffect(() => stopPolling, []);

  const finish = (err: string | null) => {
    stopPolling();
    setConnecting(false);
    onBusyChange?.(false);
    setError(err);
  };

  // Carry out a decision from lib/oauth.ts (which owns the "when do we
  // stop waiting" rules and is tested there).
  const apply = (decision: OAuthDecision) => {
    switch (decision.kind) {
      case "wait":
        return;
      case "connected":
        finish(null);
        onConnected();
        return;
      case "cancelled":
        finish(null);
        return;
      case "failed":
        finish(decision.message);
    }
  };

  const trimmed = name.trim();
  const collides = taken.has(trimmed);
  // A missing client is a hard block, not a warning: the server refuses a Drive
  // sign-in without one (rclone's shared client ID is being retired), so
  // enabling the button here would only buy the user a 400.
  const clientReady =
    !provider.needsClient || (!!client.clientId && !!client.clientSecret);
  const nameError = !trimmed
    ? "Give the remote a name."
    : /[:/]/.test(trimmed)
      ? "A remote name can’t contain “:” or “/”."
      : collides && !replace
        ? `“${trimmed}” already exists — pick another name, or confirm replacing it.`
        : null;

  // The poll loop, extracted because `cancel` may have to RESUME it: a cancel
  // that lands while the server is still finalizing has an outcome coming and
  // must not stand down (see oauthCancelOutcome). `startedAt` is deliberately
  // not reset by a resume — the wall-clock backstop belongs to the attempt, not
  // to this loop, or a cancel could extend the wait indefinitely.
  const startPolling = () => {
    stopPolling();
    failures.current = 0;
    timer.current = window.setInterval(async () => {
      let status: RemoteOAuthStatus | null = null;
      try {
        status = await getRemoteOAuthStatus();
        failures.current = 0;
      } catch {
        failures.current++; // a null status; oauthTick decides when that's fatal
      }
      if (timer.current === null) return; // canceled while the fetch was in flight
      apply(
        oauthTick(status, {
          consecutiveFailures: failures.current,
          elapsedMs: Date.now() - startedAt.current,
          label: provider.label,
        })
      );
    }, OAUTH_POLL_MS);
  };

  const begin = async () => {
    setError(null);
    setConnecting(true);
    onBusyChange?.(true);
    failures.current = 0;
    startedAt.current = Date.now();
    try {
      await startRemoteOAuth(trimmed, {
        provider: provider.key,
        replace,
        clientId: client.clientId,
        clientSecret: client.clientSecret,
      });
    } catch (e) {
      finish((e as Error).message);
      return;
    }
    // Remembered only once the SERVER accepted it — a client it rejected out of
    // hand is not one worth pre-filling next time.
    if (provider.needsClient && client.clientId && client.clientSecret) {
      saveGoogleClient(client);
      setSavedClient(client);
    }
    startPolling();
  };

  const cancel = async () => {
    // Stop polling first so an in-flight tick can't race this, but do NOT
    // report "ready" until the server confirms the child is gone: returning to
    // an enabled button while the child still holds port 53682 means the next
    // click 409s on a sign-in the user believes they cancelled.
    stopPolling();
    let canceled = false;
    try {
      ({ canceled } = await cancelRemoteOAuth());
    } catch {
      // Unreachable server: the child is killed by its own timeout regardless.
      finish(null);
      return;
    }
    // `canceled: false` is not "nothing happened" — it usually means the
    // sign-in COMPLETED in the gap before the click landed, so the result is
    // reconciled rather than discarded (lib/account.ts does the same, for the
    // same reason).
    let status: RemoteOAuthStatus | null = null;
    if (!canceled) {
      try {
        status = await getRemoteOAuthStatus();
      } catch {
        status = null;
      }
    }
    const decision = oauthCancelOutcome(canceled, status);
    if (decision.kind === "wait") {
      // The child is gone but the server is still finalizing (creating the
      // remote over rcd). An outcome IS coming, so resume the poll rather than
      // closing the modal on a sign-in that is about to succeed.
      startPolling();
      return;
    }
    apply(decision);
  };

  return (
    <div className="prefs-section">
      <p className="deploy-muted" style={{ marginTop: 0 }}>
        Opens {provider.label} in your browser to approve access. The sign-in is handled by
        rclone and the token is written straight into rclone's own config; fused-render never
        stores it. The connection is <b>read-write</b>, so edits you save under the mount are
        uploaded back.
      </p>
      {provider.key === "drive" && (
        <p className="deploy-muted" style={{ fontSize: "0.8em" }}>
          Google Docs, Sheets and Slides are skipped — they aren't real files and can't be
          opened or saved through a mount.
        </p>
      )}
      {provider.needsClient && !connecting && (
        <GoogleClientSetup
          client={client}
          onChange={setClient}
          saved={!!savedClient && savedClient.clientId === client.clientId}
          onForget={() => {
            clearGoogleClient();
            setSavedClient(null);
            setClient({ clientId: "", clientSecret: "" });
          }}
        />
      )}
      <form
        className="mount-form-row"
        onSubmit={(e) => {
          e.preventDefault();
          if (!connecting && !nameError && clientReady) void begin();
        }}
      >
        <Field label="Remote name" required>
          <TextInput
            value={name}
            disabled={connecting}
            onChange={(e) => setName(e.target.value)}
          />
        </Field>
        {/* Blank caption reserves the label row's height so the button aligns
            with the input, not the caption above it. */}
        <Field label={" "}>
          {connecting ? (
            <button type="button" className="btn btn-secondary" onClick={cancel}>
              Cancel
            </button>
          ) : (
            <button
              type="submit"
              className="btn btn-primary"
              disabled={!!nameError || !clientReady}
            >
              Sign in to {provider.label}
            </button>
          )}
        </Field>
      </form>
      {!connecting && !clientReady && (
        <p className="mount-paste-hint">
          Add your Google API client above to enable the sign-in.
        </p>
      )}
      {connecting && (
        <p className="deploy-muted">
          Waiting for you to approve access in your browser… If no tab opened, check for a blocked
          window.
        </p>
      )}
      {!connecting && collides && (
        // Re-signing in under the name you already use is the NATURAL action
        // (a revoked or expired token is the usual reason to be here), so this
        // has to be possible — just never by accident. config/create
        // overwrites, and the server refuses without this flag.
        <label className="deploy-muted mount-paste-hint">
          <input
            type="checkbox"
            checked={replace}
            onChange={(e) => setReplace(e.target.checked)}
          />{" "}
          Replace the existing “{trimmed}” remote — use this to sign in again after a
          token expired or was revoked.
        </label>
      )}
      {!connecting && nameError && trimmed !== "" && (
        <p className="deploy-muted mount-paste-hint warn">{nameError}</p>
      )}
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </div>
  );
}

// Create a remote from a credential source the server already detected, or from
// the built-in anonymous ones. Both are the SAME existing endpoint
// (createDetectedRemote, keyed by the server's own suggestion id — never by
// client-supplied rclone params); `kind` only decides which subset is offered
// and how it is explained.
function DetectedRemoteSetup({
  kind,
  suggested,
  onChanged,
  onBusyChange,
}: {
  kind: "detected" | "public";
  suggested: RemoteSuggestion[];
  onChanged: () => void;
  onBusyChange?: (busy: boolean) => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const options = suggested.filter((s) => s.kind === kind);

  const create = async (id: string) => {
    setBusy(id);
    onBusyChange?.(true);
    setError(null);
    try {
      await createDetectedRemote(id);
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
      onBusyChange?.(false);
    }
  };

  return (
    <div className="prefs-section">
      <p className="deploy-muted" style={{ marginTop: 0 }}>
        {kind === "detected" ? (
          <>
            Credentials already on this machine — your <code>~/.aws</code> profiles,
            gcloud application-default credentials, and the usual environment variables.
            Nothing is copied: the remote is created with <code>env_auth</code>, so rclone
            reads them where they already live.
          </>
        ) : (
          <>
            Anonymous access to open data — AWS Open Data, public GCS datasets, and
            anything else readable without credentials. Read-only by nature, and it works
            even when you have no cloud credentials at all.
          </>
        )}
      </p>
      {options.length === 0 ? (
        <div className="mount-empty">
          {kind === "detected" ? (
            <>
              No credentials detected. Run <code>aws sso login</code> or{" "}
              <code>gcloud auth application-default login</code>, then reopen this — or use{" "}
              <b>S3-compatible storage</b> to paste keys directly.
            </>
          ) : (
            <>Both public remotes already exist — pick them under Remote in “Add mount”.</>
          )}
        </div>
      ) : (
        <div className="mount-list">
          {options.map((s) => (
            <div className="mount-card" key={s.id}>
              <div className="mount-card-main">
                <div className="mount-card-info">
                  <div>{s.label}</div>
                  <div className="mount-remote">
                    <code>{s.remote_name}:</code>
                  </div>
                </div>
                <div className="mount-card-actions">
                  <button
                    type="button"
                    className="btn btn-primary"
                    disabled={busy !== null}
                    onClick={() => void create(s.id)}
                  >
                    {busy === s.id ? "Creating…" : "Create remote"}
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
      <p className="deploy-muted" style={{ fontSize: "0.8em", margin: 0 }}>
        Creating a remote doesn’t mount anything yet — pick it under <b>Remote</b> in “Add
        mount” and choose the bucket/prefix to surface.
      </p>
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </div>
  );
}

// -- the "Add storage" surface -------------------------------------------------
//
// One picker instead of the three disconnected entry points this replaced (the
// Add-mount form, a bare "Sign in to Google Drive" link, and an "Add a custom S3
// remote" link). The complaint that drove it is that a user could not tell what
// was POSSIBLE — the links read as footnotes, so Dropbox/Box would have been
// invisible however well they worked. Every provider is a card, and every card
// states its setup cost up front, because "one click" and "go create a Google
// Cloud project" are very different asks and the user should choose knowing which
// one they are agreeing to.
type SetupKey = OAuthProviderKey | "detected" | "s3compat" | "public";

const STORAGE_OPTIONS: { key: SetupKey; name: string; cost: string; title: string }[] = [
  {
    key: "drive",
    name: "Google Drive",
    cost: "Needs a Google API client (one-time)",
    title: "Connect Google Drive",
  },
  {
    key: "dropbox",
    name: "Dropbox",
    cost: "One-click browser sign-in",
    title: "Connect Dropbox",
  },
  { key: "box", name: "Box", cost: "One-click browser sign-in", title: "Connect Box" },
  {
    key: "detected",
    name: "AWS S3",
    cost: "Uses credentials already on this machine",
    title: "Use detected credentials",
  },
  {
    key: "s3compat",
    name: "S3-compatible",
    cost: "Endpoint + access keys — R2, MinIO, Wasabi, B2",
    title: "Add an S3-compatible remote",
  },
  {
    // "Public buckets" described the MECHANISM (an anonymous, unsigned bucket
    // read) rather than what anyone would come here wanting. These are open
    // datasets published to be read without an account — AWS Open Data,
    // public GCS collections — so the card is named for that instead.
    key: "public",
    name: "Public datasets",
    cost: "Open S3/GCS data — no account needed",
    title: "Browse public datasets",
  },
];

function AddStorage({ onPick }: { onPick: (key: SetupKey) => void }) {
  return (
    <section className="prefs-section">
      <h2>Add storage</h2>
      <p className="deploy-muted">
        Connect a storage provider, then mount a folder from it above. Credentials and
        tokens go straight into rclone's own config — fused-render never stores them.
      </p>
      <div className="mount-picker">
        {STORAGE_OPTIONS.map((o) => (
          <button
            type="button"
            key={o.key}
            className="mount-provider"
            onClick={() => onPick(o.key)}
          >
            <span className="mount-provider-head">
              <ProviderIcon provider={o.key} />
              <span className="mount-provider-name">{o.name}</span>
            </span>
            <span className="mount-provider-cost">{o.cost}</span>
          </button>
        ))}
      </div>
    </section>
  );
}

export default function Mounts() {
  const [state, setState] = useState<MountsResult | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Which provider's setup flow is open, or null. One piece of state for all six
  // — the picker and the modal are two views of the same choice.
  const [setup, setSetup] = useState<SetupKey | null>(null);
  // Lifted out of the flows so the modal can gate its Esc/backdrop/✕ close while
  // something is in flight: closing out from under a live authorize child leaves
  // it holding rclone's callback port, and the next attempt 409s on a sign-in
  // the user believes they dismissed.
  const [setupBusy, setSetupBusy] = useState(false);
  // Global "Restart all mounts": a confirm modal (it briefly disconnects ALL
  // mounts) gating the multi-second daemon restart + re-mount.
  const [confirmRestart, setConfirmRestart] = useState(false);
  const [restartBusy, setRestartBusy] = useState(false);
  const [restartError, setRestartError] = useState<string | null>(null);

  const reload = () => {
    getMounts().then(
      (r) => {
        setState(r);
        // Clear any prior load error — otherwise a stale "Failed to load mounts"
        // banner lingers over an up-to-date list after a recovered fetch (e.g.
        // the reload() a failed doRestart fires, or a transient error healing).
        setLoadError(null);
      },
      (e: Error) => setLoadError(e.message),
    );
  };
  useEffect(reload, []);
  // Coming back to the window re-reads the list — the cheap way to keep the
  // upload queue (D207) honest without polling a handler that probes every
  // mount. (Same refresh-on-return cadence the account dot uses.)
  useRefreshOnReturn(reload);
  // While something is actually DRAINING, poll: a transfer the user can watch,
  // and a failure that appears without them having to leave and come back. The
  // gate (and why it is not simply "anything queued") lives in lib/uploads.ts.
  const uploading = hasDrainingUploads(state?.mounts ?? []);
  useEffect(() => {
    if (!uploading) return;
    const id = window.setInterval(reload, UPLOAD_POLL_MS);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [uploading]);

  const doRestart = async () => {
    setRestartBusy(true);
    setRestartError(null);
    try {
      // Returns the fresh MountsResult, so swap state in directly rather than
      // firing a second GET.
      setState(await restartRclone());
      setConfirmRestart(false);
    } catch (e) {
      setRestartError((e as Error).message);
      // A failed restart isn't a no-op: the server may already have force-detached
      // mounts (or killed the daemon) before failing, so the last MountsResult is
      // stale. Re-fetch so the page shows the true post-attempt state instead of a
      // healthy view that no longer matches reality.
      reload();
    } finally {
      setRestartBusy(false);
    }
  };

  // Recovery prompt: some mounts signal that a restart would fix them (settings
  // drifted, or credentials were refreshed under a still-stale connection).
  const paramsMounts = state?.mounts.filter((m) => m.restart_reason === "params") ?? [];
  const credMounts = state?.mounts.filter((m) => m.restart_reason === "credentials") ?? [];
  const needsRestart = paramsMounts.length > 0 || credMounts.length > 0;

  // The page chrome (heading, intro, actions) renders immediately; only the
  // mount list itself waits on the async getMounts() — a blocking full-page
  // "Loading…" made the whole page feel slow when just the list is pending.
  return (
    <div className="prefs-page mounts-page">
      <header className="mounts-head">
        <div>
          <h1 className="mounts-title">Mounts</h1>
          <p className="mounts-subtitle">
            Browse remote storage as local folders. Large files are cached locally after the
            first open.
          </p>
        </div>
        {state?.rclone.available && (
          <div className="mounts-actions">
            <button
              type="button"
              className="btn btn-secondary mounts-restart"
              disabled={restartBusy}
              onClick={() => setConfirmRestart(true)}
              title="Reconnect all mounts — recovers stuck mounts and picks up refreshed credentials"
            >
              <span className="mounts-restart-icon" aria-hidden="true">
                ↻
              </span>
              {restartBusy ? "Restarting…" : "Restart all mounts"}
            </button>
          </div>
        )}
      </header>

      {loadError && <ErrorBanner>Failed to load mounts: {loadError}</ErrorBanner>}

      {!state && !loadError && (
        <div className="mount-list" aria-busy="true" aria-label="Loading mounts">
          <div className="mount-card mount-card--skeleton" />
          <div className="mount-card mount-card--skeleton" />
        </div>
      )}

      {state && !state.rclone.available && (
        <div className="mount-callout">
          <div className="mount-callout-title">rclone not found</div>
          <div className="mount-callout-body">
            rclone must be installed and on your <code>PATH</code> for mounts to work. Install it
            with <code>brew install rclone</code> (macOS), <code>apt install rclone</code> /{" "}
            <code>dnf install rclone</code> (Linux), or the{" "}
            <a href="https://rclone.org/install/" target="_blank" rel="noreferrer">
              official installer
            </a>
            , then reload this page. Distro packages can be outdated, so a recent version is
            recommended.
          </div>
        </div>
      )}

      {state && needsRestart && (
        <div className="mount-callout warn">
          <div className="mount-callout-title">Some mounts need a restart</div>
          <div className="mount-callout-body">
            {paramsMounts.length > 0 && <p>Settings changed — restart to apply them.</p>}
            {credMounts.length > 0 && (
              <p>
                Credentials were refreshed — restart to reconnect{" "}
                {credMounts.map((m) => m.name).join(", ")}.
              </p>
            )}
          </div>
          <button
            type="button"
            className="btn btn-primary"
            disabled={restartBusy}
            onClick={() => setConfirmRestart(true)}
          >
            {restartBusy ? "Restarting…" : "Restart all mounts"}
          </button>
        </div>
      )}

      {restartError && <ErrorBanner>{restartError}</ErrorBanner>}

      {state &&
        (state.mounts.length > 0 ? (
          <div className="mount-list">
            {state.mounts.map((c) => (
              <MountRow key={c.id} conn={c} onChanged={reload} />
            ))}
          </div>
        ) : (
          state.rclone.available && (
            <div className="mount-empty">
              No mounts yet — add one below to browse remote storage as local folders.
            </div>
          )
        ))}

      {state?.rclone.available && (
        <>
          <AddMount
            remotes={state.rclone.remotes}
            suggested={state.rclone.suggested ?? []}
            onChanged={reload}
          />
          <AddStorage onPick={setSetup} />
          {setup && (
            <Modal
              title={STORAGE_OPTIONS.find((o) => o.key === setup)?.title ?? "Add storage"}
              busy={setupBusy}
              onClose={() => setSetup(null)}
            >
              {/* Every flow closes the same way: reload so the new remote
                  appears in Add mount's Remote picker, then dismiss. */}
              {setup === "s3compat" ? (
                <AddRemote
                  onBusyChange={setSetupBusy}
                  onChanged={() => {
                    reload();
                    setSetup(null);
                  }}
                />
              ) : setup === "detected" || setup === "public" ? (
                <DetectedRemoteSetup
                  kind={setup}
                  suggested={state.rclone.suggested ?? []}
                  onBusyChange={setSetupBusy}
                  onChanged={() => {
                    reload();
                    setSetup(null);
                  }}
                />
              ) : (
                <OAuthSignIn
                  provider={OAUTH_PROVIDERS[setup]}
                  remotes={state.rclone.remotes}
                  onBusyChange={setSetupBusy}
                  onConnected={() => {
                    reload();
                    setSetup(null);
                  }}
                />
              )}
            </Modal>
          )}
        </>
      )}

      {confirmRestart && (
        <Modal
          title="Restart all mounts?"
          busy={restartBusy}
          onClose={() => setConfirmRestart(false)}
          footer={
            <>
              <button
                type="button"
                className="btn btn-secondary"
                disabled={restartBusy}
                onClick={() => setConfirmRestart(false)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={restartBusy}
                onClick={doRestart}
              >
                {restartBusy ? "Restarting…" : "Restart all mounts"}
              </button>
            </>
          }
        >
          <p>
            This reconnects every mount and re-reads storage credentials. <b>All</b> mounts —
            including healthy ones — briefly disconnect while it happens, and files currently open
            from a mount may need to be reopened.
          </p>
        </Modal>
      )}
    </div>
  );
}
