import { useEffect, useState, type ReactNode } from "react";
// Step 2 — Tools. ONE CHECKLIST for the three things the app needs installed
// — Claude Code, git, Google Chrome — where there used to be a step each
// (ClaudeStep, GitStep, ChromeStep; the wizard was eight steps long and the
// owner asked for fewer). Three STAGES still (progress.ts: `claude`, `git`,
// `chrome` — the server's closed set, its `_observe` overruling git/chrome on
// every read, Home/Apps cards reading them one by one), one STEP: the
// wizard's pill combines them (OnboardingWizard toolsStatus) and `?step=`
// takes any of the three ids as an alias.
//
// Rows, each done (struck through, green check), open (amber dot, its button
// attached) or unknown (muted dash — never a gate; flow's wizard blocked Next
// on an unknown and had a state with no way through):
//
//   Claude Code  installed · version · signed in · PATH (optional)
//                Checklist, not the strip: the strip renders only what is
//                wrong, a setup step exists to say "installed ✓, signed in ✓".
//                Open rows get the strip's own IssueRow — same buttons, same
//                endpoints, same polls (lib/claude-setup). The STAGE is
//                optional (progress OPTIONAL_STAGES): the explorer, previews
//                and local models work without the CLI.
//   Git          macOS only, Render only. Every Mac ships /usr/bin/git, but
//                without the developer tools it is an xcode-select SHIM that
//                pops Apple's dialog when run, so the server (shell/git_tools)
//                never runs it: it walks where a REAL git lives (the Tools,
//                Xcode, Homebrew) on every read and writes the stage itself.
//                Install = POST git/install = `xcode-select --install`, which
//                opens Apple's Software Update dialog and returns at once;
//                the download happens outside this window. `brew install git`
//                is shown to copy, never run.
//   Chrome       Both flavors. Bots drive a real Chrome over CDP
//                (bots/browser.py); the server walks the bots' own candidate
//                list on every read (Chromium / Edge / Brave count). macOS
//                has no API to install a browser: a download link is the
//                whole affordance.
//
// Git and Chrome are the server's to observe, so this step WRITES only the
// Claude stage; the other two it reads, fresh on arrival and whenever the
// window comes back from an installer (focus — visibilitychange alone does
// not fire when a native window merely regains focus). One Check again
// re-runs the Claude probe and the server read together.
import { Check, Circle, Copy, Download, Minus, RefreshCw } from "lucide-react";

import { installGit, type ClaudeHealth, type GitInstallResult } from "@platform/lib/api";
import { claudeIssues, type ClaudeIssue } from "@platform/lib/claude-health";
import type { ClaudeSetup } from "@platform/lib/claude-setup";
import { bundleName, isBot } from "@platform/lib/flavor";
import { Button } from "@platform/shadcn/ui/button";
import { Skeleton } from "@platform/shadcn/ui/skeleton";
import { IssueRow } from "@platform/ui/ClaudeHealthStrip";

import { refreshProgress, reportStage, type Stages, type StageStatus } from "./progress";
import { StepHeader } from "./StepHeader";

type RowState = "done" | "open" | "unknown";

interface ClaudeRow {
  id: "installed" | "version" | "signed-in" | "path";
  label: string;
  hint: string;
  optional?: boolean;
  state: RowState;
  /** Which strip issues belong to this row — the first present one renders. */
  issueIds: ClaudeIssue["id"][];
}

// WHO is signed in, when the CLI said. "as <email> · <org> (<plan>)" for a
// claude.ai login, "Using an API key" for a key, the generic line otherwise —
// so the green check names the account it is vouching for.
function signedInHint(h: ClaudeHealth): string {
  const a = h.account;
  if (h.signed_in !== true || !a) return "Uses your existing Claude subscription — no key to paste.";
  if (a.method === "apiKey") return "Using an API key from the environment (ANTHROPIC_API_KEY).";
  if (a.email) {
    const where = [a.org, a.plan].filter(Boolean).join(" · ");
    return `Signed in as ${a.email}${where ? ` (${where})` : ""}.`;
  }
  if (a.method === "oauthToken") return "Using an OAuth token from the environment.";
  return a.method ? `Signed in via ${a.method}.` : "Signed in.";
}

function claudeRows(h: ClaudeHealth): ClaudeRow[] {
  const runnable = h.found && !h.broken;
  return [
    {
      id: "installed",
      label: "Claude Code installed",
      hint: "The `claude` command-line tool, from Anthropic. It is what the composer, the Tasks page and every fused.ai call go through.",
      state: runnable ? "done" : "open",
      issueIds: ["missing", "unusable-override", "broken"],
    },
    {
      id: "version",
      label: `Version ${h.min_version} or newer`,
      hint: h.version ? `Found ${h.version}.` : "Could not read the version.",
      state: !runnable ? "open" : h.version == null ? "unknown" : h.outdated ? "open" : "done",
      issueIds: ["outdated"],
    },
    {
      id: "signed-in",
      label: "Signed in",
      hint: signedInHint(h),
      state: !runnable ? "open" : h.signed_in === true ? "done" : h.signed_in === false ? "open" : "unknown",
      issueIds: ["signed-out"],
    },
    {
      id: "path",
      label: "Available in your terminal",
      hint: "Optional. The app works either way; this is for typing `claude` yourself.",
      optional: true,
      // `null` is "could not tell" (Windows, an override) — never a green check.
      state: !runnable ? "open" : h.on_shell_path === false ? "open" : h.on_shell_path === true ? "done" : "unknown",
      issueIds: ["not-on-path"],
    },
  ];
}

// Mirrors shell/git_tools.BREW_COMMAND. A line to copy, never run here.
const BREW_COMMAND = "brew install git";
// An external http(s) link: the native window hands it to the default
// browser (mac_window.py), a browser tab opens a new tab.
const CHROME_URL = "https://www.google.com/chrome/";

/** "/Library/Developer/CommandLineTools/usr/bin/git" → "Command Line Tools",
 *  "/Applications/Xcode.app/…" → "Xcode", "/opt/homebrew/bin/git" → "Homebrew";
 *  anything else is shown as its path. */
function gitSource(path: string): string {
  if (path.includes("/CommandLineTools/")) return "Command Line Tools";
  if (path.includes("Xcode.app")) return "Xcode";
  if (path.startsWith("/opt/homebrew/") || path.startsWith("/usr/local/")) return "Homebrew";
  if (path.startsWith("/opt/local/")) return "MacPorts";
  return path;
}

/** "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" → "Google
 *  Chrome", "/usr/bin/google-chrome" → "google-chrome": the app, not its binary. */
function chromeApp(path: string): string {
  const m = path.match(/([^/]+)\.app\//);
  return m ? m[1] : path.split("/").pop() || path;
}

function StateIcon({ state, optional }: { state: RowState; optional?: boolean }) {
  if (state === "done")
    return (
      <span className="grid size-6 shrink-0 place-items-center rounded-full bg-emerald-500/15 text-emerald-600 dark:text-emerald-400">
        <Check className="size-3.5" strokeWidth={3} />
      </span>
    );
  if (state === "unknown" || optional)
    return (
      <span className="grid size-6 shrink-0 place-items-center rounded-full border border-border text-muted-foreground">
        <Minus className="size-3" />
      </span>
    );
  return (
    <span className="grid size-6 shrink-0 place-items-center rounded-full border border-amber-500/50 text-amber-600 dark:text-amber-400">
      <Circle className="size-2.5 fill-current" />
    </span>
  );
}

// One row of the checklist: icon, label (struck when done), hint, and the
// row's own controls underneath when it is open.
function Row({
  state,
  label,
  hint,
  optional,
  children,
}: {
  state: RowState;
  label: string;
  hint: string;
  optional?: boolean;
  children?: ReactNode;
}) {
  return (
    <li className="flex flex-col gap-2 border-t border-border px-4 py-3">
      <div className="flex items-start gap-3">
        <StateIcon state={state} optional={optional && state !== "done"} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 text-sm font-medium">
            <span className={state === "done" ? "text-muted-foreground line-through" : undefined}>{label}</span>
            {optional && (
              <span className="rounded-full border border-border px-1.5 py-px text-[11px] font-normal text-muted-foreground">
                optional
              </span>
            )}
          </div>
          <div className="text-xs text-muted-foreground">
            {state === "unknown" ? "Couldn't check — carry on, it will not block you." : hint}
          </div>
        </div>
      </div>
      {children && <div className="ml-9">{children}</div>}
    </li>
  );
}

// A small uppercase label between groups of rows: "Claude Code · optional",
// "Git", "Chrome" — where a group says what it is for. Rows draw their own
// top border (not `divide-y` on the list) so a label sits flush over its
// first row with no hairline between; the label's own top border parts it
// from the group above, except the first.
function Group({ label, optional }: { label: string; optional?: boolean }) {
  return (
    <li className="flex items-center gap-2 border-t border-border px-4 pt-3 pb-1 text-[11px] font-medium tracking-wide text-muted-foreground uppercase first:border-t-0 [&+li]:border-t-0">
      {label}
      {optional && (
        <span className="rounded-full border border-border px-1.5 py-px text-[10px] font-normal normal-case tracking-normal">
          optional
        </span>
      )}
    </li>
  );
}

// `setup` is the wizard's single machine (OnboardingWizard owns it, so a
// sign-in done here is what the First-app step reads). `stages` carries the
// server-observed git and chrome records. `showGit` is the wizard's call
// (macOS, Render) — off a Mac the row is simply not there, and the server
// counts the stage `n/a`.
export function ToolsStep({
  setup,
  stages,
  showGit,
  eyebrow,
  onWork,
}: {
  setup: ClaudeSetup;
  stages: Stages | undefined;
  showGit: boolean;
  eyebrow: ReactNode;
  onWork: (busy: boolean) => void;
}) {
  // ---- Claude Code ------------------------------------------------------
  const { health, loaded, busy, load } = setup;
  const issues = claudeIssues(health);
  const rows = health ? claudeRows(health) : null;
  // OPTIONAL ROWS ARE NOT WORK. PATH is a convenience — the app is finished
  // with Claude Code without it — so "done" here means every REQUIRED row is
  // done, whatever the optional one says.
  const required = rows?.filter((r) => !r.optional);
  const claudeDone = required?.every((r) => r.state === "done");
  // Only a REQUIRED row can claim the yellow button — the optional row's
  // button is quiet (lib/claude-health `optional`).
  const claudeActionable =
    required?.some((r) => r.state === "open" && issues.some((i) => r.issueIds.includes(i.id))) === true;
  // THE STAGE (progress.ts): complete when every required row is done;
  // partial when the CLI runs but a required row is open or unknown (an
  // unknown is never a green check); pending when it does not run at all.
  // Nothing until health has answered.
  const runnable = health ? health.found && !health.broken : false;
  const claudeStage: StageStatus | null = !health ? null : claudeDone ? "complete" : runnable ? "partial" : "pending";
  useEffect(() => {
    if (!health || !claudeStage) return;
    reportStage("claude", claudeStage, {
      version: health.version,
      outdated: health.outdated,
      signed_in: health.signed_in,
      account: health.account?.email ?? health.account?.method ?? null,
      on_shell_path: health.on_shell_path,
    });
  }, [claudeStage, health]);

  // ---- Git ----------------------------------------------------------------
  const git = stages?.git;
  const gitFound = git?.status === "complete";
  const gitPath = typeof git?.meta?.path === "string" ? (git.meta.path as string) : null;
  const gitVersion = typeof git?.meta?.version === "string" ? (git.meta.version as string) : null;
  const [installing, setInstalling] = useState(false);
  const [gitResult, setGitResult] = useState<GitInstallResult | null>(null);
  const installGitTools = () => {
    setInstalling(true);
    setGitResult(null);
    installGit()
      .then((r) => {
        setGitResult(r.install);
        // The reply IS a snapshot too, but refreshProgress merges by the
        // store's rules; one more GET is the cheap, consistent way in.
        return refreshProgress();
      })
      .catch((e: unknown) =>
        setGitResult({ ok: false, already: false, message: e instanceof Error ? e.message : String(e) }),
      )
      .finally(() => setInstalling(false));
  };
  const [copied, setCopied] = useState(false);
  const copyBrew = () => {
    navigator.clipboard?.writeText(BREW_COMMAND).then(
      () => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1500);
      },
      () => undefined,
    );
  };

  // ---- Chrome -------------------------------------------------------------
  const chrome = stages?.chrome;
  const chromeFound = chrome?.status === "complete";
  const chromePath = typeof chrome?.meta?.path === "string" ? (chrome.meta.path as string) : null;
  // Chrome owns the yellow button while missing. (`n/a` was Render with bots
  // off; the flag is gone, the server never writes it now, the guard stays
  // cheap.)
  const chromeNeeded = isBot() || chrome?.status === "pending";

  // ---- The step -----------------------------------------------------------
  // Tell the wizard whether this step still has a button of its own to
  // press: while it does, Next is not the yellow one.
  const gitWork = showGit && !gitFound;
  const chromeWork = chromeNeeded && !chromeFound;
  useEffect(() => onWork(claudeActionable || gitWork || chromeWork), [onWork, claudeActionable, gitWork, chromeWork]);

  // Fresh on arrival, and again whenever the window comes back from an
  // installer (Apple's, Chrome's) or the browser the download link opened.
  useEffect(() => {
    void refreshProgress();
    const onFocus = () => void refreshProgress();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, []);
  const [checking, setChecking] = useState(false);
  const checkAll = () => {
    setChecking(true);
    Promise.all([load(true), refreshProgress()]).finally(() => setChecking(false));
  };
  const working = checking || busy;

  const status = !loaded
    ? "Checking this machine…"
    : gitWork || chromeWork
      ? "Something still needs installing — the open rows have buttons."
      : claudeActionable
        ? "Git and Chrome are in place. Claude Code is optional; its open rows have buttons."
        : claudeDone
          ? "Everything is in place."
          : "Nothing here will block you — carry on.";

  return (
    <div className="flex flex-col gap-6">
      <StepHeader
        eyebrow={eyebrow}
        title="Install the tools"
        lead={
          <>
            {bundleName()} hands your brief to Claude Code to build apps, fetches showcase apps with git,
            and drives Google Chrome for bots. Nothing here blocks you — each row says what it is for, and
            you can finish any of it later.
          </>
        }
      />

      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground" role="status">
          {status}
        </p>
        <Button variant="outline" size="sm" onClick={checkAll} disabled={working}>
          <RefreshCw data-icon="inline-start" className={working ? "animate-spin" : undefined} />
          {working ? "Checking…" : "Check again"}
        </Button>
      </div>

      <ol className="m-0 flex list-none flex-col rounded-xl border border-border bg-card p-0">
        <Group label="Claude Code" optional />
        {!rows &&
          [0, 1, 2, 3].map((i) => (
            <li key={i} className="flex items-center gap-3 px-4 py-3">
              <Skeleton className="size-6 rounded-full" />
              <Skeleton className="h-4 w-48" />
            </li>
          ))}
        {rows?.map((row) => {
          const issue = row.state === "open" ? issues.find((i) => row.issueIds.includes(i.id)) : undefined;
          return (
            <Row key={row.id} state={row.state} label={row.label} hint={row.hint} optional={row.optional}>
              {issue && (
                <ul className="claude-health-issues onboarding-issue">
                  <IssueRow
                    issue={issue}
                    install={setup.install}
                    login={setup.login}
                    doctor={issue.id === "broken" ? setup.doctor : null}
                    onAct={setup.act}
                    onCancelLogin={setup.cancelLogin}
                    busy={setup.acting}
                    actionError={issue.action ? setup.actionError : null}
                    doneNote={issue.id === "not-on-path" ? setup.linkedNote : null}
                  />
                </ul>
              )}
            </Row>
          );
        })}

        {showGit && (
          <>
            <Group label="Git" />
            <Row
              state={gitFound ? "done" : git ? "open" : "unknown"}
              label="Git installed"
              hint={
                gitFound
                  ? gitPath
                    ? `Found git${gitVersion ? ` ${gitVersion}` : ""} from ${gitSource(gitPath)}.`
                    : "Already installed."
                  : "Showcase apps arrive by git clone, and Claude Code keeps its working copies with git. macOS does not ship a working one until Apple's Command Line Tools are installed."
              }
            >
              {!gitFound && git && (
                <div className="flex flex-col gap-2">
                  <div className="flex flex-wrap items-center gap-3">
                    <Button variant="accent" size="sm" onClick={installGitTools} disabled={installing}>
                      <Download data-icon="inline-start" />
                      {installing ? "Opening installer…" : "Install Command Line Tools"}
                    </Button>
                    <span className="text-xs text-muted-foreground" role="status">
                      {gitResult
                        ? gitResult.message
                        : "macOS opens its dialog; choose Install there. The check runs again when you come back."}
                    </span>
                  </div>
                  <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                    <span>Have Homebrew? This works too:</span>
                    <code className="rounded-md border border-border bg-muted px-1.5 py-0.5 font-mono text-[11px] text-foreground">
                      {BREW_COMMAND}
                    </code>
                    <Button variant="ghost" size="sm" onClick={copyBrew} aria-label="Copy the Homebrew command">
                      {copied ? <Check data-icon="inline-start" /> : <Copy data-icon="inline-start" />}
                      {copied ? "Copied" : "Copy"}
                    </Button>
                  </div>
                </div>
              )}
            </Row>
          </>
        )}

        <Group label="Chrome" />
        <Row
          state={chromeFound ? "done" : chrome ? "open" : "unknown"}
          label="Google Chrome installed"
          hint={
            chromeFound
              ? chromePath
                ? `Found ${chromeApp(chromePath)}.`
                : "Already installed."
              : isBot()
                ? "Every bot drives a real Chrome window of its own, with its own profile. Chromium, Edge and Brave are found too."
                : "Bots drive a real Chrome window of their own, with its own profile. Only needed if you turn bots on; Chromium, Edge and Brave are found too."
          }
        >
          {!chromeFound && chrome && (
            <div className="flex flex-wrap items-center gap-3">
              <Button
                variant={chromeNeeded ? "accent" : "outline"}
                size="sm"
                render={<a href={CHROME_URL} target="_blank" rel="noreferrer" />}
              >
                <Download data-icon="inline-start" />
                Download Chrome
              </Button>
              <span className="text-xs text-muted-foreground" role="status">
                Install it like any other app. The check runs again when you come back.
              </span>
            </div>
          )}
        </Row>
      </ol>
    </div>
  );
}
