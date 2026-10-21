// The Proposals tab (handoff A3): the inbox for an app the user owns — every
// open proposal for it, sent from Fused or opened on GitHub directly.
//
// Each card: who sent it, what it does in plain words, and the SAFETY REVIEW
// — Claude reading the proposal on this machine before anything in it runs
// (D18). Until that review is ready, Try is disabled; a "Needs a look"
// verdict names the concrete risks and makes Try ask first. Try runs the
// proposal in a separate scratch copy that can be opened beside the app and
// thrown away; the app itself is never touched. Accept merges it on GitHub
// (after a confirm), Decline closes it with an optional note.
//
// A proposal that NO LONGER FITS (GitHub's own check, `fits: false` — the
// app moved on under it) says so with a badge and two ways forward: Ask
// <sender> to update (a note on the proposal; the sender's page then offers
// Update) or Merge with Claude (Claude combines it with the app's latest
// here, shown on the card with Keep / Discard; Keep sends the result onto the
// sender's proposal, then the review reads it again). Unknown (`fits` null)
// says nothing.
//
// The list is the `inbox` field of `/api/contrib/app` — the page's one
// subscription, which polls fast while any review is still reading — so this
// tab owns no fetch of its own; it asks for a re-read after every action.
import { useEffect, useRef, useState } from "react";
import { ExternalLink, Inbox, Loader2, ShieldAlert, ShieldCheck, Sparkles, Trash2 } from "lucide-react";
import {
  acceptProposal,
  askToUpdate,
  declineProposal,
  okMessage,
  plainMessage,
  removeTry,
  reviewProposal,
  runJob,
  startInboxMerge,
  tryProposal,
  useUnmountSignal,
  type AppContribState,
  type ContribAnswer,
  type InboxItem,
} from "@platform/lib/contrib";
import { timeAgo } from "@platform/lib/format";
import { notify } from "@platform/lib/notifications";
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
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@platform/shadcn/ui/empty";
import { Field, FieldLabel } from "@platform/shadcn/ui/field";
import { Skeleton } from "@platform/shadcn/ui/skeleton";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { ClaudeMark } from "@platform/ui/ClaudeMark";
import { ContribDialog } from "./ContribDialog";
import { MergeReview, mergeShown } from "./MergeReview";
import { QuietAction } from "./StatusStrip";
import { plural, possessive } from "./words";

export function ProposalsTab({ dir, slug, contrib }: { dir: string; slug: string; contrib: AppContribState }) {
  const inbox = contrib.data?.inbox ?? null;
  // THE CARDS SURVIVE A RE-LIST. After an Accept or a Decline the server
  // reads `loading` with no items while it lists again; unmounting every
  // card then would drop another card's half-typed Decline note, its open
  // sheet and its Try state. So while loading, the last real list stays on
  // screen — minus the ones this page just accepted or declined.
  const lastItems = useRef<InboxItem[]>([]);
  const [gone, setGone] = useState<ReadonlySet<number>>(() => new Set());
  if (inbox && !inbox.loading) lastItems.current = inbox.items;
  const listed = inbox && inbox.loading && inbox.items.length === 0 ? lastItems.current : (inbox?.items ?? []);
  const items = listed.filter((i) => !gone.has(i.number));
  const settledGone = (n: number) => setGone((g) => new Set(g).add(n));

  // Can't reach GitHub: said as such, even while a listing is retrying —
  // never the bare skeleton (which reads as "almost there") and never an
  // empty inbox.
  if (inbox?.unavailable && items.length === 0) {
    return (
      <div className="contrib-inbox">
        <Empty className="contrib-inbox-empty">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <Inbox />
            </EmptyMedia>
            <EmptyTitle>Can&rsquo;t reach GitHub right now</EmptyTitle>
            <EmptyDescription>Proposals show up here once it answers. This page tries again on its own.</EmptyDescription>
          </EmptyHeader>
        </Empty>
      </div>
    );
  }
  // Still asking — or asked, but the list has never been read from GitHub
  // yet (a cold cache, `loading`): an empty answer then is not "no
  // proposals", and saying so would be wrong for the seconds until the
  // first read lands (the page polls fast meanwhile, `contribBusy`). Sized
  // like one card so the first answer does not jump.
  if (!inbox || (inbox.loading && items.length === 0)) {
    return (
      <div className="contrib-inbox" aria-busy="true">
        <Skeleton className="h-[148px] w-full rounded-[10px]" />
      </div>
    );
  }
  if (items.length === 0) {
    return (
      <div className="contrib-inbox">
        <Empty className="contrib-inbox-empty">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <Inbox />
            </EmptyMedia>
            <EmptyTitle>No proposals right now</EmptyTitle>
            <EmptyDescription>
              When someone sends changes to {slug}, they show up here with Claude&rsquo;s read of them.
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      </div>
    );
  }
  return (
    <div className="contrib-inbox">
      <p className="contrib-inbox-count">
        {plural(items.length, "proposal")} for {slug}
      </p>
      <ul className="contrib-inbox-list">
        {items.map((item) => (
          <li key={item.number}>
            <InboxCard
              dir={dir}
              slug={slug}
              item={item}
              refresh={contrib.refresh}
              onSettled={() => settledGone(item.number)}
            />
          </li>
        ))}
      </ul>
    </div>
  );
}

type Ask = null | "try" | "accept" | "decline" | "ask-update";

function InboxCard({
  dir,
  slug,
  item,
  refresh,
  onSettled,
}: {
  dir: string;
  slug: string;
  item: InboxItem;
  refresh: () => Promise<void>;
  /** Accepted or declined here: the list drops it without waiting for the
   *  re-list (which may briefly still name it). */
  onSettled: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ask, setAsk] = useState<Ask>(null);
  const [note, setNote] = useState("");
  const [askNote, setAskNote] = useState("");
  // The server asked for an explicit OK on a flagged proposal (reason
  // "confirm") although this card had not seen the verdict: the confirm then
  // reads as the flagged one, and the next press sends `confirm: true`.
  const [mustConfirm, setMustConfirm] = useState(false);
  const signal = useUnmountSignal();

  const review = item.review_status === "ready" ? item.review : null;
  const reading =
    item.review_status === "none" || item.review_status === "pending" || item.review_status === "running";
  const flagged = review?.verdict === "look";
  const who = item.author;
  const stale = item.fits === false;

  const act = async (key: string, fn: () => Promise<ContribAnswer | { ok: boolean; message?: string }>) => {
    if (busy) return false;
    setBusy(key);
    setError(null);
    const r = await fn();
    if ((r as { reason?: string }).reason === "aborted") return false; // unmounted
    if (!r.ok) setError(plainMessage(r));
    await refresh();
    setBusy(null);
    return r.ok;
  };
  const spin = (key: string) => busy === key && <Loader2 data-icon="inline-start" className="animate-spin" />;

  // The Try copy the server just made, held until the re-read lists it on
  // the item itself — so "Open the copy" is there the moment Try answers.
  const [madeTry, setMadeTry] = useState<{ url: string } | null>(null);
  const tryCopy = item.try ?? madeTry;
  // …and dropped once the server, having listed it, stops listing it (thrown
  // away elsewhere, or removed with the proposal): the held copy is gone too.
  const hadTry = useRef(false);
  useEffect(() => {
    if (item.try) hadTry.current = true;
    else if (hadTry.current) {
      hadTry.current = false;
      setMadeTry(null);
    }
  }, [item.try]);
  const startTry = (confirm: boolean) =>
    act("try", async () => {
      const r = await tryProposal(dir, item.number, confirm);
      if (r.ok && r.url) setMadeTry({ url: r.url });
      // The server asks first for a proposal Claude flagged (D18), even if
      // this card had not seen the verdict yet: ask, then re-POST.
      if (!r.ok && r.reason === "confirm") {
        setAsk("try");
        return { ok: true };
      }
      return r;
    });
  const onTry = () => (flagged ? setAsk("try") : void startTry(false));

  const age = timeAgo(Date.parse(item.created_at) / 1000);

  return (
    <article className="contrib-inbox-card" aria-labelledby={`contrib-pr-${item.number}`}>
      <header className="contrib-inbox-head">
        <div className="contrib-inbox-who">
          <span className="contrib-inbox-author">{who}</span>
          {!item.from_fused && (
            <Badge variant="outline" className="h-4.5 px-1.5 text-[10.5px] font-medium">
              from GitHub
            </Badge>
          )}
          {age && <span className="contrib-inbox-age">{age}</span>}
          {stale && (
            <Badge variant="outline" className="contrib-inbox-stale h-4.5 px-1.5 text-[10.5px] font-medium">
              No longer fits
            </Badge>
          )}
        </div>
        <QuietAction href={item.url}>
          View on GitHub
          <ExternalLink aria-hidden="true" />
        </QuietAction>
      </header>
      <h3 id={`contrib-pr-${item.number}`} className="contrib-inbox-title">
        {item.title}
      </h3>
      <Provenance item={item} who={who} />

      <ReviewBlock
        item={item}
        busy={busy === "review"}
        onRetry={() => void act("review", () => reviewProposal(dir, item.number))}
      />

      {/* Claude's merge of this proposal onto the app's latest: its clock,
          then Keep (sent onto the sender's proposal by the server) or
          Discard. While it stands, it is the card's one way forward. */}
      {mergeShown(item.merge_job, true) ? (
        <MergeReview dir={dir} job={item.merge_job} disabled={!!busy} refresh={refresh} />
      ) : stale && (
        <div className="contrib-stale" role="status">
          <span>
            {slug} changed since {who} sent this, so it no longer fits as it is.
          </span>
          <div className="contrib-try-actions">
            <Button size="sm" variant="outline" disabled={!!busy} onClick={() => setAsk("ask-update")}>
              Ask {who} to update
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={!!busy}
              onClick={() =>
                void act("merge", async () => {
                  // A merge job: its progress and review are the server's
                  // to show on the next read, never a sentence of ours.
                  const r = await startInboxMerge(dir, item.number);
                  if (r.ok && r.message) notify({ title: r.message, tone: "info" });
                  return r;
                })
              }
            >
              {busy === "merge" ? (
                <Loader2 data-icon="inline-start" className="animate-spin" />
              ) : (
                <Sparkles data-icon="inline-start" />
              )}
              Merge with Claude
            </Button>
          </div>
        </div>
      )}

      {tryCopy && (
        <div className="contrib-try" role="status">
          <span>Trying in a separate copy — your app is untouched.</span>
          <div className="contrib-try-actions">
            <Button size="sm" variant="outline" onClick={() => window.open(tryCopy.url, "_blank", "noopener")}>
              Open the copy
              <ExternalLink data-icon="inline-end" />
            </Button>
            <QuietAction
              onClick={() =>
                void act("throw", async () => {
                  const r = await removeTry(dir, item.number);
                  if (r.ok) setMadeTry(null);
                  return r;
                })
              }
              disabled={!!busy}
            >
              <Trash2 aria-hidden="true" />
              {busy === "throw" ? "Throwing away…" : "Throw away"}
            </QuietAction>
          </div>
        </div>
      )}

      {/* Said once: while a note sheet is up it shows the error itself. */}
      {error && ask !== "decline" && ask !== "ask-update" && (
        <p className="contrib-error" role="alert">
          {error}
        </p>
      )}

      <footer className="contrib-inbox-actions">
        <Button
          variant="outline"
          disabled={!!busy || !review || !!tryCopy}
          onClick={onTry}
          title={reading ? "Available once Claude has read it" : undefined}
        >
          {spin("try")}
          Try
        </Button>
        <span className="contrib-inbox-spacer" />
        <Button variant="outline" disabled={!!busy} onClick={() => setAsk("decline")}>
          Decline
        </Button>
        <Button disabled={!!busy || !review} onClick={() => setAsk("accept")}>
          {spin("accept")}
          {busy === "accept" ? "Accepting…" : "Accept"}
        </Button>
      </footer>

      <AlertDialog open={ask === "try"} onOpenChange={(o) => !o && setAsk(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Try a proposal that needs a look?</AlertDialogTitle>
            <AlertDialogDescription>
              Claude found something worth checking in {possessive(who)} changes. This copy runs on your
              computer with your GitHub access — your app stays as it is, but the code inside it can do what
              you can.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Not now</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                setAsk(null);
                void startTry(true);
              }}
            >
              Try anyway
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={ask === "accept"} onOpenChange={(o) => !o && setAsk(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Accept {possessive(who)} proposal?</AlertDialogTitle>
            <AlertDialogDescription>
              {possessive(who)} changes go into {slug}, and everyone who follows it gets them.
              {flagged || mustConfirm
                ? " Claude flagged something in it that needs a look. Accept only if you have read what it found and are fine with it."
                : ""}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                setAsk(null);
                const confirmLook = flagged || mustConfirm;
                void act("accept", async () => {
                  const r = await runJob(acceptProposal(dir, item.number, confirmLook), { signal: signal() });
                  if (r.ok) {
                    setMustConfirm(false);
                    notify({ title: okMessage(r, `Accepted. ${possessive(who)} change is in ${slug}.`), tone: "info" });
                    onSettled();
                  } else if (r.reason === "confirm" && !confirmLook) {
                    // Ask — once — with the flagged wording; no error line.
                    setMustConfirm(true);
                    setAsk("accept");
                    return { ok: true };
                  }
                  return r;
                });
              }}
            >
              Accept
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {ask === "decline" && (
        <ContribDialog
          open
          onClose={() => setAsk(null)}
          locked={busy === "decline"}
          title={`Decline ${possessive(who)} proposal`}
          description={`The proposal closes. ${who} still has the changes and can keep using them.`}
          footer={
            <>
              <Button variant="outline" onClick={() => setAsk(null)} disabled={busy === "decline"}>
                Cancel
              </Button>
              <Button
                variant="destructive"
                disabled={busy === "decline"}
                onClick={async () => {
                  let answer: unknown = null;
                  const ok = await act("decline", async () => {
                    const r = await declineProposal(dir, item.number, note.trim());
                    answer = r;
                    return r;
                  });
                  if (ok) {
                    setAsk(null);
                    setError(null);
                    notify({ title: okMessage(answer, `Declined. ${who} has been told.`), tone: "info" });
                    onSettled();
                  }
                }}
              >
                {spin("decline")}
                Decline
              </Button>
            </>
          }
        >
          <Field>
            <FieldLabel htmlFor={`contrib-decline-${item.number}`}>
              Note to {who} <span className="font-normal text-muted-foreground">(optional)</span>
            </FieldLabel>
            <Textarea
              id={`contrib-decline-${item.number}`}
              value={note}
              rows={3}
              className="min-h-[76px] resize-none"
              placeholder="Thanks for this — here's why it doesn't fit…"
              onChange={(e) => setNote(e.target.value)}
            />
          </Field>
          {error && (
            <p className="m-0 text-[13px] leading-5 text-destructive" role="alert">
              {error}
            </p>
          )}
        </ContribDialog>
      )}
      {ask === "ask-update" && (
        <ContribDialog
          open
          onClose={() => setAsk(null)}
          locked={busy === "ask-update"}
          title={`Ask ${who} to update`}
          description={`${who} sees your note on the proposal, and a way to bring it up to date with ${slug}.`}
          footer={
            <>
              <Button variant="outline" onClick={() => setAsk(null)} disabled={busy === "ask-update"}>
                Cancel
              </Button>
              <Button
                disabled={busy === "ask-update"}
                onClick={async () => {
                  let answer: unknown = null;
                  const ok = await act("ask-update", async () => {
                    const r = await askToUpdate(dir, item.number, askNote.trim());
                    answer = r;
                    return r;
                  });
                  if (ok) {
                    setAsk(null);
                    setError(null);
                    setAskNote("");
                    notify({ title: okMessage(answer, `Asked ${who} to update the proposal.`), tone: "info" });
                  }
                }}
              >
                {spin("ask-update")}
                Ask to update
              </Button>
            </>
          }
        >
          <Field>
            <FieldLabel htmlFor={`contrib-ask-${item.number}`}>
              Note to {who} <span className="font-normal text-muted-foreground">(optional)</span>
            </FieldLabel>
            <Textarea
              id={`contrib-ask-${item.number}`}
              value={askNote}
              rows={3}
              className="min-h-[76px] resize-none"
              placeholder="The app changed since you sent this. Could you bring it up to date?"
              onChange={(e) => setAskNote(e.target.value)}
            />
          </Field>
          {error && (
            <p className="m-0 text-[13px] leading-5 text-destructive" role="alert">
              {error}
            </p>
          )}
        </ContribDialog>
      )}
    </article>
  );
}

/** What the proposal says it does, with whose words they are. Claude's
 *  read (the safety review's summary) is labelled as Claude's; whatever the
 *  sender wrote — the description, or a summary that came with the proposal —
 *  is labelled "In bob's words", so a sender's text can never pass for
 *  Claude's judgement. The review block below carries Claude's verdict. */
function Provenance({ item, who }: { item: InboxItem; who: string }) {
  const claudes = item.summary_source === "review";
  const theirs = (item.description || (!claudes ? item.summary : "") || "").trim();
  // Claude's summary already shows inside a ready review: once is enough.
  const showClaude = claudes && !!item.summary && item.review?.summary !== item.summary;
  if (!showClaude && !theirs) return null;
  return (
    <div className="contrib-provenance">
      {showClaude && (
        <p className="contrib-inbox-summary">
          <span className="contrib-provenance-label">Claude&rsquo;s summary</span> {item.summary}
        </p>
      )}
      {theirs && (
        <div className="contrib-provenance-theirs">
          <span className="contrib-provenance-label">In {possessive(who)} words</span>
          <ClampedText text={theirs} />
        </div>
      )}
    </div>
  );
}

/** Text clamped to four lines, with More/Less when it runs longer. */
function ClampedText({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const long = text.length > 280 || text.split("\n").length > 4;
  return (
    <>
      <p className={"contrib-clamp" + (open ? " is-open" : "")}>{text}</p>
      {long && <QuietAction onClick={() => setOpen((o) => !o)}>{open ? "Less" : "More"}</QuietAction>}
    </>
  );
}

function ReviewBlock({ item, busy, onRetry }: { item: InboxItem; busy: boolean; onRetry: () => void }) {
  // "none": no review started yet — brief, since opening the page starts
  // one. A still mark (nothing is running yet), said as what WILL happen.
  if (item.review_status === "none") {
    return (
      <div className="contrib-review is-reading" role="status">
        <ClaudeMark />
        <span>Claude will read this proposal before anything runs.</span>
      </div>
    );
  }
  if (item.review_status === "pending" || item.review_status === "running") {
    return (
      <div className="contrib-review is-reading" role="status">
        <ClaudeMark className="contrib-claude-working" />
        <span>Claude is reading this proposal before anything runs…</span>
      </div>
    );
  }
  if (item.review_status === "failed" || !item.review) {
    return (
      <div className="contrib-review is-look" role="status">
        <ShieldAlert aria-hidden="true" />
        <div className="contrib-review-body">
          <p className="contrib-review-verdict">Claude couldn&rsquo;t finish reading this one.</p>
          <QuietAction onClick={onRetry} disabled={busy}>
            {busy ? "Reading again…" : "Read it again"}
          </QuietAction>
        </div>
      </div>
    );
  }
  const r = item.review;
  if (r.verdict === "fine") {
    return (
      <div className="contrib-review is-fine">
        <ShieldCheck aria-hidden="true" />
        <div className="contrib-review-body">
          <p className="contrib-review-verdict">Looks fine</p>
          {r.summary && <p className="contrib-review-summary">{r.summary}</p>}
        </div>
      </div>
    );
  }
  return (
    <div className="contrib-review is-look">
      <ShieldAlert aria-hidden="true" />
      <div className="contrib-review-body">
        <p className="contrib-review-verdict">Needs a look</p>
        {r.summary && <p className="contrib-review-summary">{r.summary}</p>}
        {r.risks.length > 0 && (
          <ul className="contrib-risks">
            {r.risks.map((risk, i) => (
              <li key={i}>
                {risk.detail}
                {risk.file && <code className="contrib-risk-file">{risk.file}</code>}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

