// Share any file — the one dialog behind every file Share entry (the
// explorer's kebab menu and crumb-bar right-click, task 7). Sibling of
// ShareAppModal.tsx, not a fork: same host-mounted-once-in-the-shell shape
// (platform/lib/share-file.ts's openShareFile/useShareFileRequest), same
// Stop-sharing-behind-an-AlertDialog pattern as ShareAppModal's Revoke.
//
// EXACTLY TWO SHARE ACTIONS, DELIBERATELY, NOT A VISIBILITY PICKER
// (share-any-file-plan.md's stated UX goal): "Share publicly" (mode
// "public", works forever, no session token) and "Share for 30 minutes"
// (mode "temporary", team-scoped with a session token appended to the URL).
// Once something is shared, both buttons are replaced by the link (Copy,
// Open) and Stop sharing — there is no "switch mode" affordance beyond
// stopping and starting again in the other one, which keeps the sheet a
// two-state machine (nothing shared / something shared) rather than a matrix.
//
// THE DETACHED UPLOAD IS THE ONE PLACE THIS DIFFERS FROM A PLAIN PUBLISH: a
// file over share_file.py's INLINE_PUBLISH_MAX_BYTES makes /publish answer a
// 409 (folded onto `code: "upload_required"` by share-file.ts's `withCode`)
// instead of doing the work inline. On that code alone, `useShareFile` below
// switches to driving /upload → poll /upload/status → /publish with the
// finished job's id, and the sheet shows a progress row with a Cancel button
// in the meantime — the row REPLACES the buttons, so a click never has a
// second meaning.
//
// ALL OF THAT LIVES IN `useShareFile`, SEPARATE FROM THE CHROME: the sheet's
// chrome is `@platform/shadcn/ui/dialog`'s Base UI `Dialog`/`AlertDialog`,
// which portals through `FloatingPortal` — a real `ReactDOM.createPortal`
// call that `react-test-renderer`'s mock tree cannot satisfy (confirmed
// while building this file; `apps/claude/ui/receipt-door.test.tsx` records
// the same finding and asserts its own panel does NOT use this chassis for
// exactly that reason). Since ShareAppModal's sheet is this codebase's
// reference for the shape and carries no test of its own either, the split
// here keeps parity with it in production while making the actual state
// machine — the two-action gate, the confirm-before-stop, the upload
// fallback — testable head-on through the hook (ShareFileModal.test.tsx).
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Check, Clock, Copy, ExternalLink, Globe, Loader2, XIcon } from "lucide-react";
import { copyToClipboard } from "@platform/lib/clipboard";
import { dirname } from "@platform/lib/format";
import {
  cancelUpload,
  closeShareFile,
  getShareFileStatus,
  publishShareFile,
  removeShareFile,
  startUpload,
  uploadStatus,
  useShareFileRequest,
  type ShareableFile,
  type ShareFileRequest,
  type ShareFileStatus,
  type SharedFileRecord,
  type ShareMode,
  type UploadStatus,
} from "@platform/lib/share-file";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@platform/shadcn/ui/alert-dialog";
import { Badge } from "@platform/shadcn/ui/badge";
import { Button } from "@platform/shadcn/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@platform/shadcn/ui/dialog";
import { Input } from "@platform/shadcn/ui/input";
import { Skeleton } from "@platform/shadcn/ui/skeleton";

const UPLOAD_POLL_MS = 1200;

type Busy = null | ShareMode | "remove";

/** The two, and only two, share actions the sheet ever offers. */
export const PRIMARY_ACTIONS: Array<{
  mode: ShareMode;
  label: string;
  busyLabel: string;
  icon: typeof Globe;
}> = [
  { mode: "public", label: "Share publicly", busyLabel: "Sharing…", icon: Globe },
  { mode: "temporary", label: "Share for 30 minutes", busyLabel: "Sharing…", icon: Clock },
];

export type SharePhase = "loading" | "refused" | "no-cli" | "share" | "shared" | "uploading";

export interface ShareFileHookState {
  phase: SharePhase;
  status: ShareFileStatus | null;
  shared: SharedFileRecord | null;
  busy: Busy;
  err: string;
  upload: UploadStatus | null;
  confirmStop: boolean;
  copied: boolean;
  share: (mode: ShareMode) => void;
  requestStop: () => void;
  cancelStopRequest: () => void;
  confirmStopNow: () => void;
  cancelUploadNow: () => void;
  copyLink: () => void;
}

/** All of the sheet's behaviour, with no chrome attached — see this file's
 *  header for why it is split out. */
export function useShareFile(file: ShareableFile): ShareFileHookState {
  const [status, setStatus] = useState<ShareFileStatus | null>(null);
  const [shared, setShared] = useState<SharedFileRecord | null>(null);
  const [busy, setBusy] = useState<Busy>(null);
  const [err, setErr] = useState("");
  const [copied, setCopied] = useState(false);
  const [confirmStop, setConfirmStop] = useState(false);
  const [upload, setUpload] = useState<UploadStatus | null>(null);
  const pollRef = useRef<number | null>(null);
  const goneRef = useRef(false);

  const refresh = useCallback(async () => {
    try {
      const s = await getShareFileStatus(file.path);
      if (!goneRef.current) {
        setStatus(s);
        setShared(s.shared);
      }
      return s;
    } catch (e) {
      if (!goneRef.current) setErr((e as Error).message);
      return null;
    }
  }, [file.path]);

  useEffect(() => {
    goneRef.current = false;
    void refresh();
    return () => {
      goneRef.current = true;
      if (pollRef.current !== null) window.clearInterval(pollRef.current);
    };
  }, [refresh]);

  const stopPolling = () => {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  const finishPublish = useCallback(
    async (mode: ShareMode, uploadId?: string) => {
      try {
        const rec = await publishShareFile(file.path, mode, uploadId);
        if (goneRef.current) return;
        setShared(rec);
        setUpload(null);
        setBusy(null);
      } catch (e) {
        if (goneRef.current) return;
        setErr((e as Error).message);
        setBusy(null);
        setUpload(null);
      }
    },
    [file.path],
  );

  const pollUpload = useCallback(
    (id: string, mode: ShareMode) => {
      stopPolling();
      pollRef.current = window.setInterval(() => {
        void uploadStatus(id)
          .then((s) => {
            if (goneRef.current) return;
            setUpload(s);
            if (s.state === "done") {
              stopPolling();
              void finishPublish(mode, id);
            } else if (s.state === "failed" || s.state === "cancelled") {
              stopPolling();
              setErr(s.error || `the upload was ${s.state}`);
              setBusy(null);
              setUpload(null);
            }
          })
          .catch((e: unknown) => {
            // Finding 10: a rejected uploadStatus() call (a transient
            // network blip, the server briefly unreachable) previously fell
            // through with no handler — the interval kept firing, but
            // nothing here ever ended the "uploading" phase, so the sheet
            // could get stuck on that spinner forever even after the
            // underlying upload had long since finished, failed, or was
            // cancelled. Surface it and stop polling instead of spinning
            // silently.
            if (goneRef.current) return;
            stopPolling();
            setErr((e as Error).message || "lost track of the upload");
            setBusy(null);
            setUpload(null);
          });
      }, UPLOAD_POLL_MS);
    },
    [finishPublish],
  );

  const share = useCallback(
    (mode: ShareMode) => {
      if (busy) return;
      setErr("");
      setBusy(mode);
      void (async () => {
        try {
          const rec = await publishShareFile(file.path, mode);
          if (goneRef.current) return;
          setShared(rec);
          setBusy(null);
        } catch (e) {
          const error = e as Error & { code?: string };
          if (goneRef.current) return;
          if (error.code === "upload_required") {
            try {
              const s = await startUpload(file.path);
              if (goneRef.current) return;
              setUpload(s);
              if (s.state === "done") {
                // Finding 9: `s.id` is the upload's OWN id — the one
                // /publish will look up in share_uploads/ to find this
                // upload's finished remote/s3_uri. `status?.file_id` is a
                // stale snapshot from the LAST /status poll (it can be null
                // on first share, or point at a previous file if `status`
                // hasn't refreshed yet), so using it here risked handing
                // /publish someone else's — or no — upload id.
                void finishPublish(mode, s.id);
              } else {
                pollUpload(s.id, mode);
              }
            } catch (uploadErr) {
              if (!goneRef.current) {
                setErr((uploadErr as Error).message);
                setBusy(null);
              }
            }
            return;
          }
          setErr(error.message);
          setBusy(null);
        }
      })();
    },
    [busy, file.path, finishPublish, pollUpload],
  );

  const cancelUploadNow = useCallback(() => {
    if (!upload) return;
    stopPolling();
    const id = upload.id;
    setUpload(null);
    setBusy(null);
    void cancelUpload(id).catch(() => {});
  }, [upload]);

  const requestStop = useCallback(() => setConfirmStop(true), []);
  const cancelStopRequest = useCallback(() => setConfirmStop(false), []);

  const confirmStopNow = useCallback(() => {
    setConfirmStop(false);
    if (busy) return;
    setErr("");
    setBusy("remove");
    void (async () => {
      try {
        await removeShareFile(file.path);
        if (goneRef.current) return;
        setShared(null);
      } catch (e) {
        if (!goneRef.current) setErr((e as Error).message);
      } finally {
        if (!goneRef.current) setBusy(null);
      }
    })();
  }, [busy, file.path]);

  const copyLink = useCallback(() => {
    if (!shared?.url) return;
    void copyToClipboard(shared.url).then((ok) => {
      if (ok && !goneRef.current) {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1500);
      }
    });
  }, [shared?.url]);

  let phase: SharePhase;
  if (!status && !err) phase = "loading";
  else if (status && !status.can_share) phase = "refused";
  else if (status && !status.cli_found) phase = "no-cli";
  else if (upload && upload.state === "running") phase = "uploading";
  else if (shared) phase = "shared";
  else phase = "share";

  return {
    phase,
    status,
    shared,
    busy,
    err,
    upload,
    confirmStop,
    copied,
    share,
    requestStop,
    cancelStopRequest,
    confirmStopNow,
    cancelUploadNow,
    copyLink,
  };
}

function fmtBytes(n: number | null | undefined): string {
  if (!n || n <= 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

export function ShareFileModal({
  request,
  onClose,
}: {
  request: ShareFileRequest;
  onClose: () => void;
}) {
  const { file } = request;
  const state = useShareFile(file);
  const { phase, status, shared, busy, err, upload, confirmStop, copied } = state;
  const working = busy !== null;

  let body: ReactNode;
  if (phase === "loading") {
    body = <Skeleton className="h-24 w-full" />;
  } else if (phase === "refused") {
    body = (
      <p className="m-0 text-[13px] leading-5 text-destructive" role="alert">
        {status?.refusal}
      </p>
    );
  } else if (phase === "no-cli") {
    body = (
      <p className="m-0 text-[13px] leading-5 text-muted-foreground">
        The fused CLI is not available in this server&rsquo;s environment. Install it with{" "}
        <code className="rounded bg-muted px-1 py-0.5 text-xs">
          pip install &quot;fused-render[fused]&quot;
        </code>
        .
      </p>
    );
  } else if (phase === "uploading") {
    body = (
      <div className="flex flex-col gap-2.5">
        <div className="flex items-center gap-2 text-[13px] leading-5 text-muted-foreground">
          <Loader2 className="size-3.5 animate-spin" aria-hidden />
          Uploading{upload?.bytes ? ` ${fmtBytes(upload.bytes)}` : ""}…
        </div>
        <Button size="sm" variant="outline" onClick={state.cancelUploadNow}>
          Cancel
        </Button>
      </div>
    );
  } else if (phase === "shared" && shared) {
    const url = shared.url;
    body = (
      <div className="flex flex-col gap-2.5">
        <div className="flex items-center gap-1.5">
          <Input
            type="text"
            readOnly
            value={url ?? ""}
            onFocus={(e) => e.currentTarget.select()}
            aria-label="Shared link"
            className="h-8 min-w-0 flex-1 truncate bg-muted/40 font-mono text-[12.5px] text-foreground"
          />
          <Button
            size="icon-sm"
            variant="outline"
            onClick={state.copyLink}
            disabled={!url}
            title={copied ? "Copied" : "Copy link"}
            aria-label={copied ? "Copied" : "Copy link"}
          >
            {copied ? <Check /> : <Copy />}
          </Button>
          {url && (
            <Button
              size="icon-sm"
              variant="outline"
              title="Open the shared page"
              aria-label="Open the shared page"
              render={<a href={url} target="_blank" rel="noopener noreferrer" />}
            >
              <ExternalLink />
            </Button>
          )}
        </div>
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
          <Badge variant="secondary" className="gap-1">
            {shared.mode === "temporary" ? (
              <>
                <Clock /> {shared.expired ? "Expired — share again" : "Expires in 30 minutes"}
              </>
            ) : (
              <>
                <Globe /> Anyone with the link
              </>
            )}
          </Badge>
          <Button
            size="sm"
            variant="outline"
            disabled={working}
            onClick={state.requestStop}
            className="text-muted-foreground hover:border-destructive/40 hover:bg-destructive/10 hover:text-destructive"
          >
            {busy === "remove" && <Loader2 data-icon="inline-start" className="animate-spin" />}
            {busy === "remove" ? "Stopping…" : "Stop sharing"}
          </Button>
        </div>
      </div>
    );
  } else {
    body = (
      <div className="flex flex-col gap-1.5">
        {PRIMARY_ACTIONS.map((a, i) => {
          const Icon = a.icon;
          return (
            <Button
              key={a.mode}
              size="sm"
              variant={i === 0 ? "default" : "outline"}
              disabled={working}
              onClick={() => state.share(a.mode)}
            >
              {busy === a.mode ? (
                <Loader2 data-icon="inline-start" className="animate-spin" />
              ) : (
                <Icon data-icon="inline-start" />
              )}
              {busy === a.mode ? a.busyLabel : a.label}
            </Button>
          );
        })}
      </div>
    );
  }

  return (
    <>
      <Dialog
        open
        onOpenChange={(open) => {
          if (!open && !working) onClose();
        }}
      >
        <DialogContent
          className="max-w-[min(360px,calc(100%-2rem))] gap-0 overflow-hidden p-0 sm:max-w-[min(360px,calc(100%-2rem))]"
          showCloseButton={false}
        >
          {/* `min-w-0` ON EVERY BOX A TRUNCATING CHILD SITS IN, and it is not
              decoration: a flex/grid item's automatic minimum size is its
              MIN-CONTENT, and the path below is `white-space: nowrap`, so the
              header demanded the path's full unwrapped width — measured at
              395px against this sheet's 360px cap — and `overflow-hidden` on
              the sheet then sliced the excess off. The visible fault was a
              title reading "hare index.html" and a path missing its "/U"
              (owner, 2026-09-22). `truncate` alone cannot fix it: it clips
              what has already overflowed rather than letting the box shrink. */}
          <DialogHeader className="min-w-0 gap-0 px-5 pt-3.5 pb-2.5">
            <div className="flex min-w-0 items-center justify-between gap-3">
              <DialogTitle className="min-w-0 truncate text-[15px] font-semibold leading-6">
                Share {file.name}
              </DialogTitle>
              <DialogClose
                render={
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    className="-mr-2 bg-transparent text-muted-foreground hover:text-foreground"
                  />
                }
                disabled={working}
              >
                <XIcon />
                <span className="sr-only">Close</span>
              </DialogClose>
            </div>
            <DialogDescription
              className="min-w-0 truncate text-left text-[12px] text-muted-foreground"
              dir="rtl"
              title={dirname(file.path)}
            >
              <bdi dir="ltr">{dirname(file.path)}</bdi>
            </DialogDescription>
          </DialogHeader>
          <div className="flex min-w-0 flex-col gap-3 px-5 pb-4">
            {body}
            {err && (
              <p className="m-0 text-[13px] leading-5 text-destructive" role="alert">
                {err}
              </p>
            )}
          </div>
          <AlertDialog open={confirmStop} onOpenChange={(open) => !open && state.cancelStopRequest()}>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Stop sharing?</AlertDialogTitle>
                <AlertDialogDescription>
                  The public page is deleted. Every link already sent stops working. You can
                  share it again later.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Keep sharing</AlertDialogCancel>
                <AlertDialogAction variant="destructive" onClick={state.confirmStopNow}>
                  Stop sharing
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </DialogContent>
      </Dialog>
    </>
  );
}

/** Mounted once in the shell; renders the dialog for the current request. */
export function ShareFileHost() {
  const req = useShareFileRequest();
  if (!req) return null;
  return <ShareFileModal key={req.seq} request={req} onClose={closeShareFile} />;
}

export default ShareFileModal;
