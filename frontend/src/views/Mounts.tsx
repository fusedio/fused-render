// Mounts page — the /view/_mounts sentinel, entered from the sidebar
// footer. Remote storage (S3-compatible and anything else rclone speaks)
// mounted as local folders under ~/.fused-render/mounts; everything
// downstream — previews, readers, tile servers — sees ordinary local paths.
// Backend: shell/mounts.py (rclone rcd). Credentials live in rclone's
// own config, never here. Section layout and per-action busy/error state
// follow views/Preferences.tsx.
import { useEffect, useRef, useState } from "react";
import { getMounts, restartRclone } from "../lib/api";
import type { MountsResult } from "../lib/api";
import { OAUTH_PROVIDERS } from "../lib/oauth";
import { useRefreshOnReturn } from "../lib/hooks";
import { hasDrainingUploads } from "../lib/uploads";
import { Modal } from "../components/modal/Modal";
import { ErrorBanner } from "../components/ErrorBanner";
import { AddMount } from "./mounts/AddMount";
import type { RemoteHandoff } from "./mounts/links";
import { MountRow } from "./mounts/MountList";
import {
  AddRemote,
  DetectedRemoteSetup,
  OAuthSignIn,
  STORAGE_OPTIONS,
} from "./mounts/setup";
import type { SetupKey } from "./mounts/setup";

// How often the mount list re-reads itself while an upload is in flight.
// Deliberately slow: GET /api/mounts probes every mount, so this only runs
// when there is something to watch drain.
const UPLOAD_POLL_MS = 8000;

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
  // The last setup flow to finish, handed to Add mount to pre-select. Creating
  // a remote is only half the job — it mounts nothing — and the modal simply
  // vanishing left no visible next step. Carries a NONCE, so connecting the
  // same remote twice is two handoffs rather than a silent no-op (see
  // RemoteHandoff): re-using aws-open:, or re-signing in to Drive to replace an
  // expired token, are both ordinary things to do twice.
  const [preselect, setPreselect] = useState<RemoteHandoff | null>(null);
  const handoffNonce = useRef(0);
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
    handoffNonce.current += 1;
    setPreselect({ remote, nonce: handoffNonce.current });
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

      {state && (state.mounts.length > 0 || state.rclone.available) && (
        <section className="prefs-section">
          <h2>Your mounts</h2>
          {state.mounts.length > 0 ? (
            <div className="mount-list">
              {state.mounts.map((c) => (
                <MountRow
                  key={c.id}
                  conn={c}
                  remotes={state.rclone.remotes}
                  onChanged={reload}
                />
              ))}
            </div>
          ) : (
            <div className="mount-empty">
              Nothing mounted yet. Paste a storage link below — or connect a provider — and
              remote folders show up here as ordinary local ones.
            </div>
          )}
        </section>
      )}

      {state?.rclone.available && (
        <>
          <AddMount
            remotes={state.rclone.remotes}
            suggested={state.rclone.suggested ?? []}
            preselect={preselect}
            onChanged={reload}
            onPickProvider={setSetup}
          />
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
