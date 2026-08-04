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
  RemoteKind,
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
import {
  mountRootForLink,
  parseStorageUrl,
  pickRemote,
  shouldApplyPreselect,
  suggestMountName,
} from "./mounts/links";
import type { RemoteChoice } from "./mounts/links";

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

// The Remote dropdown's groups, in display order.
//
// Grouped by HOW A REMOTE IS REACHED, never by whether it has been created yet.
// Created-ness used to be the axis — one "Remotes" group plus two groups of
// not-yet-created suggestions — which is an implementation detail no user
// thinks in, and it degenerated badly at both ends: create everything and the
// two labelled groups vanish, leaving one flat list where an anonymous
// read-only remote sits between two credentialed ones, distinguishable only by
// reading to the end of its label. A suggestion and the remote it becomes now
// live in the same group; the "+" prefix on its option is the only difference,
// since picking it costs a creation round-trip.
const REMOTE_GROUPS: { kind: RemoteKind; label: string }[] = [
  { kind: "other", label: "Your remotes" },
  { kind: "detected", label: "Detected credentials (no keys stored)" },
  { kind: "public", label: "Public datasets (no credentials)" },
];

function AddMount({
  remotes,
  suggested,
  preselect,
  onChanged,
}: {
  remotes: RcloneRemote[];
  suggested: RemoteSuggestion[];
  // A remote spec a setup flow just created (incl. trailing ':'), to select
  // here once the reload carrying it lands. Null when nothing is pending.
  preselect: string | null;
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

  const onPathChange = (v: string) => {
    setSubpath(v);
    if (!nameTouched) setName(suggestMountName(v));
  };

  // A pasted S3/GCS link (see parseStorageUrl) that auto-fills the fields below.
  const [link, setLink] = useState("");

  // The suggestions this form may OFFER. `suggested` now carries every
  // suggestion, including ones already materialized (the setup panels need to
  // show those as "already added"), but every option here submits
  // "suggest:<id>" to create the remote on the fly — offering an existing one
  // would 409 or duplicate it. The materialized ones are already listed under
  // Remotes, so nothing is lost by dropping them here.
  const offerable = suggested.filter((s) => !s.exists);

  // Pre-select the remote a setup flow just created, and focus Path — the modal
  // closing used to be the whole feedback, leaving the user to spot a new name
  // in a dropdown. Applied at most once per preselect, and only once the reload
  // carrying the remote has landed (shouldApplyPreselect owns both rules).
  const pathRef = useRef<HTMLInputElement>(null);
  const appliedPreselect = useRef<string | null>(null);
  useEffect(() => {
    if (!shouldApplyPreselect(preselect, appliedPreselect.current, remotes.map((r) => r.name)))
      return;
    appliedPreselect.current = preselect;
    setRemote(preselect!);
    pathRef.current?.focus();
  }, [preselect, remotes]);

  // Everything the Remote picker can offer, as one list. A remote the user has
  // (value = its verbatim rclone spec) and a suggestion that becomes one on
  // submit (value = "suggest:<id>") differ only in `creates`; `kind` and
  // `provider` come from the SERVER for both, classified from the stored rclone
  // config rather than sniffed out of names and label substrings on this side.
  const choices: RemoteChoice[] = [
    ...remotes.map((r) => ({
      value: r.name,
      label: r.label,
      kind: r.kind,
      provider: r.provider,
      creates: false,
    })),
    ...offerable.map((s) => ({
      value: `suggest:${s.id}`,
      label: s.label,
      kind: s.kind,
      provider: s.provider,
      creates: true,
    })),
  ];

  const parsedLink = parseStorageUrl(link);
  const linkRemote = parsedLink ? pickRemote(choices, parsedLink.provider) : undefined;

  const applyLink = (raw: string) => {
    setLink(raw);
    const parsed = parseStorageUrl(raw);
    if (!parsed) return;
    const rv = pickRemote(choices, parsed.provider);
    if (rv) setRemote(rv);
    const rooted = mountRootForLink(parsed.path);
    setSubpath(rooted);
    // Name from the MOUNTED root (the dataset/collection), not a deep scene or
    // file name — and keep it tracking Path edits (no hand-typed name yet).
    setName(suggestMountName(rooted));
    setNameTouched(false);
  };

  // The rclone spec the Add button will mount, previewed live so it matches
  // what the mounted card then shows. A "suggest:<id>" selection resolves to
  // its real remote name at submit; use the suggestion's name for the preview.
  const resolvedBase = remote.startsWith("suggest:")
    ? `${offerable.find((s) => `suggest:${s.id}` === remote)?.remote_name ?? ""}:`
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
        Surface a remote as a local folder. The <b>Remote</b> list groups by how each one is
        reached — your own remotes, credentials detected on this machine, and public data
        that needs none. An entry marked <b>+</b> isn’t set up yet; picking it creates the
        remote as part of adding the mount.
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
              {linkRemote ? "" : "; pick a remote"}.
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
            {REMOTE_GROUPS.map((g) => {
              const items = choices.filter((c) => c.kind === g.kind);
              if (items.length === 0) return null;
              return (
                <optgroup key={g.kind} label={g.label}>
                  {items.map((c) => (
                    // value is the raw rclone spec — or "suggest:<id>", which
                    // add() materializes first; only the shown text differs.
                    <option key={c.value} value={c.value}>
                      {c.creates ? `+ ${c.label}` : c.label}
                    </option>
                  ))}
                </optgroup>
              );
            })}
          </Select>
        </Field>
        <Field label="Path">
          <TextInput
            ref={pathRef}
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
      <p className="deploy-muted mount-paste-hint">
        Tip: mount a specific <b>bucket/prefix</b>, not a whole bucket — narrow mounts browse and
        search much faster.
      </p>
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </section>
  );
}

function AddRemote({
  onCreated,
  onBusyChange,
}: {
  // Reports the created remote's spec (incl. ':') so Add mount can pre-select
  // it — every setup flow ends by handing the user back to the mount form.
  onCreated: (remote: string) => void;
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
      const created = await createRemote(name, {
        access_key_id: accessKey,
        secret_access_key: secretKey,
        endpoint,
        region,
      });
      setName("");
      setAccessKey("");
      setSecretKey("");
      onCreated(created.name);
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
      {/* Same closing line as the other setup panels: a remote is not a mount,
          and the modal simply vanishing gave no clue where to go next. */}
      <p className="deploy-muted mount-paste-hint">
        Creating a remote doesn’t mount anything yet. This closes and pre-selects it under{" "}
        <b>Remote</b> in “Add mount” — type the bucket/prefix there and add the mount.
      </p>
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
            {/* Wrapped, not loose: .mount-step-body is a flex column, so every
                inline node — each <b> and each run of text between them — was
                becoming its own row. Steps 3 and 4 rendered as a stack of
                fragments ("— pick" / "External" / ", fill in…"). */}
            <p className="mount-step-lead">
              <b>Create or pick a Google Cloud project.</b> Any project works; a free one
              is fine.
            </p>
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
            <p className="mount-step-lead">
              <b>Enable the Google Drive API</b> for that project, then press Enable.
            </p>
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
            <p className="mount-step-lead">
              <b>Configure the consent screen</b> — pick <b>External</b>, fill in the
              required name/email, and set publishing status to <b>In production</b>.
            </p>
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
            <p className="mount-step-lead">
              <b>Create an OAuth client</b> of type <b>Desktop app</b>, then download its
              JSON.
            </p>
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
  // Reports the connected remote's spec (incl. ':') so Add mount can
  // pre-select it — a signed-in remote is not yet a mounted folder.
  onConnected: (remote: string) => void;
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
        // The remote is named by the (connecting-disabled) name field, so this
        // is the spec the server just wrote — no round-trip needed to learn it.
        onConnected(`${name.trim()}:`);
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
        <p className="deploy-muted mount-paste-hint">
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
      {!connecting && (
        <p className="deploy-muted mount-paste-hint">
          Signing in doesn’t mount anything yet. This closes and pre-selects the remote
          under <b>Remote</b> in “Add mount” — type the folder to surface there and add the
          mount.
        </p>
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
  onCreated,
  onBusyChange,
}: {
  kind: "detected" | "public";
  suggested: RemoteSuggestion[];
  onCreated: (remote: string) => void;
  onBusyChange?: (busy: boolean) => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Every suggestion of this kind, INCLUDING already-created ones. Dropping
  // those is what made this panel look broken: with aws-open: already created,
  // "Public datasets" showed a single lone GCS card, which reads as a bug
  // rather than as "you already have the other one". They render disabled.
  const options = suggested.filter((s) => s.kind === kind);

  const create = async (id: string) => {
    setBusy(id);
    onBusyChange?.(true);
    setError(null);
    try {
      onCreated((await createDetectedRemote(id)).name);
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
        // Only "detected" can be empty — the two public remotes are built in.
        <div className="mount-empty">
          No credentials detected. Run <code>aws sso login</code> or{" "}
          <code>gcloud auth application-default login</code>, then reopen this — or use{" "}
          <b>S3-compatible storage</b> to paste keys directly.
        </div>
      ) : (
        <div className="mount-list">
          {options.map((s) => (
            <div
              className={"mount-card" + (s.exists ? " mount-card--added" : "")}
              key={s.id}
            >
              <div className="mount-card-main">
                <div className="mount-card-info">
                  <div>{s.label}</div>
                  <div className="mount-remote">
                    <code>{s.remote_name}:</code>
                  </div>
                </div>
                <div className="mount-card-actions">
                  {s.exists ? (
                    <span className="mount-card-status">Already added</span>
                  ) : (
                    <button
                      type="button"
                      className="btn btn-primary"
                      disabled={busy !== null}
                      onClick={() => void create(s.id)}
                    >
                      {busy === s.id ? "Creating…" : "Create remote"}
                    </button>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
      <p className="deploy-muted mount-paste-hint">
        {options.every((s) => s.exists) && options.length > 0 ? (
          <>
            All set — close this and pick the remote under <b>Remote</b> in “Add mount”,
            then type the bucket/prefix to surface.
          </>
        ) : (
          <>
            Creating a remote doesn’t mount anything yet. This closes and pre-selects it
            under <b>Remote</b> in “Add mount” — type the bucket/prefix there and add the
            mount.
          </>
        )}
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
            {/* The icon is a full-height column of its own, not a glyph inline
                with the name — it reads as the card's mark that way. */}
            <span className="mount-provider-mark">
              <ProviderIcon provider={o.key} />
            </span>
            <span className="mount-provider-text">
              <span className="mount-provider-name">{o.name}</span>
              <span className="mount-provider-cost">{o.cost}</span>
            </span>
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
  // The remote a setup flow just created, handed to Add mount to pre-select.
  // Creating a remote is only half the job — it mounts nothing — and the modal
  // simply vanishing left no visible next step.
  const [preselect, setPreselect] = useState<string | null>(null);
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

  // Shared close path for all six setup flows.
  const finishSetup = (remote: string) => {
    reload();
    setSetup(null);
    setPreselect(remote);
  };

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
            preselect={preselect}
            onChanged={reload}
          />
          <AddStorage onPick={setSetup} />
          {setup && (
            <Modal
              title={STORAGE_OPTIONS.find((o) => o.key === setup)?.title ?? "Add storage"}
              busy={setupBusy}
              onClose={() => setSetup(null)}
            >
              {/* Every flow ends the same way: reload so the new remote appears
                  in Add mount's Remote picker, dismiss, and hand the remote to
                  that picker so the next step is already staged. */}
              {setup === "s3compat" ? (
                <AddRemote onBusyChange={setSetupBusy} onCreated={finishSetup} />
              ) : setup === "detected" || setup === "public" ? (
                <DetectedRemoteSetup
                  kind={setup}
                  suggested={state.rclone.suggested ?? []}
                  onBusyChange={setSetupBusy}
                  onCreated={finishSetup}
                />
              ) : (
                <OAuthSignIn
                  provider={OAUTH_PROVIDERS[setup]}
                  remotes={state.rclone.remotes}
                  onBusyChange={setSetupBusy}
                  onConnected={finishSetup}
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
