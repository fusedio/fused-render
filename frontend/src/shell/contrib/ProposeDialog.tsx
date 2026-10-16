// "Propose to alice" (handoff A2): send your changes to the author for review.
//
//   sign-in  only when GitHub is not signed in — the shared sign-in step
//            (platform/ui/GitHubSignIn), inline, then straight on.
//   form     a title and a description Claude drafts from your conversation
//            (skeletons sized like the fields while it writes), both editable,
//            plus an optional note to the author. The draft NEVER gates
//            sending: "Write it myself" drops the skeletons at once, a failed
//            draft says so quietly and leaves the fields empty, and a draft
//            that lands late fills only the fields the user has not touched.
//   sending  the job's steps in plain words; the sheet will not close.
//   sent     a check, one sentence, the link to the proposal.
//
// Mounted per open by the caller (it renders this only while open), so every
// open starts with a fresh draft and a fresh form.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Check, ExternalLink, Loader2 } from "lucide-react";
import {
  runJob,
  sendProposal,
  startProposalDraft,
  type ProposalDraft,
  type SentProposal,
} from "@platform/lib/contrib";
import { Button } from "@platform/shadcn/ui/button";
import { Field, FieldGroup, FieldLabel } from "@platform/shadcn/ui/field";
import { Input } from "@platform/shadcn/ui/input";
import { Skeleton } from "@platform/shadcn/ui/skeleton";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { GitHubSignIn } from "@platform/ui/GitHubSignIn";
import { ContribDialog } from "./ContribDialog";
import { JobProgress } from "./JobProgress";
import { QuietAction } from "./StatusStrip";
import { possessive, SEND_STEPS } from "./words";

type Phase = "signin" | "form" | "sending" | "sent";
type Draft = "drafting" | "ready" | "failed" | "own";

export function ProposeDialog({
  dir,
  author,
  signedIn,
  again,
  onClose,
  onSent,
}: {
  dir: string;
  author: string;
  signedIn: boolean;
  /** "Propose again" after a decline — same sheet, its own title. */
  again?: boolean;
  onClose: () => void;
  /** After a successful send, so the page re-reads its state. */
  onSent: () => void;
}) {
  const [phase, setPhase] = useState<Phase>(signedIn ? "form" : "signin");
  const [draft, setDraft] = useState<Draft>("drafting");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [note, setNote] = useState("");
  const touched = useRef({ title: false, description: false });
  const [step, setStep] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState<SentProposal | null>(null);
  const live = useRef(true);
  useEffect(
    () => () => {
      live.current = false;
    },
    [],
  );

  // The draft starts as soon as the form is reachable — behind the sign-in
  // step it waits, since drafting needs no GitHub but the user may never get
  // past signing in.
  const drafting = useRef(false);
  useEffect(() => {
    if (phase !== "form" || drafting.current) return;
    drafting.current = true;
    void runJob<ProposalDraft>(startProposalDraft(dir)).then((r) => {
      if (!live.current) return;
      if (!r.ok || !r.result) {
        setDraft((d) => (d === "own" ? d : "failed"));
        return;
      }
      const { title: t, description: d } = r.result;
      if (!touched.current.title && t) setTitle(t);
      if (!touched.current.description && d) setDescription(d);
      setDraft((cur) => (cur === "own" ? cur : "ready"));
    });
  }, [phase, dir]);

  const showSkeleton = draft === "drafting";
  const canSend = phase === "form" && title.trim().length > 0;

  const send = async () => {
    if (!canSend) return;
    setPhase("sending");
    setError(null);
    setStep(null);
    const r = await runJob<SentProposal>(
      sendProposal(dir, { title: title.trim(), description: description.trim(), note: note.trim() }),
      { onTick: (j) => live.current && setStep(j.step ?? null) },
    );
    if (!live.current) return;
    if (r.ok) {
      setSent(r.result ?? {});
      setPhase("sent");
      onSent();
      return;
    }
    if (r.reason === "signed-out") {
      setPhase("signin");
      return;
    }
    setError(r.message);
    setPhase("form");
  };

  const sheetTitle = again ? `Propose again to ${author}` : `Propose to ${author}`;
  const description_ =
    phase === "sent"
      ? `${author} can review it now.`
      : `${author} gets your changes to review. Nothing changes in ${author}'s app unless ${author} accepts them.`;

  let body: ReactNode;
  let footer: ReactNode = null;
  if (phase === "signin") {
    body = (
      <GitHubSignIn prompt="Sign in to GitHub to send your proposal." onSignedIn={() => setPhase("form")} onCancel={onClose} />
    );
  } else if (phase === "sent") {
    body = (
      <div className="contrib-sent">
        <span className="contrib-sent-mark" aria-hidden="true">
          <Check />
        </span>
        <div className="flex min-w-0 flex-col gap-1">
          <p className="m-0 text-sm font-medium">Sent to {author}.</p>
          <p className="m-0 text-[13px] leading-5 text-muted-foreground">
            This page shows {possessive(author)} answer as soon as there is one.
          </p>
        </div>
      </div>
    );
    footer = (
      <>
        {sent?.url && (
          <Button variant="outline" render={<a href={sent.url} target="_blank" rel="noopener noreferrer" />}>
            View on GitHub
            <ExternalLink data-icon="inline-end" />
          </Button>
        )}
        <Button onClick={onClose}>Done</Button>
      </>
    );
  } else {
    const sending = phase === "sending";
    body = sending ? (
      <JobProgress steps={SEND_STEPS} current={step} />
    ) : (
      <>
        <FieldGroup className="gap-4">
          <Field>
            <FieldLabel htmlFor="contrib-propose-title">Title</FieldLabel>
            {showSkeleton ? (
              <Skeleton className="h-8 w-full" aria-hidden="true" />
            ) : (
              <Input
                id="contrib-propose-title"
                value={title}
                maxLength={120}
                placeholder="What your changes do, in a few words"
                onChange={(e) => {
                  touched.current.title = true;
                  setTitle(e.target.value);
                }}
              />
            )}
          </Field>
          <Field>
            <FieldLabel htmlFor="contrib-propose-description">Description</FieldLabel>
            {showSkeleton ? (
              <Skeleton className="h-[116px] w-full" aria-hidden="true" />
            ) : (
              <Textarea
                id="contrib-propose-description"
                value={description}
                rows={5}
                className="max-h-56 min-h-[116px] resize-none overflow-y-auto"
                placeholder={`What changed and why, for ${author}`}
                onChange={(e) => {
                  touched.current.description = true;
                  setDescription(e.target.value);
                }}
              />
            )}
          </Field>
          {showSkeleton && (
            <p className="contrib-drafting" role="status">
              <Loader2 className="animate-spin" aria-hidden="true" />
              <span>Claude is writing a description from your conversation…</span>
              <QuietAction
                onClick={() => {
                  // Choosing to write it is touching both: a late draft must
                  // not drop text into fields the user is about to fill.
                  touched.current = { title: true, description: true };
                  setDraft("own");
                }}
              >
                Write it myself
              </QuietAction>
            </p>
          )}
          {draft === "failed" && (
            <p className="m-0 text-[13px] leading-5 text-muted-foreground" role="status">
              Claude couldn&rsquo;t draft this one. Write it your way.
            </p>
          )}
          <Field>
            <FieldLabel htmlFor="contrib-propose-note">
              Add a note to {author} <span className="font-normal text-muted-foreground">(optional)</span>
            </FieldLabel>
            <Textarea
              id="contrib-propose-note"
              value={note}
              rows={2}
              className="min-h-[60px] resize-none"
              onChange={(e) => setNote(e.target.value)}
            />
          </Field>
        </FieldGroup>
        {error && (
          <p className="m-0 text-[13px] leading-5 text-destructive" role="alert">
            {error}
          </p>
        )}
      </>
    );
    footer = (
      <>
        <Button variant="outline" onClick={onClose} disabled={sending}>
          Cancel
        </Button>
        <Button onClick={() => void send()} disabled={!canSend}>
          {sending && <Loader2 data-icon="inline-start" className="animate-spin" />}
          {sending ? "Sending…" : "Send proposal"}
        </Button>
      </>
    );
  }

  return (
    <ContribDialog
      open
      onClose={onClose}
      locked={phase === "sending"}
      title={sheetTitle}
      description={description_}
      footer={footer}
    >
      {body}
    </ContribDialog>
  );
}
