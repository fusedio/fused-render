// Share an app — the one dialog behind every Share entry (the /apps card's
// hover chip and right-click menu, the app page header, the explorer kebab).
// One host (`ShareAppHost`, mounted once in the shell) renders it for
// whichever `openShareApp` request is current, so the menu entries that
// cannot own a dialog still get one (platform/lib/share-app.ts).
//
// TWO CARDS, ONE SHEET. Until this existed the two ways to hand an app to
// someone — a public link on the user's Fused account and a `.fused` file —
// were two unrelated buttons on every surface, and a reader had to already
// know they produced the same artifact to pick between them. Here they are
// the two rows of one Share sheet, the shape every reference share dialog
// (Figma, Notion, Linear, Google Docs) settles on: the link first, because it
// is the thing most people came for, and the file as the route that needs no
// account and no network — "send it any way you like".
//
//   • Public link (share_app.py owns what each call means):
//       not signed in → "Sign in to Fused", polling /api/canvases/status
//         until the browser login lands. Spelled here rather than imported:
//         platform may not import from apps/canvases.
//       signed in, never shared → one primary, Create link. On open a remote
//         lookup runs in the background (the app may have been shared from
//         another machine); Create link does not wait for it — publishing
//         adopts an existing canvas by name anyway.
//       shared → the link in a read-only field with Copy and Open, the facts
//         under it (public, last published, the account), and Update /
//         Revoke. Revoke confirms in an AlertDialog because it is the one
//         action here that breaks something already handed out: every link
//         sent stops working.
//       snapshot (request.link false) → the card stands down with one
//         sentence; links always publish the live app.
//     PUBLIC ONLY, by decision — there is no visibility control here. The
//     one line under the link says so, because "anyone with the link" is
//     the fact a reader needs before pasting it somewhere.
//
//   • App file: the same `.fused` saved to Downloads (api.saveAppFileToDisk).
//     The result lands INLINE in the card — the path, Reveal folder, Open
//     file — rather than as a toast, because the reader is looking at the
//     card that did it.
//
// NEITHER ROUTE PHOTOGRAPHS THE SCREEN. The .fused and the public link carry
// the folder's authored preview.png or none; a missing thumbnail is App
// Doctor's `preview` check to surface, not this sheet's to paper over with a
// native screen shot (retired 2026-09-18 — appShot.ts has the why).
//
// While either route is in flight the dialog refuses to close, so the inline
// result has a card to land in.
//
// Shared by the shell and the explorer, so it spells the two canvases routes it
// touches rather than importing either app's helpers.
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  Check,
  Copy,
  ExternalLink,
  FolderOpen,
  Globe,
  Loader2,
  Package,
  XIcon,
} from "lucide-react";
import { getJson, postJson, revealPath, saveAppFileToDisk } from "@platform/lib/api";
import { copyToClipboard } from "@platform/lib/clipboard";
import { timeAgo } from "@platform/lib/format";
import { notify } from "@platform/lib/notifications";
import { navigate } from "@platform/lib/router";
import { cn } from "@platform/lib/utils";
import {
  closeShareApp,
  getShareStatus,
  lookupShare,
  publishShare,
  removeShare,
  useShareAppRequest,
  type ShareAppRequest,
  type ShareStatus,
  type SharedAppRecord,
} from "@platform/lib/share-app";
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

const LOGIN_POLL_MS = 1500;
// The guarded GETs (whoami runs a CLI child) carry the X-Fused header too —
// the server 403s them without it (apps/canvases/api.ts's GUARD, restated).
const GUARD = { headers: { "X-Fused": "1" } };

interface CanvasesStatusLite {
  logged_in: boolean;
  creds_stamp: number | null;
  login_in_flight: boolean;
}

// The four actions that write something (a .fused, a published share) — at
// most one runs at a time. Sign-in is NOT one of them: it is a browser round-trip
// the user may take minutes over (or never finish), and the file route needs
// no account, so it has its own flag (`loggingIn`) and may overlap an export.
// One shared flag for both was the bug: an export started mid-login and the
// login poll's `setBusy(null)` then dropped the export lock (Bugbot, #1207).
type Busy = null | "publish" | "update" | "remove" | "export";

// One row of the sheet: a glyph plate, a title with its one-line description,
// the row's action at the right, and whatever the route has to show once it
// has acted underneath. `muted` is the standing-down state (a snapshot's link
// card): the row stays so the sheet keeps its shape, but reads as inert.
function OptionCard({
  icon,
  title,
  description,
  action,
  muted,
  children,
}: {
  icon: ReactNode;
  title: ReactNode;
  description: ReactNode;
  action?: ReactNode;
  muted?: boolean;
  children?: ReactNode;
}) {
  return (
    <section
      className={cn(
        "flex flex-col gap-3 rounded-xl border border-border bg-muted/30 p-4 text-card-foreground transition-opacity",
        muted && "opacity-60",
      )}
    >
      <div className="flex items-start gap-3">
        <div className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-muted text-foreground [&_svg]:size-4">
          {icon}
        </div>
        <div className="flex min-w-0 flex-1 flex-col gap-0.5 pt-0.5">
          <div className="flex items-center gap-2 text-sm font-medium leading-5">{title}</div>
          <p className="m-0 text-[13px] leading-5 text-muted-foreground">{description}</p>
        </div>
        {action && <div className="flex shrink-0 items-center gap-2 pt-0.5">{action}</div>}
      </div>
      {children}
    </section>
  );
}

export function ShareAppModal({
  request,
  onClose,
}: {
  request: ShareAppRequest;
  onClose: () => void;
}) {
  const { app, file, link: linkEligible, versionLabel } = request;
  const [status, setStatus] = useState<ShareStatus | null>(null);
  const [shared, setShared] = useState<SharedAppRecord | null>(null);
  const [handle, setHandle] = useState<string | null>(null);
  const [busy, setBusyState] = useState<Busy>(null);
  // Mirrored in a ref so the background lookup's late callback can read what
  // is running NOW, not the value its closure captured when it started.
  const busyRef = useRef<Busy>(null);
  const setBusy = (next: Busy) => {
    busyRef.current = next;
    setBusyState(next);
  };
  const [loggingIn, setLoggingIn] = useState(false);
  const [linkErr, setLinkErr] = useState("");
  const [fileErr, setFileErr] = useState("");
  const [copied, setCopied] = useState(false);
  const [confirmRevoke, setConfirmRevoke] = useState(false);
  const [looking, setLooking] = useState(false);
  const [saved, setSaved] = useState<{ name: string; path: string } | null>(null);
  const loginStampRef = useRef<number | null>(null);
  const pollRef = useRef<number | null>(null);

  // `status.logged_in` is the credentials FILE existing (share_app.py, like
  // canvases.py). A file can exist and be dead — the token behind it refused
  // (a 401 with `code: not_logged_in` from publish/lookup/remove) — and only
  // that call can tell. Remembered here so the card drops to its sign-in
  // view; cleared when a sign-in completes (the file's stamp changes).
  const [denied, setDenied] = useState(false);
  const signedIn = !!status && status.logged_in && !denied;

  const refresh = useCallback(async () => {
    try {
      const s = await getShareStatus(app.path);
      setStatus(s);
      setShared(s.shared);
      return s;
    } catch (e) {
      setLinkErr((e as Error).message);
      return null;
    }
  }, [app.path]);

  // `goneRef` stands in for an effect's `cancelled` flag so a read started
  // after a sign-in is dropped on unmount the same way as the mount one.
  const goneRef = useRef(false);

  // Who the link publishes as — a name beside the link, never a verdict:
  // a failed whoami leaves the card nameless and changes nothing else.
  const readHandle = useCallback(() => {
    getJson<{ handle: string | null }>("/api/canvases/whoami", GUARD)
      .then((w) => {
        if (!goneRef.current) setHandle(w.handle);
      })
      .catch(() => {});
  }, []);

  // Ask Fused once whether a canvas for this app already exists somewhere
  // (shared from another machine, or before the local record was lost).
  const lookup = useCallback(
    (s: ShareStatus | null) => {
      if (!s || !s.logged_in || !s.can_share || !s.app_id) return;
      readHandle();
      if (s.shared) return;
      setLooking(true);
      lookupShare(app.path)
        .then((r) => {
          if (!goneRef.current && r.found && r.shared) setShared(r.shared);
        })
        .catch((e: Error & { code?: string }) => {
          // A failed lookup is not an error worth a sentence — Create link
          // still works — EXCEPT a refused token, which the sign-in view must
          // show. Not while a publish/update/remove is running, though: that
          // call gets the same 401 and reports it itself once it ends, whereas
          // flipping the view mid-flight would hide the running export behind
          // a Sign in button that `busy` keeps inert.
          if (!goneRef.current && busyRef.current === null && e.code === "not_logged_in") {
            setDenied(true);
          }
        })
        .finally(() => {
          if (!goneRef.current) setLooking(false);
        });
    },
    [app.path, readHandle],
  );

  // First read, then the remote lookup when signed in with no local record.
  // Skipped entirely for a snapshot request: the link card stands down and
  // has nothing to ask.
  useEffect(() => {
    goneRef.current = false;
    if (linkEligible) {
      void refresh().then((s) => {
        if (!goneRef.current) lookup(s);
      });
    }
    return () => {
      goneRef.current = true;
    };
  }, [refresh, lookup, linkEligible]);

  useEffect(
    () => () => {
      if (pollRef.current !== null) window.clearInterval(pollRef.current);
    },
    [],
  );

  const onLogin = async () => {
    // Not while another action runs (same predicate as its button's
    // `disabled`). Not twice, either — a second poll would run alongside the
    // first.
    if (loggingIn || busy !== null) return;
    setLinkErr("");
    setLoggingIn(true);
    loginStampRef.current = status?.creds_stamp ?? null;
    try {
      await postJson<{ ok: boolean }>("/api/canvases/login", {});
    } catch (e) {
      setLoggingIn(false);
      setLinkErr((e as Error).message);
      return;
    }
    pollRef.current = window.setInterval(() => {
      void getJson<CanvasesStatusLite>("/api/canvases/status").then((s) => {
        const completed = s.logged_in && s.creds_stamp !== loginStampRef.current;
        if (completed) {
          if (pollRef.current !== null) window.clearInterval(pollRef.current);
          pollRef.current = null;
          setLoggingIn(false);
          setDenied(false);
          // The same two reads the open does: a freshly signed-in account
          // may already hold a canvas for this app from another machine, and
          // the primary action must then be its link, not a second Create.
          void refresh().then((s) => {
            if (!goneRef.current) lookup(s);
          });
        } else if (!s.login_in_flight) {
          if (pollRef.current !== null) window.clearInterval(pollRef.current);
          pollRef.current = null;
          setLoggingIn(false);
          setLinkErr("Sign-in was not completed — try again.");
        }
      });
    }, LOGIN_POLL_MS);
  };

  const doPublish = async (kind: "publish" | "update") => {
    if (busy) return;
    setLinkErr("");
    setBusy(kind);
    try {
      const rec = await publishShare(app);
      setShared(rec);
      notify({ title: kind === "update" ? "Link updated" : "Link ready", tone: "info" });
    } catch (e) {
      const error = e as Error & { code?: string };
      if (error.code === "not_logged_in") {
        setDenied(true);
        void refresh();
      }
      setLinkErr(error.message);
    } finally {
      setBusy(null);
    }
  };

  const doRevoke = async () => {
    if (busy) return;
    setLinkErr("");
    setBusy("remove");
    try {
      await removeShare(app.path);
      setShared(null);
      notify({ title: "Link revoked", tone: "info" });
    } catch (e) {
      const error = e as Error & { code?: string };
      if (error.code === "not_logged_in") setDenied(true);
      setLinkErr(error.message);
    } finally {
      setBusy(null);
    }
  };

  const doExport = async () => {
    // The file route needs no account, so a sign-in in flight (its own flag)
    // does not block it — only another running action does. Same
    // predicate as the button's `disabled` (`working`), so the click never
    // lands on a button that then does nothing (Bugbot, #1207).
    if (busy !== null) return;
    setFileErr("");
    setBusy("export");
    try {
      const realPath = await saveAppFileToDisk(file.path, file.name);
      setSaved({ name: file.name, path: realPath });
    } catch (e) {
      setFileErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const copy = async () => {
    if (!shared?.url) return;
    const ok = await copyToClipboard(shared.url);
    if (ok) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    }
  };

  // Publishing and exporting both land a result in the sheet; a sheet
  // dismissed mid-flight would have nowhere to put it.
  const working = busy !== null;
  const linkBusy = busy === "publish" || busy === "update" || busy === "remove";
  const isLiveFile = !versionLabel || versionLabel === "Live";

  // ---- the link card ---------------------------------------------------------
  let linkAction: ReactNode = null;
  let linkBody: ReactNode = null;
  if (!linkEligible) {
    // handled by the muted card below
  } else if (!status && !linkErr) {
    linkAction = <Skeleton className="h-8 w-24" />;
  } else if (status && !status.can_share) {
    linkBody = (
      <p className="m-0 text-[13px] leading-5 text-destructive" role="alert">
        {status.refusal}
      </p>
    );
  } else if (status && !status.cli_found) {
    linkBody = (
      <p className="m-0 text-[13px] leading-5 text-muted-foreground">
        The fused CLI is not available in this server&rsquo;s environment. Install it with{" "}
        <code className="rounded bg-muted px-1 py-0.5 text-xs">
          pip install &quot;fused-render[fused]&quot;
        </code>
        .
      </p>
    );
  } else if (status && !signedIn) {
    // Disabled while ANY action runs, matching onLogin's own guard.
    linkAction = (
      <Button size="sm" onClick={onLogin} disabled={loggingIn || working}>
        {loggingIn && <Loader2 data-icon="inline-start" className="animate-spin" />}
        {loggingIn ? "Waiting for sign-in…" : "Sign in to Fused"}
      </Button>
    );
    linkBody = loggingIn ? (
      <p className="m-0 text-[13px] leading-5 text-muted-foreground">
        Finish signing in in the browser window that just opened.
      </p>
    ) : null;
  } else if (!shared) {
    linkAction = (
      <Button size="sm" disabled={working} onClick={() => void doPublish("publish")}>
        {busy === "publish" && <Loader2 data-icon="inline-start" className="animate-spin" />}
        {busy === "publish" ? "Publishing…" : "Create link"}
      </Button>
    );
    linkBody =
      busy === "publish" ? (
        <p className="m-0 text-[13px] leading-5 text-muted-foreground">
          Exporting and uploading — this can take up to a minute.
        </p>
      ) : looking ? (
        <p className="m-0 text-[13px] leading-5 text-muted-foreground">
          Checking whether it is already shared…
        </p>
      ) : null;
  } else {
    const url = shared.url;
    linkBody = (
      <div className="flex flex-col gap-2.5">
        <div className="flex items-center gap-1.5">
          <Input
            type="text"
            readOnly
            value={url ?? "No public link yet — press Update to publish one."}
            onFocus={(e) => e.currentTarget.select()}
            aria-label="Public link"
            className="h-8 flex-1 truncate bg-muted/40 font-mono text-[12.5px] text-foreground"
          />
          <Button
            size="icon-sm"
            variant="outline"
            onClick={copy}
            disabled={!url}
            title={copied ? "Copied" : "Copy link"}
            aria-label={copied ? "Copied" : "Copy link"}
            className={copied ? "text-foreground" : "text-muted-foreground hover:text-foreground"}
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
          <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
            <Badge variant="secondary" className="gap-1">
              <Globe /> Anyone with the link
            </Badge>
            {shared.adopted ? (
              <span>Found on your account — Update publishes this folder.</span>
            ) : shared.updated_at ? (
              <span title={new Date(shared.updated_at * 1000).toLocaleString()}>
                Published {timeAgo(shared.updated_at) ?? "just now"}
              </span>
            ) : null}
            {handle && <span className="truncate">as @{handle}</span>}
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            {/* Outline, not ghost: the shell ships Tailwind without preflight,
                so a variant that sets no background inherits the base
                `button` fill and reads as a disabled grey slab. Same weight
                as Update beside it; the red arrives only on hover. */}
            <Button
              size="sm"
              variant="outline"
              disabled={working}
              onClick={() => setConfirmRevoke(true)}
              className="text-muted-foreground hover:border-destructive/40 hover:bg-destructive/10 hover:text-destructive"
            >
              {busy === "remove" && <Loader2 data-icon="inline-start" className="animate-spin" />}
              {busy === "remove" ? "Revoking…" : "Revoke"}
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={working}
              onClick={() => void doPublish("update")}
            >
              {busy === "update" && <Loader2 data-icon="inline-start" className="animate-spin" />}
              {busy === "update" ? "Updating…" : "Update"}
            </Button>
          </div>
        </div>
        {busy === "update" && (
          <p className="m-0 text-[13px] leading-5 text-muted-foreground">
            Exporting and uploading — this can take up to a minute.
          </p>
        )}
      </div>
    );
  }

  const linkTitle = (
    <>
      Public link
      {linkEligible && shared?.url && !linkBusy && (
        <Badge variant="outline" className="h-4.5 px-1.5 text-[10.5px] font-medium">
          Live
        </Badge>
      )}
    </>
  );
  const linkDescription = !linkEligible
    ? "Links always publish the live app. Pick Live in the version picker to share one."
    : shared
      ? "A page on udf.ai with the app's README and a download. Update replaces what's behind the link."
      : "Publish to your Fused account as a public page anyone can open.";

  // ---- the file card ----------------------------------------------------------
  const fileAction = (
    <Button size="sm" variant={linkEligible && signedIn ? "outline" : "default"} disabled={working} onClick={() => void doExport()}>
      {busy === "export" && <Loader2 data-icon="inline-start" className="animate-spin" />}
      {busy === "export" ? "Exporting…" : isLiveFile ? "Download" : `Download ${versionLabel}`}
    </Button>
  );
  const fileBody = saved ? (
    <div className="flex items-center gap-2 rounded-lg bg-muted/50 px-3 py-2">
      <Check className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
      <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground" title={saved.path}>
        Saved <span className="text-foreground">{saved.name}.fused</span>
        <span className="mx-1.5 opacity-50">·</span>
        {saved.path}
      </span>
      {/* `bg-transparent` on both: no preflight, so a ghost button would
          otherwise show the UA's grey buttonface fill. */}
      <Button
        size="xs"
        variant="ghost"
        className="bg-transparent"
        onClick={() => {
          revealPath(saved.path).catch(() => {});
        }}
      >
        <FolderOpen data-icon="inline-start" />
        Reveal
      </Button>
      <Button
        size="xs"
        variant="ghost"
        className="bg-transparent"
        onClick={() => {
          onClose();
          navigate(saved.path, { isDir: false });
        }}
      >
        Open
      </Button>
    </div>
  ) : busy === "export" ? (
    <p className="m-0 text-[13px] leading-5 text-muted-foreground">
      Bundling the app…
    </p>
  ) : null;

  return (
    <>
      <Dialog
        open
        onOpenChange={(open) => {
          if (!open && !working) onClose();
        }}
      >
        <DialogContent className="gap-0 overflow-hidden p-0 sm:max-w-[560px]" showCloseButton={false}>
          {/* Title bar only — the two cards under it each carry their own
              sentence, so a description here said nothing twice. The sheet
              is still named for assistive tech: `sr-only` description. */}
          <DialogHeader className="gap-0 px-6 pt-4 pb-3">
            <div className="flex items-center justify-between gap-3">
              <DialogTitle className="truncate text-[15px] font-semibold leading-6">
                Share {app.name}
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
            <DialogDescription className="sr-only">
              Share this app as a public link or as a .fused file.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-3 px-6 pb-5">
            <OptionCard
              icon={<Globe />}
              title={linkTitle}
              description={linkDescription}
              action={linkAction}
              muted={!linkEligible}
            >
              {linkBody}
              {linkErr && (
                <p className="m-0 text-[13px] leading-5 text-destructive" role="alert">
                  {linkErr}
                </p>
              )}
            </OptionCard>
            <OptionCard
              icon={<Package />}
              title="App file"
              description={
                <>
                  A single <code className="font-mono text-[12px]">.fused</code> file that
                  opens in Fused. Send it any way you like — no account needed.
                </>
              }
              action={fileAction}
            >
              {fileBody}
              {fileErr && (
                <p className="m-0 text-[13px] leading-5 text-destructive" role="alert">
                  {fileErr}
                </p>
              )}
            </OptionCard>
          </div>
          {/* NESTED inside DialogContent, not a sibling of the Dialog: Base UI
              only coordinates dialogs through the React tree — a nested root
              registers with its parent, which then ignores presses landing in
              it. As a sibling, a click on Keep/Revoke would read as an OUTSIDE
              press on the sheet, close it, and unmount this alert with it. */}
          <AlertDialog open={confirmRevoke} onOpenChange={setConfirmRevoke}>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Revoke this link?</AlertDialogTitle>
                <AlertDialogDescription>
                  The public page and its canvas are deleted. Every link already sent
                  stops working. You can create a new link later.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Keep link</AlertDialogCancel>
                <AlertDialogAction
                  variant="destructive"
                  onClick={() => {
                    setConfirmRevoke(false);
                    void doRevoke();
                  }}
                >
                  Revoke link
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
export function ShareAppHost() {
  const req = useShareAppRequest();
  if (!req) return null;
  return <ShareAppModal key={req.seq} request={req} onClose={closeShareApp} />;
}

export default ShareAppModal;
