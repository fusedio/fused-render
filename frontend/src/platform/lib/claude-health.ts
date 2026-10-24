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
import { CLAUDE_INSTALL_COMMAND, CODEX_INSTALL_COMMAND, GEMINI_INSTALL_COMMAND, troubleHelpUrl, type TroubleKind } from "./trouble";

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
  /** Nothing in the APP is worse off while this is unfixed — it is a
      convenience the user may take or leave. Set on `not-on-path` and nothing
      else, and it costs the row the accent fill everywhere it renders: an
      always-optional extra must never look like the thing standing between the
      user and a working app, and must never be the one yellow button on a
      screen (see the wizard's Next). */
  optional?: boolean;
  /** What the APP can do about it, when it can do anything.
   *
   *  This is the field that turns the strip from a notice into a repair. It is
   *  set only where the server has an endpoint that actually applies the fix,
   *  and its absence is meaningful: `unusable-override` has no action because
   *  the value lives in a shell profile we cannot edit, and offering a button
   *  that silently does nothing is worse than the sentence it replaced. */
  action?: {
    kind: "install" | "update" | "doctor" | "login" | "link-path";
    label: string;
  };
}

/** `claude update`, the fix for an install that is merely old. Not in
    lib/trouble because that module never had a case for it: an outdated CLI
    fails with an unknown-option error, which classifies as `raw` and gets told
    to work out what the error is about. Knowing the version is what turns that
    into one command. */
export const CLAUDE_UPDATE_COMMAND = "claude update";

/** The env var that points the app at a CLI it cannot otherwise see. */
export const CLAUDE_BIN_ENV = "FUSED_RENDER_CLAUDE_BIN";
/** Copilot's twin (fused_render/copilot_health.py `BIN_ENV`). */
export const COPILOT_BIN_ENV = "FUSED_RENDER_COPILOT_BIN";
/** Codex's twin (fused_render/codex_health.py, docs/CODEX_BACKEND.md D10). */
export const CODEX_BIN_ENV = "FUSED_RENDER_CODEX_BIN";
/** Gemini's twin (fused_render/gemini_health.py, docs/GEMINI_BACKEND.md D10). */
export const GEMINI_BIN_ENV = "FUSED_RENDER_GEMINI_BIN";

/** What differs between the agents' setup copy. Everything else — the rules,
 *  the order, the ids — is the same for every agent. */
interface AgentSetupWords {
  /** The product's name in a sentence. */
  name: string;
  /** The command a user types in a terminal. */
  cli: string;
  binEnv: string;
  /** The fallback install line for a server too old to state its own. */
  install?: string;
  /** The fallback update line for a self-updating install. */
  update?: string;
  /** `claude doctor` — the only agent with a diagnostics subcommand. */
  doctor: boolean;
  /** Offer the `link-path` repair. Never for Copilot (the UI has offered it
   *  for Claude alone since the second agent); Codex's and Gemini's contracts
   *  list the route (docs/CODEX_BACKEND.md D10, docs/GEMINI_BACKEND.md D10). */
  linkPath: boolean;
  /** The signed-out row's sentence: what pressing "Sign in" does. */
  signIn: string;
}

const SETUP_WORDS: Record<string, AgentSetupWords> = {
  claude: {
    name: "Claude Code",
    cli: "claude",
    binEnv: CLAUDE_BIN_ENV,
    install: CLAUDE_INSTALL_COMMAND,
    update: CLAUDE_UPDATE_COMMAND,
    doctor: true,
    linkPath: true,
    signIn: "Sign in with your browser — Claude Code opens the page and "
      + "finishes on its own.",
  },
  copilot: {
    name: "Copilot",
    cli: "copilot",
    binEnv: COPILOT_BIN_ENV,
    doctor: false,
    linkPath: false,
    signIn: "Sign in with GitHub — Copilot opens the page and finishes on its own. "
      + "A `gh auth login` on this machine counts too.",
  },
  codex: {
    name: "Codex",
    cli: "codex",
    binEnv: CODEX_BIN_ENV,
    install: CODEX_INSTALL_COMMAND,
    // npm reinstalls the newest over the old: the install line IS the update.
    update: CODEX_INSTALL_COMMAND,
    doctor: false,
    linkPath: true,
    // A DEVICE CODE, not a loopback: the strip shows the code and the page
    // to type it on while the sign-in is open (`ClaudeLoginStatus.detail`).
    signIn: "Sign in with ChatGPT (device code) — Codex shows a one-time code "
      + "to enter on OpenAI's page, and finishes on its own. A `codex login` "
      + "in a terminal counts too.",
  },
  gemini: {
    name: "Gemini CLI",
    cli: "gemini",
    binEnv: GEMINI_BIN_ENV,
    install: GEMINI_INSTALL_COMMAND,
    // npm reinstalls the newest over the old: the install line IS the update.
    update: GEMINI_INSTALL_COMMAND,
    doctor: false,
    linkPath: true,
    signIn: "Sign in with Google — Gemini CLI opens the page and finishes on "
      + "its own. A `GEMINI_API_KEY` in the environment, or a sign-in from "
      + "`gemini` in a terminal, counts too.",
  },
};

function setupWords(backend: string): AgentSetupWords {
  return SETUP_WORDS[backend] ?? SETUP_WORDS.claude;
}

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
/** The "Signed in" line for ANY agent, or null when it is not signed in.
 *  Copilot's three methods, Codex's two and Gemini's four are their own words (D10); Claude's
 *  are the setup wizard's (shell/onboarding/ToolsStep `signedInHint`), which
 *  the caller passes in so there stays one copy of them. */
export function agentAccountLine(
  health: ClaudeHealth,
  backend: string,
  claudeLine: (h: ClaudeHealth) => string,
): string | null {
  if (health.signed_in !== true) return null;
  const method = health.account?.method;
  if (backend === "codex") {
    if (method === "chatgpt") {
      const plan = health.account?.plan || health.account?.planType;
      // `planType` is Codex's word ("go", "plus"); capitalised as ChatGPT
      // names its plans.
      return plan
        ? `Signed in with ChatGPT (${plan.charAt(0).toUpperCase()}${plan.slice(1)} plan)`
        : "Signed in with ChatGPT";
    }
    if (method === "api_key") return "Signed in with an OpenAI API key";
    return "Signed in";
  }
  if (backend === "gemini") {
    const who = health.account?.email ? ` as ${health.account.email}` : "";
    if (method === "google") return `Signed in with Google${who}`;
    if (method === "api_key") return "Signed in with a Gemini API key";
    if (method === "vertex") return "Signed in through Vertex AI";
    if (method === "gateway") return "Signed in through an AI API gateway";
    return `Signed in${who}`;
  }
  if (backend !== "copilot") return claudeLine(health);
  // The user, when the CLI named one (`account.login`), and the plan.
  const who = health.account?.login ? ` as ${health.account.login}` : "";
  const plan = health.account?.plan ? ` (${health.account.plan} plan)` : "";
  if (method === "copilot") return `Signed in with copilot login${who}${plan}`;
  if (method === "gh") return `Signed in through gh${who}${plan}`;
  if (method === "token") return `Signed in with a token from the environment${who}${plan}`;
  return `Signed in${who}${plan}`;
}

export function claudeIssues(health: ClaudeHealth | null): ClaudeIssue[] {
  return agentIssues(health, "claude");
}

/**
 * The same classifier for EVERY agent (docs/COPILOT_BACKEND.md D10,
 * docs/CODEX_BACKEND.md D10, docs/GEMINI_BACKEND.md D10): each health snapshot carries the same keys, so
 * the rules are the rules — only the words change (`SETUP_WORDS`), and the
 * repairs an agent has no endpoint for (`doctor` but for Claude, `link-path`
 * for Copilot) are not offered. The Claude output is byte-identical to what
 * `claudeIssues` always said.
 */
export function agentIssues(health: ClaudeHealth | null, backend: string): ClaudeIssue[] {
  if (!health) return []; // a failed probe is not a finding — see the strip
  const issues: ClaudeIssue[] = [];
  const words = setupWords(backend);
  const name = words.name;

  if (!health.found) {
    // A pointing-at-nothing override is its own diagnosis, and telling this
    // user to install Claude Code would be wrong twice: they may well have it,
    // and the thing actually breaking is a setting they can see.
    if (health.source === "override") {
      issues.push({
        id: "unusable-override",
        title: `${words.binEnv} points at something that cannot run`,
        detail:
          `The app was told to use ${health.path ?? "a specific path"}, and there ` +
          "is no runnable file there. Correct it or unset it — with it unset, " +
          `the app looks for ${name} in the usual places.`,
        helpKind: "notfound",
      });
      return issues;
    }
    issues.push({
      id: "missing",
      title: `The app can't find ${name}`,
      detail:
        `Fused Render uses ${name} on this computer to build and fix ` +
        "things. Install it and the app will pick it up — no restart needed.",
      helpKind: "notfound",
      // THE SERVER'S LINE, not the module constant: the server knows its own
      // platform; the fallback is only for a payload from a server too old to
      // say.
      command: health.install_command || words.install,
      action: { kind: "install", label: `Install ${name}` },
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
      title: `${name} is installed, but it isn't answering`,
      detail:
        `There is a ${name} at ${health.path ?? "the resolved path"}, and it ` +
        `did not report its version when asked.${named}` +
        (words.doctor ? " Run the built-in diagnostics to see what it says about itself." : ""),
      helpKind: "notfound",
      // `doctor` is Claude's own subcommand; no other agent has a twin (D10).
      action: words.doctor ? { kind: "doctor", label: "Run diagnostics" } : undefined,
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
    // install. Homebrew, apt, dnf and apk own their own binary and
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
      title: `${name} ${health.version} is older than this app needs`,
      detail: selfUpdates
        ? `Fused Render starts sessions with options added in ${health.min_version}. ` +
          "Update it and the app will use the version you already have."
        : `Fused Render starts sessions with options added in ${health.min_version}. ` +
          `Running \`${words.update ?? `${words.cli} update`}\` here would not change anything — ` +
          `${health.update_blocked_reason ?? "something else owns this install"}` +
          (health.update_command
            ? `, so run the command below instead.`
            : `. Upgrade it the way you installed it.`),
      helpKind: "raw",
      // Whichever command actually works: `claude update`, or the owning
      // manager's own upgrade line. Never a command we know to be a no-op.
      command: health.update_command ?? (selfUpdates ? words.update : undefined),
      // A button ONLY where running it does something. A managed install gets
      // the right command to copy and no button — we will not run a `sudo` line
      // on someone's behalf, and `brew` is theirs to drive.
      action: selfUpdates
        ? { kind: "update", label: `Update ${name}` }
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
      title: `${name} isn't signed in`,
      // NOT "run `claude`, type /login" any more. That was the only route
      // through the TUI door, but `claude auth login` opens the browser itself
      // and finishes on its own loopback callback — so this is a button, and
      // the code never comes anywhere near the app. The sentence describes what
      // pressing it does, because a browser window about to appear over the app
      // is a thing to announce first.
      detail: words.signIn,
      helpKind: "login",
      action: { kind: "login", label: "Sign in" },
    });
  }

  // ONLY ON AN EXPLICIT `false` — a login-shell probe that came back empty.
  // `null` is unknown (an override, an old server) and unknown never
  // produces advice, the same rule as `signed_in` above. This is the quietest
  // issue here because nothing in the APP is wrong: the installer created the
  // binary and the app adopted it, but the installer never edits an rc file —
  // and the one warning it prints about that is suppressed by the augmented
  // PATH the app runs it under. Without this row, the terminal is the only
  // place the user finds out, as `command not found`.
  // `link-path` is the installer's gap and the agent's own endpoint; nothing
  // to say for Copilot here (`SETUP_WORDS.copilot.linkPath`).
  if (health.on_shell_path === false && words.linkPath) {
    issues.push({
      id: "not-on-path",
      title: `${name} works in this app, but not in your terminal yet`,
      detail:
        "The install finished and Fused Render is already using it. Your " +
        `terminal looks in a different set of places, so \`${words.cli}\` there says ` +
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
      // The app already works. Typing the CLI in a terminal yourself is the
      // only thing this buys, so it never takes the accent and never gates.
      optional: true,
    });
  }

  return issues;
}

/** The deep link for an issue — the download page's matching tab. */
export function issueHelpUrl(issue: ClaudeIssue): string {
  return troubleHelpUrl(issue.helpKind);
}
