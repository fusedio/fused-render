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
// The list is the `inbox` field of `/api/contrib/app` — the page's one
// subscription, which polls fast while any review is still reading — so this
// tab owns no fetch of its own; it asks for a re-read after every action.
import { useState } from "react";
import { ExternalLink, Inbox, Loader2, ShieldAlert, ShieldCheck, Trash2 } from "lucide-react";
import {
  acceptProposal,
  declineProposal,
  plainMessage,
  removeTry,
  reviewProposal,
  runJob,
  tryProposal,
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
import { QuietAction } from "./StatusStrip";
import { plural, possessive } from "./words";

export function ProposalsTab({ dir, slug, contrib }: { dir: string; slug: string; contrib: AppContribState }) {
  const inbox = contrib.data?.inbox ?? null;
  if (!inbox) {
    // Still asking: sized like one card so the first answer does not jump.
    return (
      <div className="contrib-inbox" aria-busy="true">
        <Skeleton className="h-[148px] w-full rounded-[10px]" />
      </div>
    );
  }
  if (inbox.items.length === 0) {
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
        {plural(inbox.items.length, "proposal")} for {slug}
      </p>
      <ul className="contrib-inbox-list">
        {inbox.items.map((item) => (
          <li key={item.number}>
            <InboxCard dir={dir} slug={slug} item={item} refresh={contrib.refresh} />
          </li>
        ))}
      </ul>
    </div>
  );
}

type Ask = null | "try" | "accept" | "decline";

function InboxCard({
  dir,
  slug,
  item,
  refresh,
}: {
  dir: string;
  slug: string;
  item: InboxItem;
  refresh: () => Promise<void>;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ask, setAsk] = useState<Ask>(null);
  const [note, setNote] = useState("");

  const review = item.review_status === "ready" ? item.review : null;
  const reading = item.review_status === "pending" || item.review_status === "running";
  const flagged = review?.verdict === "look";
  const who = item.author;

  const act = async (key: string, fn: () => Promise<ContribAnswer | { ok: boolean; message?: string }>) => {
    if (busy) return false;
    setBusy(key);
    setError(null);
    const r = await fn();
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
        </div>
        <QuietAction href={item.url}>
          View on GitHub
          <ExternalLink aria-hidden="true" />
        </QuietAction>
      </header>
      <h3 id={`contrib-pr-${item.number}`} className="contrib-inbox-title">
        {item.title}
      </h3>
      {item.summary && <p className="contrib-inbox-summary">{item.summary}</p>}

      <ReviewBlock
        item={item}
        busy={busy === "review"}
        onRetry={() => void act("review", () => reviewProposal(dir, item.number))}
      />

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

      {error && (
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
              Claude found something worth checking in {possessive(who)} changes. Trying runs them on this Mac,
              in a separate copy — your app stays as it is.
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
              {flagged ? " Claude flagged something in it — make sure you've looked." : ""}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                setAsk(null);
                void act("accept", async () => {
                  const r = await runJob(acceptProposal(dir, item.number));
                  if (r.ok) notify({ title: `Accepted. ${possessive(who)} change is in ${slug}.`, tone: "info" });
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
                  const ok = await act("decline", () => declineProposal(dir, item.number, note.trim()));
                  if (ok) {
                    setAsk(null);
                    notify({ title: `Declined. ${who} has been told.`, tone: "info" });
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
    </article>
  );
}

function ReviewBlock({ item, busy, onRetry }: { item: InboxItem; busy: boolean; onRetry: () => void }) {
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

