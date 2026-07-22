// Mounts page — the /view/_mounts sentinel, entered from the sidebar
// footer. Remote storage (S3-compatible and anything else rclone speaks)
// mounted as local folders under ~/.fused-render/mounts; everything
// downstream — previews, readers, tile servers — sees ordinary local paths.
// Backend: shell/mounts.py (rclone rcd). Credentials live in rclone's
// own config, never here. Section layout and per-action busy/error state
// follow views/Preferences.tsx.
import { useEffect, useState } from "react";
import {
  createDetectedRemote,
  createMount,
  createRemote,
  deleteMount,
  getMounts,
  reconnectMount,
  restartRclone,
} from "../lib/api";
import type { Mount, MountsResult, RcloneRemote, RemoteSuggestion } from "../lib/api";
import { navigate } from "../lib/router";
import { Modal } from "../components/modal/Modal";
import { ErrorBanner } from "../components/ErrorBanner";
import { Field, Select, TextInput } from "../components/field/fields";

function MountRow({ conn, onChanged }: { conn: Mount; onChanged: () => void }) {
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
        <span
          className={`mount-dot ${conn.state}`}
          role="img"
          aria-label={dotLabel}
          title={dotLabel}
        />
        <div className="mount-card-info">
          <div style={{ fontWeight: 600 }}>
            {conn.name}
            {conn.read_only && (
              <span
                className="mount-hint"
                title="This remote rejects writes — files open read-only"
              >
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
        </div>
        <div className="mount-card-actions">
          {conn.state === "mounted" ? (
            <button
              type="button"
              disabled={busy}
              onClick={() => navigate(conn.mountpoint, { isDir: true })}
            >
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
  const segs = u.pathname
    .split("/")
    .filter(Boolean)
    .map((x) => {
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
  const folderSafe = (s: string) =>
    s
      .trim()
      .replace(/[/\\:]/g, "")
      .replace(/^\.+/, "");

  const onPathChange = (v: string) => {
    setSubpath(v);
    if (!nameTouched) {
      // Last non-blank segment: trim first so a trailing "/" or a whitespace
      // tail ("bucket/  ") derives the real segment, never a spaces-only name.
      const seg =
        v
          .split("/")
          .map((s) => s.trim())
          .filter(Boolean)
          .pop() ?? "";
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
    const seg =
      rooted
        .split("/")
        .map((s) => s.trim())
        .filter(Boolean)
        .pop() ?? "";
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
  const nameValid =
    trimmedName !== "" && !/[/\\:]/.test(trimmedName) && !trimmedName.startsWith(".");

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
        <b>Public buckets</b> for anonymous access to open data (no credentials needed).
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
              <optgroup label="Public buckets (no credentials)">
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
          <button
            type="submit"
            className="btn btn-primary"
            disabled={busy || !nameValid || !remote}
          >
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
            <span className="warn"> — name can’t contain / \ : or start with “.”</span>
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
        fused-render never stores them. For plain AWS S3 use <b>Detected credentials</b> instead,
        and for <b>Google Drive</b> or other sign-in backends run <code>rclone config</code> in a
        terminal — either then appears in the remote dropdown on reload.
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

export default function Mounts() {
  const [state, setState] = useState<MountsResult | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [showAddRemote, setShowAddRemote] = useState(false);
  // Lifted from AddRemote so the modal can gate its Esc/backdrop/✕ close while a
  // create is in flight (previously the backdrop close was ungated).
  const [remoteBusy, setRemoteBusy] = useState(false);
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
            Browse remote storage as local folders. Large files are cached locally after the first
            open.
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
          <button type="button" className="mount-link" onClick={() => setShowAddRemote(true)}>
            Add a custom S3 remote (R2, MinIO, …)
          </button>
          {showAddRemote && (
            <Modal
              title="Add a custom S3 remote"
              busy={remoteBusy}
              onClose={() => setShowAddRemote(false)}
            >
              <AddRemote
                onBusyChange={setRemoteBusy}
                onChanged={() => {
                  reload();
                  setShowAddRemote(false);
                }}
              />
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
