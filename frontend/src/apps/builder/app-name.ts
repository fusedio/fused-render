// Naming a new app from its prompt (the Home hero composer, HomeHero.tsx).
//
// Two rungs, so that a broken model call never leaves the user without a name:
//   1. haiku on the Claude tier of /api/ai — about a second, the best names;
//   2. the prompt's own subject words, picked here with no model — what a
//      busy relay or an unusable reply falls to.
// Claude Code missing or signed out is NOT papered over by rung 2: the result
// says so (`claude`), and the composer tells the user that auto-naming needs
// Claude Code and asks for a name, with the subject words as the suggestion.
// The model rung has a deadline, and its failure is logged with the reason:
// the old single call swallowed every error, so "naming keeps failing" could
// not be told apart from "Claude is signed out" or "the relay is busy".
//
// The composer's other agents (Copilot / Codex / Gemini) are deliberately not
// a rung: their one-shots are slow and may bill a request per name.
//
// Only a prompt with no subject word at all ("make me an app") ends with no
// name; the composer then asks for one.
import { aiComplete } from "@platform/lib/api";
import { troubleKind } from "@platform/lib/trouble";

// What a model's answer must look like once cleaned: lowercase kebab-case,
// letters and digits only, no leading/trailing/double hyphens.
const KEBAB_RE = /^[a-z0-9]+(-[a-z0-9]+)*$/;

// Normalize anything name-like to a filesystem-safe kebab-case folder name:
// lowercase, [a-z0-9-] only, at most five words. Returns "" when nothing
// usable survives.
export function kebabName(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .split("-")
    .filter(Boolean)
    .slice(0, 5)
    .join("-")
    .slice(0, 48);
}

// The refusal token. Asked for explicitly so a request that is not an app
// description gets a deterministic answer instead of an invented brand —
// Haiku, given nothing concrete, will happily produce "flux-name-bot".
const NAME_NONE = "none";

// The ask is a folder name, so it reads like one: the subject first, 2-3
// words, no filler. Examples carry more of the standard than the rules do —
// without them the model pads ("expense-tracker-app") or leads with the verb
// ("track-my-expenses"). A name the description itself states wins outright.
// The two-word examples are there because haiku answered "none" to bare asks
// like "todo list" and "notes": a short ask is still a description.
export const NAME_SYSTEM_PROMPT =
  "You name software projects. Given a description of an app, reply with a " +
  "short folder name for it in kebab-case: 1-3 lowercase words joined by " +
  "hyphens, letters and digits only, at most 4 words when 3 cannot say it.\n" +
  "Rules:\n" +
  "- Lead with the subject (what it is about), then what it does: " +
  "\"expense-tracker\", not \"track-expenses\".\n" +
  "- Use words from the description or plain words for what the app does. " +
  "Never invent brand words or puns.\n" +
  "- No filler: never app, tool, my, simple, new, basic, project, helper, " +
  "manager (unless the thing IS a manager), or the user's name.\n" +
  "- If the description names the app itself, use that name, kebab-cased.\n" +
  "- The description may be long and full of layout or code detail: name " +
  "the app it describes, ignore the detail.\n" +
  "- A short or vague description is still a description: name its subject.\n" +
  "- Only if the text names no subject at all (a greeting, a question about " +
  "you), reply with exactly: " + NAME_NONE + "\n" +
  "Examples:\n" +
  "\"a page that shows my monthly spending by category\" -> spending-by-category\n" +
  "\"something to track which plants I watered and when\" -> plant-watering-log\n" +
  "\"a kanban board for my reading list, call it Shelf\" -> shelf\n" +
  "\"todo list\" -> todo-list\n" +
  "\"notes\" -> notes\n" +
  "\"make me a website for my bakery\" -> bakery-website\n" +
  "\"hello, what can you do?\" -> " + NAME_NONE + "\n" +
  "Reply with ONLY the name — no quotes, no prose, no explanation.";

// The relay alias the Claude rung pins. Named explicitly so the call never
// depends on the relay's own default: when that followed a model preference, a
// preference of fable/opus turned a two-second utility call into a
// twenty-second one.
const NAME_MODEL = "haiku";

// How long the model may take. A warm call is about a second; this also
// covers a cold spawn (2-3s) or a short wait behind another fused.ai call on
// the relay's one shared instance. Past it the subject words are the name —
// a create must not sit on "naming" behind a hung relay.
const MODEL_DEADLINE_MS = 6_000;

// A naming ask does not need the whole of a pasted spec.
const PROMPT_CAP = 2000;

// One model reply -> a name, "none" (the model's refusal), or null (prose,
// nothing usable). Lenient about the wrapping models add in spite of "ONLY
// the name" — quotes, backticks, a trailing period, "Name: x", snake_case —
// and strict about the content: what is left must be ONE bare token. Words
// with spaces are never joined ("Let me know!" must not become
// "let-me-know"), and a reply of several lines only counts where a line is a
// hyphenated token on its own ("Sure!" above the answer is not a name). The
// subject-word rung catches whatever this turns away.
export function parseModelName(reply: string): string | "none" | null {
  const lines = reply
    .split("\n")
    .map((l) =>
      l
        .trim()
        .toLowerCase()
        .replace(/^(?:folder\s+)?name\s*[:=]\s*/, "")
        .replace(/^[\s"'`*“”‘’>-]+|[\s"'`*“”‘’.,;:!]+$/g, "")
        .replace(/_/g, "-"),
    )
    .filter(Boolean);
  if (lines.length === 1) {
    if (lines[0] === NAME_NONE) return "none";
    return KEBAB_RE.test(lines[0]) ? kebabName(lines[0]) || null : null;
  }
  const hit = lines.find((l) => l.includes("-") && KEBAB_RE.test(l));
  return hit ? kebabName(hit) || null : null;
}

// Words that say nothing about WHAT the app is: the ask itself ("make me
// a…"), filler the model is also told to drop, and grammar.
const STOP = new Set(
  (
    "a an the i im me my mine we our us you your it its this that these those " +
    "to for of in on at by with from into about and or but so as if is are be " +
    "been was can could would should will shall may might must do does did " +
    "have has had please pls kindly want wants wanted need needs like love " +
    "help let lets make makes build builds create creates generate generates " +
    "write design develop code give get show shows showing display displays " +
    "use uses using app apps application tool tools project simple new basic " +
    "small little quick nice cool some something thing things one where which " +
    "what who when how there here just really very also able hi hello hey " +
    "thanks thank all any each every style based layout"
  ).split(" "),
);

// A clause that opens with one of these asks something; it describes nothing.
const QUESTION = new Set(
  "what whats who whos whom whose why where wheres when whens how hows which".split(" "),
);

// Words for what an app is built WITH. A clause made only of these (and
// filler) is a stack preface; beside a subject word they stay in the name
// ("react-dashboard").
const STACK = new Set(
  (
    "react vue svelte angular solid next nextjs nuxt vite tailwind tailwindcss " +
    "bootstrap shadcn html css js ts javascript typescript python node nodejs " +
    "bun deno flask django fastapi sql sqlite postgres duckdb"
  ).split(" "),
);

// The last rung: the prompt's own subject words, no model. NOT a slug of the
// prompt — that produced "make-me-a-tool-that" — but its first clause with
// the ask and the filler removed: "Make me a tool that tracks gym workouts"
// -> "tracks-gym-workouts", "A habit tracker. Layout: …" -> "habit-tracker".
// Two kinds of clause are passed over, since their words are not the subject:
// a question ("What's your name?" must not become "whats-name") and a clause
// that only names the stack ("Use React and Tailwind. Build a recipe
// organizer" is a recipe organizer, not "react-tailwind").
// Returns "" when the prompt has no subject word at all.
export function nameFromPrompt(prompt: string): string {
  const words = (text: string) =>
    text
      .toLowerCase()
      .replace(/['’]/g, "")
      .split(/[^a-z0-9]+/)
      .filter(Boolean);
  const subject = prompt
    .split(/[.:;!?\n]+|\s[—–-]\s/)
    .map(words)
    .filter((ws) => ws.length > 0 && !QUESTION.has(ws[0]))
    .map((ws) => ws.filter((w) => !STOP.has(w)))
    .find((ws) => ws.some((w) => !STACK.has(w)));
  return kebabName((subject ?? []).slice(0, 3).join("-"));
}

// Why Claude Code could not be asked at all, when that is the reason: not
// installed, or installed and signed out. The relay answers the first as
// `ai_unavailable` ("claude binary not found…") and the second in the CLI's
// own words, which `troubleKind` already reads for the chat's trouble cards.
export type ClaudeGap = "install" | "login";

export function claudeGap(err: unknown): ClaudeGap | null {
  const body = (err as { body?: { error?: { message?: string } } } | null)?.body;
  const message = body?.error?.message ?? "";
  if (/binary not found/i.test(message)) return "install";
  const kind = troubleKind(message);
  if (kind === "notfound") return "install";
  if (kind === "login") return "login";
  return null;
}

export interface NameResult {
  /** The name to create under, or null: ask the user. */
  name: string | null;
  /** A name to OFFER when `name` is null (the prompt's subject words), or "". */
  suggestion: string;
  /** Set when no name was made because Claude Code is unavailable. */
  claude: ClaudeGap | null;
}

// The model rung: ask haiku, give up at the deadline.
async function askModel(
  prompt: string,
): Promise<{ name: string | "none" | null; claude: ClaudeGap | null }> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), MODEL_DEADLINE_MS);
  try {
    const text = await aiComplete(prompt, NAME_SYSTEM_PROMPT, NAME_MODEL, {
      signal: ctl.signal,
    });
    const name = parseModelName(text);
    if (name === null)
      console.warn("[app-name] unusable reply:", JSON.stringify(text.slice(0, 200)));
    return { name, claude: null };
  } catch (e) {
    // Logged, never thrown. The reason is what a "naming keeps failing"
    // report needs (signed out, binary missing, the model id refused, timed
    // out).
    console.warn(
      "[app-name] naming call failed:",
      ctl.signal.aborted ? `no answer within ${MODEL_DEADLINE_MS}ms` : e,
    );
    return { name: null, claude: ctl.signal.aborted ? null : claudeGap(e) };
  } finally {
    clearTimeout(timer);
  }
}

// A kebab-case folder name for an app described by `prompt`. `name` is null
// when the user must be asked: Claude Code is unavailable (`claude` says
// which way), or the prompt names no subject.
export async function suggestAppName(prompt: string): Promise<NameResult> {
  const asked = await askModel(prompt.length > PROMPT_CAP ? prompt.slice(0, PROMPT_CAP) : prompt);
  if (asked.name && asked.name !== "none")
    return { name: asked.name, suggestion: "", claude: null };
  const words = nameFromPrompt(prompt);
  if (asked.claude) return { name: null, suggestion: words, claude: asked.claude };
  return { name: words || null, suggestion: "", claude: null };
}
