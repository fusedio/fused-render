// Mounts page — the /view/_mounts sentinel, entered from the sidebar
// footer. Remote storage (S3-compatible and anything else rclone speaks)
// mounted as local folders under ~/.fused-render/mounts; everything
// downstream — previews, readers, tile servers — sees ordinary local paths.
// Backend: shell/mounts.py (rclone rcd). Credentials live in rclone's
// own config, never here. Section layout and per-action busy/error state
// follow views/Preferences.tsx.
//
// Visual language: Flow. The list is dense bordered rows (mounts/MountList),
// the add flow sits under it, and the two dialogs (provider setup, restart
// confirm) are shadcn Dialogs that keep the old Modal's busy gating — no
// Esc/backdrop/✕ close while a flow holds rclone's callback port.
import { useEffect, useRef, useState } from "react";
import { RefreshCwIcon } from "lucide-react";
import { getMounts, restartRclone } from "@platform/lib/api";
import type { MountsResult } from "@platform/lib/api";
import { OAUTH_PROVIDERS } from "@platform/lib/oauth";
import { useRefreshOnReturn } from "@platform/lib/hooks";
import { hasDrainingUploads } from "@platform/lib/uploads";
import { Button } from "@platform/shadcn/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@platform/shadcn/ui/dialog";
import { Skeleton } from "@platform/shadcn/ui/skeleton";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { EntityList } from "@platform/ui/flow/EntityRow";
import { Muted, Page, PageBody, PageHeader, SectionHeading } from "@platform/ui/flow/Typography";
import { AddMount } from "./mounts/AddMount";
import { Callout, Code } from "./mounts/bits";
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
  // Lifted out of the flows so the dialog can gate its Esc/backdrop/✕ close while
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
  // Global "Restart all mounts": a confirm dialog (it briefly disconnects ALL
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
  // upload queue (D221) honest without polling a handler that probes every
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

  const restartLabel = restartBusy ? "Restarting…" : "Restart all mounts";

  // The page chrome (heading, intro, actions) renders immediately; only the
  // mount list itself waits on the async getMounts() — a blocking full-page
  // "Loading…" made the whole page feel slow when just the list is pending.
  return (
    <Page>
      <PageHeader
        title="Mounts"
        description="Browse remote storage as local folders. Large files are cached locally after the first open."
        actions={
          state?.rclone.available && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={restartBusy}
              onClick={() => setConfirmRestart(true)}
              title="Reconnect all mounts — recovers stuck mounts and picks up refreshed credentials"
            >
              <RefreshCwIcon data-icon="inline-start" className={restartBusy ? "motion-safe:animate-spin" : undefined} />
              {restartLabel}
            </Button>
          )
        }
      />
      <PageBody className="max-w-3xl">
        {loadError && <ErrorBanner>Failed to load mounts: {loadError}</ErrorBanner>}

        {!state && !loadError && (
          <EntityList aria-busy="true" aria-label="Loading mounts">
            {[0, 1].map((i) => (
              <div key={i} className="flex items-center gap-3 px-4 py-2.5 border-b border-border last:border-b-0">
                <Skeleton className="size-3.5 rounded-full" />
                <Skeleton className="size-4" />
                <div className="flex-1 space-y-1.5">
                  <Skeleton className="h-3.5 w-40" />
                  <Skeleton className="h-3 w-64" />
                </div>
              </div>
            ))}
          </EntityList>
        )}

        {state && !state.rclone.available && (
          <Callout title="rclone not found">
            rclone must be installed and on your <Code>PATH</Code> for mounts to work. Install it with{" "}
            <Code>brew install rclone</Code> (macOS), <Code>apt install rclone</Code> /{" "}
            <Code>dnf install rclone</Code> (Linux), or the{" "}
            <a href="https://rclone.org/install/" target="_blank" rel="noreferrer">
              official installer
            </a>
            , then reload this page. Distro packages can be outdated, so a recent version is recommended.
          </Callout>
        )}

        {state && needsRestart && (
          <Callout
            title="Some mounts need a restart"
            warn
            action={
              <Button type="button" size="sm" disabled={restartBusy} onClick={() => setConfirmRestart(true)}>
                {restartLabel}
              </Button>
            }
          >
            {paramsMounts.length > 0 && <p>Settings changed — restart to apply them.</p>}
            {credMounts.length > 0 && (
              <p>
                Credentials were refreshed — restart to reconnect {credMounts.map((m) => m.name).join(", ")}.
              </p>
            )}
          </Callout>
        )}

        {restartError && <ErrorBanner>{restartError}</ErrorBanner>}

        {state && (state.mounts.length > 0 || state.rclone.available) && (
          <section className="space-y-3">
            <SectionHeading>Your mounts</SectionHeading>
            {state.mounts.length > 0 ? (
              <EntityList>
                {state.mounts.map((c) => (
                  <MountRow key={c.id} conn={c} remotes={state.rclone.remotes} onChanged={reload} />
                ))}
              </EntityList>
            ) : (
              <Muted className="border border-dashed border-border rounded-lg p-4 text-center">
                Nothing mounted yet. Paste a storage link below — or connect a provider — and remote folders show
                up here as ordinary local ones.
              </Muted>
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
            {/* Busy gating, as the old Modal chassis did it: while a flow is in
                flight the dialog refuses to close (Esc, backdrop, ✕ — the ✕ is
                hidden outright), because every busy flow has its own Cancel in
                its body that stands the sign-in down properly. */}
            <Dialog
              open={setup !== null}
              onOpenChange={(open) => {
                if (open) return;
                if (setupBusy) return;
                setSetup(null);
              }}
            >
              <DialogContent className="sm:max-w-[600px] max-h-[85vh] overflow-y-auto scrollbar-auto-hide" showCloseButton={!setupBusy}>
                <DialogHeader>
                  <DialogTitle>{STORAGE_OPTIONS.find((o) => o.key === setup)?.title ?? "Add storage"}</DialogTitle>
                </DialogHeader>
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
                ) : setup ? (
                  <OAuthSignIn
                    provider={OAUTH_PROVIDERS[setup]}
                    remotes={state.rclone.remotes}
                    onBusyChange={setSetupBusy}
                    onConnected={finishSetup}
                  />
                ) : null}
              </DialogContent>
            </Dialog>
          </>
        )}

        <Dialog
          open={confirmRestart}
          onOpenChange={(open) => {
            if (open) return;
            if (restartBusy) return;
            setConfirmRestart(false);
          }}
        >
          <DialogContent showCloseButton={!restartBusy}>
            <DialogHeader>
              <DialogTitle>Restart all mounts?</DialogTitle>
              <DialogDescription>
                This reconnects every mount and re-reads storage credentials. <b>All</b> mounts — including
                healthy ones — briefly disconnect while it happens, and files currently open from a mount may
                need to be reopened.
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button type="button" variant="outline" disabled={restartBusy} onClick={() => setConfirmRestart(false)}>
                Cancel
              </Button>
              <Button type="button" disabled={restartBusy} onClick={doRestart}>
                {restartLabel}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </PageBody>
    </Page>
  );
}
