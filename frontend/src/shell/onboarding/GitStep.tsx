import { useEffect, useState, type ReactNode } from "react";
// Step 3 — Git. The showcase apps arrive by `git clone` (community.py, which
// refuses up front without a usable git), and Claude Code keeps its worktrees
// with it; a Mac without the Command Line Tools has neither, and used to find
// out only when the first showcase clone said so. This step says it first.
//
// macOS ONLY — the owner's call. Every Mac ships /usr/bin/git, but on a
// machine without the developer tools it is an xcode-select SHIM that pops
// Apple's "install the command line developer tools?" dialog when run, so the
// server (shell/git_tools.py) never runs it: it walks the places a REAL git
// lives (the Tools, Xcode, Homebrew) on every /api/config read, memoized, and
// overrules this stage with what it finds. Like ChromeStep, NOTHING TO WRITE:
// `complete` with `meta.path`/`meta.version` = found; `pending` = none. Off
// macOS the wizard does not show the step and the server counts it `n/a`.
//
// The Install button is the one thing here that acts: POST git/install runs
// `xcode-select --install`, which opens Apple's Software Update dialog and
// returns at once. The download happens there, outside this window, so the
// step re-reads on window focus and on Check again. Homebrew's `brew install
// git` is shown as the other way for someone who would rather not take the
// full Tools; the app does not run it.
import { Check, Copy, Download, FolderGit2, GitBranch, RefreshCw, Terminal } from "lucide-react";

import { installGit, type GitInstallResult } from "@platform/lib/api";
import { Button } from "@platform/shadcn/ui/button";

import { refreshProgress, type Stages } from "./progress";
import { StepHeader } from "./StepHeader";

// Mirrors shell/git_tools.BREW_COMMAND. A line to copy, never run here.
const BREW_COMMAND = "brew install git";

// "/Library/Developer/CommandLineTools/usr/bin/git" → "Command Line Tools",
// "/Applications/Xcode.app/…" → "Xcode", "/opt/homebrew/bin/git" → "Homebrew".
function sourceName(path: string): string {
  if (path.includes("/CommandLineTools/")) return "Command Line Tools";
  if (path.includes("Xcode.app")) return "Xcode";
  if (path.startsWith("/opt/homebrew/") || path.startsWith("/usr/local/")) return "Homebrew";
  if (path.startsWith("/opt/local/")) return "MacPorts";
  return path;
}

export function GitStep({
  stages,
  eyebrow,
  onWork,
}: {
  stages: Stages | undefined;
  eyebrow: ReactNode;
  onWork: (busy: boolean) => void;
}) {
  const stage = stages?.git;
  const found = stage?.status === "complete";
  const path = typeof stage?.meta?.path === "string" ? (stage.meta.path as string) : null;
  const version = typeof stage?.meta?.version === "string" ? (stage.meta.version as string) : null;
  // Install owns the yellow button until a git is found; then Next does.
  useEffect(() => onWork(!found), [onWork, found]);

  const [checking, setChecking] = useState(false);
  const check = () => {
    setChecking(true);
    refreshProgress().finally(() => setChecking(false));
  };

  const [installing, setInstalling] = useState(false);
  const [result, setResult] = useState<GitInstallResult | null>(null);
  const install = () => {
    setInstalling(true);
    setResult(null);
    installGit()
      .then((r) => {
        setResult(r.install);
        // The reply IS a snapshot too, but refreshProgress merges by the
        // store's rules; one more GET is the cheap, consistent way in.
        return refreshProgress();
      })
      .catch((e: unknown) => setResult({ ok: false, already: false, message: e instanceof Error ? e.message : String(e) }))
      .finally(() => setInstalling(false));
  };

  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard?.writeText(BREW_COMMAND).then(
      () => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1500);
      },
      () => undefined,
    );
  };

  // Fresh on arrival, and again whenever the window comes back from Apple's
  // installer (focus — visibilitychange alone does not fire when a native
  // window merely regains focus).
  useEffect(() => {
    void refreshProgress();
    const onFocus = () => void refreshProgress();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, []);

  return (
    <div className="flex flex-col gap-6">
      <StepHeader
        eyebrow={eyebrow}
        title="Install Git"
        lead="The showcase apps arrive by git clone, and Claude Code keeps its working copies with git. macOS does not ship a working one until Apple's Command Line Tools are installed — a one-time download, done from here."
      />

      <ul className="m-0 grid list-none gap-3 p-0 sm:grid-cols-3">
        {[
          {
            icon: <FolderGit2 className="size-4" />,
            title: "Showcase apps",
            body: "The community showcase is a git repository. Opening a showcase app clones it once; without git there is nothing to open.",
          },
          {
            icon: <GitBranch className="size-4" />,
            title: "Claude Code",
            body: "Claude Code uses git to keep a history of what it changes and to work on several things at once in worktrees.",
          },
          {
            icon: <Terminal className="size-4" />,
            title: "Apple's tools, or Homebrew",
            body: "The Command Line Tools install from a system dialog. A git from Homebrew, MacPorts or Xcode is found the same way.",
          },
        ].map((c) => (
          <li key={c.title} className="flex flex-col gap-1.5 rounded-xl border border-border bg-card p-4">
            <div className="flex items-center gap-2 text-sm font-medium">
              <span className="grid size-7 place-items-center rounded-md bg-muted text-foreground">{c.icon}</span>
              {c.title}
            </div>
            <p className="text-xs leading-relaxed text-muted-foreground">{c.body}</p>
          </li>
        ))}
      </ul>

      <div className="flex flex-col gap-3 rounded-xl border border-border bg-card p-4">
        {found ? (
          <div className="flex items-center gap-3">
            <span className="grid size-6 place-items-center rounded-full bg-emerald-500/15 text-emerald-600 dark:text-emerald-400">
              <Check className="size-3.5" strokeWidth={3} />
            </span>
            <div className="min-w-0">
              <div className="text-sm font-medium text-muted-foreground line-through">Install Git</div>
              <div className="truncate text-xs text-muted-foreground" title={path ?? undefined}>
                {path
                  ? `Found git${version ? ` ${version}` : ""} from ${sourceName(path)} — nothing to do here.`
                  : "Already installed — nothing to do here."}
              </div>
            </div>
          </div>
        ) : (
          <>
            <ol className="m-0 flex list-none flex-col gap-1.5 p-0 text-sm">
              {[
                "Press Install — macOS opens its Command Line Tools dialog. Choose Install there and let it download.",
                "Come back here — the check runs again on its own.",
              ].map((s, i) => (
                <li key={s} className="flex gap-2">
                  <span className="w-4 shrink-0 text-muted-foreground">{i + 1}.</span>
                  <span>{s}</span>
                </li>
              ))}
            </ol>
            <div className="flex flex-wrap items-center gap-3">
              <Button variant="accent" onClick={install} disabled={installing}>
                <Download data-icon="inline-start" />
                {installing ? "Opening installer…" : "Install Command Line Tools"}
              </Button>
              <Button variant="ghost" size="sm" onClick={check} disabled={checking}>
                <RefreshCw data-icon="inline-start" className={checking ? "animate-spin" : undefined} />
                Check again
              </Button>
              <span className="text-xs text-muted-foreground" role="status">
                {result ? result.message : stage ? "No usable git found on this Mac." : "Checking…"}
              </span>
            </div>
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span>Have Homebrew? This works too:</span>
              <code className="rounded-md border border-border bg-muted px-1.5 py-0.5 font-mono text-[11px] text-foreground">
                {BREW_COMMAND}
              </code>
              <Button variant="ghost" size="sm" onClick={copy} aria-label="Copy the Homebrew command">
                {copied ? <Check data-icon="inline-start" /> : <Copy data-icon="inline-start" />}
                {copied ? "Copied" : "Copy"}
              </Button>
            </div>
          </>
        )}
      </div>

      <p className="text-xs text-muted-foreground">
        You can do this later. Opening a showcase app without git says so again, with the same way in.
      </p>
    </div>
  );
}
