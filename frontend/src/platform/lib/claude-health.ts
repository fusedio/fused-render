// Turning a Claude Code health snapshot into the things worth SAYING about it,
// and remembering what the user has already dismissed.
//
// **Why a module and not logic inside the strip.** This is a classifier with an
// ordering argument and a dismissal state machine, and both are the kind of
// thing that is wrong in ways no screenshot shows — the same reason lib/trouble
// exists beside TroubleCard. A component effect is not a testable place to keep
// "which of three problems leads" or "does dismissing one hide the next".
//
// It is the PROACTIVE half of lib/trouble. That module classifies a failure
// that already happened, in the CLI's own words; this one reads facts gathered
// before anything was attempted. They share a vocabulary on purpose: the
// `helpKind` below is one of the download page's four tabs, so a heads-up and a
// failure card send the user to the same place.
import type { ClaudeHealth } from "./api";
import { CLAUDE_INSTALL_COMMAND, troubleHelpUrl, type TroubleKind } from "./trouble";

/** One thing worth telling the user, already in their terms. */
export interface ClaudeIssue {
  /** Stable id — the dismissal signature is built from these, so renaming one
      un-dismisses it for everybody. Worth it when the MEANING changed; never
      do it for wording. */
  id: "missing" | "unusable-override" | "shell-only" | "outdated" | "signed-out";
  /** The one-line statement. Says what is true, not what is polite. */
  title: string;
  /** What to do about it, in a sentence. */
  detail: string;
  /** Which troubleshooting tab this maps to, so the link is a deep link. */
  helpKind: TroubleKind;
  /** A terminal command that fixes it, when one does. */
  command?: string;
}

/** `claude update`, the fix for an install that is merely old. Not in
    lib/trouble because that module never had a case for it: an outdated CLI
    fails with an unknown-option error, which classifies as `raw` and gets told
    to work out what the error is about. Knowing the version is what turns that
    into one command. */
export const CLAUDE_UPDATE_COMMAND = "claude update";

/** The env var that points the app at a CLI it cannot otherwise see. */
export const CLAUDE_BIN_ENV = "FUSED_RENDER_CLAUDE_BIN";

/**
 * Everything unresolved about this snapshot, most-blocking first.
 *
 * ORDER IS THE CORRECTNESS ARGUMENT, the same one lib/trouble makes for its two
 * tiers. A machine with no `claude` at all is also "not signed in" and also has
 * no readable version, and reporting all three would bury the only one that
 * matters under two consequences of it. So a missing install short-circuits, and
 * everything after it is a statement about an install we know exists.
 *
 * An EMPTY array means "nothing to say" — which is the common case and the
 * reason the strip renders nothing at all most of the time.
 */
export function claudeIssues(health: ClaudeHealth | null): ClaudeIssue[] {
  if (!health) return []; // a failed probe is not a finding — see the strip
  const issues: ClaudeIssue[] = [];

  if (!health.found) {
    // A pointing-at-nothing override is its own diagnosis, and telling this
    // user to install Claude Code would be wrong twice: they may well have it,
    // and the thing actually breaking is a setting they can see.
    if (health.source === "override") {
      issues.push({
        id: "unusable-override",
        title: `${CLAUDE_BIN_ENV} points at something that cannot run`,
        detail:
          `The app was told to use ${health.path ?? "a specific path"}, and there ` +
          "is no runnable file there. Correct it or unset it — with it unset, " +
          "the app looks for Claude Code in the usual places.",
        helpKind: "notfound",
      });
      return issues;
    }
    issues.push({
      id: "missing",
      title: "The app can't find Claude Code",
      detail:
        "Fused Render uses Claude Code on this computer to build and fix " +
        "things. Install it, then use Check again.",
      helpKind: "notfound",
      command: CLAUDE_INSTALL_COMMAND,
    });
    return issues; // everything below is about an install that exists
  }

  // Found only by asking the login shell: the binary is fine, the app's PATH is
  // not. A GUI-launched app inherits no shell profile, so this will keep
  // happening on every start until the override is set — and "install Claude
  // Code" would be exactly the wrong advice for it.
  if (health.source === "shell") {
    issues.push({
      id: "shell-only",
      title: "Claude Code is installed somewhere the app can't see",
      detail:
        `Your shell finds it at ${health.path}, but Fused Render does not ` +
        `inherit your shell's PATH. Set ${CLAUDE_BIN_ENV} to that path so it ` +
        "is found on every start.",
      helpKind: "notfound",
    });
  }

  // Only ever set for a version we actually read AND that is below the floor —
  // the server never guesses this from an unreadable version string.
  if (health.outdated) {
    issues.push({
      id: "outdated",
      title: `Claude Code ${health.version} is older than this app needs`,
      detail:
        `Fused Render starts sessions with options added in ${health.min_version}. ` +
        "Update it and the app will use the version you already have.",
      helpKind: "raw",
      command: CLAUDE_UPDATE_COMMAND,
    });
  }

  // STRICTLY `false`, never falsy. `null` means the CLI could not be ASKED
  // (missing, or older than `claude auth status`), which is not the same as it
  // answering no — and telling a signed-in user to go and sign in is the
  // wrong-advice failure this whole module is arranged to avoid.
  if (health.signed_in === false) {
    issues.push({
      id: "signed-out",
      title: "Claude Code isn't signed in",
      detail: "Open a terminal, run `claude`, type /login and finish signing in.",
      helpKind: "login",
    });
  }

  return issues;
}

/** The deep link for an issue — the download page's matching tab. */
export function issueHelpUrl(issue: ClaudeIssue): string {
  return troubleHelpUrl(issue.helpKind);
}

// -- dismissal ----------------------------------------------------------------
//
// The strip is a heads-up, so it has to be dismissible. What it must NOT be is
// dismissible once and forever: the interesting case is a user who dismisses
// "not signed in", signs in a week later, and then upgrades into a version
// problem — and must hear about that one.

const DISMISS_KEY = "fused-render.claude-health.dismissed";

/**
 * A stable identity for "this exact set of problems".
 *
 * Built from the issue IDS, sorted — never from the titles, which carry a path
 * and a version and would therefore change signature on every patch upgrade,
 * re-showing a strip the user has already dealt with. Sorted, because
 * `claudeIssues`' order is a presentation decision and must not be able to
 * invalidate a dismissal by changing.
 */
export function issuesSignature(issues: ClaudeIssue[]): string {
  return issues.map((i) => i.id).sort().join(",");
}

/** Whether this set of problems has already been dismissed.
 *
 * A DIFFERENT set is not dismissed, which is the whole point: dismissing
 * "signed-out" leaves a later "outdated" free to appear. Storage failures
 * (private mode, a full quota) read as "not dismissed" — showing a strip too
 * often is a much smaller harm than silently withholding the one fact that
 * explains why nothing works. */
export function isDismissed(issues: ClaudeIssue[]): boolean {
  if (!issues.length) return true;
  try {
    return localStorage.getItem(DISMISS_KEY) === issuesSignature(issues);
  } catch {
    return false;
  }
}

/** Remember this set as dismissed. */
export function dismiss(issues: ClaudeIssue[]): void {
  try {
    localStorage.setItem(DISMISS_KEY, issuesSignature(issues));
  } catch {
    // Nothing to do and nothing worth saying: the strip closes either way, it
    // just comes back on the next load.
  }
}

/** Forget any dismissal — for the Preferences entry point, whose whole job is
    to show the user this again on purpose. */
export function undismiss(): void {
  try {
    localStorage.removeItem(DISMISS_KEY);
  } catch {
    // see dismiss()
  }
}
