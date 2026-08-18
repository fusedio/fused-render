// When the app cannot do the thing at all — Claude Code missing, a session
// that would not start, the config that would not load, a template registry
// that will not parse (SPEC §43).
//
// **Why this is a module and not four `catch` blocks.** These failures share a
// shape: the app is fine, something *around* it is not, and the person looking
// at the screen can usually fix it in a minute IF they are told which minute to
// spend. A bare red string is the opposite of that — it names a symptom in our
// vocabulary and leaves the user to guess whether they broke it, we broke it,
// or it is broken for everyone.
//
// So every one of them resolves to the same three answers: what happened in
// plain words, what to do about it, and one block of text they can paste
// somewhere — into their own AI, or into an issue — that carries enough for
// somebody else to act on. The download page (render.fused.io) already sorts
// these into four cases with working deep links, so this file speaks its
// vocabulary rather than inventing a second one.

/** The download page's four troubleshooting tabs, verbatim (`data-err`). */
export type TroubleKind = "notfound" | "login" | "limit" | "raw";

export const HELP_BASE = "https://render.fused.io";

/** The install line the download page tells people to run (guide step 1). */
export const CLAUDE_INSTALL_COMMAND = "curl -fsSL https://claude.ai/install.sh | bash";

// TWO TIERS, and the split is the whole correctness argument.
//
// Some phrases NAME the thing — "claude cli not found", "please run /login" —
// and mean what they say wherever they appear. Others describe only the SHAPE
// of a failure: `ENOENT`, "no such file", "command not found", "authentication
// failed". Those say nothing about the subject, and this app produces them
// constantly about files, mounts and credentials that have nothing to do with
// Claude Code.
//
// Treating the second tier as unconditional is how "could not write the
// incident file: [Errno 2] No such file or directory" ends up telling a user to
// install Claude Code — a disk-path problem answered with a download link. That
// is the same wrong-advice failure TR-2 exists to prevent, arriving from the
// other side, so a shape only counts when the message is ABOUT Claude.
const ABOUT_CLAUDE = /claude/i;

// Unconditional: these name Claude Code or quote the CLI's own vocabulary.
const NAMED: [TroubleKind, RegExp][] = [
  ["login", /please run \/login|invalid api key/i],
  ["limit", /usage limit|session limit/i],
  ["notfound", /claude code isn'?t installed|claude cli not found|claude not found/i],
];

// Only when the message is about Claude — see above.
const SHAPES: [TroubleKind, RegExp][] = [
  ["login", /not (?:signed|logged) in|oauth|authenticat/i],
  ["limit", /rate.?limit|quota/i],
  ["notfound", /couldn'?t be found|command not found|enoent|no such file/i],
];

/** Which of the download page's cases this message is, for the deep link and
    the wording. Anything unrecognised is `raw`, which is a real answer there
    ("Some other error message") and not a fallback we invented.
    
    Both tiers are ordered most-specific first within themselves: "not signed
    in" and "usage limit" are things a FOUND claude says, so they are tested
    before the could-not-find patterns, which are the broadest. */
export function troubleKind(message: string): TroubleKind {
  const text = String(message || "");
  for (const [kind, re] of NAMED) if (re.test(text)) return kind;
  if (ABOUT_CLAUDE.test(text)) {
    for (const [kind, re] of SHAPES) if (re.test(text)) return kind;
  }
  return "raw";
}

/** The troubleshooting section, opened on the matching case. The page reads
    `#troubleshooting-<kind>` and selects that tab (its `honorHash`), so this is
    a deep link and not just a pointer at a long page. */
export function troubleHelpUrl(kind: TroubleKind): string {
  return `${HELP_BASE}/#troubleshooting-${kind}`;
}

/** Whether this failure is about Claude Code itself rather than about us —
    the cases where installing or signing in is the fix. */
export function isClaudeTrouble(kind: TroubleKind): boolean {
  return kind !== "raw";
}

export interface TroubleFacts {
  /** Where fused-render is installed — the first thing anyone asks. */
  install_root?: string;
  version?: string;
  platform?: string;
  python?: string;
  /** The route this happened on, when it narrows anything down. */
  page?: string;
}

export interface TroubleContext extends TroubleFacts {
  /** What the app was trying to do, in the user's terms. */
  what: string;
  /** The failure, verbatim. Never reworded — a paraphrase is not searchable. */
  error: string;
}

/** The block behind "Copy the details".
 *
 * Written to be PASTED — into the user's own AI, or into an issue — so it is
 * self-describing rather than a dump: it says what was happening before it says
 * what went wrong, and it carries the installation it happened in, because
 * "which copy of the app is this?" is the question every answer depends on and
 * the one a user cannot look up.
 *
 * Deliberately plain text with no secrets: paths, a version, an OS string and
 * the error. Nothing here is read from the user's files.
 */
export function troubleReport(ctx: TroubleContext): string {
  const kind = troubleKind(ctx.error);
  const lines = [
    "Fused Render — problem report",
    "",
    `What the app was doing: ${ctx.what}`,
    "",
    "Error:",
    String(ctx.error || "(no message)").trim(),
    "",
  ];
  const facts: string[] = [];
  if (ctx.version) facts.push(`Fused Render v${ctx.version}`);
  if (ctx.install_root) facts.push(`Installation: ${ctx.install_root}`);
  if (ctx.platform) facts.push(`Platform: ${ctx.platform}`);
  if (ctx.python) facts.push(`Python: ${ctx.python}`);
  if (ctx.page) facts.push(`Page: ${ctx.page}`);
  if (facts.length) lines.push(...facts, "");
  lines.push(`Help: ${troubleHelpUrl(kind)}`);
  return lines.join("\n");
}


/** How to find the installation when we cannot state it.
 *
 * SEVERAL SURFACES HAVE NO PATH TO STATE, for two different reasons. The boot
 * failure cannot describe itself at all — `/api/config` is what failed, so
 * there is no version, no platform and no install root. The others simply do
 * not pay for one: the preview fallback and the builder's hero render from
 * config alone, and the snapshot that carries the install root costs a `brew`
 * shell-out, which is not a thing to spend on a card that may render on any
 * failed preview. The chat template never knows, being a page rendered inside
 * the app.
 *
 * The brief is the same in all of them, so the line says only what is true
 * everywhere — I do not know — and never why. Naming a cause ("the failure
 * above is what would have told me") reads as a deduction, and an agent that
 * catches the app deducing wrong about its own state has reason to discount
 * everything else in the brief.
 *
 * What matters is that SOME answer is here: a brief that says "something around
 * Fused Render is broken, please fix it" and names no directory sends an agent
 * to look for an app it has no way to locate; it will guess, or ask, and both
 * cost the user the minutes this button exists to save.
 *
 * So the path is a FACT when we have it and a TASK when we do not, and the
 * order is the order people actually install this: the DMG drops
 * `FusedRender.app` into Applications, and that is where to look first. The
 * second line is the same bundle one level in — the Python the app runs, which
 * is what a fix session edits, and not something anyone would guess from the
 * .app alone.
 *
 * `brew` is here for a different question. The cask's `app` artifact moves the
 * bundle to the same place, so it does not find a NEW path; it answers who
 * MANAGES that path, which is what decides whether the fix ends in
 * `brew reinstall --cask` or in dragging a DMG over the top (`reinstall_advice`
 * splits on exactly this).
 *
 * DELIBERATELY NO `python3 -c "import fused_render"` OR `pip show`. Neither is
 * a supported way to install this, and both mislead: a bare `python3` on PATH
 * is not the interpreter inside the bundle, so it either fails or finds an
 * unrelated copy — and an agent handed that path goes and edits a copy the app
 * does not run.
 */
export const FIND_INSTALL_COMMANDS: string[] = [
  "ls -d /Applications/FusedRender.app ~/Applications/FusedRender.app",
  "ls -d /Applications/FusedRender.app/Contents/Resources/lib/python3.*/fused_render   # the code a fix edits",
  "brew list --cask fused-render   # if this answers, Homebrew manages that copy",
  'dir "%LOCALAPPDATA%\\Programs\\FusedRenderPy"   # Windows',
];

/** User data — NOT the installation, and the distinction is load-bearing: a
    reinstall replaces one and never touches the other. It holds the template
    registry, which is itself one of the four failures here (§43). */
export const USER_DATA_DIR = "~/.fused-render";

/** This session's log. Per-pid in the system temp dir (fused_render/logs.py),
    so it is a glob rather than a path, and `-t` puts the live one first. */
export const LOG_LIST_COMMAND =
  'ls -t "${TMPDIR:-/tmp}"/fused-render-*.log | head -3';

/** The "where to look" section of the agent brief. */
function whereToLook(ctx: TroubleContext): string[] {
  const lines = ["Where to look on this machine:"];
  if (ctx.install_root) {
    lines.push(`- The installed app: ${ctx.install_root}`);
  } else {
    lines.push(
      "- I do not know where the app is installed, and little else here is",
      "  actionable without it. Find it first, and say which command answered:"
    );
    FIND_INSTALL_COMMANDS.forEach((cmd) => lines.push(`    ${cmd}`));
  }
  lines.push(
    `- Settings, the template registry and the staged core templates: ${USER_DATA_DIR}`,
    "  (a different place from the installation — a reinstall does not touch it)",
    `- This session's log: ${LOG_LIST_COMMAND}`,
    ""
  );
  return lines;
}

/** The block behind "Copy Claude Code instructions".
 *
 * A DIFFERENT DOCUMENT from `troubleReport`, and the difference is the reader.
 * The report describes a problem to a person: here is what broke, here is which
 * copy of the app it broke in. This is a brief for an agent that can act — it
 * states the goal, names the checks worth running first, and says what "fixed"
 * looks like, because a prompt that only pastes an error gets a guess back.
 *
 * The steps are per-case, since the useful first command is different for each:
 * there is no point asking an agent to check PATH when the CLI is installed and
 * simply signed out.
 */
export function troubleInstructions(ctx: TroubleContext): string {
  const kind = troubleKind(ctx.error);
  const steps: Record<TroubleKind, string[]> = {
    notfound: [
      "Check whether the CLI exists and where: `which claude`, and look in ~/.local/bin and /opt/homebrew/bin.",
      "If it is missing, install it: `curl -fsSL https://claude.ai/install.sh | bash`.",
      "If it exists but the app cannot see it, the app's PATH is the problem — a GUI app on macOS does not inherit a shell's PATH. Say which shell profile sets it and what the app would need instead.",
      "Confirm with `claude --version`, then tell me to quit Fused Render and reopen it.",
    ],
    login: [
      "Confirm the CLI runs: `claude --version`.",
      "Sign in: run `claude`, then `/login`, and complete it in the browser.",
      "Confirm the session works, then tell me to retry in Fused Render.",
    ],
    limit: [
      "Confirm this is a plan limit rather than a fault — the message should say when it resets.",
      "Tell me when it resets and whether anything else is worth doing in the meantime.",
      "Do not change any configuration for this: nothing is broken.",
    ],
    raw: [
      "Work out what this error is actually about before changing anything.",
      "If the app's own server is what failed (a fetch error, a 5xx, a refused connection), check whether fused-render is still running and read the log named above before assuming the app itself is broken.",
      "If it names a path, check whether that path exists and is writable.",
      "Tell me the smallest change that would fix it, and what to check afterwards.",
    ],
  };
  const lines = [
    "I am using Fused Render (a local file explorer / app builder) and something",
    "around it is broken. Please diagnose and fix it on this machine.",
    "",
    `What the app was doing: ${ctx.what}`,
    "",
    "The exact error it reported:",
    String(ctx.error || "(no message)").trim(),
    "",
  ];
  const facts: string[] = [];
  if (ctx.version) facts.push(`Fused Render version: ${ctx.version}`);
  if (ctx.install_root) facts.push(`Fused Render is installed at: ${ctx.install_root}`);
  if (ctx.platform) facts.push(`Platform: ${ctx.platform}`);
  if (ctx.python) facts.push(`Python: ${ctx.python}`);
  if (facts.length) lines.push(...facts, "");
  lines.push(...whereToLook(ctx));
  lines.push("Please:");
  steps[kind].forEach((step, i) => lines.push(`${i + 1}. ${step}`));
  lines.push(
    "",
    "Explain what you find in plain language before you change anything, and tell",
    "me what to check in the app once you are done."
  );
  return lines.join("\n");
}

// -- The template registry, which fails PARTIALLY -----------------------------
//
// A `~/.fused-render` registry that will not parse does NOT leave a file with no
// views: the built-in registry still matches, so `.csv` still previews and only
// the USER's own bindings quietly stop applying. That is the reported symptom —
// "apps aren't rendering properly" — and it is invisible by construction,
// because what the user sees is the app working slightly wrong rather than
// failing.
//
// So the fallback card (a file with NO view at all) is not enough on its own: it
// only fires in the total case. The partial case needs to be announced, and
// announced ONCE — the error rides every stat, so a card per file would be a
// card on every click for as long as the registry stays broken.
const announced = new Set<string>();

/** The sticky registry toast's whole lifecycle, in one place.
 *
 * IT LIVES HERE RATHER THAN IN THE COMPONENT because it is a state machine
 * with three transitions and no rendering, and the two bugs it has already had
 * were both in the transitions: an ungated push (a toast on a file that had a
 * better answer beside it) and an overwritten id (a second kind of broken
 * pushing a new toast while the first stayed on screen for good). A component
 * effect is not a testable place to keep three transitions.
 *
 * The rules, in the order they are checked:
 *
 *  1. NO ERROR — the registry parses again, so whatever claim is up is now
 *     false: dismiss it, and forget the message so the same fault arriving
 *     later is announced again rather than reading as a fix that held.
 *  2. NOT ANNOUNCEABLE — this file has a better answer beside it, so say
 *     nothing AND leave any existing toast alone. Walking onto a file with no
 *     view must not take down a claim that is still true.
 *  3. ALREADY SAID — the error rides every stat; saying it once is the point.
 *  4. A DIFFERENT ERROR SUPERSEDES the old one. The registry has one state at a
 *     time, so the previous toast is stale by definition and comes down as the
 *     new one goes up. Without this the two stack, and only the newer is ever
 *     dismissed.
 */
export function syncRegistryToast(
  message: string,
  announceable: boolean,
  io: { push: () => number; dismiss: (id: number) => void }
): void {
  const text = String(message || "").trim();
  if (!text) {
    if (outstandingToast !== null) {
      io.dismiss(outstandingToast);
      outstandingToast = null;
      resetAnnouncedTemplateErrors();
    }
    return;
  }
  if (!announceable) return;
  if (!shouldAnnounceTemplateError(text)) return;
  if (outstandingToast !== null) io.dismiss(outstandingToast);
  outstandingToast = io.push();
}

/** The toast currently claiming the registry is broken, if any. Module state
    because the toast outlives any one view: it is pushed while looking at one
    file and is still up several files later, which is the point of it. */
let outstandingToast: number | null = null;

/** Whether this registry error still needs saying, marking it said.
 *
 * Keyed on the MESSAGE rather than on the path: one broken registry produces
 * one error text across every file in the app, and the user needs to hear about
 * the registry once, not about each file that noticed it. Editing the registry
 * to a NEW kind of broken is a new message and is therefore said again, which
 * is right — that is a different mistake.
 *
 * Module state, so it resets on reload. A fault the user has fixed should not
 * stay quiet because a previous session already mentioned it.
 */
export function shouldAnnounceTemplateError(message: string): boolean {
  const key = String(message || "").trim();
  if (!key || announced.has(key)) return false;
  announced.add(key);
  return true;
}

/** Test seam — the set is module state and suites share a module. */
export function resetAnnouncedTemplateErrors(): void {
  announced.clear();
  outstandingToast = null;
}
