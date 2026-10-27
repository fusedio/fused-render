// The Source panel (docs/CONTRIB_LOOP.md §16.10): the top of the Overview,
// saying where this copy stands against where it came from and offering every
// way to move it — automatic moves included (P6, the owner: "things should
// not be magic … manual trigger and manual fix ways should be there").
//
// THE SERVER DECIDES. `/api/contrib/app` sends `panel` (one sentence, the
// files it is about, when GitHub was last checked; null = say nothing),
// `actions` (the buttons, in order, exactly one primary) and `activity` (what
// happened, newest first). This file renders those and nothing else: it never
// derives a button from a state, so a button exists exactly when
// contrib/actions.py says so (flags included).
//
//   sentence   the server's, whole.
//   files      the paths the sentence is about, a few at a time.
//   actions    the primary as a button, the rest as quiet text actions;
//              `danger` reads red; `confirm` asks first on an in-page sheet
//              (never window.confirm), as does a "confirm" refusal. A button
//              with `dialog` opens its sheet instead of posting — Propose,
//              Publish as remix, Sign in. Update proposal asks first which
//              new files to send when the copy has any (`include`, as
//              Propose does), and Discard's confirm carries "Also withdraw my
//              proposal" while one is open. Everything else is ONE route,
//              POST /api/contrib/action; a long one answers a job, shown as
//              progress until it ends.
//   merge      `panel.merge`, Claude's merge for this copy: the clock while it
//              runs, Keep / Discard once ready (MergeReview). The sentence
//              already says what Claude did.
//   checked    "Checked 3m ago · Check now" (Check now only when sent).
//   activity   a disclosure of what moved and when, each line with Undo when
//              something moved away (POST /api/contrib/restore).
//
// A merged or declined answer stands until the user dismisses it (✕ → ack
// "settled"), the one control here the server does not list: it is how the
// server's own state is told it was read.
//
// STAGE B (CONTRIB_LOOP.md §18.7). A Marketplace copy is one more kind the
// server words for itself; this file adds three things around it:
//   block      `panel.block` / `panel.block_warn` → BlockedStrip above the
//              strip (reason, what to do, where the copy went, Open anyway
//              for a soft block, Remove this app for a hard one).
//   listing    the author's Marketplace buttons (`publish-market`, …) arrive
//              in the same `actions` list for an own app; they are drawn by
//              the ListingStrip under this panel and posted to their own
//              route, so they are taken out of this row.
//   switch     Activity's `switch` lines carry Undo like any other line
//              (the server's `undo`); a `rollback` line never does.
//
// The sheets are mounted outside any one state's branch, so a background
// re-read that moves the state on never unmounts one mid-flight.
//
// TWO RENDERERS, ONE LOGIC (§19.4). `useSourcePanel` holds everything above
// — the state, `act`/`post`/`press`, the sheets — and hands back the pieces;
// `SourcePanel` draws them as one strip, and the Community tab
// (CommunityTab.tsx) places the same pieces in its cards. Either renders
// `kit.sheets` exactly once, outside every card, so the rule above holds.
import { useEffect, useState, type ReactNode } from "react";
import { Check, ChevronRight, Clock, Loader2, RotateCcw, TriangleAlert } from "lucide-react";
import {
  LISTING_ACTION_IDS,
  ackContrib,
  okMessage,
  plainMessage,
  restoreActivity,
  runAction,
  runJob,
  useUnmountSignal,
  type ActivityLine,
  type AppContrib,
  type AppContribState,
  type ContribAction,
  type ContribJob,
  type CopyBlock,
  type MergeJobView,
  type SourcePanel as SourcePanelView,
} from "@platform/lib/contrib";
import { timeAgo } from "@platform/lib/format";
import { notify } from "@platform/lib/notifications";
import { cn } from "@platform/lib/utils";
import { openUninstallApp } from "@platform/lib/uninstall-app";
import { Button } from "@platform/shadcn/ui/button";
import { Checkbox } from "@platform/shadcn/ui/checkbox";
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
import { BlockedStrip, CONFIRM_BLOCK_ID, REMOVE_APP_ID } from "./BlockedStrip";
import { ContribDialog } from "./ContribDialog";
import { IncludeFiles, pickedOf } from "./IncludeFiles";
import { JobProgress } from "./JobProgress";
import { MergeReview, mergeShown } from "./MergeReview";
import { ProposeDialog } from "./ProposeDialog";
import { RemixDialog } from "./RemixDialog";
import { SignInSheet } from "./SignInSheet";
import { QuietAction, StatusStrip, type StripTone } from "./StatusStrip";
import { authorOf, checksWhen, plural, STEPS_FOR } from "./words";

/** Drawn in the "checked" line, not the button row. */
export const CHECK_ID = "check";
/** Files listed before "N more". */
const FILES_SHOWN = 6;
/** States whose sentence names a problem only the user can fix. */
const ATTENTION_STATES = new Set([
  "needs-merge",
  "blocked",
  "other-branch",
  "error",
  "github-changed",
  "edits",
  "publish-failed",
  "publish-auth",
  "blocked-release",
  "soft-blocked",
]);
/** States that say all is well. */
const QUIET_STATES = new Set(["following", "merged", "published", "delisted"]);
/** Check now's own line (the one action whose progress is not a step list). */
const DOING = { check: "Checking…" };
/** Opens the new-files sheet first when the copy has new files. */
const UPDATE_ID = "update-proposal";
const DISCARD_ID = "discard";
/** States that stand until the user dismisses them (ack "settled"). */
const SETTLED_STATES = new Set(["merged", "declined"]);

type Sheet =
  | null
  | { k: "propose" }
  | { k: "remix" }
  | { k: "confirm"; action: ContribAction; sentence: string }
  | { k: "update"; action: ContribAction }
  | { k: "undo"; line: ActivityLine };

/** Any action's answer, as `act` reads it. */
type Outcome = { ok: boolean; message?: string; reason?: string };

/** What `useSourcePanel` hands a renderer: the server's panel, its buttons
 *  (listing / block ids already taken out), and every way to press them. */
export interface SourcePanelKit {
  c: AppContrib;
  panel: SourcePanelView;
  /** The panel's buttons in the server's order, minus Check now when it is
   *  not the primary (that one is `footCheck`). */
  rest: ContribAction[];
  /** The primary, when the server sent one (first of `rest`). */
  first: ContribAction | null;
  /** `rest` without `first`. */
  others: ContribAction[];
  /** Check now as the quiet "checked" line's action, when not the primary. */
  footCheck: ContribAction | null;
  /** Every action the server sent (the BlockedStrip reads its own ids). */
  allActions: ContribAction[];
  activity: ActivityLine[];
  who: string;
  block: CopyBlock | null;
  blockWarn: CopyBlock | null;
  busy: string | null;
  error: string | null;
  /** A confirm sheet is open (it shows the error inside, not the strip). */
  sheetOpen: boolean;
  job: { id: string; step: string | null; since: number } | null;
  merge: MergeJobView | null;
  mergeOpen: boolean;
  working: boolean;
  tone: StripTone;
  icon: ReactNode;
  /** "3m ago", or null when never checked. */
  checked: string | null;
  /** The ✕ for a merged / declined answer; undefined for any other state. */
  dismiss: (() => void) | undefined;
  press: (a: ContribAction) => void;
  /** The primary as a Button, any other as a quiet text action. */
  button: (a: ContribAction) => ReactNode;
  /** Ask before undoing one Activity line. */
  askUndo: (line: ActivityLine) => void;
  /** Check now's quiet label ("Checking…" while it runs). */
  checkLabel: (a: ContribAction) => string;
  /** Every sheet the panel can open. Render exactly once, outside any card. */
  sheets: ReactNode;
}

/** The Source panel's logic, for any renderer. Null when the server has
 *  nothing to say (no panel, or a settled answer the user dismissed). */
export function useSourcePanel({
  dir,
  slug,
  contrib,
  blockShown,
  onChanged,
}: {
  dir: string;
  slug: string;
  contrib: AppContribState;
  /** The page already shows the block (BlockedNotice): no strip of our own. */
  blockShown?: boolean;
  /** Called after every action's re-read (the Community tab re-reads its
   *  ladder too). */
  onChanged?: () => void;
}): SourcePanelKit | null {
  const c = contrib.data;
  const [sheet, setSheet] = useState<Sheet>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The job a long action is running, for its progress line.
  const [job, setJob] = useState<{ id: string; step: string | null; since: number } | null>(null);
  const [withdrawToo, setWithdrawToo] = useState(false);
  const [include, setInclude] = useState<ReadonlySet<string>>(() => new Set());
  // Dismissed here ahead of the re-read, keyed by which settlement it was,
  // so a LATER proposal's answer is not hidden by this one's ✕.
  const [acked, setAcked] = useState<string | null>(null);
  // A refused action waiting on a sign-in: what to say, and what to run again.
  const [signIn, setSignIn] = useState<{ doing: string; again: () => void } | null>(null);
  const signal = useUnmountSignal();

  // A new folder is a new panel: nothing carries over.
  useEffect(() => {
    setSheet(null);
    setBusy(null);
    setError(null);
    setJob(null);
    setWithdrawToo(false);
    setInclude(new Set());
    setAcked(null);
    setSignIn(null);
  }, [dir]);

  // An error belongs to the sentence it was said under: when the server's
  // sentence moves on, the old error goes.
  const sentence = c?.panel?.sentence ?? null;
  useEffect(() => setError(null), [sentence]);

  const panel = c?.panel;
  if (!c || !panel || !panel.sentence) return null;
  const proposal = c.proposal;
  const block = blockShown ? null : (panel.block ?? null);
  const blockWarn = blockShown ? null : (panel.block_warn ?? null);
  // The listing's buttons belong to the ListingStrip, Open anyway and Remove
  // this app to the BlockedStrip (or the page's BlockedNotice): never twice.
  const allActions = c.actions ?? [];
  const actions = allActions.filter(
    (a) =>
      !LISTING_ACTION_IDS.has(a.id) &&
      a.id !== CONFIRM_BLOCK_ID &&
      !(a.id === REMOVE_APP_ID && (block?.level === "hard" || blockShown)),
  );
  const activity = c.activity ?? [];
  const who = panel.who || authorOf(c.copy);
  const signedIn = c.github.signed_in;
  const settledKey = `${panel.state}:${proposal?.number ?? ""}`;
  const untracked = panel.untracked ?? [];
  const openProposal = !!proposal && (proposal.state === "waiting" || proposal.state === "commented");
  if (SETTLED_STATES.has(panel.state) && acked === settledKey) return null;

  /** Run one thing: one at a time ("busy" when another is running — a double
   *  click is a no-op), the server's sentence on a refusal, a re-read either
   *  way. A "signed-out" refusal closes whatever sheet asked (a confirm),
   *  opens the sign-in sheet, and runs the same thing ONCE more after it by
   *  itself — a second "signed-out" is said as an error, never a loop. */
  const act = async (key: string, fn: () => Promise<Outcome>, retried = false): Promise<"busy" | boolean> => {
    if (busy && !retried) return "busy";
    setBusy(key);
    setError(null);
    const r = await fn();
    if (r.reason === "aborted") return false; // unmounted: nobody to tell
    if (!r.ok && r.reason !== "confirm") {
      if (r.reason === "signed-out" && !retried) {
        setSheet(null);
        setSignIn({ doing: "keep going", again: () => void act(key, fn, true) });
      } else {
        setError(plainMessage(r));
      }
    }
    setJob(null);
    await contrib.refresh();
    onChanged?.();
    setBusy(null);
    return r.ok;
  };

  /** The server's own success sentence as a toast, when it sent one — the
   *  panel's re-read already shows the outcome, so no sentence of our own. */
  const say = (answer: unknown) => {
    const m = okMessage(answer, "");
    if (m) notify({ title: m, tone: "info" });
  };

  /** POST one action; a long one's job is watched to its end. A "confirm"
   *  refusal (the server wants a yes first) opens the confirm sheet. */
  const post = (a: ContribAction, confirm = false, args?: Record<string, unknown>) =>
    act(a.id, async () => {
      const started = await runAction(dir, a.id, { confirm, args });
      if (!started.ok && started.reason === "confirm" && !confirm) {
        setSheet({ k: "confirm", action: a, sentence: plainMessage(started) });
        return started;
      }
      // Merge with Claude answers its merge job (`merge: true`): its
      // progress and review ride on `panel.merge`, read by the re-read.
      if (!started.ok || !started.job || started.merge) {
        if (started.ok) say(started);
        return started;
      }
      const since = Date.now();
      setJob({ id: a.id, step: null, since });
      const r = await runJob(Promise.resolve(started), {
        signal: signal(),
        onTick: (j: ContribJob) => setJob({ id: a.id, step: j.step ?? null, since }),
      });
      if (r.ok) say(r);
      return r;
    });

  // Check now lives in the "checked" line — unless it is the panel's one way
  // forward (the primary), when it is a real button in the strip.
  const check = actions.find((a) => a.id === CHECK_ID) ?? null;
  const footCheck = check && !check.primary ? check : null;
  const rest = actions.filter((a) => a !== footCheck);

  const press = (a: ContribAction) => {
    if (busy) return;
    // The shell's own uninstall dialog (data kept or not, the Trash).
    if (a.id === REMOVE_APP_ID) {
      return openUninstallApp({ path: dir, name: dir.replace(/\/+$/, "").split("/").pop() || slug });
    }
    if (a.dialog === "propose") return setSheet({ k: "propose" });
    if (a.dialog === "remix") return setSheet({ k: "remix" });
    if (a.dialog === "sign-in") {
      // Signing in may change what this app is (its owner is checked once
      // GitHub knows who the user is): run Check now after, when offered.
      return setSignIn({
        doing: "keep going",
        again: () => void (check ? post(check) : contrib.refresh()),
      });
    }
    if (a.confirm) return setSheet({ k: "confirm", action: a, sentence: a.confirm });
    if (a.id === UPDATE_ID && untracked.length > 0) {
      setInclude(new Set());
      return setSheet({ k: "update", action: a });
    }
    void post(a);
  };

  const dismiss = () => {
    setAcked(settledKey);
    void ackContrib(dir, "settled").then(() => contrib.refresh());
  };

  const spin = (key: string) => busy === key && <Loader2 data-icon="inline-start" className="animate-spin" />;

  // ---- the buttons, in the server's order (its one primary first) ------------
  const label = (a: ContribAction) => (busy === a.id && !a.dialog ? `${a.label}…` : a.label);
  const button = (a: ContribAction) =>
    a.primary ? (
      <Button key={a.id} variant={a.danger ? "destructive" : "default"} onClick={() => press(a)} disabled={!!busy}>
        {spin(a.id)}
        {label(a)}
      </Button>
    ) : (
      <QuietAction key={a.id} onClick={() => press(a)} disabled={!!busy} tone={a.danger ? "danger" : undefined}>
        {label(a)}
      </QuietAction>
    );
  const first = rest[0]?.primary ? rest[0] : null;
  const others = first ? rest.slice(1) : rest;

  const merge = mergeShown(panel.merge, true) ? (panel.merge ?? null) : null;
  // Nothing may move the copy back while Claude's merge is open.
  const mergeOpen = merge?.status === "running" || merge?.status === "landing" || merge?.status === "ready";
  const working = !!job || merge?.status === "running" || merge?.status === "landing" || panel.state === "pending";
  const tone: StripTone = working
    ? "working"
    : merge?.status === "ready" || merge?.status === "failed" || ATTENTION_STATES.has(panel.state)
      ? "attention"
      : QUIET_STATES.has(panel.state)
        ? "success"
        : "neutral";
  const icon = working ? (
    <Loader2 className="animate-spin" />
  ) : tone === "attention" ? (
    <TriangleAlert />
  ) : tone === "success" ? (
    <Check />
  ) : (
    <Clock />
  );

  const sheets = (
    <>
      {sheet?.k === "propose" && (
        <ProposeDialog
          dir={dir}
          author={who}
          signedIn={signedIn}
          again={proposal?.state === "declined"}
          untracked={panel.untracked ?? []}
          onClose={() => setSheet(null)}
          onSent={() => {
            void contrib.refresh();
            onChanged?.();
          }}
        />
      )}
      {sheet?.k === "remix" && (
        <RemixDialog
          dir={dir}
          slug={slug}
          author={who}
          original={panel.repo}
          signedIn={signedIn}
          onClose={() => setSheet(null)}
          onPublished={() => {
            void contrib.refresh();
            onChanged?.();
          }}
        />
      )}
      {signIn && (
        <SignInSheet
          doing={signIn.doing}
          onClose={() => setSignIn(null)}
          onSignedIn={() => {
            const again = signIn.again;
            setSignIn(null);
            again();
          }}
        />
      )}

      <AlertDialog
        open={sheet?.k === "confirm"}
        onOpenChange={(o) => {
          if (!o && !busy) {
            setSheet(null);
            setWithdrawToo(false);
            setError(null);
          }
        }}
      >
        {sheet?.k === "confirm" && (
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>{sheet.action.label}?</AlertDialogTitle>
              <AlertDialogDescription>{sheet.sentence}</AlertDialogDescription>
            </AlertDialogHeader>
            {sheet.action.id === DISCARD_ID && openProposal && (
              <label className="contrib-check">
                <Checkbox checked={withdrawToo} onCheckedChange={(v) => setWithdrawToo(v === true)} />
                <span>Also withdraw my proposal</span>
              </label>
            )}
            {error && (
              <p className="contrib-error" role="alert">
                {error}
              </p>
            )}
            <AlertDialogFooter>
              <AlertDialogCancel disabled={!!busy}>Cancel</AlertDialogCancel>
              <AlertDialogAction
                variant={sheet.action.danger ? "destructive" : "default"}
                disabled={!!busy}
                onClick={async () => {
                  const a = sheet.action;
                  const args = a.id === DISCARD_ID && openProposal ? { withdraw: withdrawToo } : undefined;
                  const r = await post(a, true, args);
                  // Another action running: nothing happened, keep the sheet.
                  // A refusal keeps it too, its sentence shown inside — the
                  // next move is to wait and press again, not to start over.
                  if (r === "busy" || r === false) return;
                  setSheet(null);
                  setWithdrawToo(false);
                }}
              >
                {spin(sheet.action.id)}
                {sheet.action.label}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        )}
      </AlertDialog>

      {sheet?.k === "update" && (
        <ContribDialog
          open
          onClose={() => setSheet(null)}
          title={sheet.action.label}
          description={`${who} gets what your copy runs now. Your new files go too, but only the ones you tick.`}
          footer={
            <>
              <Button variant="outline" onClick={() => setSheet(null)}>
                Cancel
              </Button>
              <Button
                onClick={() => {
                  const a = sheet.action;
                  setSheet(null);
                  // Progress, a refusal and a sign-in all show on the panel.
                  void post(a, false, { include: pickedOf(untracked, include) });
                }}
              >
                {sheet.action.label}
              </Button>
            </>
          }
        >
          <IncludeFiles files={untracked} picked={include} onChange={setInclude} />
        </ContribDialog>
      )}

      <AlertDialog
        open={sheet?.k === "undo"}
        onOpenChange={(o) => {
          if (!o && !busy) setSheet(null);
        }}
      >
        {sheet?.k === "undo" && (
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>{sheet.line.kind === "switch" ? "Switch back?" : "Undo this?"}</AlertDialogTitle>
              <AlertDialogDescription>
                {sheet.line.kind === "switch" ? (
                  <>
                    This undoes &ldquo;{sheet.line.text}&rdquo; and puts the app back the way it was before the
                    switch.
                  </>
                ) : (
                  <>
                    The app goes back to how it was before this: &ldquo;{sheet.line.text}&rdquo;. What it has now is
                    kept in the activity list too, so nothing is lost either way.
                  </>
                )}
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel disabled={!!busy}>Cancel</AlertDialogCancel>
              <AlertDialogAction
                disabled={!!busy}
                onClick={async () => {
                  const line = sheet.line;
                  const r = await act(`undo:${line.id}`, async () => {
                    const a = await restoreActivity(dir, line.id);
                    if (a.ok) say(a);
                    return a;
                  });
                  if (r !== "busy") setSheet(null);
                }}
              >
                {spin(`undo:${sheet.line.id}`)}
                Undo
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        )}
      </AlertDialog>
    </>
  );

  return {
    c,
    panel,
    rest,
    first,
    others,
    footCheck,
    allActions,
    activity,
    who,
    block,
    blockWarn,
    busy,
    error,
    sheetOpen: sheet?.k === "confirm",
    job,
    merge,
    mergeOpen,
    working,
    tone,
    icon,
    checked: timeAgo(panel.last_checked),
    dismiss: SETTLED_STATES.has(panel.state) ? dismiss : undefined,
    press,
    button,
    askUndo: (line) => setSheet({ k: "undo", line }),
    checkLabel: (a) => (busy === CHECK_ID ? DOING.check : a.label),
    sheets,
  };
}

/** The progress line of a long action the kit is running (Check now says
 *  "Checking…" in its own line: no second spinner). */
export function KitProgress({ kit }: { kit: SourcePanelKit }) {
  const job = kit.job;
  if (!job || job.id === CHECK_ID) return null;
  return (
    <JobProgress
      steps={STEPS_FOR[job.id] ?? [{ key: "run", label: "Working…" }]}
      current={job.step ?? STEPS_FOR[job.id]?.[0]?.key ?? "run"}
      since={job.since}
    />
  );
}

/** What moved and when, each line with Undo when something moved away. */
export function ActivityList({ kit }: { kit: SourcePanelKit }) {
  if (kit.activity.length === 0) return null;
  return (
    <ol className="contrib-activity" aria-label="Activity">
      {kit.activity.map((line) => (
        <li key={line.id} className="contrib-activity-line">
          <span className="contrib-activity-at">{timeAgo(line.at) ?? ""}</span>
          <span className="contrib-activity-text">{line.text}</span>
          {line.undo && (
            <QuietAction onClick={() => kit.askUndo(line)} disabled={!!kit.busy || kit.mergeOpen}>
              <RotateCcw aria-hidden="true" />
              {kit.busy === `undo:${line.id}` ? "Putting it back…" : "Undo"}
            </QuietAction>
          )}
        </li>
      ))}
    </ol>
  );
}

export function SourcePanel({
  dir,
  slug,
  contrib,
  blockShown,
}: {
  dir: string;
  slug: string;
  contrib: AppContribState;
  /** The page already shows the block (BlockedNotice): no strip of our own. */
  blockShown?: boolean;
}) {
  const kit = useSourcePanel({ dir, slug, contrib, blockShown });
  const [activityOpen, setActivityOpen] = useState(false);
  useEffect(() => setActivityOpen(false), [dir]);
  if (!kit) return null;
  const { panel, first, others, footCheck, block, blockWarn, busy } = kit;

  return (
    <>
      <div className="contrib-stack">
        {(block || blockWarn) && (
          <BlockedStrip
            dir={dir}
            block={block}
            warn={blockWarn}
            who={kit.who}
            actions={kit.allActions}
            disabled={!!busy}
            onChanged={() => void contrib.refresh()}
          />
        )}
        <StatusStrip
          tone={kit.tone}
          icon={kit.icon}
          busy={kit.working}
          sentence={panel.sentence}
          primary={first ? kit.button(first) : null}
          secondary={others.length > 0 ? others.map(kit.button) : null}
          onDismiss={kit.dismiss}
        >
          <FileList files={panel.files} />
          <KitProgress kit={kit} />
          {kit.merge && <MergeReview dir={dir} job={kit.merge} brief disabled={!!busy} refresh={contrib.refresh} />}
          {kit.error && !kit.sheetOpen && (
            <p className="contrib-error" role="alert">
              {kit.error}
            </p>
          )}
          <div className="contrib-panel-foot">
            <span className="contrib-panel-checked">
              {kit.checked ? `Checked ${kit.checked}` : "Not checked yet"}
              {footCheck && (
                <>
                  {" · "}
                  <QuietAction onClick={() => kit.press(footCheck)} disabled={!!busy}>
                    {kit.checkLabel(footCheck)}
                  </QuietAction>
                </>
              )}
            </span>
            {(panel.state === "proposed" || panel.state === "merged") && (
              <span className="contrib-panel-when">{checksWhen(panel.state, kit.who)}</span>
            )}
            {kit.activity.length > 0 && (
              <button
                type="button"
                className={cn("contrib-panel-disclose", activityOpen && "is-open")}
                aria-expanded={activityOpen}
                onClick={() => setActivityOpen((o) => !o)}
              >
                <ChevronRight aria-hidden="true" />
                Activity
              </button>
            )}
          </div>
          {activityOpen && <ActivityList kit={kit} />}
        </StatusStrip>
      </div>
      {kit.sheets}
    </>
  );
}

/** The paths a sentence is about, a few at a time with "N more". */
export function FileList({ files }: { files: string[] }) {
  const [all, setAll] = useState(false);
  if (files.length === 0) return null;
  const shown = all ? files : files.slice(0, FILES_SHOWN);
  const more = files.length - shown.length;
  return (
    <ul className="contrib-files" aria-label="Files">
      {shown.map((f) => (
        <li key={f}>{f}</li>
      ))}
      {more > 0 && (
        <li className="is-more">
          <QuietAction onClick={() => setAll(true)}>{plural(more, "more file", "more files")}</QuietAction>
        </li>
      )}
    </ul>
  );
}
