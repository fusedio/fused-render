// The Source panel (docs/CONTRIB_LOOP.md §16.10): the top of the Overview,
// saying where this copy stands against where it came from and offering every
// way to move it — automatic moves included (P6, the owner: "things should
// not be magic … manual trigger and manual fix ways should be there").
//
// THE SERVER DECIDES. `/api/contrib/app` sends `panel` (one sentence, the
// files it is about, when GitHub was last checked), `actions` (the buttons,
// in order) and `activity` (what happened, newest first). This file renders
// those and nothing else: it never derives a button from a state, so a
// button exists exactly when contrib/actions.py says so (flags included).
//
//   sentence   the server's, as is; its `detail` under it.
//   files      the paths the sentence is about, a few at a time.
//   actions    primary ones as buttons, the rest as quiet text actions;
//              `danger` reads red; `confirm` asks first on an in-page sheet.
//              A few ids open a sheet instead of posting at once — Propose,
//              Publish as remix, Sign in — and Discard's confirm carries the
//              "Also withdraw my proposal" box. Everything else is ONE route,
//              POST /api/contrib/action; a long one answers a job, shown as
//              progress until it ends.
//   checked    "Checked 3m ago · Check now" (Check now only when sent).
//   activity   a disclosure of what moved and when, each line with Undo /
//              Restore when something moved away (POST /api/contrib/restore).
//
// Claude's merge review (running → ready → Keep / Discard) still rides on
// `copy.merge_job`, drawn inside the panel: the sentence says what Claude is
// doing or did, the panel adds the elapsed clock or the two choices a ready
// merge means (the merge job's own accept/discard routes). A failed merge is
// the sentence's to say, with its fix buttons.
//
// The sheets are mounted outside any one state's branch, so a background
// re-read that moves the state on never unmounts one mid-flight.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Check, ChevronRight, Clock, Loader2, RotateCcw, TriangleAlert } from "lucide-react";
import {
  acceptMerge,
  discardMerge,
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
  type MergeJobView,
} from "@platform/lib/contrib";
import { timeAgo } from "@platform/lib/format";
import { notify } from "@platform/lib/notifications";
import { cn } from "@platform/lib/utils";
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
import { ClaudeMark } from "@platform/ui/ClaudeMark";
import { JobProgress } from "./JobProgress";
import { ProposeDialog } from "./ProposeDialog";
import { RemixDialog } from "./RemixDialog";
import { SignInSheet } from "./SignInSheet";
import { QuietAction, StatusStrip, type StripTone } from "./StatusStrip";
import { authorOf, elapsed, plural, possessive } from "./words";

/** Ids that open a sheet here instead of posting at once, for a server that
 *  does not mark them with `dialog` itself. */
const SHEET_IDS = new Set(["propose", "remix", "sign-in"]);
/** Drawn in the "checked" line, not the button row. */
const CHECK_ID = "check";
/** Files listed before "N more". */
const FILES_SHOWN = 6;
/** States whose sentence names a problem only the user can fix. */
const ATTENTION_STATES = new Set([
  "needs-merge",
  "blocked",
  "github-changed",
  "edits",
  "other-branch",
  "error",
  "sync-error",
]);
/** States that say all is well. */
const QUIET_STATES = new Set(["following", "ok", "merged"]);

type Sheet =
  | null
  | { k: "propose" }
  | { k: "remix" }
  | { k: "confirm"; action: ContribAction; sentence: string }
  | { k: "undo"; line: ActivityLine };

/** Any action's answer, as `act` reads it. */
type Outcome = { ok: boolean; message?: string; reason?: string };

/** A merge review in flight or waiting for the user's choice. A failed one
 *  is the server's to say: its sentence and fix buttons cover it. */
function liveMerge(c: AppContrib): MergeJobView | null {
  const job = c.copy?.merge_job;
  if (!job) return null;
  return job.status === "running" || job.status === "landing" || job.status === "ready" ? job : null;
}

/** The sheet (or flow) a button opens instead of posting, if any. */
function dialogOf(a: ContribAction): string | null {
  return a.dialog || (SHEET_IDS.has(a.id) ? a.id : null);
}

export function SourcePanel({ dir, slug, contrib }: { dir: string; slug: string; contrib: AppContribState }) {
  const c = contrib.data;
  const [sheet, setSheet] = useState<Sheet>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The job a long action is running, for its progress line.
  const [job, setJob] = useState<{ label: string; step: string | null; steps?: StepList } | null>(null);
  const [withdrawToo, setWithdrawToo] = useState(false);
  const [activityOpen, setActivityOpen] = useState(false);
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
    setActivityOpen(false);
    setSignIn(null);
  }, [dir]);

  // An error belongs to the sentence it was said under: when the server's
  // sentence moves on, the old error goes.
  const sentence = c?.panel?.sentence ?? null;
  useEffect(() => setError(null), [sentence]);

  const merge = c ? liveMerge(c) : null;
  const clockFrom = useMergeClock(merge);

  if (!c?.panel) return null;
  const { panel, proposal } = c;
  const actions = c.actions ?? [];
  const activity = c.activity ?? [];
  // Nothing to say: the phase is off (an empty sentence), or an app of the
  // user's own with no GitHub repo and no history to show.
  if (!panel.sentence) return null;
  if (panel.state === "local" && actions.length === 0 && activity.length === 0) return null;
  const who = panel.who || authorOf(c.copy);
  const signedIn = c.github.signed_in;
  const openProposal = !!proposal && (proposal.state === "waiting" || proposal.state === "commented");

  /** Run one thing: one at a time ("busy" when another is running — a double
   *  click is a no-op), the server's sentence on a refusal, a re-read either
   *  way. A "signed-out" refusal opens the sign-in sheet and runs it once
   *  more after, instead of an error. */
  const act = async (key: string, fn: () => Promise<Outcome>, signInTo?: string): Promise<"busy" | boolean> => {
    if (busy) return "busy";
    setBusy(key);
    setError(null);
    const r = await fn();
    if (r.reason === "aborted") return false; // unmounted: nobody to tell
    if (!r.ok && r.reason !== "confirm") {
      if (r.reason === "signed-out" && signInTo) {
        setSignIn({ doing: signInTo, again: () => void act(key, fn) });
      } else {
        setError(plainMessage(r));
      }
    }
    setJob(null);
    await contrib.refresh();
    setBusy(null);
    return r.ok;
  };

  /** The server's own success sentence as a toast, when it sent one — the
   *  panel's re-read already shows the outcome, so no sentence of our own. */
  const say = (answer: unknown) => {
    const m = okMessage(answer, "");
    if (m) notify({ title: m, tone: "info" });
  };

  /** POST one action; a long one's job is watched to its end. A
   *  `reason:"confirm"` answer (the server wants a yes first, although no
   *  confirm sentence came with the button) opens the confirm sheet. */
  const post = (a: ContribAction, opts: { confirm?: boolean; args?: Record<string, unknown> } = {}) =>
    act(
      a.id,
      async () => {
        const started = await runAction(dir, a.id, opts);
        if (!started.ok && started.reason === "confirm" && !opts.confirm) {
          setSheet({ k: "confirm", action: a, sentence: plainMessage(started) });
          return started;
        }
        // Merge with Claude answers its merge job (`merge: true`): its
        // progress and review ride on `copy.merge_job`, read by the re-read.
        if (!started.ok || !started.job || started.merge) {
          if (started.ok) say(started);
          return started;
        }
        setJob({ label: a.label, step: null });
        const r = await runJob<Record<string, unknown>>(Promise.resolve(started), {
          signal: signal(),
          onTick: (j: ContribJob & { steps?: StepList }) =>
            setJob({ label: a.label, step: j.step ?? null, steps: j.steps }),
        });
        if (r.ok) say(r);
        return r;
      },
      "keep going",
    );

  const press = (a: ContribAction) => {
    if (busy) return;
    const dialog = dialogOf(a);
    if (dialog === "propose") return setSheet({ k: "propose" });
    if (dialog === "remix") return setSheet({ k: "remix" });
    if (dialog === "sign-in") {
      return setSignIn({ doing: "keep going", again: () => void contrib.refresh() });
    }
    if (a.confirm || a.id === "discard") {
      return setSheet({ k: "confirm", action: a, sentence: a.confirm || "" });
    }
    void post(a);
  };

  const spin = (key: string) => busy === key && <Loader2 data-icon="inline-start" className="animate-spin" />;

  // ---- the merge review (copy.merge_job) -------------------------------------
  const onKeepMerge = (m: MergeJobView) =>
    act("keep-merge", async () => {
      const r = await acceptMerge(dir, m.id);
      if (r.ok) notify({ title: okMessage(r, "Merged. Your copy has both versions."), tone: "info" });
      return r;
    });
  const onDiscardMerge = (m: MergeJobView) => act("discard-merge", () => discardMerge(dir, m.id));

  // The panel's sentence already says what Claude is doing or did (and its
  // files are the merge's): the block adds only the clock, or the choice.
  let mergeBlock: ReactNode = null;
  if (merge && (merge.status === "running" || merge.status === "landing")) {
    mergeBlock = (
      <div className="contrib-panel-merge" role="status">
        <ClaudeMark className="contrib-claude-working" />
        <span>
          {merge.status === "landing" ? (
            "Keeping the merge…"
          ) : (
            <>
              <span className="contrib-clock">{clockFrom}</span> so far. Your copy stays as it is until you choose.
            </>
          )}
        </span>
      </div>
    );
  } else if (merge?.status === "ready") {
    mergeBlock = (
      <div className="contrib-panel-merge is-ready">
        <ClaudeMark />
        <div className="contrib-panel-merge-body">
          <p className="contrib-panel-merge-title">Nothing changes in your copy until you keep it.</p>
          <div className="contrib-panel-actions">
            <Button onClick={() => void onKeepMerge(merge)} disabled={!!busy}>
              {spin("keep-merge")}
              Keep the merge
            </Button>
            <QuietAction onClick={() => void onDiscardMerge(merge)} disabled={!!busy}>
              {busy === "discard-merge" ? "Discarding…" : "Discard the merge"}
            </QuietAction>
          </div>
        </div>
      </div>
    );
  }

  // ---- the buttons, in the server's order, primary ones first -----------------
  const check = actions.find((a) => a.id === CHECK_ID) ?? null;
  const rest = actions.filter((a) => a.id !== CHECK_ID);
  const primaries = rest.filter((a) => a.primary);
  const quiet = rest.filter((a) => !a.primary);
  const label = (a: ContribAction) => (busy === a.id && !dialogOf(a) ? `${a.label}…` : a.label);
  const primaryButtons = primaries.map((a, i) => (
    <Button
      key={a.id}
      variant={a.danger ? "destructive" : i === 0 ? "default" : "outline"}
      onClick={() => press(a)}
      disabled={!!busy}
    >
      {spin(a.id)}
      {label(a)}
    </Button>
  ));
  const quietButtons = quiet.map((a) => (
    <QuietAction key={a.id} onClick={() => press(a)} disabled={!!busy} tone={a.danger ? "danger" : undefined}>
      {label(a)}
    </QuietAction>
  ));

  const working = !!job || merge?.status === "running" || merge?.status === "landing";
  const tone: StripTone = working
    ? "working"
    : merge?.status === "ready" || ATTENTION_STATES.has(panel.state)
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
  const checked = timeAgo(panel.last_checked);

  return (
    <>
      <div className="contrib-stack">
        <StatusStrip
          tone={tone}
          icon={icon}
          busy={working}
          sentence={panel.sentence}
          detail={panel.detail || null}
          primary={primaryButtons.length > 0 ? primaryButtons[0] : null}
          secondary={
            primaryButtons.length > 1 || quietButtons.length > 0 ? (
              <>
                {primaryButtons.slice(1)}
                {quietButtons}
              </>
            ) : null
          }
        >
          <FileList files={panel.files} label="Files this is about" />
          {job && (
            <JobProgress
              steps={job.steps?.length ? job.steps : [{ key: "run", label: `${job.label}…` }]}
              current={job.step}
            />
          )}
          {mergeBlock}
          {error && !(sheet?.k === "confirm") && (
            <p className="contrib-error" role="alert">
              {error}
            </p>
          )}
          <div className="contrib-panel-foot">
            <span className="contrib-panel-checked">
              {checked ? `Checked ${checked}` : "Not checked yet"}
              {check && (
                <>
                  {" · "}
                  <QuietAction onClick={() => press(check)} disabled={!!busy}>
                    {busy === CHECK_ID ? "Checking…" : check.label}
                  </QuietAction>
                </>
              )}
            </span>
            {activity.length > 0 && (
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
          {activityOpen && activity.length > 0 && (
            <ol className="contrib-activity" aria-label="Activity">
              {activity.map((line) => (
                <li key={line.id} className="contrib-activity-line">
                  <span className="contrib-activity-at">{timeAgo(line.at) ?? ""}</span>
                  <span className="contrib-activity-text">{line.text}</span>
                  {line.undo && (
                    <QuietAction onClick={() => setSheet({ k: "undo", line })} disabled={!!busy}>
                      <RotateCcw aria-hidden="true" />
                      {busy === `undo:${line.id}` ? "Putting it back…" : line.undo_label || "Undo"}
                    </QuietAction>
                  )}
                </li>
              ))}
            </ol>
          )}
        </StatusStrip>
      </div>

      {sheet?.k === "propose" && (
        <ProposeDialog
          dir={dir}
          author={who}
          signedIn={signedIn}
          again={proposal?.state === "declined"}
          untracked={panel.untracked ?? []}
          onClose={() => setSheet(null)}
          onSent={() => void contrib.refresh()}
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
          onPublished={() => void contrib.refresh()}
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
              <AlertDialogDescription>
                {sheet.sentence ||
                  (sheet.action.id === "discard"
                    ? `Your edits to ${slug} go away, and the app goes back to ${possessive(who)} latest version.`
                    : "")}
              </AlertDialogDescription>
            </AlertDialogHeader>
            {sheet.action.id === "discard" && openProposal && (
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
                  const args = a.id === "discard" ? { withdraw: openProposal && withdrawToo } : undefined;
                  const r = await post(a, { confirm: true, args });
                  // Another action running: nothing happened, keep the sheet.
                  // A refusal keeps it too, its sentence shown inside — the
                  // next move is to wait and press again, not to start over.
                  if (r === "busy" || r === false) return;
                  setSheet(null);
                  setWithdrawToo(false);
                }}
              >
                {spin(sheet.action.id)}
                {sheet.action.confirm_label || sheet.action.label}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        )}
      </AlertDialog>

      <AlertDialog
        open={sheet?.k === "undo"}
        onOpenChange={(o) => {
          if (!o && !busy) setSheet(null);
        }}
      >
        {sheet?.k === "undo" && (
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>{sheet.line.undo_label || "Undo"}?</AlertDialogTitle>
              <AlertDialogDescription>
                The app goes back to how it was before this: &ldquo;{sheet.line.text}&rdquo;. What it has now is
                kept in the activity list too, so nothing is lost either way.
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
                {sheet.line.undo_label || "Undo"}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        )}
      </AlertDialog>
    </>
  );
}

type StepList = { key: string; label: string }[];

/** The paths a sentence is about, a few at a time with "N more". */
function FileList({ files, label }: { files: string[]; label: string }) {
  const [all, setAll] = useState(false);
  if (files.length === 0) return null;
  const shown = all ? files : files.slice(0, FILES_SHOWN);
  const more = files.length - shown.length;
  return (
    <ul className="contrib-files" aria-label={label}>
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

/** "1:05" since the merge job started — the server's `started_at` when it
 *  sends one, else the first time this page saw the job running. Ticks once
 *  a second only while a job is running. */
function useMergeClock(job: MergeJobView | null): string {
  const firstSeen = useRef<{ id: string; at: number } | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const running = job?.status === "running";
  if (job && firstSeen.current?.id !== job.id) firstSeen.current = { id: job.id, at: Date.now() };
  useEffect(() => {
    if (!running) return;
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [running]);
  if (!job) return "";
  const from = job.started_at ? job.started_at * 1000 : firstSeen.current!.at;
  return elapsed(Math.max(0, now - from));
}
