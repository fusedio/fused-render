// Mounts page — the /view/_mounts sentinel, entered from the sidebar
// footer. Remote storage (S3-compatible and anything else rclone speaks)
// mounted as local folders under ~/.fused-render/mounts; everything
// downstream — previews, readers, tile servers — sees ordinary local paths.
// Backend: shell/mounts.py (rclone rcd). Credentials live in rclone's
// own config, never here. Section layout and per-action busy/error state
// follow views/Preferences.tsx.
import { useEffect, useState, type ReactNode } from "react";
import {
  createDetectedRemote,
  createMount,
  createRemote,
  deleteMount,
  getMounts,
  reconnectMount,
} from "../lib/api";
import type { Mount, MountsResult, RemoteSuggestion } from "../lib/api";
import { navigate } from "../lib/router";

// Lightweight modal reusing the Deploy modal's overlay/dialog chrome
// (.deploy-* in shell.css): Escape or a click on the backdrop closes it.
function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="deploy-overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className="deploy-dialog"
        role="dialog"
        aria-modal="true"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="deploy-head">
          <h2>{title}</h2>
          <button type="button" className="deploy-close" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        <div className="deploy-body">{children}</div>
      </div>
    </div>
  );
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
        <div style={{ flex: 1, minWidth: 0 }}>
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
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          {conn.state === "mounted" ? (
            <button type="button" disabled={busy} onClick={() => navigate(conn.mountpoint)}>
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
      </div>
      {error && <div className="deploy-error">{error}</div>}
    </div>
  );
}

// A labelled form control: a small uppercase caption above the input, so the
// Add-mount row and the custom-remote modal read as named fields instead of a
// row of bare placeholders. `required` shows an accent marker.
function Field({
  label,
  required,
  children,
}: {
  label: string;
  required?: boolean;
  children: ReactNode;
}) {
  return (
    <label className="mount-field">
      <span>
        {label}
        {required && (
          <span className="req" title="required" aria-hidden="true">
            {" "}
            *
          </span>
        )}
      </span>
      {children}
    </label>
  );
}

function AddMount({
  remotes,
  suggested,
  onChanged,
}: {
  remotes: string[];
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
        Surface an rclone remote as a local folder. Pick a remote you created, one under{" "}
        <b>Detected credentials</b> (from your AWS / gcloud config — no keys stored), or{" "}
        <b>Public buckets</b> for anonymous access to open data (no credentials needed).
      </p>
      <div style={{ display: "flex", gap: 10, alignItems: "flex-end", flexWrap: "wrap" }}>
        <Field label="Name" required>
          <input
            placeholder="e.g. sensor-data"
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              setNameTouched(true);
            }}
          />
        </Field>
        <Field label="Remote" required>
          <select value={remote} onChange={(e) => setRemote(e.target.value)}>
            <option value="">— remote —</option>
            {remotes.length > 0 && (
              <optgroup label="Remotes">
                {remotes.map((r) => (
                  <option key={r} value={r}>
                    {r}
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
          </select>
        </Field>
        <Field label="Path">
          <input
            placeholder="bucket/prefix"
            style={{ minWidth: 200 }}
            value={subpath}
            onChange={(e) => onPathChange(e.target.value)}
          />
        </Field>
        {/* Blank caption reserves the label row's height so the button
            aligns with the input boxes, not the labels above them. */}
        <Field label={" "}>
          <button type="button" disabled={busy || !nameValid || !remote} onClick={add}>
            {busy ? "Mounting…" : "Add & mount"}
          </button>
        </Field>
      </div>
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
      {error && <div className="deploy-error">{error}</div>}
    </section>
  );
}

function AddRemote({ onChanged }: { onChanged: () => void }) {
  const [name, setName] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [region, setRegion] = useState("");
  const [accessKey, setAccessKey] = useState("");
  const [secretKey, setSecretKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const add = async () => {
    setBusy(true);
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
      <div style={{ display: "flex", gap: 10, alignItems: "flex-end", flexWrap: "wrap" }}>
        <Field label="Remote name" required>
          <input placeholder="e.g. r2" value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Endpoint">
          <input
            placeholder="blank for AWS S3"
            style={{ minWidth: 240 }}
            value={endpoint}
            onChange={(e) => setEndpoint(e.target.value)}
          />
        </Field>
        <Field label="Region">
          <input placeholder="optional" value={region} onChange={(e) => setRegion(e.target.value)} />
        </Field>
        <Field label="Access key ID" required>
          <input value={accessKey} onChange={(e) => setAccessKey(e.target.value)} />
        </Field>
        <Field label="Secret access key" required>
          <input type="password" value={secretKey} onChange={(e) => setSecretKey(e.target.value)} />
        </Field>
        {/* Blank caption reserves the label row's height so the button aligns
            with the inputs, not the captions above them. */}
        <Field label={" "}>
          <button
            type="button"
            disabled={busy || !name || !accessKey || !secretKey}
            onClick={add}
          >
            {busy ? "Creating…" : "Create remote"}
          </button>
        </Field>
      </div>
      {error && <div className="deploy-error">{error}</div>}
    </div>
  );
}

export default function Mounts() {
  const [state, setState] = useState<MountsResult | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [showAddRemote, setShowAddRemote] = useState(false);

  const reload = () => {
    getMounts().then(setState, (e: Error) => setLoadError(e.message));
  };
  useEffect(reload, []);

  if (loadError) {
    return <div className="status-message error">Failed to load mounts: {loadError}</div>;
  }
  if (!state) {
    return <div className="status-message">Loading…</div>;
  }

  return (
    <div className="prefs-page">
      {!state.rclone.available && (
        <div
          style={{
            padding: "12px 14px",
            border: "1px solid var(--border)",
            borderLeft: "3px solid var(--error)",
            borderRadius: 8,
            background: "var(--bg-alt)",
          }}
        >
          <div style={{ fontWeight: 700, marginBottom: 4 }}>rclone not found</div>
          <div style={{ fontSize: "0.9em" }}>
            rclone must be installed and on your <code>PATH</code> for mounts to work. Install it
            with <code>brew install rclone</code> (macOS), <code>apt install rclone</code> /{" "}
            <code>dnf install rclone</code> (Linux), or the{" "}
            <a href="https://rclone.org/install/" target="_blank" rel="noreferrer">
              official installer
            </a>
            , then reload this page. Distro packages can be outdated, so a recent rclone is
            recommended.
          </div>
        </div>
      )}
      <p className="deploy-muted" style={{ marginTop: 0 }}>
        Remote storage mounted as local folders
        {state.rclone.version ? ` (${state.rclone.version})` : ""}. The <b>first</b> open of a
        large remote file downloads what it needs and can be slow; repeat opens are served from
        a local cache and are fast. Mounts stay up automatically, including across restarts;
        if one stops responding, use <b>Reconnect</b>.
      </p>
      {state.mounts.length > 0 ? (
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
      )}
      {state.rclone.available && (
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
            <Modal title="Add a custom S3 remote" onClose={() => setShowAddRemote(false)}>
              <AddRemote
                onChanged={() => {
                  reload();
                  setShowAddRemote(false);
                }}
              />
            </Modal>
          )}
        </>
      )}
    </div>
  );
}
