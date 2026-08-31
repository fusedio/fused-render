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
  id: "missing" | "unusable-override" | "outdated" | "signed-out" | "broken"
    | "not-on-path";
  /** The one-line statement. Says what is true, not what is polite. */
  title: string;
  /** What to do about it, in a sentence. */
  detail: string;
  /** Which troubleshooting tab this maps to, so the link is a deep link. */
  helpKind: TroubleKind;
  /** A terminal command that fixes it, when one does. */
  command?: string;
  /** What the APP can do about it, when it can do anything.
   *
   *  This is the field that turns the strip from a notice into a repair. It is
   *  set only where the server has an endpoint that actually applies the fix,
   *  and its absence is meaningful: `unusable-override` has no action because
   *  the value lives in a shell profile we cannot edit, and offering a button
   *  that silently does nothing is worse than the sentence it replaced. */
  action?: { kind: "install" | "update" | "doctor" | "link-path"; label: string };
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
        "things. Install it and the app will pick it up — no restart needed.",
      helpKind: "notfound",
      // THE SERVER'S LINE, not the module constant, and the difference is a
      // bug this fixes: the constant is the macOS/Linux `curl … | bash`, and it
      // used to be attached here unconditionally — so a Windows user with no
      // Claude Code was handed a command their shell cannot run. The server
      // knows its own platform; the fallback is only for a payload from a
      // server too old to say.
      command: health.install_command || CLAUDE_INSTALL_COMMAND,
      action: { kind: "install", label: "Install Claude Code" },
    });
    return issues; // everything below is about an install that exists
  }

  // FOUND, RUNNABLE-LOOKING, AND IT WILL NOT SAY WHAT VERSION IT IS. This state
  // was measured long before it was reported: the module refuses to guess a
  // cause from one failed probe, correctly, and so said nothing at all — leaving
  // a user with an app that does not work and no sentence anywhere explaining
  // why. `claude doctor` is the way out, because the diagnosis is then the CLI's
  // own rather than ours.
  //
  // Ordered before `outdated` and `signed-out` for the same reason `missing`
  // short-circuits above: neither of those can be trusted about a binary that
  // would not answer the first question we asked it.
  if (health.broken) {
    const named = health.doctor?.warnings?.length
      ? ` It reports: ${health.doctor.warnings[0].problem}.`
      : "";
    issues.push({
      id: "broken",
      title: "Claude Code is installed, but it isn't answering",
      detail:
        `There is a Claude Code at ${health.path ?? "the resolved path"}, and it ` +
        `did not report its version when asked.${named} Run the built-in ` +
        "diagnostics to see what it says about itself.",
      helpKind: "notfound",
      action: { kind: "doctor", label: "Run diagnostics" },
    });
    return issues;
  }

  // NOTHING IS SAID ABOUT source === "shell", deliberately. A binary only the
  // login shell can see is a real problem — a GUI-launched app inherits no
  // shell profile, so neither spawn path would find it — but it is one the app
  // fixes for itself: the server publishes the discovered path as the override
  // the moment it probes (claude_health.adopt), and both spawn paths already
  // honour that. Telling the user to go and set an environment variable we were
  // holding the value for was asking them to do our work, so the message is
  // gone rather than reworded. `source` stays on the payload because it is
  // worth having in a bug report, not because the strip acts on it.

  // Only ever set for a version we actually read AND that is below the floor —
  // the server never guesses this from an unreadable version string.
  if (health.outdated) {
    // WHETHER TO OFFER THE BUTTON AT ALL, and the whole subtlety of this issue.
    //
    // `claude update` only updates a CLI that updates itself — a native or npm
    // install. Homebrew, WinGet, apt, dnf and apk own their own binary and
    // answer `claude update` with "Claude is up to date!" while changing
    // nothing. An Update button there is a button that cannot work, which is a
    // worse answer than the plain sentence it replaced.
    //
    // So the gate is on an explicit `false` only, exactly like `signed_in`
    // above: `null` means the server could not tell what kind of install this
    // is, and not-knowing is not evidence that updating would fail — `claude
    // update` is the CLI's own generic answer and stays on offer.
    const selfUpdates = health.updatable !== false;
    issues.push({
      id: "outdated",
      title: `Claude Code ${health.version} is older than this app needs`,
      detail: selfUpdates
        ? `Fused Render starts sessions with options added in ${health.min_version}. ` +
          "Update it and the app will use the version you already have."
        : `Fused Render starts sessions with options added in ${health.min_version}. ` +
          `Running \`claude update\` here would not change anything — ` +
          `${health.update_blocked_reason ?? "something else owns this install"}` +
          (health.update_command
            ? `, so run the command below instead.`
            : `. Upgrade it the way you installed it.`),
      helpKind: "raw",
      // Whichever command actually works: `claude update`, or the owning
      // manager's own upgrade line. Never a command we know to be a no-op.
      command: health.update_command ?? (selfUpdates ? CLAUDE_UPDATE_COMMAND : undefined),
      // A button ONLY where running it does something. A managed install gets
      // the right command to copy and no button — we will not run a `sudo` line
      // on someone's behalf, and `brew`/`winget` are theirs to drive.
      action: selfUpdates
        ? { kind: "update", label: "Update Claude Code" }
        : undefined,
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

  // ONLY ON AN EXPLICIT `false` — a login-shell probe that came back empty.
  // `null` is unknown (Windows, an override, an old server) and unknown never
  // produces advice, the same rule as `signed_in` above. This is the quietest
  // issue here because nothing in the APP is wrong: the installer created the
  // binary and the app adopted it, but the installer never edits an rc file —
  // and the one warning it prints about that is suppressed by the augmented
  // PATH the app runs it under. Without this row, the terminal is the only
  // place the user finds out, as `command not found`.
  if (health.on_shell_path === false) {
    issues.push({
      id: "not-on-path",
      title: "Claude Code works in this app, but not in your terminal yet",
      detail:
        "The install finished and Fused Render is already using it. Your " +
        "terminal looks in a different set of places, so `claude` there says " +
        "command not found until its folder is added to your shell's PATH.",
      helpKind: "notfound",
      // The server's exact line — shown beside the button, so what the user is
      // told will run and what runs are the same sentence. Absent (fish, or a
      // binary outside the home directory) there is no button either, and the
      // row is a plain notice with the help link.
      command: health.path_fix_command ?? undefined,
      action: health.path_fix_command
        ? { kind: "link-path", label: "Add to PATH for me" }
        : undefined,
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
