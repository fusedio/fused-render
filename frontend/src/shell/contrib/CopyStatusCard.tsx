// The app page's contribution-loop surface on the Overview tab, above the
// running app (handoff A2/A4/A5): where this copy stands against its author,
// in ONE state at a time, as one sentence and at most one button.
//
// WHICH STATE (contract §6.4's precedence, plus the merge review on top):
//
//   merge job  Claude is combining, or has combined, two versions — progress
//              with an elapsed clock, then Claude's summary and the files it
//              touched → Keep the merge / Discard the merge. A FAILED job
//              only shows while the overlap it was for still stands: once
//              the copy has moved on, a day-old failure must not outrank it.
//   overlap    "alice changed the same parts you did." → Merge with Claude.
//   merged     "alice merged your change." (+ "You're on alice's latest
//              version." only once that is true).
//   declined   "alice declined your proposal. Your changes are still here."
//   proposed   Waiting for alice / alice commented: "…"; when what this copy
//              runs differs from what was sent (`proposal.stale`) → Update
//              proposal.
//   changes    "Your changes: 3 edits" → Propose to alice.
//   original   a remix whose original moved → Bring it in (same merge review).
//   sync error your own app's edits did not reach GitHub → a plain sentence
//              + Try again (no activity-dock failure row exists for these
//              apps any more, so the page is where it is said).
//   remix's    a proposal sent before publishing a remix, still open.
//   proposal
//   behind     automatic updates are off and the author has new ones:
//              "alice has updates. Bring them in?" → Bring them in. The strip
//              itself only when following with no changes; under the changes
//              strip it is a slim line of its own.
//   following  nothing at all — quiet on success.
//
// Above that, at most one slim notice for a quiet thing that happened in the
// background ("Updated to alice's latest, your changes kept"), dismissed for
// good with its ✕ (`/api/contrib/ack`, optimistic — the only optimistic
// write here; everything else waits for the server's answer).
//
// The dialogs (Propose, Publish as remix, Discard, Withdraw, the
// collaborator's direct update, the sign-in sheet) are mounted HERE, outside
// any one state's branch, so a background re-read that moves the state on (a
// send that just turned "changes" into "proposed") never unmounts a sheet
// mid-flight.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { ArrowDownToLine, Check, Clock, GitCompareArrows, Loader2, MessageSquare, PenLine, Sparkles, TriangleAlert } from "lucide-react";
import {
  ackContrib,
  acceptMerge,
  bringInOriginal,
  discardChanges,
  discardMerge,
  okMessage,
  plainMessage,
  pushToMain,
  updateCopy,
  retrySync,
  runJob,
  startMerge,
  updateProposal,
  useUnmountSignal,
  withdrawProposal,
  type AppContrib,
  type AppContribState,
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
import { SignInSheet } from "./SignInSheet";
import { QuietAction, StatusStrip } from "./StatusStrip";
import { atStart, authorOf, editCount, elapsed, plural, possessive, syncErrorSentence } from "./words";

type Dialog = null | "propose" | "propose-again" | "remix" | "discard" | "withdraw" | "direct";

type View =
  | { k: "merge"; job: MergeJobView }
  | { k: "overlap" }
  | { k: "merged" }
  | { k: "declined" }
  | { k: "proposed" }
  | { k: "changes" }
  | { k: "behind" }
  | { k: "original" }
  | { k: "remix-proposal" }
  | { k: "sync-error"; message: string };

/** Any action's answer, as `act` reads it. */
type Outcome = { ok: boolean; message?: string; reason?: string };

/** Automatic updates are off and the author's latest has moved on. */
function waitingUpdates(c: AppContrib): boolean {
  return c.copy.kind === "github" && c.copy.auto_sync === false && (c.copy.behind ?? 0) > 0;
}

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
  if (job) {
    const live = job.status === "running" || job.status === "landing" || job.status === "ready";
    // A failure is worth showing only while what it failed at still waits.
    const stillWaiting = copy.needs_merge || (job.purpose === "original" && copy.original_update);
    if (live || (job.status === "failed" && stillWaiting)) return { k: "merge", job };
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
    if (waitingUpdates(c)) return { k: "behind" };
  }
  if (remix && copy.original_update) return { k: "original" };
  if (copy.kind === "own" && copy.sync_error) return { k: "sync-error", message: copy.sync_error };
  // A proposal sent before publishing the remix stays open and tracked (§11).
  if (remix && c.proposal && (c.proposal.state === "waiting" || c.proposal.state === "commented")) {
    return { k: "remix-proposal" };
  }
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
  // The merged banner was dismissed on this page: the "merged-back" notice
  // that follows once the copy returns to the author's latest says the same
  // thing again, so it is not shown (and is acked) for this visit.
  const mergedDismissed = useRef(false);
  const [withdrawToo, setWithdrawToo] = useState(false);
  // A refused action waiting on a sign-in: what to say, and what to run again.
  const [signIn, setSignIn] = useState<{ doing: string; again: () => void } | null>(null);
  const signal = useUnmountSignal();

  // A new folder is a new card: nothing carries over.
  useEffect(() => {
    setDialog(null);
    setBusy(null);
    setError(null);
    setAckedEvent(null);
    setAckedSettled(null);
    setSignIn(null);
    mergedDismissed.current = false;
  }, [dir]);

  const view = c ? pickView(c) : null;
  // An error belongs to the state it was said in: when the card moves on
  // (the merge finished, the proposal went out) the old sentence goes.
  const viewKind = view?.k ?? null;
  useEffect(() => setError(null), [viewKind]);
  const clockFrom = useMergeClock(view?.k === "merge" ? view.job : null);

  // The merged-back notice, acked as soon as it shows up after the merged
  // banner was dismissed here (see `mergedDismissed`).
  const ev = c?.copy.event ?? null;
  useEffect(() => {
    if (ev?.kind === "merged-back" && mergedDismissed.current && ev.at !== ackedEvent) {
      setAckedEvent(ev.at);
      void ackContrib(dir, "event");
    }
  }, [ev?.kind, ev?.at, ackedEvent, dir]);

  if (!c) return null;

  const { copy, proposal, flags } = c;
  const author = authorOf(copy);
  const Author = atStart(author);
  const edits = editCount(copy);
  // A remix's own author is the user; the ORIGINAL's author is who updates it.
  const originalAuthor = copy.remix_of?.split("/")[0] || author;
  const OriginalAuthor = atStart(originalAuthor);
  const signedIn = c.github.signed_in;
  const settledKey = `${copy.state}:${proposal?.number ?? ""}`;
  const settledShown = ackedSettled !== settledKey;

  /** Run one action: one at a time ("busy" when another is running — a
   *  double click is a no-op), the server's sentence on a refusal, a re-read
   *  either way. With `signInTo`, a "signed-out" refusal opens the sign-in
   *  sheet and runs the action once more after it, instead of an error. */
  const act = async (
    key: string,
    fn: () => Promise<Outcome>,
    signInTo?: string,
  ): Promise<"busy" | boolean> => {
    if (busy) return "busy";
    setBusy(key);
    setError(null);
    const r = await fn();
    if (r.reason === "aborted") return false; // unmounted: nobody to tell
    if (!r.ok) {
      if (r.reason === "signed-out" && signInTo) {
        setSignIn({ doing: signInTo, again: () => void act(key, fn) });
      } else {
        setError(plainMessage(r));
      }
    }
    await contrib.refresh();
    setBusy(null);
    return r.ok;
  };

  const spin = (key: string) => busy === key && <Loader2 data-icon="inline-start" className="animate-spin" />;

  const onUpdateProposal = () =>
    act(
      "update",
      async () => {
        const r = await runJob(updateProposal(dir), { signal: signal() });
        if (r.ok) notify({ title: okMessage(r, `Proposal updated. ${Author} sees what you run now.`), tone: "info" });
        return r;
      },
      "update your proposal",
    );

  const onMerge = () => act("merge", () => startMerge(dir));
  const onBringUpdates = () =>
    act("bring", async () => {
      const r = await updateCopy(dir);
      if (r.ok) notify({ title: okMessage(r, `Updated to ${possessive(author)} latest.`), tone: "info" });
      return r;
    });
  const bringSentence = `${Author} has updates. Bring them in?`;
  const onBringIn = () => act("merge", () => bringInOriginal(dir));
  const onKeepMerge = (job: MergeJobView) =>
    act("keep", async () => {
      const r = await acceptMerge(dir, job.id);
      if (r.ok) {
        notify({
          title: okMessage(
            r,
            job.purpose === "original"
              ? `${possessive(OriginalAuthor)} update is in your remix.`
              : "Merged. Your copy has both versions.",
          ),
          tone: "info",
        });
      }
      return r;
    });
  const onDiscardMerge = (job: MergeJobView) => act("discard-merge", () => discardMerge(dir, job.id));
  const onRetryMerge = (job: MergeJobView) => (job.purpose === "original" ? onBringIn() : onMerge());

  const ackSettled = () => {
    setAckedSettled(settledKey);
    const wasMerged = copy.state === "merged";
    if (wasMerged) mergedDismissed.current = true;
    // Ack the merged-back event too when it is already here: one ✕, one
    // message gone — never a second banner saying the same thing.
    const alsoEvent = wasMerged && ev?.kind === "merged-back" ? ev.at : null;
    if (alsoEvent !== null) setAckedEvent(alsoEvent);
    void (async () => {
      await ackContrib(dir, "settled");
      if (alsoEvent !== null) await ackContrib(dir, "event");
      await contrib.refresh();
    })();
  };
  const ackEvent = (at: number) => {
    setAckedEvent(at);
    void ackContrib(dir, "event").then(() => contrib.refresh());
  };

  // ---- the quiet notice ------------------------------------------------------
  let notice: ReactNode = null;
  if (ev && ev.at !== ackedEvent) {
    const sentence =
      ev.kind === "updated-kept"
        ? `Updated to ${possessive(author)} latest, your changes kept.`
        : ev.kind === "merged-back" && !mergedDismissed.current
          ? `${Author} merged your change. You're on ${possessive(author)} latest version.`
          : null;
    // Only the two things that happened while the user was away. Everything
    // else in the event log is something the user just did and already saw
    // confirmed — repeating it here would be noise.
    if (sentence && view?.k !== "merged") {
      notice = (
        <StatusStrip slim tone="success" icon={<Check />} sentence={sentence} onDismiss={() => ackEvent(ev.at)} />
      );
    }
  }

  // ---- the one state ---------------------------------------------------------
  const remixAction = flags.remix && (
    <QuietAction onClick={() => setDialog("remix")} disabled={!!busy}>
      Publish as remix
    </QuietAction>
  );
  // Nothing to discard, no Discard (a proposal whose edits all went out and
  // were since taken in, a copy mid-refresh).
  const discardAction = edits > 0 && (
    <QuietAction onClick={() => setDialog("discard")} disabled={!!busy} tone="danger">
      Discard my changes
    </QuietAction>
  );

  let strip: ReactNode = null;
  if (view?.k === "merge") {
    const job = view.job;
    const original = job.purpose === "original";
    if (job.status === "running" || job.status === "landing") {
      const landing = job.status === "landing";
      strip = (
        <StatusStrip
          tone="working"
          busy
          icon={<ClaudeMark className="contrib-claude-working" />}
          sentence={
            landing
              ? "Keeping the merge…"
              : original
                ? `Claude is bringing ${possessive(originalAuthor)} update into your remix…`
                : "Claude is combining both versions…"
          }
          detail={
            landing ? null : (
              <>
                <span className="contrib-clock">{clockFrom}</span> so far. Your copy stays as it is until you
                choose.
              </>
            )
          }
        />
      );
    } else if (job.status === "ready") {
      strip = (
        <StatusStrip
          tone="attention"
          icon={<ClaudeMark />}
          sentence={
            original ? `Claude brought in ${possessive(originalAuthor)} update.` : "Claude combined both versions."
          }
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
          ownApp ? "Your edits and the latest on GitHub changed the same parts." : `${Author} changed the same parts you did.`
        }
        detail={
          proposal
            ? "Your copy stays as it is. Merge first, then update your proposal."
            : "Your copy stays as it is until you merge."
        }
        primary={
          <Button onClick={() => void onMerge()} disabled={!!busy}>
            {busy === "merge" ? (
              <Loader2 data-icon="inline-start" className="animate-spin" />
            ) : (
              <Sparkles data-icon="inline-start" />
            )}
            Merge with Claude
          </Button>
        }
      />
    );
  } else if (view?.k === "merged" && settledShown) {
    // "You're on alice's latest version" only once it is TRUE: the copy has
    // returned (the merged-back event), or nothing of the author's is left
    // to bring in and every change still here is one never proposed. Until
    // then the merge has happened on GitHub but this copy has not caught up.
    const caughtUp =
      flags.loop && (ev?.kind === "merged-back" || (copy.behind === 0 && copy.changes === copy.unproposed));
    const credit = flags.loop && copy.repo ? `Your name is on ${copy.repo} as a contributor.` : null;
    strip = (
      <StatusStrip
        tone="success"
        icon={<Check />}
        sentence={
          caughtUp
            ? `${Author} merged your change. You're on ${possessive(author)} latest version.`
            : `${Author} merged your change.`
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
        sentence={`${Author} declined your proposal. Your changes are still here.`}
        detail={proposal?.comment ? <Quote author={proposal.comment.author} body={proposal.comment.body} /> : null}
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
      />
    );
  } else if (view?.k === "proposed") {
    // Committed edits since the send, plus the unsaved ones on top.
    const unproposed = (proposal?.unproposed ?? copy.unproposed) + (copy.dirty ? 1 : 0);
    // Offered on what the server says is true — this copy runs something the
    // proposal does not have — not on the edit count, which misses a merge
    // with Claude (no new edit, but a different result).
    const offerUpdate = !!proposal?.stale && proposal.can_update !== false;
    const comment = proposal?.state === "commented" ? proposal.comment : undefined;
    const behindLine =
      unproposed > 0
        ? `${plural(unproposed, "edit")} not in your proposal`
        : offerUpdate
          ? "Your copy has changed since you sent it."
          : null;
    strip = (
      <StatusStrip
        tone="neutral"
        icon={comment ? <MessageSquare /> : <Clock />}
        sentence={comment ? `${atStart(comment.author)} commented:` : `Waiting for ${author}`}
        // A comment is the news: it comes first, under the sentence; what the
        // proposal is and what has changed since follow it.
        detail={comment ? <Quote author={comment.author} body={comment.body} hideAuthor /> : behindLine}
        primary={
          offerUpdate ? (
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
            {proposal?.can_withdraw !== false && (
              <QuietAction onClick={() => setDialog("withdraw")} disabled={!!busy}>
                Withdraw proposal
              </QuietAction>
            )}
            {discardAction}
          </>
        }
      >
        {(comment ? [proposal ? `You proposed “${proposal.title}”.` : null, behindLine] : [
          !behindLine && proposal ? `You proposed “${proposal.title}”.` : null,
        ])
          .filter(Boolean)
          .map((line) => (
            <p key={line} className="contrib-strip-detail">
              {line}
            </p>
          ))}
      </StatusStrip>
    );
  } else if (view?.k === "changes") {
    strip = (
      <StatusStrip
        tone="neutral"
        icon={<PenLine />}
        sentence={`Your changes: ${plural(edits, "edit")}`}
        detail={`Only in your copy. ${Author} sees them if you propose them.`}
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
      >
        {/* Both apply: the waiting updates ride under the changes as their
            own slim line — the changes stay the strip's one decision. */}
        {waitingUpdates(c) && (
          <div className="contrib-strip-inline">
            <span>{bringSentence}</span>
            <Button size="sm" variant="outline" onClick={() => void onBringUpdates()} disabled={!!busy}>
              {spin("bring")}
              Bring them in
            </Button>
          </div>
        )}
      </StatusStrip>
    );
  } else if (view?.k === "behind") {
    strip = (
      <StatusStrip
        tone="neutral"
        icon={<ArrowDownToLine />}
        sentence={bringSentence}
        primary={
          <Button onClick={() => void onBringUpdates()} disabled={!!busy}>
            {spin("bring")}
            Bring them in
          </Button>
        }
      />
    );
  } else if (view?.k === "original") {
    strip = (
      <StatusStrip
        tone="attention"
        icon={<Sparkles />}
        sentence={`${OriginalAuthor} updated the original. Bring it into your remix?`}
        detail="Claude combines it with your version, and you look before anything is kept."
        primary={
          <Button onClick={() => void onBringIn()} disabled={!!busy}>
            {spin("merge")}
            Bring it in
          </Button>
        }
      />
    );
  } else if (view?.k === "sync-error") {
    strip = (
      <StatusStrip
        tone="attention"
        icon={<TriangleAlert />}
        sentence={syncErrorSentence(view.message)}
        detail="Your edits are safe here."
        primary={
          <Button
            variant="outline"
            onClick={() =>
              void act("retry", async () => {
                const r = await retrySync(dir);
                // The sync is queued, not run: give it a moment before the
                // re-read `act` does, so the answer is usually the outcome.
                if (r.ok) await new Promise((res) => window.setTimeout(res, 3000));
                return r;
              })
            }
            disabled={!!busy}
          >
            {spin("retry")}
            Try again
          </Button>
        }
      />
    );
  } else if (view?.k === "remix-proposal" && proposal) {
    const comment = proposal.state === "commented" ? proposal.comment : undefined;
    strip = (
      <StatusStrip
        tone="neutral"
        icon={comment ? <MessageSquare /> : <Clock />}
        sentence={
          comment
            ? `${atStart(comment.author)} commented on your proposal:`
            : `Your proposal to ${originalAuthor} is still waiting.`
        }
        detail={
          comment ? (
            <Quote author={comment.author} body={comment.body} hideAuthor />
          ) : (
            `You proposed “${proposal.title}” before publishing your remix.`
          )
        }
        secondary={
          <>
            {proposal.url && <QuietAction href={proposal.url}>View on GitHub</QuietAction>}
            {proposal.can_withdraw !== false && (
              <QuietAction onClick={() => setDialog("withdraw")} disabled={!!busy}>
                Withdraw proposal
              </QuietAction>
            )}
          </>
        }
      />
    );
  }

  const openProposal = !!proposal && (proposal.state === "waiting" || proposal.state === "commented");

  // A confirm's button: an action refused because another one is running
  // keeps the confirm open (nothing happened yet); anything else closes it.
  const confirmThen = async (run: () => Promise<"busy" | boolean>, after?: (ok: boolean) => void) => {
    const r = await run();
    if (r === "busy") return;
    setDialog(null);
    after?.(r);
  };

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
              onClick={() =>
                void confirmThen(
                  () => act("discard", () => discardChanges(dir, openProposal && withdrawToo)),
                  (ok) => {
                    setWithdrawToo(false);
                    if (ok) notify({ title: `Back on ${possessive(author)} version.`, tone: "info" });
                  },
                )
              }
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
              {Author} won&rsquo;t be asked to review it any more. Your changes stay here.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy === "withdraw"}>Keep it open</AlertDialogCancel>
            <AlertDialogAction
              disabled={busy === "withdraw"}
              onClick={() =>
                void confirmThen(
                  () => act("withdraw", () => withdrawProposal(dir), "withdraw your proposal"),
                  (ok) => ok && notify({ title: "Proposal withdrawn.", tone: "info" }),
                )
              }
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
              onClick={() =>
                void confirmThen(
                  () => act("direct", () => pushToMain(dir), `update ${possessive(author)} app`),
                  (ok) => ok && notify({ title: `Your edits are in ${possessive(author)} app.`, tone: "info" }),
                )
              }
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

/** A comment, clamped to four lines with "More" when it runs longer. */
function Quote({ author, body, hideAuthor }: { author: string; body: string; hideAuthor?: boolean }) {
  const [open, setOpen] = useState(false);
  // A cheap stand-in for "would it clamp": long, or several lines.
  const long = body.length > 280 || body.split("\n").length > 4;
  return (
    <div className="contrib-quote-wrap">
      <blockquote className={"contrib-quote" + (open ? " is-open" : "")}>
        {!hideAuthor && <span className="contrib-quote-author">{author}:</span>} {body}
      </blockquote>
      {long && (
        <QuietAction onClick={() => setOpen((o) => !o)}>{open ? "Less" : "More"}</QuietAction>
      )}
    </div>
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
