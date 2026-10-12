import { useEffect, useState, type FormEvent, type ReactNode } from "react";
// The Updates step — an email for the Fused newsletter (new apps, what we
// ship next). OPTIONAL and NOT A STAGE: it never enters the progress meter
// (progress.ts counts stageIds() only), Next walks past it like any step, and
// the pill shows a tick once someone has subscribed. Both flavors.
//
// The server owns the send (shell/onboarding.py `_forward_newsletter`, where
// the provider URL plugs in) and the record (`newsletter` in the snapshot),
// so a reopened wizard says "subscribed" instead of asking twice. The field is
// prefilled with the global git `user.email` when there is one.
import { Check, Mail, Sparkles } from "lucide-react";

import { getNewsletter, subscribeNewsletter } from "@platform/lib/api";
import { bundleName } from "@platform/lib/flavor";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";

import { setProgress, useOnboardingState } from "./progress";
import { StepHeader } from "./StepHeader";

// Same loose shape the server checks (onboarding.py _EMAIL_RE).
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

export function NewsletterStep({
  eyebrow,
  onWork,
}: {
  eyebrow: ReactNode;
  onWork: (busy: boolean) => void;
}) {
  const subscribed = useOnboardingState()?.newsletter ?? null;
  const done = subscribed?.subscribed_at != null;

  const [email, setEmail] = useState("");
  const [touched, setTouched] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Prefill once, and only into an untouched, empty field.
  useEffect(() => {
    let live = true;
    getNewsletter().then(
      (n) => {
        if (!live) return;
        setEmail((cur) => cur || n.email || n.suggested || "");
      },
      () => undefined,
    );
    return () => {
      live = false;
    };
  }, []);

  const valid = EMAIL_RE.test(email.trim());
  // One yellow button: Subscribe owns it while there is an address to send.
  useEffect(() => onWork(valid && !done), [onWork, valid, done]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (!valid || sending) return;
    setSending(true);
    setError(null);
    subscribeNewsletter(email.trim())
      .then((s) => setProgress(s))
      .catch((err: Error) => setError(err.message || "Couldn't subscribe. Try again."))
      .finally(() => setSending(false));
  };

  return (
    <div className="flex flex-col gap-6">
      <StepHeader
        eyebrow={eyebrow}
        title="Get new apps as we build them"
        lead={`We keep shipping new ${bundleName()} apps and features. Leave your email and we'll send the occasional update — no spam, unsubscribe anytime. This step is optional.`}
      />

      <div className="flex flex-col gap-4 rounded-xl border border-border bg-card p-5">
        <div className="flex items-center gap-3">
          <span className="grid size-8 shrink-0 place-items-center rounded-md bg-muted">
            {done ? <Check className="size-4 text-emerald-600 dark:text-emerald-400" /> : <Mail className="size-4" />}
          </span>
          <div className="min-w-0">
            <div className="text-sm font-medium">Fused newsletter</div>
            <div className="text-xs text-muted-foreground">
              {done ? (
                <>
                  Subscribed as <span className="font-medium text-foreground">{subscribed?.email}</span>.
                </>
              ) : (
                "New apps, releases and tips, a few times a month at most."
              )}
            </div>
          </div>
        </div>

        {!done && (
          <form onSubmit={submit} className="flex flex-col gap-2 sm:flex-row sm:items-start" noValidate>
            <div className="flex min-w-0 flex-1 flex-col gap-1.5">
              <Input
                type="email"
                inputMode="email"
                autoComplete="email"
                placeholder="you@example.com"
                aria-label="Email address"
                value={email}
                onChange={(e) => {
                  setEmail(e.target.value);
                  setError(null);
                }}
                onBlur={() => setTouched(true)}
                aria-invalid={(touched && email !== "" && !valid) || !!error || undefined}
                disabled={sending}
              />
              {error ? (
                <p className="m-0 text-xs text-destructive">{error}</p>
              ) : touched && email !== "" && !valid ? (
                <p className="m-0 text-xs text-destructive">That doesn't look like an email address.</p>
              ) : null}
            </div>
            <Button type="submit" variant={valid ? "accent" : "outline"} disabled={!valid || sending}>
              {sending ? "Subscribing…" : "Subscribe"}
            </Button>
          </form>
        )}
      </div>

      <p className="m-0 flex items-center gap-2 text-xs text-muted-foreground">
        <Sparkles className="size-3.5 shrink-0" />
        {done ? "You're all set. Press Next to continue." : "Not now? Press Next to skip — nothing is sent."}
      </p>
    </div>
  );
}
