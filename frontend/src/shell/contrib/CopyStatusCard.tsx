// The app page's contribution-loop surface on the Overview tab, above the
// running app (handoff A2/A4/A5): where this copy stands against its author,
// in ONE state at a time, as one sentence and at most one button.
//
// WHICH STATE (contract §6.4's precedence, plus the merge review on top):
//
//   merge job  Claude is combining, or has combined, two versions — progress
//              with an elapsed clock, then Claude's summary and the files it
//              touched → Keep the merge / Discard the merge.
//   overlap    "alice changed the same parts you did." → Merge with Claude.
//   merged     "alice merged your change. You're on alice's latest version."
//   declined   "alice declined your proposal. Your changes are still here."
//   proposed   Waiting for alice / alice commented: "…"; edits made since →
//              "2 edits not in your proposal" + Update proposal.
//   changes    "Your changes: 3 edits" → Propose to alice.
//   original   a remix whose original moved → Bring it in (same merge review).
//   sync error your own app's edits did not reach GitHub → the server's plain
//              sentence + Try again (no activity-dock failure row exists for
//              these apps any more, so the page is where it is said).
//   following  nothing at all — quiet on success.
//
// Above that, at most one slim notice for a quiet thing that happened in the
// background ("Updated to alice's latest, your changes kept"), dismissed for
// good with its ✕ (`/api/contrib/ack`, optimistic — the only optimistic
// write here; everything else waits for the server's answer).
//
// The dialogs (Propose, Publish as remix, Discard, Withdraw, the
// collaborator's direct update) are mounted HERE, outside any one state's
// branch, so a background re-read that moves the state on (a send that just
// turned "changes" into "proposed") never unmounts a sheet mid-flight.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Check, Clock, GitCompareArrows, Loader2, MessageSquare, PenLine, Sparkles, TriangleAlert } from "lucide-react";
import {
  ackContrib,
  acceptMerge,
  refreshContrib,
  bringInOriginal,
  discardChanges,
  discardMerge,
  plainMessage,
  pushToMain,
  runJob,
  startMerge,
  updateProposal,
  withdrawProposal,
  type AppContrib,
  type AppContribState,
  type ContribAnswer,
  type MergeJobView,
} from "@platform/lib/contrib";
import { notify } from "@platform/lib/notifications";
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
import { ProposeDialog } from "./ProposeDialog";
import { RemixDialog } from "./RemixDialog";
import { QuietAction, StatusStrip } from "./StatusStrip";
import { authorOf, editCount, elapsed, plural, possessive } from "./words";

type Dialog = null | "propose" | "propose-again" | "remix" | "discard" | "withdraw" | "direct";

type View =
  | { k: "merge"; job: MergeJobView }
  | { k: "overlap" }
  | { k: "merged" }
  | { k: "declined" }
  | { k: "proposed" }
  | { k: "changes" }
  | { k: "original" }
  | { k: "sync-error"; message: string };

/** The one state the card shows, or null for "say nothing". */
export function pickView(c: AppContrib): View | null {
  const { copy, flags } = c;
  const github = copy.kind === "github" && flags.changes;
  const remix = copy.kind === "own" && !!copy.remix_of && flags.remix;
  // An own app with a repo can overlap too (Alice's own copy against her
  // repo, the engine's "own-sync"): same sentence shape, same button.
  const own = copy.kind === "own" && flags.inbox;
  if (!github && !remix && !own) return null;
  const job = copy.merge_job;
  if (job && (job.status === "running" || job.status === "ready" || job.status === "failed")) {
    return { k: "merge", job };
  }
  if (copy.needs_merge || copy.state === "needs-merge") return { k: "overlap" };
  if (github) {
    switch (copy.state) {
      case "merged":
        return { k: "merged" };
      case "declined":
        return { k: "declined" };
      case "proposed":
        return { k: "proposed" };
      case "changes":
        return { k: "changes" };
    }
  }
  if (remix && copy.original_update) return { k: "original" };
  if (copy.kind === "own" && copy.sync_error) return { k: "sync-error", message: copy.sync_error };
  return null;
}

export function CopyStatusCard({ dir, slug, contrib }: { dir: string; slug: string; contrib: AppContribState }) {
  const c = contrib.data;
  const [dialog, setDialog] = useState<Dialog>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Acknowledged here ahead of the re-read, so the ✕ takes effect on the click.
  const [ackedEvent, setAckedEvent] = useState<number | null>(null);
  // Keyed by which settlement it was ("merged:12"), so a LATER proposal's
  // answer is not hidden by this one's ✕.
  const [ackedSettled, setAckedSettled] = useState<string | null>(null);
  const [withdrawToo, setWithdrawToo] = useState(false);

  // A new folder is a new card: nothing carries over.
  useEffect(() => {
    setDialog(null);
    setBusy(null);
    setError(null);
    setAckedEvent(null);
    setAckedSettled(null);
  }, [dir]);

  const view = c ? pickView(c) : null;
  const clockFrom = useMergeClock(view?.k === "merge" ? view.job : null);
  if (!c) return null;

  const { copy, proposal, flags } = c;
  const author = authorOf(copy);
  const edits = editCount(copy);
  const signedIn = c.github.signed_in;
  const settledKey = `${copy.state}:${proposal?.number ?? ""}`;
  const settledShown = ackedSettled !== settledKey;

  /** Run one action: one at a time (double-submit is a no-op), the server's
   *  sentence on a refusal, a re-read either way. */
  const act = async (key: string, fn: () => Promise<ContribAnswer | { ok: boolean; message?: string }>) => {
    if (busy) return false;
    setBusy(key);
    setError(null);
    const r = await fn();
    if (!r.ok) setError(plainMessage(r));
    await contrib.refresh();
    setBusy(null);
    return r.ok;
  };

  const spin = (key: string) => busy === key && <Loader2 data-icon="inline-start" className="animate-spin" />;

  const onUpdateProposal = () =>
    act("update", async () => {
      const r = await runJob(updateProposal(dir));
      if (r.ok) notify({ title: `Proposal updated. ${author} sees your latest edits.`, tone: "info" });
      return r;
    });

  const onMerge = () => act("merge", () => startMerge(dir));
  const onBringIn = () => act("merge", () => bringInOriginal(dir));
  const onKeepMerge = (job: MergeJobView) =>
    act("keep", async () => {
      const r = await acceptMerge(dir, job.id);
      if (r.ok) {
        notify({
          title: job.purpose === "original" ? `${possessive(author)} update is in your remix.` : "Merged. Your copy has both versions.",
          tone: "info",
        });
      }
      return r;
    });
  const onDiscardMerge = (job: MergeJobView) => act("discard-merge", () => discardMerge(dir, job.id));
  const onRetryMerge = (job: MergeJobView) => (job.purpose === "original" ? onBringIn() : onMerge());

  const ackSettled = () => {
    setAckedSettled(settledKey);
    void ackContrib(dir, "settled").then(() => contrib.refresh());
  };
  const ackEvent = (at: number) => {
    setAckedEvent(at);
    void ackContrib(dir, "event").then(() => contrib.refresh());
  };

  // ---- the quiet notice ------------------------------------------------------
  const ev = copy.event;
  let notice: ReactNode = null;
  if (ev && ev.at !== ackedEvent) {
    const sentence =
      ev.kind === "updated-kept"
        ? `Updated to ${possessive(author)} latest, your changes kept.`
        : ev.kind === "merged-back"
          ? `${author} merged your change. You're on ${possessive(author)} latest version.`
          : null;
    // Only the two things that happened while the user was away. Everything
    // else in the event log is something the user just did and already saw
    // confirmed — repeating it here would be noise.
    if (sentence && view?.k !== "merged") {
      notice = (
        <StatusStrip
          slim
          tone="success"
          icon={<Check />}
          sentence={sentence}
          onDismiss={() => ackEvent(ev.at)}
        />
      );
    }
  }

  // ---- the one state ---------------------------------------------------------
  const remixAction = flags.remix && (
    <QuietAction onClick={() => setDialog("remix")} disabled={!!busy}>
      Publish as remix
    </QuietAction>
  );
  const discardAction = (
    <QuietAction onClick={() => setDialog("discard")} disabled={!!busy} tone="danger">
      Discard my changes
    </QuietAction>
  );

  let strip: ReactNode = null;
  if (view?.k === "merge") {
    const job = view.job;
    const original = job.purpose === "original";
    if (job.status === "running") {
      strip = (
        <StatusStrip
          tone="working"
          busy
          icon={<ClaudeMark className="contrib-claude-working" />}
          sentence={
            original
              ? `Claude is bringing ${possessive(author)} update into your remix…`
              : "Claude is combining both versions…"
          }
          detail={
            <>
              <span className="contrib-clock">{clockFrom}</span> so far. Your copy stays as it is until you
              choose.
            </>
          }
        />
      );
    } else if (job.status === "ready") {
      strip = (
        <StatusStrip
          tone="attention"
          icon={<ClaudeMark />}
          sentence={original ? `Claude brought in ${possessive(author)} update.` : "Claude combined both versions."}
          detail="Nothing changes in your copy until you keep it."
          primary={
            <Button onClick={() => void onKeepMerge(job)} disabled={!!busy}>
              {spin("keep")}
              Keep the merge
            </Button>
          }
          secondary={
            <QuietAction onClick={() => void onDiscardMerge(job)} disabled={!!busy}>
              {busy === "discard-merge" ? "Discarding…" : "Discard the merge"}
            </QuietAction>
          }
        >
          {job.summary && <p className="contrib-summary">{job.summary}</p>}
          {job.files.length > 0 && (
            <ul className="contrib-files" aria-label="Files Claude combined">
              {job.files.map((f) => (
                <li key={f}>{f}</li>
              ))}
            </ul>
          )}
        </StatusStrip>
      );
    } else {
      strip = (
        <StatusStrip
          tone="attention"
          icon={<TriangleAlert />}
          sentence="Claude couldn't combine the two versions."
          detail={job.error || "Nothing changed in your copy."}
          primary={
            <Button onClick={() => void onRetryMerge(job)} disabled={!!busy}>
              {spin("merge")}
              Try again
            </Button>
          }
          secondary={
            <QuietAction onClick={() => void onDiscardMerge(job)} disabled={!!busy}>
              Leave it for now
            </QuietAction>
          }
        />
      );
    }
  } else if (view?.k === "overlap") {
    // Any own copy (an app the user made, or a remix) overlapping its OWN
    // repo — the engine's "own-sync" — reads the same; only a GitHub-link
    // copy has an author to name.
    const ownApp = copy.kind === "own";
    strip = (
      <StatusStrip
        tone="attention"
        icon={<GitCompareArrows />}
        sentence={
          ownApp
            ? "Your edits and the latest on GitHub changed the same parts."
            : `${author} changed the same parts you did.`
        }
        detail={
          proposal
            ? "Your copy stays as it is. Merge first, then update your proposal."
            : "Your copy stays as it is until you merge."
        }
        primary={
          <Button onClick={() => void onMerge()} disabled={!!busy}>
            {busy === "merge" ? <Loader2 data-icon="inline-start" className="animate-spin" /> : <Sparkles data-icon="inline-start" />}
            Merge with Claude
          </Button>
        }
      />
    );
  } else if (view?.k === "merged" && settledShown) {
    const credit = flags.loop && copy.repo ? `Your name is on ${copy.repo} as a contributor.` : null;
    strip = (
      <StatusStrip
        tone="success"
        icon={<Check />}
        // The second sentence only with A4 on: without it nothing moves this
        // copy back onto the author's version, so it would not be true.
        sentence={
          flags.loop
            ? `${author} merged your change. You're on ${possessive(author)} latest version.`
            : `${author} merged your change.`
        }
        detail={
          copy.unproposed > 0
            ? `${credit ? credit + " " : ""}${plural(copy.unproposed, "edit")} you didn't propose ${copy.unproposed === 1 ? "is" : "are"} still here.`
            : credit
        }
        onDismiss={ackSettled}
      />
    );
  } else if (view?.k === "declined" && settledShown) {
    strip = (
      <StatusStrip
        tone="neutral"
        icon={<PenLine />}
        sentence={`${author} declined your proposal. Your changes are still here.`}
        primary={
          <Button onClick={() => setDialog("propose-again")} disabled={!!busy}>
            Propose again
          </Button>
        }
        secondary={
          <>
            {remixAction}
            {discardAction}
          </>
        }
        onDismiss={ackSettled}
      >
        {proposal?.comment && <Quote author={proposal.comment.author} body={proposal.comment.body} />}
      </StatusStrip>
    );
  } else if (view?.k === "proposed") {
    const unproposed = proposal?.unproposed ?? copy.unproposed;
    const comment = proposal?.state === "commented" ? proposal.comment : undefined;
    strip = (
      <StatusStrip
        tone="neutral"
        icon={comment ? <MessageSquare /> : <Clock />}
        sentence={comment ? `${comment.author} commented:` : `Waiting for ${author}`}
        detail={
          unproposed > 0
            ? `${plural(unproposed, "edit")} not in your proposal`
            : proposal
              ? `You proposed “${proposal.title}”.`
              : null
        }
        primary={
          unproposed > 0 ? (
            <Button onClick={() => void onUpdateProposal()} disabled={!!busy}>
              {spin("update")}
              {busy === "update" ? "Updating…" : "Update proposal"}
            </Button>
          ) : null
        }
        secondary={
          <>
            {proposal?.url && <QuietAction href={proposal.url}>View on GitHub</QuietAction>}
            {remixAction}
            <QuietAction onClick={() => setDialog("withdraw")} disabled={!!busy}>
              Withdraw proposal
            </QuietAction>
            {discardAction}
          </>
        }
      >
        {comment && <Quote author={comment.author} body={comment.body} hideAuthor />}
      </StatusStrip>
    );
  } else if (view?.k === "changes") {
    strip = (
      <StatusStrip
        tone="neutral"
        icon={<PenLine />}
        sentence={`Your changes: ${plural(edits, "edit")}`}
        detail={`Only in your copy. ${author} sees them if you propose them.`}
        primary={
          <Button onClick={() => setDialog("propose")} disabled={!!busy}>
            Propose to {author}
          </Button>
        }
        secondary={
          <>
            {remixAction}
            {discardAction}
            {copy.can_push && (
              <QuietAction onClick={() => setDialog("direct")} disabled={!!busy}>
                Update {possessive(author)} app directly
              </QuietAction>
            )}
          </>
        }
      />
    );
  } else if (view?.k === "original") {
    strip = (
      <StatusStrip
        tone="attention"
        icon={<Sparkles />}
        sentence={`${author} updated the original. Bring it into your remix?`}
        detail="Claude combines it with your version, and you look before anything is kept."
        primary={
          <Button onClick={() => void onBringIn()} disabled={!!busy}>
            {spin("merge")}
            Bring it in
          </Button>
        }
      />
    );
  }

  if (!strip && view?.k === "sync-error") {
    strip = (
      <StatusStrip
        tone="attention"
        icon={<TriangleAlert />}
        sentence={view.message}
        detail="Your edits are safe here."
        primary={
          <Button
            variant="outline"
            onClick={() => void act("retry", () => refreshContrib(dir))}
            disabled={!!busy}
          >
            {spin("retry")}
            Try again
          </Button>
        }
      />
    );
  }

  const openProposal = !!proposal && (proposal.state === "waiting" || proposal.state === "commented");

  return (
    <>
      {(notice || strip) && (
        <div className="contrib-stack">
          {notice}
          {strip}
          {error && (
            <p className="contrib-error" role="alert">
              {error}
            </p>
          )}
        </div>
      )}

      {(dialog === "propose" || dialog === "propose-again") && (
        <ProposeDialog
          dir={dir}
          author={author}
          signedIn={signedIn}
          again={dialog === "propose-again"}
          onClose={() => setDialog(null)}
          onSent={() => void contrib.refresh()}
        />
      )}
      {dialog === "remix" && (
        <RemixDialog
          dir={dir}
          slug={slug}
          author={author}
          original={copy.repo}
          signedIn={signedIn}
          onClose={() => setDialog(null)}
          onPublished={() => void contrib.refresh()}
        />
      )}

      <AlertDialog
        open={dialog === "discard"}
        onOpenChange={(o) => {
          if (!o && busy !== "discard") {
            setDialog(null);
            setWithdrawToo(false);
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Discard your changes?</AlertDialogTitle>
            <AlertDialogDescription>
              Your {plural(edits, "edit")} to {slug} go away, and the app goes back to {possessive(author)} latest
              version.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {openProposal && (
            <label className="contrib-check">
              <Checkbox checked={withdrawToo} onCheckedChange={(v) => setWithdrawToo(v === true)} />
              <span>Also withdraw my proposal</span>
            </label>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy === "discard"}>Keep my changes</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              disabled={busy === "discard"}
              onClick={async () => {
                const ok = await act("discard", () => discardChanges(dir, openProposal && withdrawToo));
                setDialog(null);
                setWithdrawToo(false);
                if (ok) notify({ title: `Back on ${possessive(author)} version.`, tone: "info" });
              }}
            >
              {spin("discard")}
              Discard changes
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={dialog === "withdraw"}
        onOpenChange={(o) => {
          if (!o && busy !== "withdraw") setDialog(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Withdraw your proposal?</AlertDialogTitle>
            <AlertDialogDescription>
              {author} won&rsquo;t be asked to review it any more. Your changes stay here, and you can propose
              again any time.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy === "withdraw"}>Keep it open</AlertDialogCancel>
            <AlertDialogAction
              disabled={busy === "withdraw"}
              onClick={async () => {
                await act("withdraw", () => withdrawProposal(dir));
                setDialog(null);
              }}
            >
              {spin("withdraw")}
              Withdraw
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={dialog === "direct"}
        onOpenChange={(o) => {
          if (!o && busy !== "direct") setDialog(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Update {possessive(author)} app directly?</AlertDialogTitle>
            <AlertDialogDescription>
              Your {plural(edits, "edit")} go straight into {copy.repo ?? `${possessive(author)} app`} without{" "}
              {author} reviewing them first, and everyone who follows it gets them. You can do this because{" "}
              {author} gave you access.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy === "direct"}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              disabled={busy === "direct"}
              onClick={async () => {
                const ok = await act("direct", () => pushToMain(dir));
                setDialog(null);
                if (ok) notify({ title: `Your edits are in ${possessive(author)} app.`, tone: "info" });
              }}
            >
              {spin("direct")}
              Update directly
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

function Quote({ author, body, hideAuthor }: { author: string; body: string; hideAuthor?: boolean }) {
  return (
    <blockquote className="contrib-quote">
      {!hideAuthor && <span className="contrib-quote-author">{author}:</span>} {body}
    </blockquote>
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
  return elapsed(now - from);
}
