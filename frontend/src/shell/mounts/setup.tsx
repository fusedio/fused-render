// The six provider setup flows and the picker that opens them. Each one ends by
// handing a created remote back to the Add-mount form. Split out of
// views/Mounts.tsx.
import { useEffect, useRef, useState } from "react";
import {
  cancelRemoteOAuth,
  createDetectedRemote,
  createRemote,
  getRemoteOAuthStatus,
  startRemoteOAuth,
} from "../../lib/api";
import type { RcloneRemote, RemoteOAuthStatus, RemoteSuggestion } from "../../lib/api";
import { oauthCancelOutcome, oauthTick } from "../../lib/oauth";
import type { OAuthDecision, OAuthProvider, OAuthProviderKey } from "../../lib/oauth";
import {
  clearGoogleClient,
  googleConsoleUrls,
  loadGoogleClient,
  parseGoogleClientJson,
  saveGoogleClient,
} from "../../lib/google-client";
import type { GoogleOAuthClient } from "../../lib/google-client";
import { ErrorBanner } from "../../components/ErrorBanner";
import { Field, TextInput } from "../../components/field/fields";
import { ProviderIcon } from "../../components/ProviderIcons";

export function AddRemote({
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
    <div className="mount-panel">
      <p className="mount-panel-lede">
        Storage that speaks S3 at its own endpoint — Cloudflare R2, Backblaze B2, Wasabi,
        MinIO.
      </p>
      <form
        className="mount-panel-grid"
        onSubmit={(e) => {
          e.preventDefault();
          if (canSubmit) void add();
        }}
      >
        <div className="mount-panel-wide">
          <Field label="Remote name" required>
            <TextInput
              placeholder="e.g. r2"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </Field>
        </div>
        {/* Endpoint and Region share a row: they are the same fact (where this
            storage lives), and the endpoint is what makes a remote
            "S3-compatible" at all. The old order put Region beside the remote's
            NAME — two unrelated answers on one line. */}
        <Field label="Endpoint">
          <TextInput
            placeholder="blank for AWS S3"
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
        <div className="mount-panel-wide mount-panel-actions">
          <button type="submit" className="btn btn-primary" disabled={!canSubmit}>
            {busy ? "Creating…" : "Create remote"}
          </button>
        </div>
      </form>
      <p className="mount-note">
        Keys go straight into rclone’s own config; fused-render never stores them. For plain
        AWS S3 use <b>AWS S3</b>, and for Drive, Dropbox or Box use those — they have no keys
        to paste.
      </p>
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </div>
  );
}

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
  const [dragOver, setDragOver] = useState(false);
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
      {/* The list header, not a second .mount-panel-lede: the modal already
          opened with one body-size sentence and this checklist is content
          under it. */}
      <p className="mount-steps-head">
        Drive needs <b>your own</b> Google API client — one time, on this machine.
      </p>
      {/* One bordered checklist, not four button blocks: each step is a single
          row — numeral, bold title, a right-aligned link that opens the exact
          console page — with at most one muted caveat under it. The numbering
          is real sequence (each console page depends on the one before), and
          the quiet link treatment keeps the visual weight on the drop zone
          below, which is the only input this modal actually exists to
          collect. */}
      <ol className="mount-steps">
        <li className="mount-step">
          <div className="mount-step-row">
            <span className="mount-step-title">Create or pick a Google Cloud project</span>
            <button
              type="button"
              className="mount-step-open"
              onClick={() => open(urls.createProject)}
            >
              Open&nbsp;↗
            </button>
          </div>
          <p className="mount-note">Any project works; a free one is fine.</p>
          <TextInput
            className="mount-step-project"
            placeholder="Project ID (optional — the links below jump straight to it)"
            aria-label="Project ID (optional — the links below jump straight to it)"
            value={project}
            onChange={(e) => setProject(e.target.value)}
          />
        </li>
        <li className="mount-step">
          <div className="mount-step-row">
            <span className="mount-step-title">Enable the Google Drive API</span>
            <button
              type="button"
              className="mount-step-open"
              onClick={() => open(urls.enableApi)}
            >
              Open&nbsp;↗
            </button>
          </div>
        </li>
        <li className="mount-step">
          <div className="mount-step-row">
            <span className="mount-step-title">Configure the consent screen</span>
            <button
              type="button"
              className="mount-step-open"
              onClick={() => open(urls.consentScreen)}
            >
              Open&nbsp;↗
            </button>
          </div>
          <p className="mount-note">
            Pick <b>External</b>, fill in the required name/email, and publish{" "}
            <b>In production</b>.
          </p>
          {/* The one caveat that keeps two sentences, because both are costly
              and neither is guessable. "Testing" LOOKS like the cautious
              choice and silently breaks the mount a week later — Google drops
              refresh tokens issued by a Testing-mode client after 7 days. And
              the scary "unverified app" warning stops people mid-flow even
              though verification is simply not required at this scale. */}
          <p className="mount-note warn">
            Do <b>not</b> leave it in “Testing” — Google expires those sign-ins after 7
            days and the mount stops working. The “unverified app” warning is expected;
            click through <i>Advanced → Go to … (unsafe)</i>.
          </p>
        </li>
        <li className="mount-step">
          <div className="mount-step-row">
            <span className="mount-step-title">
              Create an OAuth client, type <b>Desktop app</b>
            </span>
            <button
              type="button"
              className="mount-step-open"
              onClick={() => open(urls.createClient)}
            >
              Open&nbsp;↗
            </button>
          </div>
          {/* Desktop app is not a preference: rclone authorize serves a
              loopback redirect, which is exactly what a Desktop client
              permits and a Web client does not without a registered URI. */}
          <p className="mount-note">
            Only Desktop app allows the local sign-in redirect. Download its JSON.
          </p>
        </li>
      </ol>

      {/* A <label> around a hidden file input, so the whole zone is the
          browse button — the visible native control matched nothing else on
          the page and buried the modal's one real input. */}
      <label
        className={"mount-drop" + (dragOver ? " mount-drop--over" : "")}
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          void takeFile(e.dataTransfer.files?.[0]);
        }}
      >
        <input
          type="file"
          accept=".json,application/json"
          onChange={(e) => void takeFile(e.target.files?.[0])}
        />
        <b>Drop the downloaded <code>client_secret_….json</code> here</b>
        <span className="mount-drop-sub">or click to browse for it</span>
      </label>
      {fileError && <ErrorBanner>{fileError}</ErrorBanner>}

      {/* The fallback, deliberately below the file path: it works, but it is
          the step people mistype. */}
      <details className="mount-manual">
        <summary>Or paste the client ID and secret</summary>
        <div className="mount-panel-grid">
          <Field label="Client ID" required>
            <TextInput
              placeholder="….apps.googleusercontent.com"
              value={client.clientId}
              onChange={(e) => onChange({ ...client, clientId: e.target.value.trim() })}
            />
          </Field>
          <Field label="Client secret" required>
            <TextInput
              type="password"
              value={client.clientSecret}
              onChange={(e) =>
                onChange({ ...client, clientSecret: e.target.value.trim() })
              }
            />
          </Field>
        </div>
      </details>

      {client.clientId && client.clientSecret && (
        <p className="mount-note">
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
export function OAuthSignIn({
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
    <div className="mount-panel">
      <p className="mount-panel-lede">
        Opens {provider.label} in your browser to approve access, read-write.
      </p>
      <p className="mount-note">
        rclone handles the sign-in and writes the token into its own config; fused-render
        never stores it.
        {provider.key === "drive" &&
          " Google Docs, Sheets and Slides are skipped — they aren’t real files and can’t be opened or saved through a mount."}
      </p>
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
      {/* Name + action on one grid row, bottom-aligned by the grid rather than
          by a blank <Field label=" "> caption. */}
      <form
        className="mount-panel-row"
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
      </form>
      {!connecting && !clientReady && (
        <p className="mount-note">Add your Google API client above to enable the sign-in.</p>
      )}
      {connecting && (
        <p className="mount-note" role="status">
          Waiting for you to approve access in your browser… If no tab opened, check for a
          blocked window.
        </p>
      )}
      {!connecting && collides && (
        // Re-signing in under the name you already use is the NATURAL action
        // (a revoked or expired token is the usual reason to be here), so this
        // has to be possible — just never by accident. config/create
        // overwrites, and the server refuses without this flag.
        <label className="mount-note mount-note-check">
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
        <p className="mount-note warn">{nameError}</p>
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
export function DetectedRemoteSetup({
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
  // rather than as "you already have the other one".
  const options = suggested.filter((s) => s.kind === kind);

  // ONE verb for every row, and no dead ends. An already-created remote used to
  // render as a disabled "Already added" row: true, useless, and a dead stop
  // for the user who came here to mount something from it — they had to close
  // the modal and go hunt the name in a dropdown. "Use" now means the same
  // thing in both rows (select this remote in the form and close); whether that
  // costs a creation round-trip first is our problem, not theirs.
  const use = async (s: RemoteSuggestion) => {
    if (s.exists) {
      onCreated(`${s.remote_name}:`);
      return;
    }
    setBusy(s.id);
    onBusyChange?.(true);
    setError(null);
    try {
      onCreated((await createDetectedRemote(s.id)).name);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
      onBusyChange?.(false);
    }
  };

  return (
    <div className="mount-panel">
      <p className="mount-panel-lede">
        {kind === "detected"
          ? "Credentials already on this machine — your ~/.aws profiles, gcloud application-default credentials, and the usual environment variables."
          : "Anonymous access to open data — AWS Open Data, public GCS datasets, and anything else readable without an account."}
      </p>
      <p className="mount-note">
        {kind === "detected" ? (
          <>
            Nothing is copied: the remote is created with <code>env_auth</code>, so rclone
            reads them where they already live.
          </>
        ) : (
          <>Read-only by nature, and it works even with no cloud credentials at all.</>
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
            <div className="mount-card" key={s.id}>
              <div className="mount-card-main">
                <div className="mount-card-info">
                  <div className="mount-card-name">{s.label}</div>
                  <div className="mount-remote">
                    <code>{s.remote_name}:</code>
                    {s.exists && <span className="mount-hint"> — already set up</span>}
                  </div>
                </div>
                <div className="mount-card-actions">
                  <button
                    type="button"
                    className={"btn " + (s.exists ? "btn-secondary" : "btn-primary")}
                    disabled={busy !== null}
                    onClick={() => void use(s)}
                  >
                    {busy === s.id ? "Setting up…" : "Use"}
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
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
export type SetupKey = OAuthProviderKey | "detected" | "s3compat" | "public";

export const STORAGE_OPTIONS: { key: SetupKey; name: string; cost: string; title: string }[] = [
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

// The bottom of the Add-mount section, not a section of its own: this is the
// branch you take when you have no link to paste yet, so it lives inside the
// one add flow rather than under a second heading with its own vocabulary.
export function ProviderPicker({ onPick }: { onPick: (key: SetupKey) => void }) {
  return (
    <div className="mount-providers">
      <div className="mount-providers-head">
        No link? Connect a provider first — tokens and keys go straight into rclone’s own
        config, never here.
      </div>
      <div className="mount-picker">
        {STORAGE_OPTIONS.map((o) => (
          <button
            type="button"
            key={o.key}
            className="mount-provider"
            onClick={() => onPick(o.key)}
          >
            {/* The icon is a column of its own, not a glyph inline with the
                name — it reads as the card's mark that way. */}
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
    </div>
  );
}
