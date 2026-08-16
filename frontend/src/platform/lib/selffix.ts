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

// What the session is told is wrong — from EITHER of the two ways in.
//
//   a failed row   carries `message` (the error) and the row's own fields.
//   a description  carries `note`, and nothing else exists: no exception, no
//                  failed job. The server's incident then leans on the app log
//                  instead, and the prompt tells the session to reproduce
//                  before it diagnoses.
//
// Every field is optional on the wire, but the server requires at least one of
// `message` / `note` / `title`: a session handed nothing has nothing to look at.
export interface FailureContext {
  job_id?: string;
  title?: string;
  detail?: string;
  state?: string;
  kind?: string;
  message?: string;
  page?: string;
  /** The user's own words, when they are reporting behaviour rather than a
      crash (the Preferences tab). */
  note?: string;
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
// THE STAMP: when a fix last started. Read to pick the cadence, and persisted
// rather than held in memory so a reload mid-session comes back in the fast lane.
export const SELFFIX_PING_KEY = "fused-render:selffix-ping";

// THE NUDGE: "self-fix state changed — re-read now", on two channels, because
// neither reaches everyone.
//
//   the KEY    fires `storage` in every same-origin document EXCEPT the writer,
//              so it reaches other tabs and never this one.
//   the EVENT  reaches this document and no other.
//
// Both are needed and neither is redundant, and getting that wrong is not a
// corner case in either direction: the surface that STARTS a fix (a
// download-manager row) and the one that DISMISSES a badge (the Preferences
// tab) are both normally in the very document the chip lives in, so the storage
// write alone would leave the badge stale for a full idle interval in exactly
// the cases the user is looking straight at it.
//
// ONE nudge for both events rather than two, because the listener's response is
// identical — cancel the pending timer, re-read now — and the cadence is decided
// separately, from the stamp above. A "started" nudge dispatched on a dismiss
// would be a lie; a second event with the same handler would be ceremony.
export const SELFFIX_CHANGED_KEY = "fused-render:selffix-changed";
export const SELFFIX_CHANGED_EVENT = "fused:selffix-changed";

// How long after a start the fast poll stays on. A session that takes longer
// than this has stopped being something the user is watching.
export const WATCH_WINDOW_MS = 30 * 60_000;
export const POLL_IDLE_MS = 60_000;
export const POLL_WATCH_MS = 5_000;

/** Whether a `storage` event is one of ours. Exported so the guard is pinned by
    a test rather than by a condition in a component: a guard that accepted only
    one of the two keys would silently swallow the other channel, which is a
    thing no screenshot shows. */
export function isSelfFixStorageKey(key: string | null): boolean {
  return key === SELFFIX_PING_KEY || key === SELFFIX_CHANGED_KEY;
}

/** "Something about self-fix changed — re-read." Both channels, always. */
export function notifySelfFixChanged(now = Date.now()): void {
  try {
    localStorage.setItem(SELFFIX_CHANGED_KEY, String(now));
  } catch {
    /* private mode / disabled storage — the idle poll is the floor */
  }
  try {
    window.dispatchEvent(new Event(SELFFIX_CHANGED_EVENT));
  } catch {
    /* no window (a test, a worker) — the idle poll is still the floor */
  }
}

export function noteFixStarted(now = Date.now()): void {
  try {
    // The stamp goes down FIRST: the nudge below makes listeners re-read the
    // cadence, and they must see this start rather than the previous one.
    localStorage.setItem(SELFFIX_PING_KEY, String(now));
  } catch {
    /* private mode / disabled storage — the idle poll is the floor */
  }
  notifySelfFixChanged(now);
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

// The Preferences tab's brief: the user's own description, and nothing else to
// go on. Trimmed here rather than at the button so "is there anything to send"
// is one answer — the Start control's enabled state and the request body must
// not disagree about whether a box holding three spaces counts.
export function failureContextFromNote(note: string): FailureContext {
  return { note: note.trim(), source: "preferences" };
}

export function describedProblemIsSendable(note: string): boolean {
  return note.trim().length > 0;
}

export function getSelfFix(): Promise<SelfFixSnapshot> {
  return getJson<SelfFixSnapshot>("/api/selffix");
}

// Dismissing is a state change like any other, and it can be made from EITHER
// surface — the chip's own popover or the Preferences tab. Nudging here rather
// than at each call site is what stops the two disagreeing: a dismiss from
// Preferences used to clear the marker and refresh that page while the sidebar
// chip stayed amber until its next idle poll, which reads as a dismiss that
// did not work.
export async function clearSelfFix(): Promise<{ cleared: boolean }> {
  const result = await postJson<{ cleared: boolean }>("/api/selffix/clear", {});
  notifySelfFixChanged();
  return result;
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
