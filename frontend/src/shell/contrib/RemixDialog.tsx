// "Publish as remix" (handoff A5): publish your version as your own app,
// credited to the original. A name (prefilled at once with "<app>-remix", a
// better suggestion from Claude replaces it only if untouched) and the credit
// line for the readme, which Claude drafts and the user is invited to rewrite
// in their own voice. Publishing needs GitHub, so the sign-in step comes
// first when needed. Success hands over the remix's own share link.
//
// Same posture as ProposeDialog: the draft never gates publishing, the sheet
// will not close mid-publish, and it is mounted per open.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Check, Copy, Loader2 } from "lucide-react";
import {
  publishRemix,
  runJob,
  useUnmountSignal,
  startRemixDraft,
  type PublishedRemix,
  type RemixDraft,
} from "@platform/lib/contrib";
import { copyToClipboard } from "@platform/lib/clipboard";
import { Button } from "@platform/shadcn/ui/button";
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from "@platform/shadcn/ui/field";
import { Input } from "@platform/shadcn/ui/input";
import { Skeleton } from "@platform/shadcn/ui/skeleton";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { GitHubSignIn } from "@platform/ui/GitHubSignIn";
import { ContribDialog } from "./ContribDialog";
import { JobProgress } from "./JobProgress";
import { QuietAction } from "./StatusStrip";
import { possessive } from "./words";

type Phase = "signin" | "form" | "publishing" | "done";

const REMIX_STEPS = [
  { key: "create", label: "Making your app on GitHub…" },
  { key: "send", label: "Publishing your version…" },
];

// GitHub's own rule for a repository name.
const NAME_OK = /^[A-Za-z0-9._-]{1,100}$/;

export function RemixDialog({
  dir,
  slug,
  author,
  original,
  signedIn,
  onClose,
  onPublished,
}: {
  dir: string;
  slug: string;
  author: string;
  /** "alice/x" — what the remix credits. */
  original: string | null;
  signedIn: boolean;
  onClose: () => void;
  onPublished: () => void;
}) {
  const [phase, setPhase] = useState<Phase>(signedIn ? "form" : "signin");
  // Set when the SERVER said "signed out" mid-flight: the sign-in step then
  // runs with `recheck` (a cached "signed in" is the answer not to trust,
  // or it would advance at once and the retry refuse again, round and
  // round), and once signed in the same send runs again by itself.
  const [recheck, setRecheck] = useState(false);
  const [retry, setRetry] = useState(false);
  // At most ONE automatic retry per sheet: if GitHub's own check is ever
  // wrong, a second "signed out" waits for the user's press instead of
  // looping sign-in → retry → refusal.
  const retried = useRef(false);
  const signal = useUnmountSignal();
  const [name, setName] = useState(`${slug}-remix`);
  const [credit, setCredit] = useState("");
  const [drafting, setDrafting] = useState(true);
  const touched = useRef({ name: false, credit: false });
  const [step, setStep] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [link, setLink] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const live = useRef(true);
  useEffect(
    () => () => {
      live.current = false;
    },
    [],
  );

  const started = useRef(false);
  useEffect(() => {
    if (phase !== "form" || started.current) return;
    started.current = true;
    void runJob<RemixDraft>(startRemixDraft(dir), { signal: signal() }).then((r) => {
      // The user already chose to write it: a late draft changes nothing.
      if (!live.current || touched.current.credit) return;
      if (r.ok && r.result) {
        if (!touched.current.name && r.result.name) setName(r.result.name);
        if (!touched.current.credit && r.result.credit) setCredit(r.result.credit);
      } else if (!touched.current.credit && original) {
        // No draft: the plainest true credit, still the user's to rewrite.
        setCredit(`Remixed from [${original}](https://github.com/${original}) by ${author}.`);
      }
      setDrafting(false);
    });
  }, [phase, dir, original, author]);

  const nameOk = NAME_OK.test(name.trim());
  const canPublish = phase === "form" && nameOk && !drafting;

  // The retry after a mid-flight sign-in (see `recheck`): exactly once.
  useEffect(() => {
    if (phase !== "form" || !retry) return;
    setRetry(false);
    void publish();
  }, [phase, retry]);

  const publish = async () => {
    if (!canPublish) return;
    setPhase("publishing");
    setError(null);
    setStep(null);
    const r = await runJob<PublishedRemix>(publishRemix(dir, { name: name.trim(), credit: credit.trim() }), {
      signal: signal(),
      onTick: (j) => live.current && setStep(j.step ?? null),
    });
    if (!live.current) return;
    if (r.ok) {
      setLink(r.result?.share_url ?? r.result?.url ?? null);
      setPhase("done");
      onPublished();
      return;
    }
    if (r.reason === "aborted") return;
    // The first "signed out" runs the sign-in step and retries once. A
    // second one, after that retry, is not sent round again: the server's
    // own sentence is shown in the form, and pressing again is the user's.
    if (r.reason === "signed-out" && !retried.current) {
      retried.current = true;
      setRecheck(true);
      setRetry(true);
      setPhase("signin");
      return;
    }
    setError(r.message);
    setPhase("form");
  };

  const copy = async () => {
    if (!link) return;
    if (await copyToClipboard(link)) {
      setCopied(true);
      window.setTimeout(() => live.current && setCopied(false), 1500);
    }
  };

  let body: ReactNode;
  let footer: ReactNode = null;
  if (phase === "signin") {
    body = (
      <GitHubSignIn
        recheck={recheck}
        prompt="Sign in to GitHub to publish your remix."
        onSignedIn={() => setPhase("form")}
        onCancel={onClose}
      />
    );
  } else if (phase === "done") {
    body = (
      <>
        <div className="contrib-sent">
          <span className="contrib-sent-mark" aria-hidden="true">
            <Check />
          </span>
          <div className="flex min-w-0 flex-col gap-1">
            <p className="m-0 text-sm font-medium">Your remix is published.</p>
            <p className="m-0 text-[13px] leading-5 text-muted-foreground">
              It&rsquo;s your app now. Anyone with the link can install it.
            </p>
          </div>
        </div>
        {link && (
          <div className="flex items-center gap-1.5">
            <Input
              type="text"
              readOnly
              value={link}
              onFocus={(e) => e.currentTarget.select()}
              aria-label="Share link"
              className="h-8 flex-1 truncate bg-muted/40 font-mono text-[12.5px] text-foreground"
            />
            <Button variant="outline" onClick={() => void copy()}>
              {copied ? <Check data-icon="inline-start" /> : <Copy data-icon="inline-start" />}
              {copied ? "Copied" : "Copy link"}
            </Button>
          </div>
        )}
      </>
    );
    footer = <Button onClick={onClose}>Done</Button>;
  } else {
    const publishing = phase === "publishing";
    body = publishing ? (
      <JobProgress steps={REMIX_STEPS} current={step} />
    ) : (
      <>
        <FieldGroup className="gap-4">
          <Field data-invalid={!nameOk || undefined}>
            <FieldLabel htmlFor="contrib-remix-name">Name</FieldLabel>
            <Input
              id="contrib-remix-name"
              value={name}
              aria-invalid={!nameOk || undefined}
              onChange={(e) => {
                touched.current.name = true;
                setName(e.target.value);
              }}
            />
            {!nameOk && <FieldError>Use letters, numbers, dashes, dots or underscores.</FieldError>}
          </Field>
          <Field>
            <FieldLabel htmlFor="contrib-remix-credit">Credit line</FieldLabel>
            {drafting ? (
              <Skeleton className="h-[76px] w-full" aria-hidden="true" />
            ) : (
              <Textarea
                id="contrib-remix-credit"
                value={credit}
                rows={3}
                className="min-h-[76px] resize-none"
                onChange={(e) => {
                  touched.current.credit = true;
                  setCredit(e.target.value);
                }}
              />
            )}
            {drafting ? (
              <p className="contrib-drafting" role="status">
                <Loader2 className="animate-spin" aria-hidden="true" />
                <span>Claude is suggesting one…</span>
                <QuietAction
                  onClick={() => {
                    touched.current.credit = true;
                    setDrafting(false);
                  }}
                >
                  Write it myself
                </QuietAction>
              </p>
            ) : (
              <FieldDescription>
                Goes in your remix&rsquo;s readme. Write it your way — people appreciate a personal thanks.
              </FieldDescription>
            )}
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
        <Button variant="outline" onClick={onClose} disabled={publishing}>
          Cancel
        </Button>
        <Button onClick={() => void publish()} disabled={!canPublish}>
          {publishing && <Loader2 data-icon="inline-start" className="animate-spin" />}
          {publishing ? "Publishing…" : "Publish remix"}
        </Button>
      </>
    );
  }

  return (
    <ContribDialog
      open
      onClose={onClose}
      locked={phase === "publishing"}
      title="Publish as remix"
      description={
        phase === "done"
          ? `Credited as a remix of ${original ?? `${possessive(author)} app`}.`
          : `Your version becomes your own app on your GitHub, credited as a remix of ${original ?? `${possessive(author)} app`}. Any proposal you sent stays open.`
      }
      footer={footer}
    >
      {body}
    </ContribDialog>
  );
}
