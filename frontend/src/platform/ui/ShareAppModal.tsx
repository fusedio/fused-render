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
//     No visibility control here: every publish asks for public, and the
//     one line under the link says so, because "anyone with the link" is
//     the fact a reader needs before pasting it somewhere. A deployment
//     that forbids public canvases makes the shim fall back to a team
//     canvas with a 30-minute session token (`mode: temporary`,
//     `public_refused`); the badge then says the link expires and the
//     note under it says why — same Clock badge as ShareFileModal.
//
//   • App file: the same `.fused` saved to Downloads (api.saveAppFileToDisk).
//     The result lands INLINE in the card — the path, Reveal folder, Open
//     file — rather than as a toast, because the reader is looking at the
//     card that did it.
//
//   • GitHub link (only with Community → Where apps come from on —
//     contrib-flags.ts `sources`; the old `github_link_enabled` switch is
//     folded into it server-side and nothing here reads it): a udf.ai page
//     that installs the app from its
//     GitHub repo. Nothing is uploaded, so it is copyable only when the
//     folder IS the repo root, `origin` is GitHub, and nothing is unpushed
//     (share_app.py `github_link_state`); otherwise the card stays muted with
//     the one sentence saying which condition failed. Muted for a snapshot
//     too, like the public link: the repo is the live app.
//     It is then the sheet's public link: first and primary, with the
//     Fused-storage link card second (owner 2026-10-27, see below).
//     For an app the user OWNS (`/api/contrib/ladder` answers `mine`), the
//     card is the ladder's GitHub door instead (docs/CONTRIB_LOOP.md §19.3,
//     `GithubLadderCard` below): rung 1 puts it on GitHub inline — three
//     check rows and one button, no trip through the Git panel — rung 2 is
//     the install link plus a "Next: list it in the Marketplace" strip, rung
//     3 the listing's status. It shows for every own app WHATEVER the
//     switches say (owner, 2026-10-26): putting an app on GitHub is plain
//     publishing; Contributing off hides only the install link, and the
//     Marketplace switch off only the Next strip. A copy of someone else's
//     app (`mine:false`) or a server without the ladder keeps the link card
//     as described above, switches included.
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
  Clock,
  Copy,
  ExternalLink,
  FolderOpen,
  GitBranch,
  Globe,
  Loader2,
  Package,
  XIcon,
} from "lucide-react";
import { getJson, postJson, revealPath, saveAppFileToDisk } from "@platform/lib/api";
import { copyToClipboard } from "@platform/lib/clipboard";
import { timeAgo } from "@platform/lib/format";
import { notify } from "@platform/lib/notifications";
import { encodeFsPathSegments, navigate, navigateUrl, urlForFsPath } from "@platform/lib/router";
import { cn } from "@platform/lib/utils";
import {
  getAppContrib,
  getLadder,
  ladderListing,
  opensDoctor,
  plainMessage,
  publishGithub,
  runAction,
  runJob,
  useUnmountSignal,
  type ContribAction,
  type LadderAnswer,
} from "@platform/lib/contrib";
import {
  cancelGithubLogin,
  getGithubInstall,
  getGithubLogin,
  getGithubPublish,
  getGithubStatus,
  pollRecord,
  recordRunning,
  slugRepoName,
  startGithubInstall,
  startGithubLogin,
  type GithubStatus,
  type LoginRecord,
  type SetupRecord,
} from "@platform/lib/github-setup";
import {
  closeShareApp,
  getGithubLink,
  getShareStatus,
  lookupShare,
  publishShare,
  removeShare,
  useShareAppRequest,
  type GithubLinkState,
  type ShareAppRequest,
  type ShareStatus,
  type SharedAppRecord,
} from "@platform/lib/share-app";
import { useContribFlags } from "@platform/lib/contrib-flags";
import { useAppSharingFeature } from "@platform/lib/share-app-flag";
import { AppDoctorModal } from "@platform/ui/AppDoctorModal";
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
import { Label } from "@platform/shadcn/ui/label";
import { RadioGroup, RadioGroupItem } from "@platform/shadcn/ui/radio-group";
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

// Why an app has no GitHub link yet, in product words (docs/CONTRIB_LOOP.md
// §0.1, §13): keyed on share_app.github_link_state's `code`, because its
// `reason` speaks git. An unknown or missing code (an older server) gets the
// plain catch-all rather than the raw reason.
//
// This is the fallback card's mapping (a copy of someone else's app, or a
// server without `/api/contrib/ladder`); an app the user owns gets
// `GithubLadderCard`, which puts it on GitHub inline (§19.3). The sheet never
// deep-links the Git panel's Publish to GitHub dialog (`panel=publish`) any
// more, so "not on GitHub" here is a sentence with no button. Unpushed edits
// keep their one next step: the app folder's git view (the `git` template,
// the same place App Doctor's "Open in git" lands).
type GithubNextStep = "changes" | null;

function githubLinkSentence(code: GithubLinkState["code"]): { text: string; next: GithubNextStep } {
  switch (code) {
    case "no_remote":
    case "not_repo":
      return { text: "Publish this app to GitHub first, then share its link here.", next: null };
    case "dirty":
    case "unpushed":
      return { text: "Your latest edits aren't on GitHub yet.", next: "changes" };
    case "nested":
      return {
        text: "This app lives inside a bigger project on GitHub, so it can't have a link of its own.",
        next: null,
      };
    case "not_github":
      return {
        text: "This app is stored somewhere other than GitHub, so it can't be shared by link.",
        next: null,
      };
    case "no_default":
      return { text: "GitHub hasn't finished setting this app up yet. Try again in a moment.", next: null };
    default:
      return { text: "This app isn't on GitHub yet.", next: null };
  }
}

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

// ---- the GitHub card on the ladder (docs/CONTRIB_LOOP.md §19.3) -------------
//
// The door from rung 1 (only on this Mac) to rung 2 (on GitHub), inline:
//
//   rung 1 → three check rows, each with the one action that clears it —
//            GitHub CLI (Install), Signed in as <login> (Sign in / Cancel),
//            Repository (github.com/<login>/ + a name, Public or Private) —
//            and ONE button, "Put it on GitHub" (`publish-github`), whose
//            progress line is the publish record's `detail`.
//   rung 2 → the install link (Copy + Open the install page), the unpushed
//            edits' "Publish changes" (the contrib action the Source panel
//            runs, not the Git panel), and a "Next" strip pointing at the
//            Community tab — or saying what Doctor still needs.
//   rung 3 → the listing's state in that strip ("Live · v1.2.0", "In
//            review") with the same "Open Community".
//
// Every step's truth is a GET: install and sign-in start a record and the
// card polls it (github-setup.ts `pollRecord`) until it settles, then
// re-reads the ladder, which is what moves the card to its next state.
// Exported for its test: the sheet around it portals (Base UI Dialog), which
// react-test-renderer cannot host — ShareFileModal.test.tsx has the why.

type LadderStep = null | "install" | "login" | "cancel" | "publish" | "push";

/** `_set_publish(done)` lands before the server's `classify.rebind`, so the
 *  first ladder read after a done publish can still say rung 1. */
const PUBLISH_SETTLE_TRIES = 4;
const PUBLISH_SETTLE_MS = 700;
/** While the ladder says `pending`: re-read this often, this many times
 *  (contrib.ts `useLadder`'s cadence). */
const LADDER_PENDING_MS = 5_000;
const LADDER_PENDING_TRIES = 12;

/** The app page's Community tab (§19.4). Spelled here, not imported from the
 *  shell's current-apps-lib: this sheet renders inside the explorer too, and
 *  platform may not import the shell (appdoctor-lib's `tasksTabUrl`, same). */
export function communityTabUrl(dir: string): string {
  return "/apps/" + encodeFsPathSegments(dir.replace(/\/+$/, "")) + "?_tab=community";
}

function folderName(path: string): string {
  const parts = path.replace(/\/+$/, "").split("/");
  return parts[parts.length - 1] || path;
}

const sleep = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));

/** An answer the ladder card can stand on: an own app's, with its facts. */
function ownLadder(l: LadderAnswer | null | undefined): l is LadderAnswer {
  return !!l && l.ok === true && l.mine === true && !!l.github;
}

/** One prerequisite of rung 1: a tick (or an empty ring), what it is, the
 *  state it is in, and the action that clears it. */
function CheckRow({
  done,
  label,
  detail,
  action,
  children,
}: {
  done: boolean;
  label: ReactNode;
  detail?: ReactNode;
  action?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="flex items-start gap-3" data-check={done ? "done" : "todo"}>
      <span
        aria-hidden
        className={cn(
          "mt-0.5 flex size-4.5 shrink-0 items-center justify-center rounded-full border [&_svg]:size-3",
          done ? "border-primary bg-primary text-primary-foreground" : "border-border bg-background",
        )}
      >
        {done && <Check />}
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <span className="text-[13px] font-medium leading-5">{label}</span>
        {detail && <span className="text-xs leading-4 text-muted-foreground">{detail}</span>}
        {children}
      </div>
      {action && <div className="flex shrink-0 items-center gap-1.5">{action}</div>}
    </div>
  );
}

/** The listing's state as the strip's one line. Keyed on the answer's
 *  `state` (`actions_listing.decide` — the same value the server picks rung 3
 *  by, ladder.py RUNG3_STATES), else the listing's own review status. */
function listingLine(ladder: LadderAnswer): { text: string; detail: string | null } {
  const { listing, sentence, version } = ladderListing(ladder.listing);
  const status =
    ladder.listing?.state ?? listing?.review?.status ?? (listing?.listed ? "live" : null);
  switch (status) {
    case "removing":
      return { text: "Leaving the Community", detail: sentence };
    case "live":
      return { text: version ? `Live · v${version.replace(/^v/i, "")}` : "Live in the Community", detail: null };
    case "in-review":
      return { text: "In review", detail: sentence };
    case "needs-confirmation":
      return { text: "Waiting for you to confirm", detail: sentence };
    case "failed":
      return { text: "The review turned it down", detail: sentence };
    default:
      return { text: listing?.listed ? "In the Community" : "In review", detail: sentence };
  }
}

export function GithubLadderCard({
  dir,
  ladder,
  reload,
  primary,
  locked,
  onBusy,
  onClose,
}: {
  /** The app folder. */
  dir: string;
  /** An own app's ladder (`ok && mine`). */
  ladder: LadderAnswer;
  /** Re-read the ladder; resolves with the new answer (null on failure). */
  reload: () => Promise<LadderAnswer | null>;
  /** The card leads the sheet: its action is the sheet's primary. */
  primary: boolean;
  /** Another of the sheet's actions is running (an export). */
  locked: boolean;
  /** A publish is running: the sheet refuses to close. */
  onBusy?: (busy: boolean) => void;
  onClose: () => void;
}) {
  const gh = ladder.github;
  // Never null for an own app's answer (`ownLadder`); 1 is the safe floor.
  const rung = ladder.rung ?? 1;
  const flags = ladder.flags ?? { sources: false, marketplace: false };
  const goneRef = useRef(false);
  useEffect(() => {
    goneRef.current = false;
    return () => {
      goneRef.current = true;
    };
  }, []);
  const isGone = useCallback(() => goneRef.current, []);
  const unmountSignal = useUnmountSignal();

  const [status, setStatus] = useState<GithubStatus | null>(null);
  const [install, setInstall] = useState<SetupRecord | null>(null);
  const [login, setLogin] = useState<LoginRecord | null>(null);
  const [publish, setPublish] = useState<SetupRecord | null>(null);
  const [step, setStep] = useState<LadderStep>(null);
  const [err, setErr] = useState("");
  // Put it on GitHub refused for something App Doctor fixes (409 reason
  // "doctor": sharing checks left, which a public repo needs clear; or
  // "app-id": the app has no fused-app-id yet — contrib.ts `opensDoctor`):
  // the error line
  // then offers App Doctor itself, opened over the sheet — for as long as
  // that refusal is the line on screen (any later error replaces it).
  const [doctorErr, setDoctorErr] = useState<string | null>(null);
  const [doctorOpen, setDoctorOpen] = useState(false);
  const [name, setName] = useState(() => slugRepoName(folderName(dir)));
  const [visibility, setVisibility] = useState<"public" | "private">("public");
  const [pushAction, setPushAction] = useState<ContribAction | null>(null);
  const [pushConfirm, setPushConfirm] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  // The status read is the authority on rung 1's first two rows; the
  // ladder's copy of the same facts paints until it lands.
  const found = status ? status.found : gh.installed;
  const signedIn = status ? status.found && status.signed_in : gh.signed_in;
  const account = (status?.account ?? gh.login) || null;
  const installing = recordRunning(install);
  const signingIn = recordRunning(login);
  const publishing = step === "publish" || recordRunning(publish);

  // Tells the sheet while a publish runs, and lets go on unmount. `onBusy`
  // is expected to be stable (the sheet passes a state setter).
  useEffect(() => {
    if (!publishing) return;
    onBusy?.(true);
    return () => onBusy?.(false);
  }, [publishing, onBusy]);

  /** After a step settles: the fresh status, then the ladder. */
  const afterStep = useCallback(async () => {
    const s = await getGithubStatus().catch(() => null);
    if (goneRef.current) return;
    if (s) setStatus(s);
    await reload();
  }, [reload]);

  const watchInstall = useCallback(async () => {
    const done = await pollRecord(getGithubInstall, { isGone, onTick: setInstall });
    if (!done || goneRef.current) return;
    setInstall(done);
    if (done.state === "error") {
      setErr(done.error || "The GitHub CLI didn't install. Try again.");
      return;
    }
    await afterStep();
  }, [isGone, afterStep]);

  const watchLogin = useCallback(async () => {
    const done = await pollRecord(getGithubLogin, { isGone, onTick: setLogin });
    if (!done || goneRef.current) return;
    setLogin(done);
    if (done.error && !done.canceled) setErr(done.error);
    await afterStep();
  }, [isGone, afterStep]);

  const watchPublish = useCallback(async (): Promise<SetupRecord | null> => {
    const done = await pollRecord(getGithubPublish, { isGone, onTick: setPublish });
    if (!done || goneRef.current) return null;
    setPublish(done);
    return done;
  }, [isGone]);

  /** A done publish: re-read until the ladder says rung 2 (see
   *  PUBLISH_SETTLE_TRIES). The card re-renders in its rung-2 state then. */
  const settlePublish = useCallback(async () => {
    for (let i = 0; i < PUBLISH_SETTLE_TRIES; i++) {
      const next = await reload();
      if (goneRef.current || (next?.ok && next.mine && (next.rung ?? 1) >= 2)) return;
      await sleep(PUBLISH_SETTLE_MS);
      if (goneRef.current) return;
    }
  }, [reload]);

  // Rung 1 at open: the status read, and any record ALREADY running (a sign-in
  // waiting in the browser, an install, a publish started a moment ago) is
  // adopted and watched. A settled one is an earlier attempt's — another
  // sheet's, the Git panel's — and must not paint its error here.
  useEffect(() => {
    if (rung !== 1) return;
    let cancelled = false;
    void Promise.allSettled([getGithubStatus(), getGithubInstall(), getGithubLogin(), getGithubPublish()]).then(
      ([s, i, l, p]) => {
        if (cancelled || goneRef.current) return;
        if (s.status === "fulfilled") setStatus(s.value);
        if (i.status === "fulfilled" && recordRunning(i.value)) {
          setInstall(i.value);
          void watchInstall();
        }
        if (l.status === "fulfilled" && recordRunning(l.value)) {
          setLogin(l.value);
          void watchLogin();
        }
        // A publish is single-flight across repos: only one for THIS folder
        // is this card's to show (`root` is the realpath'd repo root).
        const pubRoot = p.status === "fulfilled" ? (p.value.root ?? "").replace(/\/+$/, "") : "";
        if (p.status === "fulfilled" && recordRunning(p.value) && pubRoot === dir.replace(/\/+$/, "")) {
          setPublish(p.value);
          void watchPublish().then((done) => {
            if (done?.state === "done") void settlePublish();
            else if (done?.state === "error") setErr(done.error || "Couldn't put it on GitHub.");
          });
        }
      },
    );
    return () => {
      cancelled = true;
    };
    // Once per rung-1 open; the watchers only close over stable refs.
  }, [rung]);

  // Rung 2+ with edits not on GitHub: the action that publishes them, as the
  // Source panel offers it (`publish-now` for an own app; `push-main` is a
  // collaborator's and refuses an own one). None offered (the sources switch
  // off, a state with other fixes first) → the git view instead.
  const unpushed = rung >= 2 ? gh.unpushed : 0;
  useEffect(() => {
    if (unpushed <= 0) {
      setPushAction(null);
      return;
    }
    let cancelled = false;
    getAppContrib(dir)
      .then((a) => {
        if (cancelled) return;
        const act = (a.actions ?? []).find((x) => x.id === "publish-now" || x.id === "push-main") ?? null;
        setPushAction(act);
      })
      .catch(() => {
        if (!cancelled) setPushAction(null);
      });
    return () => {
      cancelled = true;
    };
  }, [dir, unpushed]);

  const busy = step !== null || locked;

  const onInstall = async () => {
    if (busy || installing) return;
    setErr("");
    setStep("install");
    try {
      const rec = await startGithubInstall();
      if (goneRef.current) return;
      setInstall(rec);
      void watchInstall();
    } catch (e) {
      if (!goneRef.current) setErr((e as Error).message);
    } finally {
      if (!goneRef.current) setStep(null);
    }
  };

  const onLogin = async () => {
    if (busy || signingIn || !found) return;
    setErr("");
    setStep("login");
    try {
      const rec = await startGithubLogin();
      if (goneRef.current) return;
      setLogin(rec);
      void watchLogin();
    } catch (e) {
      if (!goneRef.current) setErr((e as Error).message);
    } finally {
      if (!goneRef.current) setStep(null);
    }
  };

  const onCancelLogin = async () => {
    if (step !== null) return;
    setStep("cancel");
    try {
      const rec = await cancelGithubLogin();
      if (!goneRef.current) setLogin(rec);
    } catch {
      // The poll still settles the record; nothing to say here.
    } finally {
      if (!goneRef.current) setStep(null);
    }
  };

  const repoName = name.trim();
  // The sentence Put it on GitHub would refuse with (nested in a bigger
  // project, a remote already there) — said up front, button off.
  const blocked = rung === 1 ? (gh.blocked ?? null) : null;
  const ready = found && signedIn && !!repoName && !installing && !signingIn && !blocked;

  const onPublish = async () => {
    if (busy || publishing || !ready) return;
    setErr("");
    setStep("publish");
    try {
      const rec = await publishGithub(dir, { name: repoName, visibility });
      if (goneRef.current) return;
      setPublish(rec);
      const done = recordRunning(rec) ? await watchPublish() : rec;
      if (!done || goneRef.current) return;
      if (done.state === "error") {
        setErr(done.error || "Couldn't put it on GitHub.");
        return;
      }
      await settlePublish();
    } catch (e) {
      // A refusal (409): the server's sentence as-is.
      if (goneRef.current) return;
      const said = (e as Error).message;
      setErr(said);
      const reason = (e as { body?: { reason?: unknown } }).body?.reason;
      setDoctorErr(typeof reason === "string" && opensDoctor(reason) ? said : null);
    } finally {
      if (!goneRef.current) setStep(null);
    }
  };

  const runPush = async (confirmed: boolean) => {
    if (!pushAction || busy) return;
    if (!confirmed && pushAction.confirm) {
      setPushConfirm(pushAction.confirm);
      return;
    }
    setErr("");
    setPushConfirm(null);
    setStep("push");
    try {
      const started = await runAction(dir, pushAction.id, { confirm: confirmed });
      if (goneRef.current) return;
      if (!started.ok && started.reason === "confirm" && !confirmed) {
        setPushConfirm(plainMessage(started));
        return;
      }
      if (started.ok && !started.job) {
        await reload();
        return;
      }
      const r = await runJob(Promise.resolve(started), { signal: unmountSignal() });
      if (goneRef.current || (!r.ok && r.reason === "aborted")) return;
      if (!r.ok) setErr(r.message);
      await reload();
    } finally {
      if (!goneRef.current) setStep(null);
    }
  };

  const showChanges = () => {
    if (busy) return;
    onClose();
    navigateUrl(urlForFsPath(dir, "?_mode=git"), { isDir: true });
  };

  const openCommunity = () => {
    if (publishing) return;
    onClose();
    navigateUrl(communityTabUrl(dir));
  };

  // The install link follows Contributing (`sources`) alone — the legacy
  // `github_link_enabled` switch is folded into it on the server.
  const link = flags.sources ? gh.link : null;
  const copyLink = async () => {
    if (!link) return;
    if (await copyToClipboard(link)) {
      setCopied(true);
      window.setTimeout(() => {
        if (!goneRef.current) setCopied(false);
      }, 1500);
    }
  };

  const errLine = err ? (
    <>
      <p className="m-0 text-[13px] leading-5 text-destructive" role="alert">
        {err}
      </p>
      {doctorErr !== null && err === doctorErr && (
        <div>
          <Button size="sm" variant="outline" onClick={() => setDoctorOpen(true)}>
            Open App Doctor
          </Button>
        </div>
      )}
      {/* Nested inside the sheet's own Dialog through this card (see the
          note on the revoke alert below): a sibling would read its clicks
          as outside presses on the sheet. */}
      {doctorOpen && (
        <AppDoctorModal
          dir={dir}
          onClose={() => {
            setDoctorOpen(false);
            void reload();
          }}
        />
      )}
    </>
  ) : null;

  // ---- rung 1: on this Mac --------------------------------------------------
  if (rung === 1) {
    const login_ = account ?? "you";
    // While running, the record's own line; once done (the server's rebind
    // still landing), a plain "almost there" — `detail` is the repo URL then.
    const progress = publishing
      ? publish?.state === "done"
        ? "On GitHub — finishing up…"
        : (publish?.state === "running" && publish.detail) || "Creating the repository and pushing…"
      : null;
    return (
      <OptionCard
        icon={<GitBranch />}
        title="GitHub"
        description="Only you have it. Put it on GitHub and anyone with the link can install it — and it stays up to date."
      >
        <div className="flex flex-col gap-3 rounded-lg border border-border bg-background/60 p-3">
          <CheckRow
            done={found}
            label="GitHub CLI"
            detail={
              found
                ? status?.version
                  ? `Installed · ${status.version}`
                  : "Installed"
                : installing
                  ? install?.detail || "Installing…"
                  : "Fused uses GitHub's own tool to create the repository."
            }
            action={
              !found && (
                <Button size="xs" variant="outline" disabled={busy || installing} onClick={() => void onInstall()}>
                  {installing && <Loader2 data-icon="inline-start" className="animate-spin" />}
                  {installing ? "Installing…" : "Install"}
                </Button>
              )
            }
          />
          <CheckRow
            done={found && signedIn}
            label={signedIn && account ? `Signed in as ${account}` : "Signed in to GitHub"}
            detail={
              signedIn
                ? null
                : signingIn
                  ? login?.code
                    ? `Finish in the browser window that just opened. Your code: ${login.code}`
                    : "Finish signing in in the browser window that just opened."
                  : found
                    ? "Sign in with your GitHub account in the browser."
                    : "After the GitHub CLI is installed."
            }
            action={
              !signedIn &&
              (signingIn ? (
                <Button size="xs" variant="outline" disabled={step !== null} onClick={() => void onCancelLogin()}>
                  Cancel
                </Button>
              ) : (
                <Button size="xs" variant="outline" disabled={busy || !found} onClick={() => void onLogin()}>
                  {step === "login" && <Loader2 data-icon="inline-start" className="animate-spin" />}
                  Sign in
                </Button>
              ))
            }
          />
          <CheckRow done={found && signedIn && !!repoName} label="Repository">
            <div className="flex min-w-0 items-center gap-1.5">
              <span className="shrink-0 font-mono text-[12.5px] text-muted-foreground">
                github.com/{login_}/
              </span>
              <Input
                type="text"
                value={name}
                onChange={(e) => setName(e.currentTarget.value)}
                disabled={publishing}
                aria-label="Repository name on GitHub"
                spellCheck={false}
                className="h-7 min-w-0 flex-1 font-mono text-[12.5px]"
              />
            </div>
            <RadioGroup
              value={visibility}
              onValueChange={(v) => setVisibility(v === "private" ? "private" : "public")}
              disabled={publishing}
              aria-label="Who can see the repository"
              className="flex w-auto flex-wrap items-center gap-x-4 gap-y-1 pt-0.5"
            >
              <Label className="flex items-center gap-1.5 text-[13px] font-normal">
                <RadioGroupItem value="public" />
                Public
              </Label>
              <Label className="flex items-center gap-1.5 text-[13px] font-normal">
                <RadioGroupItem value="private" />
                Private
              </Label>
            </RadioGroup>
            <span className="text-xs leading-4 text-muted-foreground">
              {visibility === "public"
                ? "Public is needed for the Community."
                : "The Community needs a public repository. You can still share the install link."}
            </span>
          </CheckRow>
        </div>
        {blocked && (
          <p className="m-0 text-[13px] leading-5 text-muted-foreground" role="note">
            {blocked}
          </p>
        )}
        <div className="flex flex-wrap items-center justify-end gap-x-3 gap-y-2">
          {progress && (
            <p className="m-0 mr-auto min-w-0 text-[13px] leading-5 text-muted-foreground">{progress}</p>
          )}
          <Button
            size="sm"
            variant={primary ? "default" : "outline"}
            disabled={!ready || busy || publishing}
            onClick={() => void onPublish()}
          >
            {publishing && <Loader2 data-icon="inline-start" className="animate-spin" />}
            {publishing ? "Putting it on GitHub…" : "Put it on GitHub"}
          </Button>
        </div>
        {errLine}
      </OptionCard>
    );
  }

  // ---- rung 2 / 3: on GitHub (and maybe in the Marketplace) ----------------
  const vis = gh.visibility;
  const description = gh.repo
    ? `On GitHub as ${gh.repo}${vis ? ` · ${vis}` : ""}. Anyone with the link installs the app, and it stays up to date.`
    : "On GitHub. Anyone with the link installs the app, and it stays up to date.";
  // The Next strip: rung 3's listing state, or rung 2's way to it — only while
  // the Marketplace is offered (`marketplace_ui()` off hides rung 3).
  let strip: { text: string; detail: string | null } | null = null;
  if (flags.marketplace && rung === 3) {
    strip = listingLine(ladder);
  } else if (flags.marketplace) {
    // `clear` null = Doctor could not say: no verdict, so no "fix first".
    const failing = ladder.doctor?.failing ?? 0;
    strip = {
      text: "Next: list it in the Community",
      detail:
        ladder.doctor?.clear === false
          ? `Doctor has ${failing || "some"} check${failing === 1 ? "" : "s"} to fix first.`
          : vis === "private"
            ? "The Community needs a public repository."
            : null,
    };
  }
  return (
    <OptionCard
      icon={<GitBranch />}
      title={
        <>
          GitHub
          {vis && (
            <Badge variant="outline" className="h-4.5 px-1.5 text-[10.5px] font-medium capitalize">
              {vis}
            </Badge>
          )}
        </>
      }
      description={description}
      action={
        link ? (
          <Button size="sm" variant={primary ? "default" : "outline"} onClick={() => void copyLink()}>
            {copied ? <Check data-icon="inline-start" /> : <Copy data-icon="inline-start" />}
            {copied ? "Copied" : flags.marketplace ? "Copy link" : "Copy install link"}
          </Button>
        ) : null
      }
    >
      {link && (
        <div className="flex items-center gap-1.5">
          <Input
            type="text"
            readOnly
            value={link}
            onFocus={(e) => e.currentTarget.select()}
            aria-label="GitHub link"
            className="h-8 flex-1 truncate bg-muted/40 font-mono text-[12.5px] text-foreground"
          />
          <Button
            size="icon-sm"
            variant="outline"
            title="Open the install page"
            aria-label="Open the install page"
            render={<a href={link} target="_blank" rel="noopener noreferrer" />}
          >
            <ExternalLink />
          </Button>
        </div>
      )}
      {unpushed > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
          <p className="m-0 min-w-0 text-[13px] leading-5 text-muted-foreground">
            {step === "push" ? "Publishing your changes…" : "Your latest edits aren't on GitHub yet."}
          </p>
          {pushConfirm === null && (
            <Button
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={() => (pushAction ? void runPush(false) : showChanges())}
            >
              {step === "push" && <Loader2 data-icon="inline-start" className="animate-spin" />}
              {pushAction ? "Publish changes" : "Show changes"}
            </Button>
          )}
        </div>
      )}
      {pushConfirm !== null && (
        <div className="flex flex-col gap-2 rounded-lg border border-border bg-background/60 p-3">
          <p className="m-0 text-[13px] leading-5">{pushConfirm}</p>
          <div className="flex justify-end gap-1.5">
            <Button size="sm" variant="outline" onClick={() => setPushConfirm(null)}>
              Cancel
            </Button>
            <Button size="sm" disabled={busy} onClick={() => void runPush(true)}>
              Publish changes
            </Button>
          </div>
        </div>
      )}
      {strip && (
        <div
          className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2 rounded-lg border border-border bg-background/60 px-3 py-2"
          data-strip={rung === 3 ? "listing" : "next"}
        >
          <div className="flex min-w-0 flex-col gap-0.5">
            <span className="text-[13px] font-medium leading-5">{strip.text}</span>
            {strip.detail && <span className="text-xs leading-4 text-muted-foreground">{strip.detail}</span>}
          </div>
          <Button size="sm" variant="outline" disabled={locked} onClick={openCommunity}>
            Open Community
          </Button>
        </div>
      )}
      {errLine}
    </OptionCard>
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
  // Community → Where apps come from (contrib-flags.ts) makes the GitHub link
  // THE public way to share: its card shows, first, as the primary — and only
  // then (the legacy `github_link_enabled` switch is folded into this one on
  // the server, so a user who once turned it on does not keep a card no
  // switch on Preferences can turn off). The Fused-storage link card (the
  // public link on the user's Fused account) stays on the sheet either way
  // — owner 2026-10-27: "let's keep that around for a bit more" — second
  // under Community, first without it. `null` until the prefs read settles
  // (a failed read settles as all off); meanwhile the link slot is one
  // placeholder (see the render) and the Fused link card's reads wait, so
  // nothing reorders when the flags land.
  const contribFlags = useContribFlags();
  const githubFirst = contribFlags?.sources === true;
  const flagsKnown = contribFlags !== null;
  // Owner 2026-10-27: three switches — Public link (this card), Community
  // apps (the GitHub card), Remix; the sheet opens when any is on.
  const publicLinkOn = useAppSharingFeature();
  const fusedLinkShown = publicLinkOn;
  const fusedLinkReads = flagsKnown && publicLinkOn;
  const [github, setGithub] = useState<GithubLinkState | null>(null);
  const [githubErr, setGithubErr] = useState("");
  const [githubCopied, setGithubCopied] = useState(false);
  // An app the user owns: its ladder (§19.2), which turns the GitHub card
  // into `GithubLadderCard`. Null while unread, and for good when the folder
  // is not the user's own (`mine:false`) or the server has no ladder — the
  // link card below then reads `/api/share/github` as before.
  const [ladder, setLadder] = useState<LadderAnswer | null>(null);
  const [ladderPublishing, setLadderPublishing] = useState(false);

  // The ladder first, WHATEVER the switches say (§19.2): an app the user owns
  // always gets the ladder card — putting it on GitHub is plain publishing,
  // no flag; inside the card Contributing off hides only the install link and
  // `marketplace` only the Next strip. `notOwn` flips once the answer says
  // this is not an own app's (a copy, or a server without the ladder); only
  // then does the switch-gated link card read `/api/share/github` (local git
  // only, no fetch). Not for a snapshot: that card stands down without asking.
  // `pending` (who owns the repo is still being asked): the answer so far is
  // shown and the ladder re-read every LADDER_PENDING_MS, as `useLadder` does,
  // so a copy that turns out to be the user's own swaps to the ladder card.
  const [notOwn, setNotOwn] = useState(false);
  useEffect(() => {
    if (!linkEligible) return;
    let cancelled = false;
    void (async () => {
      for (let i = 0; i < LADDER_PENDING_TRIES; i++) {
        const l = await getLadder(app.path);
        if (cancelled) return;
        if (ownLadder(l)) {
          setLadder(l);
          setNotOwn(false);
        } else {
          setNotOwn(true);
        }
        if (!(l.ok && l.pending)) return;
        await sleep(LADDER_PENDING_MS);
        if (cancelled) return;
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [linkEligible, app.path]);

  useEffect(() => {
    if (!githubFirst || !linkEligible || !notOwn) return;
    let cancelled = false;
    getGithubLink(app.path)
      .then((g) => {
        if (!cancelled) setGithub(g);
      })
      .catch((e: Error) => {
        if (!cancelled) setGithubErr(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, [githubFirst, linkEligible, notOwn, app.path]);

  // Re-read after each ladder step; an answer that is no longer an own
  // app's (a transient failure) keeps the card where it was.
  const reloadLadder = useCallback(async (): Promise<LadderAnswer | null> => {
    const l = await getLadder(app.path, { fresh: true });
    if (ownLadder(l)) {
      setLadder(l);
      return l;
    }
    return null;
  }, [app.path]);

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
    if (linkEligible && fusedLinkReads) {
      void refresh().then((s) => {
        if (!goneRef.current) lookup(s);
      });
    }
    return () => {
      goneRef.current = true;
    };
  }, [refresh, lookup, linkEligible, fusedLinkReads]);

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

  const copyGithub = async () => {
    if (!github?.url) return;
    const ok = await copyToClipboard(github.url);
    if (ok) {
      setGithubCopied(true);
      window.setTimeout(() => setGithubCopied(false), 1500);
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
            value={url ?? "No link yet — press Update to publish one."}
            onFocus={(e) => e.currentTarget.select()}
            aria-label={shared.mode === "temporary" ? "Team link" : "Public link"}
            className="h-8 flex-1 truncate bg-muted/40 font-mono text-[12.5px] text-foreground"
          />
          <Button
            size="icon-sm"
            variant="outline"
            onClick={copy}
            disabled={!url || shared.expired}
            title={copied ? "Copied" : "Copy link"}
            aria-label={copied ? "Copied" : "Copy link"}
            className={copied ? "text-foreground" : "text-muted-foreground hover:text-foreground"}
          >
            {copied ? <Check /> : <Copy />}
          </Button>
          {url && !shared.expired && (
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
              {shared.mode === "temporary" ? (
                <>
                  <Clock /> {shared.expired ? "Expired — Update for a fresh link" : "Expires in 30 minutes"}
                </>
              ) : (
                <>
                  <Globe /> Anyone with the link
                </>
              )}
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
        {shared.public_refused && (
          <p className="m-0 text-[13px] leading-5 text-muted-foreground">
            Public links are off for this Fused environment, so this one is shared with your
            team and expires after 30 minutes. Update mints a fresh link.
          </p>
        )}
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
      {shared?.mode === "temporary" ? "Team link" : "Public link"}
      {linkEligible && shared?.url && !shared.expired && !linkBusy && (
        <Badge variant="outline" className="h-4.5 px-1.5 text-[10.5px] font-medium">
          Live
        </Badge>
      )}
    </>
  );
  const linkDescription = !linkEligible
    ? "Links always publish the live app. Pick Live in the version picker to share one."
    : shared?.mode === "temporary"
      ? "A page with the app's README and a download, for your team. Update replaces what's behind the link and renews it."
      : shared
      ? "A page on udf.ai with the app's README and a download. Update replaces what's behind the link."
      : "Publish to your Fused account as a public page anyone can open.";

  // ---- the file card ----------------------------------------------------------
  // The file is the primary only while the card above it has no primary of
  // its own to offer.
  // The ladder card's primary is "Put it on GitHub" on rung 1 and Copy link
  // from rung 2 (when a link is offered).
  const fileSecondary = githubFirst
    ? ladder
      ? ladder.rung === 1 || (!!ladder.flags?.sources && !!ladder.github.link)
      : linkEligible && !!github && (github.ok || githubLinkSentence(github.code).next !== null)
    : linkEligible && signedIn;
  const fileAction = (
    <Button size="sm" variant={fileSecondary ? "outline" : "default"} disabled={working} onClick={() => void doExport()}>
      {busy === "export" && <Loader2 data-icon="inline-start" className="animate-spin" />}
      {busy === "export" ? "Exporting…" : isLiveFile ? "Download" : `Download ${versionLabel}`}
    </Button>
  );
  const fileBody = saved ? (
    <div className="flex min-w-0 items-start gap-2 rounded-lg bg-muted/50 px-3 py-2">
      <Check className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" aria-hidden />
      {/* Path on its own line under the name: a Downloads path is longer than
          the card, so on one line it pushed Reveal/Open past the edge. */}
      <div className="flex min-w-0 flex-1 flex-col gap-0.5 text-xs text-muted-foreground">
        <span className="truncate">
          Saved <span className="text-foreground">{saved.name}.fused</span>
        </span>
        <span className="break-all text-[11px] leading-4 opacity-80">{saved.path}</span>
      </div>
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

  // ---- the GitHub link card ----------------------------------------------------
  // Third and secondary by default; with Community sources on it is the first
  // card and the primary (see `githubFirst`), never dimmed for an unshareable
  // state — its one sentence is then the most useful thing on the sheet — and
  // the sentence is mapped from the server's `code`, never its git-worded
  // `reason`.
  const githubMapped =
    githubFirst && linkEligible && github && !github.ok ? githubLinkSentence(github.code) : null;
  const githubNotice = githubMapped
    ? githubMapped.text
    : linkEligible && github && !github.ok && !githubFirst
      ? github.reason
      : null;
  // The author's one next step, in the action slot where Copy link would sit
  // (disabled, it would only repeat that there is nothing to copy). Leaves
  // the sheet for the app folder's git view; not while an export is landing
  // its result in the sheet.
  const githubNext = githubMapped?.next ?? null;
  const goGithubNext = () => {
    if (!githubNext || working) return;
    onClose();
    navigateUrl(urlForFsPath(app.path, "?_mode=git"), { isDir: true });
  };
  // A failed read says so in plain words when the loop owns this card; the
  // flag-off sheet keeps its message as it was.
  const githubErrText = githubErr && githubFirst
    ? "Couldn't check this app's GitHub link just now. Close and try again in a moment."
    : githubErr;
  // A GitHub-link copy of someone else's app shares the AUTHOR's version
  // (code "contributor"); when the reader has edits of their own, one line
  // says the link leaves them out and where they can go instead. "Publish a
  // remix" only while that phase is on.
  const author = github?.repo?.split("/")[0] ?? "the author";
  const githubLeavesOut =
    githubFirst && linkEligible && github?.ok && github.code === "contributor" && (github.changes ?? 0) > 0
      ? contribFlags?.remix
        ? `This shares ${author}'s version. Your changes aren't included — propose them or publish a remix.`
        : `This shares ${author}'s version. Your changes aren't included — propose them to ${author}.`
      : null;
  const githubCard = ladder ? (
    <GithubLadderCard
      dir={app.path}
      ladder={ladder}
      reload={reloadLadder}
      primary={githubFirst}
      locked={working}
      onBusy={setLadderPublishing}
      onClose={onClose}
    />
  ) : (
    <OptionCard
      icon={<GitBranch />}
      title="GitHub link"
      description={
        !linkEligible
          ? "Links always install the live app. Pick Live in the version picker to share one."
          : githubFirst
            ? github?.repo
              ? `Anyone with the link installs the app from github.com/${github.repo}, and it stays up to date.`
              : "Anyone with the link installs the app from GitHub, and it stays up to date."
            : github?.repo
              ? `Installs the app straight from github.com/${github.repo}.`
              : "Installs the app straight from its GitHub repository."
      }
      muted={!linkEligible || (!githubFirst && !!github && !github.ok)}
      action={
        linkEligible && !github && !githubErr ? (
          <Skeleton className="h-8 w-24" />
        ) : githubNext ? (
          <Button size="sm" disabled={working} onClick={goGithubNext}>
            Show changes
          </Button>
        ) : (
          <Button
            size="sm"
            variant={githubFirst && github?.ok ? "default" : "outline"}
            disabled={!github?.ok}
            onClick={() => void copyGithub()}
          >
            {githubCopied ? <Check data-icon="inline-start" /> : <Copy data-icon="inline-start" />}
            {githubCopied ? "Copied" : "Copy link"}
          </Button>
        )
      }
    >
      {linkEligible && github?.ok && github.url && (
        <div className="flex items-center gap-1.5">
          <Input
            type="text"
            readOnly
            value={github.url}
            onFocus={(e) => e.currentTarget.select()}
            aria-label="GitHub link"
            className="h-8 flex-1 truncate bg-muted/40 font-mono text-[12.5px] text-foreground"
          />
          <Button
            size="icon-sm"
            variant="outline"
            title="Open the install page"
            aria-label="Open the install page"
            render={<a href={github.url} target="_blank" rel="noopener noreferrer" />}
          >
            <ExternalLink />
          </Button>
        </div>
      )}
      {githubNotice && (
        <p className="m-0 text-[13px] leading-5 text-muted-foreground">{githubNotice}</p>
      )}
      {githubLeavesOut && (
        <p className="m-0 text-[13px] leading-5 text-muted-foreground">{githubLeavesOut}</p>
      )}
      {githubErrText && (
        <p className="m-0 text-[13px] leading-5 text-destructive" role="alert">
          {githubErrText}
        </p>
      )}
    </OptionCard>
  );

  return (
    <>
      <Dialog
        open
        onOpenChange={(open) => {
          if (!open && !working && !ladderPublishing) onClose();
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
                disabled={working || ladderPublishing}
              >
                <XIcon />
                <span className="sr-only">Close</span>
              </DialogClose>
            </div>
            <DialogDescription className="sr-only">
              {githubFirst
                ? "Share this app as a GitHub link, a public link, or a .fused file."
                : ladder !== null
                  ? "Share this app as a public link, a .fused file, or a GitHub link."
                  : "Share this app as a public link or as a .fused file."}
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-3 px-6 pb-5">
            {/* Until the flags answer, which link card leads is unknown: one
                card-sized placeholder holds the link slot, so nothing
                reorders when they land. (A failed read lands as all off —
                contrib-flags.ts — so this never outlives a broken prefs GET.) */}
            {!flagsKnown && (
              <Skeleton className="h-[74px] w-full rounded-xl" aria-label="Loading share options" />
            )}
            {githubFirst && githubCard}
            {flagsKnown && fusedLinkShown && (
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
            )}
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
            {/* Third and secondary without `sources`: the ladder card for an
                own app whatever the switches. A copy's link card needs
                `sources`, so it is never drawn here. */}
            {flagsKnown && !githubFirst && ladder !== null && githubCard}
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
