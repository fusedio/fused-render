// Self-fix — asking Claude to repair this installation, and what it leaves
// behind (fused_render/selffix.py, SPEC §42).
//
// Two halves that meet at a file on disk:
//
//   THE TRIGGER    a failed row in the download manager grows one more option
//                  beside Dismiss. It POSTs the failure, gets back a live run,
//                  and lands the user in the explorer's chat sidebar OPEN ON
//                  THE INSTALLATION — so the fix is something they watch, not
//                  something that happens to their app.
//   THE MARK       if that session changed anything, the app stamps the
//                  install and the sidebar's version chip turns amber. Clicking
//                  it leads to the session's report, to the developers, and to
//                  a clean reinstall.
//
// The pure parts live here rather than inside the two components, because they
// are the parts that are wrong in ways a screenshot does not show: a deep link
// that drops the run id lands the user in an empty chat, and an issue URL that
// forgets the version is a bug report nobody can act on.
import { getJson, postJson } from "@platform/lib/api";
import { urlForFsPath } from "@platform/lib/router";

// One recorded fix — a session that changed this installation. Absolute paths:
// the server stores them relative to the install and resolves them on the way
// out, because an install can be moved.
export interface SelfFixEntry {
  at: number;
  updated_at: number;
  run_id: string;
  session_id: string;
  title: string;
  // The session's own account. Never null in practice — the server pre-creates
  // it holding the incident — but the marker predates nothing, so treat it as
  // optional rather than assume.
  report: string | null;
  incident: string | null;
}

// /api/config's `modified_install`: present ONLY when this installation has
// been changed by a fix session. Its absence is the unmodified state — there is
// no `modified: false` to check for.
export interface ModifiedInstall {
  modified: true;
  version: string | null;
  install_root: string;
  state_dir: string;
  first_modified_at: number | null;
  modified_at: number | null;
  fixes: SelfFixEntry[];
  // The report to open: the newest one. Chosen server-side so the chip and the
  // panel cannot disagree about which report "the" report is.
  latest_report: string | null;
}

export interface ReinstallAdvice {
  // brew | dmg | windows | linux | source | pip
  method: string;
  headline: string;
  // Empty when there is nothing to type — a DMG is dragged, not run. The panel
  // reads that as "the link IS the instruction" and promotes the link to the
  // section's primary action; see reinstall_advice's docstring.
  command: string;
  note: string;
  url: string;
  // Wording for the link, from the server (which already words the rest per
  // method). Older servers omit it — fall back to the bare URL.
  url_label?: string;
}

export interface SelfFixReport {
  path: string;
  name: string;
  at: number;
  size: number;
}

export interface SelfFixSnapshot {
  modified: boolean;
  version: string;
  install_root: string;
  // Whether a fix could be applied here at all. False for an installation the
  // user does not own — the panel says so instead of offering a fix that would
  // spend minutes and change nothing.
  writable: boolean;
  marker: ModifiedInstall | null;
  reports: SelfFixReport[];
  reinstall: ReinstallAdvice;
  issues_url: string;
  machine: Record<string, string | boolean>;
}

export interface SelfFixStart {
  run_id: string;
  target: string;
  incident: string;
  report: string;
}

// What the session is told went wrong. Every field optional on the wire: a user
// may well click Fix on a row whose reporter never set a message, and a session
// that has only the title still has the logs and the code.
export interface FailureContext {
  job_id?: string;
  title?: string;
  detail?: string;
  state?: string;
  kind?: string;
  message?: string;
  page?: string;
  // Which surface offered the fix ("download manager"), so the report says how
  // the user got here.
  source?: string;
}

// -- The badge has to appear while you are watching ---------------------------
//
// The mark is set by the SERVER, mid-session, the moment the fix session's first
// edit lands (routers/selffix.py's watcher). Nothing pushes that to the shell —
// `modified_install` rides /api/config, which the sidebar reads once at boot.
// Left there, the badge for a fix you just watched happen would appear on your
// next launch, which is precisely when it is least intelligible.
//
// So the chip polls, at two cadences, the way the download manager does: slow
// forever, fast while a fix session plausibly runs. "Plausibly" is a timestamp
// this module writes when a fix STARTS — in localStorage rather than in memory,
// because the surface that starts the session (a download-manager row) and the
// surface that shows the badge (the sidebar chip) can be in different tabs, and
// because a reload during a long session must not drop back to the slow poll.
export const SELFFIX_PING_KEY = "fused-render:selffix-ping";

// ...and the SAME-document half of that ping, which the storage key cannot be.
// `storage` fires in every same-origin document EXCEPT the one that wrote the
// value — and the surface that starts a fix (a download-manager row) is
// normally in the SAME document as the chip, so the localStorage write alone
// covers only the rarer case. Without this the badge for a fix you started in
// this very tab waits out the running idle timer: up to POLL_IDLE_MS, which is
// precisely the delay the two cadences exist to remove.
export const SELFFIX_PING_EVENT = "fused:selffix-started";

// How long after a start the fast poll stays on. A session that takes longer
// than this has stopped being something the user is watching.
export const WATCH_WINDOW_MS = 30 * 60_000;
export const POLL_IDLE_MS = 60_000;
export const POLL_WATCH_MS = 5_000;

// Announce a started fix to every chip that might be watching — in this
// document and in any other tab. BOTH channels are needed and neither is
// redundant: `storage` reaches other tabs and skips this one, the event reaches
// this one and no other.
export function noteFixStarted(now = Date.now()): void {
  try {
    // Cross-tab. Same mechanism as the job manager's ping (lib/jobs'
    // JOB_PING_KEY), and it also persists the stamp the cadence is computed
    // from, so a reload mid-session comes back in the fast lane.
    localStorage.setItem(SELFFIX_PING_KEY, String(now));
  } catch {
    /* private mode / disabled storage — the idle poll is the floor */
  }
  try {
    // Same document. Dispatched AFTER the write, so a listener that re-reads
    // `lastFixStartedAt()` sees the new stamp rather than the previous one.
    window.dispatchEvent(new Event(SELFFIX_PING_EVENT));
  } catch {
    /* no window (a test, a worker) — the idle poll is still the floor */
  }
}

export function lastFixStartedAt(): number {
  try {
    return Number(localStorage.getItem(SELFFIX_PING_KEY)) || 0;
  } catch {
    return 0;
  }
}

// A clock that has gone BACKWARDS past the stamp (a suspend, a manual clock
// change) reads as "not watching" rather than as watching forever: `now - at`
// negative fails the window test, which is the safe direction — a badge a
// minute late costs nothing, a permanent 5s poll costs a request every 5s for
// the life of the app.
export function selffixPollInterval(startedAt: number, now = Date.now()): number {
  const age = now - startedAt;
  return startedAt > 0 && age >= 0 && age < WATCH_WINDOW_MS
    ? POLL_WATCH_MS
    : POLL_IDLE_MS;
}

export async function startSelfFix(context: FailureContext): Promise<SelfFixStart> {
  const started = await postJson<SelfFixStart>("/api/selffix/start", context);
  // Only after the server said yes: a refused start (a read-only install) is
  // not a session anyone should be polling for.
  noteFixStarted();
  return started;
}

// A download-manager row as the fix session's brief. Kept here, next to the
// wire type, rather than inlined at the button: what a session is told about
// the failure is the difference between a useful diagnosis and a shrug, and the
// fields worth sending are not obvious from the row (the `page` attribution and
// the job id are what tie the report back to the call log).
//
// `Job` is deliberately not imported — that would tie the self-fix module to the
// job registry, when the trigger is meant to be usable from any surface that
// knows a title and a message. A structural parameter says the same thing and
// costs nothing.
export function failureContextFromJob(job: {
  id: string;
  title: string;
  detail: string;
  kind: string;
  state: string;
  message: string;
  page: string;
}): FailureContext {
  return {
    job_id: job.id,
    title: job.title,
    detail: job.detail,
    kind: job.kind,
    state: job.state,
    message: job.message,
    page: job.page,
    source: "download manager",
  };
}

export function getSelfFix(): Promise<SelfFixSnapshot> {
  return getJson<SelfFixSnapshot>("/api/selffix");
}

export function clearSelfFix(): Promise<{ cleared: boolean }> {
  return postJson<{ cleared: boolean }>("/api/selffix/clear", {});
}

// Where the user lands once the session is running: the INSTALL FOLDER, with
// the chat sidebar open on the run that was just started.
//
// `_side=claude` is the explorer's companion-column param (listing/pane-side)
// and `run` is the chat template's re-attach param — the same pair the Inbox
// and scheduled messages hand over on (shell/schedule-lib's explorerUrl), and
// the same mechanism: neither param is in the pane's iframe src, the template
// reads them off the shell URL through fused.params' ancestor climb.
//
// The run id is what makes this a HANDOFF rather than a new chat. Without it
// the sidebar opens on the folder, sees no run, and shows an empty composer
// while a session is already working three feet away in a process nobody is
// watching.
export function fixSessionUrl(start: { target: string; run_id: string }): string {
  const params = new URLSearchParams({ _side: "claude", run: start.run_id });
  return urlForFsPath(start.target.replace(/[/\\]+$/, ""), "?" + params.toString());
}

// A GitHub issue with the machine facts already in it. The report itself is NOT
// inlined: it can be thousands of words, a URL that long is refused by servers
// long before it reaches GitHub, and the report is a file the user can attach
// or paste. What goes in the URL is the part they would otherwise have to be
// asked for twice — version, platform, and where the report is.
export function issueUrl(snapshot: SelfFixSnapshot): string {
  const version = snapshot.version || "unknown";
  const report = snapshot.marker?.latest_report || snapshot.reports[0]?.path || "";
  const body = [
    "<!-- Paste the self-fix report below, or attach the file. -->",
    "",
    "**Report file**: " + (report || "(none written)"),
    "",
    "| | |",
    "| --- | --- |",
    "| fused-render | v" + version + " |",
    "| platform | " + (snapshot.machine.platform ?? "unknown") + " |",
    "| python | " + (snapshot.machine.python ?? "unknown") + " |",
    "| install | `" + snapshot.install_root + "` |",
    "",
    "## What Claude changed here",
    "",
    "",
  ].join("\n");
  const params = new URLSearchParams({
    title: `Self-fix report — v${version}`,
    body,
    labels: "self-fix",
  });
  return `${snapshot.issues_url}?${params.toString()}`;
}

// The line under the chip's heading. Says WHEN, because "this install was
// modified" without a date is a fact the user cannot place — and the most
// common reaction to the badge is "when did that happen?".
export function modifiedSummary(marker: ModifiedInstall, now = Date.now()): string {
  const at = marker.modified_at;
  if (!at) return "Claude changed files in this installation.";
  const days = Math.floor((now / 1000 - at) / 86400);
  const when =
    days <= 0 ? "today" : days === 1 ? "yesterday" : `${days} days ago`;
  const count = marker.fixes.length;
  const what =
    count > 1 ? `${count} fix sessions have changed` : "A fix session changed";
  return `${what} files in this installation — most recently ${when}.`;
}
