// Step 2 — Claude Code. A CHECKLIST, not the strip: the strip renders only
// what is wrong and nothing when all is well, which is right for a page
// header and wrong for a setup step, where "installed ✓, signed in ✓" is the
// reassurance the step exists to give. So the four facts are rows, each done
// (struck through, green check) or open (the strip's own IssueRow attached —
// same buttons, same endpoints, same polls, via lib/claude-setup).
//
// Never blocks Next. An unknown (the probe could not tell) is a muted row,
// not a gate — flow's wizard blocked Next on an unknown and had a state with
// no way through.
import { Check, Circle, Minus, RefreshCw } from "lucide-react";

import type { ClaudeHealth } from "@platform/lib/api";
import { claudeIssues, type ClaudeIssue } from "@platform/lib/claude-health";
import type { ClaudeSetup } from "@platform/lib/claude-setup";
import { Button } from "@platform/shadcn/ui/button";
import { Skeleton } from "@platform/shadcn/ui/skeleton";
import { IssueRow } from "@platform/ui/ClaudeHealthStrip";

import { StepHeader } from "./StepHeader";

type RowState = "done" | "open" | "unknown";

interface Row {
  id: "installed" | "version" | "signed-in" | "path";
  label: string;
  hint: string;
  optional?: boolean;
  state: RowState;
  /** Which strip issues belong to this row — the first present one renders. */
  issueIds: ClaudeIssue["id"][];
}

function rowsFor(h: ClaudeHealth): Row[] {
  const runnable = h.found && !h.broken;
  return [
    {
      id: "installed",
      label: "Claude Code installed",
      hint: "The `claude` command-line tool, from Anthropic.",
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
      hint: "Uses your existing Claude subscription — no key to paste.",
      state: !runnable ? "open" : h.signed_in === true ? "done" : h.signed_in === false ? "open" : "unknown",
      issueIds: ["signed-out"],
    },
    {
      id: "path",
      label: "Available in your terminal",
      hint: "Optional. The app works either way; this is for typing `claude` yourself.",
      optional: true,
      state: !runnable ? "open" : h.on_shell_path === false ? "open" : "done",
      issueIds: ["not-on-path"],
    },
  ];
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

// `setup` is the wizard's single machine (OnboardingWizard owns it, so what
// gets fixed here is what step 4 reads).
export function ClaudeStep({ setup }: { setup: ClaudeSetup }) {
  const { health, loaded, busy, load } = setup;
  const issues = claudeIssues(health);
  const rows = health ? rowsFor(health) : null;
  const allDone = rows?.every((r) => r.state === "done" || (r.optional && r.state !== "open"));
  const anyActionable = rows?.some((r) => r.state === "open" && issues.some((i) => r.issueIds.includes(i.id)));

  return (
    <div className="flex flex-col gap-6">
      <StepHeader
        eyebrow="Step 2 of 4"
        title="Connect Claude Code"
        lead={
          <>
            FusedRender builds apps by handing your brief to Claude Code running on
            this machine. It is what the composer, the Tasks page and every{" "}
            <code className="rounded bg-muted px-1 py-0.5 text-[0.85em]">fused.ai</code>{" "}
            call go through. The file explorer, previews and local models work without
            it — you can finish this later.
          </>
        }
      />

      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground" role="status">
          {!loaded
            ? "Checking this machine…"
            : allDone
              ? "Everything is in place."
              : anyActionable
                ? "A few things still need doing — the open rows have buttons."
                : "Nothing here will block you — carry on."}
        </p>
        <Button variant="ghost" size="sm" onClick={() => load(true)} disabled={busy}>
          <RefreshCw data-icon="inline-start" className={busy ? "animate-spin" : undefined} />
          {busy ? "Checking…" : "Check again"}
        </Button>
      </div>

      <ol className="flex flex-col divide-y divide-border rounded-xl border border-border bg-card">
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
            <li key={row.id} className="flex flex-col gap-2 px-4 py-3">
              <div className="flex items-start gap-3">
                <StateIcon state={row.state} optional={row.optional && row.state !== "done"} />
                <div className="min-w-0 flex-1">
                  <div
                    className={
                      row.state === "done"
                        ? "text-sm font-medium text-muted-foreground line-through decoration-emerald-500/60"
                        : "text-sm font-medium"
                    }
                  >
                    {row.label}
                    {row.optional && (
                      <span className="ml-2 rounded-full border border-border px-1.5 py-px text-[11px] font-normal text-muted-foreground no-underline">
                        optional
                      </span>
                    )}
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {row.state === "unknown" ? "Couldn't check — carry on, it will not block you." : row.hint}
                  </div>
                </div>
              </div>
              {issue && (
                <ul className="claude-health-issues onboarding-issue ml-9">
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
            </li>
          );
        })}
      </ol>
    </div>
  );
}
