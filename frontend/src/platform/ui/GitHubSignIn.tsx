// Signing in to GitHub, as one inline step another surface drops into its own
// flow — the Propose dialog's "Sign in to GitHub to send your proposal"
// (docs/CONTRIB_LOOP.md §13) is the first. Renders one block, no dialog
// chrome of its own, and calls `onSignedIn(login)` once GitHub says yes.
//
// Over the server's existing routes (server/routers/github.py), the same ones
// the git template's Publish modal drives:
//
//   GET  /api/github/status   → {found, signed_in, account}  the truth
//   POST /api/github/install  → start setting the helper up  (only when !found)
//   GET  /api/github/install  → {state: idle|running|done|error, started_at}
//   POST /api/github/login    → start the browser sign-in
//   GET  /api/github/login    → {in_flight, started_at, error, code, url}
//   POST /api/github/login/cancel
//
// THE STEPS, each one sentence and one button (handoff principle 5):
//   checking → set up (helper missing) → sign in → waiting for GitHub (the
//   one-time code, Copy code, reopen the page) → signed in. An opening read
//   that fails is its own step ("Couldn't check GitHub just now." + Try
//   again), never a "Checking…" left up for good.
// No CLI, gh or terminal words reach the reader; the server's own error text
// does not either (it is gh's), only a calm sentence and Try again.
//
// THE BASELINE. Install and login are single records shared by the whole
// app, and a settled one keeps its `error` until the next attempt — so a
// failure from an attempt made an hour ago in another surface must not paint
// here. Like the template's `ghOwnRecord`: the `started_at` each record had
// when this mounted is remembered, and a settled record with that same stamp
// counts as never having happened. A record still RUNNING is always adopted
// (a sign-in already waiting in the browser is this sign-in too).
//
// Success is `/api/github/status` saying `signed_in`, never the login record
// alone — that record deliberately carries no verdict (github_login._record).
// The login worker refreshes the status cache before it settles, so one read
// is normally enough; one forced refresh covers a stale cache.
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Check, Copy, ExternalLink, Loader2 } from "lucide-react";
import { getJson, postJson, type HttpError } from "@platform/lib/api";
import { copyToClipboard } from "@platform/lib/clipboard";
import { cn } from "@platform/lib/utils";
import { Button } from "@platform/shadcn/ui/button";
import { Skeleton } from "@platform/shadcn/ui/skeleton";

const POLL_MS = 1500;
const SIGNUP_URL = "https://github.com/signup";
const DEVICE_URL = "https://github.com/login/device";

interface GithubStatus {
  found: boolean;
  signed_in: boolean;
  account: string | null;
}

interface InstallRecord {
  state: "idle" | "running" | "done" | "error";
  started_at: number | null;
  error?: string | null;
}

interface LoginRecord {
  in_flight: boolean;
  started_at: number | null;
  error: string | null;
  /** The one-time code GitHub's page asks for — absent on servers that
   *  predate it, then the step falls back to "finish in the browser". */
  code?: string | null;
  url?: string | null;
}

type Step =
  | { kind: "checking" }
  | { kind: "unreachable" }
  | { kind: "setup"; failed: boolean }
  | { kind: "installing" }
  | { kind: "signin"; failed: boolean }
  | { kind: "waiting"; code: string | null; url: string }
  | { kind: "done"; login: string | null };

export function GitHubSignIn({
  onSignedIn,
  onCancel,
  prompt = "Sign in to GitHub to continue.",
  recheck = false,
  className,
}: {
  /** GitHub said yes. `login` can be null: the account name is read off
   *  GitHub's own status line and an unrecognised one leaves it unnamed.
   *  Called on MOUNT too when this computer is already signed in, so a
   *  caller may render the step unconditionally and simply advance. */
  onSignedIn: (login: string | null) => void;
  /** Shown as a Cancel button beside the step's action when given. Cancel
   *  while waiting on GitHub stops that sign-in server-side AND calls this
   *  (the waiting step always offers Cancel, given or not). */
  onCancel?: () => void;
  /** The sign-in step's one sentence — why the surface needs GitHub. */
  prompt?: string;
  /** The caller has just been told GitHub refused this computer (a job that
   *  came back "signed out"), so the status CACHE — up to a minute old — is
   *  exactly what must not decide: the opening read re-probes GitHub
   *  (POST /api/github/status/refresh) and only that fresh answer may
   *  auto-advance. Later reads use the cache, which the sign-in worker
   *  refreshes itself before it settles. */
  recheck?: boolean;
  className?: string;
}) {
  const [step, setStep] = useState<Step>({ kind: "checking" });
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const baseline = useRef<{ install: number | null; login: number | null } | null>(null);
  const pollRef = useRef<number | null>(null);
  const goneRef = useRef(false);
  const readOnceRef = useRef(false);
  // Kept current so a poll tick's late answer reports to the caller's latest
  // callback, not the one this effect closed over.
  const onSignedInRef = useRef(onSignedIn);
  onSignedInRef.current = onSignedIn;

  const stopPolling = () => {
    if (pollRef.current !== null) window.clearInterval(pollRef.current);
    pollRef.current = null;
  };
  // Through a ref so `tick` can start the poll that calls it.
  const tickRef = useRef<() => Promise<void>>(async () => {});
  const startPolling = () => {
    if (pollRef.current !== null) return;
    pollRef.current = window.setInterval(() => void tickRef.current(), POLL_MS);
  };

  const finish = useCallback((s: GithubStatus) => {
    stopPolling();
    setStep({ kind: "done", login: s.account });
    onSignedInRef.current(s.account);
  }, []);

  // A settled record counts only when its stamp moved past the baseline.
  const own = <T extends { started_at: number | null }>(
    rec: T,
    which: "install" | "login",
    running: boolean,
  ): T | null => {
    if (running) return rec;
    return baseline.current && rec.started_at === baseline.current[which] ? null : rec;
  };

  // One read of the three records, folded into a step. Drives both the
  // opening read and every poll tick.
  const tick = useCallback(async () => {
    let status: GithubStatus;
    let install: InstallRecord;
    let login: LoginRecord;
    // Until one read has succeeded, the step has nothing true to show — so
    // that read is the one `recheck` makes fresh, and the one whose failure
    // must not leave "Checking GitHub…" up forever.
    const opening = !readOnceRef.current;
    try {
      [status, install, login] = await Promise.all([
        opening && recheck
          ? postJson<GithubStatus>("/api/github/status/refresh", {})
          : getJson<GithubStatus>("/api/github/status"),
        getJson<InstallRecord>("/api/github/install"),
        getJson<LoginRecord>("/api/github/login"),
      ]);
    } catch {
      if (goneRef.current) return;
      // A poll hiccup keeps the last step and the next tick tries again; an
      // opening read that failed says so, with Try again.
      if (opening) {
        stopPolling();
        setStep({ kind: "unreachable" });
      }
      return;
    }
    if (goneRef.current) return;
    readOnceRef.current = true;
    if (baseline.current === null) {
      baseline.current = { install: install.started_at, login: login.started_at };
    }
    if (status.signed_in) return finish(status);

    if (!status.found) {
      const rec = own(install, "install", install.state === "running");
      if (rec?.state === "running") {
        // Watched until it lands, whoever started it.
        startPolling();
        return setStep({ kind: "installing" });
      }
      stopPolling();
      return setStep({ kind: "setup", failed: rec?.state === "error" });
    }

    const rec = own(login, "login", login.in_flight);
    if (rec?.in_flight) {
      startPolling();
      return setStep({
        kind: "waiting",
        code: rec.code ?? null,
        url: rec.url || DEVICE_URL,
      });
    }
    if (rec && !rec.error) {
      // Settled without an error: the worker's own re-probe should already
      // have flipped the status cache. If this read still says signed out,
      // ask once more, freshly, before calling it a failure.
      stopPolling();
      try {
        const fresh = await postJson<GithubStatus>("/api/github/status/refresh", {});
        if (goneRef.current) return;
        if (fresh.signed_in) return finish(fresh);
      } catch {
        // fall through to the failure sentence
      }
      if (!goneRef.current) setStep({ kind: "signin", failed: true });
      return;
    }
    stopPolling();
    // A settled error of ours (timed out, declined in the browser) — or
    // nothing of ours yet. (A cancel from this step never lands here: it
    // stops the poll before it asks the server to stop.)
    setStep({ kind: "signin", failed: !!rec?.error });
  }, [finish, recheck]);
  tickRef.current = tick;

  useEffect(() => {
    goneRef.current = false;
    // Mount, or `recheck` turning on under a mounted step: either way the
    // next read is an opening one again.
    readOnceRef.current = false;
    void tick();
    return () => {
      goneRef.current = true;
      stopPolling();
    };
  }, [tick]);

  const install = async () => {
    if (busy) return;
    setBusy(true);
    try {
      await postJson("/api/github/install", {});
    } catch {
      // A 409 is "already running" — the poll below finds it either way.
    } finally {
      setBusy(false);
    }
    setStep({ kind: "installing" });
    startPolling();
  };

  const signIn = async () => {
    if (busy) return;
    setBusy(true);
    let opened: LoginRecord | null = null;
    try {
      opened = await postJson<LoginRecord>("/api/github/login", {});
    } catch (e) {
      setBusy(false);
      if (goneRef.current) return;
      // A 409 is a sign-in already waiting in the browser — adopt it below.
      // Anything else never started one: say so rather than "waiting".
      if ((e as HttpError).status !== 409) {
        setStep({ kind: "signin", failed: true });
        return;
      }
    }
    setBusy(false);
    if (goneRef.current) return;
    setStep({ kind: "waiting", code: opened?.code ?? null, url: opened?.url || DEVICE_URL });
    startPolling();
  };

  const cancel = async () => {
    if (step.kind === "waiting") {
      stopPolling();
      try {
        await postJson("/api/github/login/cancel", {});
      } catch {
        // Idempotent server-side; nothing to tell the reader.
      }
    }
    onCancel?.();
    if (!goneRef.current && step.kind === "waiting") setStep({ kind: "signin", failed: false });
  };

  const copyCode = async () => {
    if (step.kind !== "waiting" || !step.code) return;
    if (await copyToClipboard(step.code)) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    }
  };

  const cancelButton = onCancel && (
    <Button size="sm" variant="outline" onClick={() => void cancel()}>
      Cancel
    </Button>
  );

  let sentence: string;
  let body: ReactNode = null;
  let actions: ReactNode = null;
  switch (step.kind) {
    case "checking":
      sentence = "Checking GitHub…";
      actions = <Skeleton className="h-8 w-36" />;
      break;
    case "unreachable":
      sentence = "Couldn't check GitHub just now.";
      actions = (
        <>
          {cancelButton}
          <Button
            size="sm"
            onClick={() => {
              setStep({ kind: "checking" });
              void tick();
            }}
          >
            Try again
          </Button>
        </>
      );
      break;
    case "setup":
      sentence = step.failed
        ? "Couldn't get GitHub ready on this computer. Check your connection and try again."
        : "Fused needs to set up GitHub on this computer first. It takes a few seconds.";
      actions = (
        <>
          {cancelButton}
          <Button size="sm" disabled={busy} onClick={() => void install()}>
            {step.failed ? "Try again" : "Set up GitHub"}
          </Button>
        </>
      );
      break;
    case "installing":
      sentence = "Setting up GitHub…";
      actions = (
        <>
          {cancelButton}
          <Button size="sm" disabled>
            <Loader2 data-icon="inline-start" className="animate-spin" />
            Setting up…
          </Button>
        </>
      );
      break;
    case "signin":
      sentence = step.failed ? "Sign-in didn't finish. Try again when you're ready." : prompt;
      body = (
        <p className="m-0 text-xs leading-5 text-muted-foreground">
          No GitHub account?{" "}
          <a
            href={SIGNUP_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="text-foreground underline underline-offset-2"
          >
            Create one
          </a>
          , then come back here.
        </p>
      );
      actions = (
        <>
          {cancelButton}
          <Button size="sm" disabled={busy} onClick={() => void signIn()}>
            {busy && <Loader2 data-icon="inline-start" className="animate-spin" />}
            {step.failed ? "Try again" : "Sign in to GitHub"}
          </Button>
        </>
      );
      break;
    case "waiting":
      sentence = step.code
        ? "Paste this code on the GitHub page that just opened in your browser."
        : "Finish signing in on the GitHub page that just opened in your browser.";
      body = (
        <div className="flex flex-col gap-2.5">
          {step.code && (
            <div className="flex items-center gap-2">
              <output
                aria-label="One-time code"
                className="flex h-9 flex-1 select-all items-center justify-center rounded-lg border border-border bg-background font-mono text-[17px] font-semibold tracking-[0.18em] text-foreground"
              >
                {step.code}
              </output>
              <Button size="sm" variant="outline" className="h-9" onClick={() => void copyCode()}>
                {copied ? <Check data-icon="inline-start" /> : <Copy data-icon="inline-start" />}
                {copied ? "Copied" : "Copy code"}
              </Button>
            </div>
          )}
          <div className="flex items-center gap-2 text-xs leading-5 text-muted-foreground">
            <Loader2 className="size-3.5 animate-spin" aria-hidden />
            <span>Waiting for GitHub…</span>
            <a
              href={step.url}
              target="_blank"
              rel="noopener noreferrer"
              className="ml-auto inline-flex items-center gap-1 text-foreground underline-offset-2 hover:underline"
            >
              Open the GitHub page again
              <ExternalLink className="size-3" aria-hidden />
            </a>
          </div>
        </div>
      );
      // Cancel here also stops the waiting sign-in, so it shows whether or
      // not the caller gave an onCancel of its own.
      actions = (
        <Button size="sm" variant="outline" onClick={() => void cancel()}>
          Cancel
        </Button>
      );
      break;
    case "done":
      sentence = step.login ? `Signed in to GitHub as ${step.login}.` : "Signed in to GitHub.";
      break;
  }

  return (
    <section
      className={cn(
        "flex flex-col gap-3 rounded-xl border border-border bg-muted/30 p-4 text-card-foreground",
        className,
      )}
      aria-label="Sign in to GitHub"
    >
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <p
          className="m-0 min-w-0 flex-1 basis-56 text-[13px] leading-5 text-foreground"
          role="status"
          aria-live="polite"
        >
          {sentence}
        </p>
        {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
      </div>
      {body}
    </section>
  );
}

export default GitHubSignIn;
