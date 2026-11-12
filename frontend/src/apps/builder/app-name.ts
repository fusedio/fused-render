// Naming a new app from its prompt (the Home hero composer, HomeHero.tsx).
//
// Two rungs, so that a broken model call never leaves the user without a name:
//   1. haiku on the Claude tier of /api/ai — about a second, the best names;
//   2. the prompt's own subject words, picked here with no model — what a
//      machine with no Claude, a signed-out CLI, a busy relay or an unusable
//      reply falls to.
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

// The last rung: the prompt's own subject words, no model. NOT a slug of the
// prompt — that produced "make-me-a-tool-that" — but its first clause with
// the ask and the filler removed: "Make me a tool that tracks gym workouts"
// -> "tracks-gym-workouts", "A habit tracker. Layout: …" -> "habit-tracker".
// Returns "" when the prompt has no subject word at all.
export function nameFromPrompt(prompt: string): string {
  const pick = (text: string) =>
    text
      .toLowerCase()
      .replace(/['’]/g, "")
      .split(/[^a-z0-9]+/)
      .filter((w) => w && !STOP.has(w))
      .slice(0, 3)
      .join("-");
  const clauses = prompt.split(/[.:;!?\n]+|\s[—–-]\s/).filter((c) => c.trim());
  for (const clause of clauses.slice(0, 3)) {
    const name = kebabName(pick(clause));
    if (name) return name;
  }
  return kebabName(pick(prompt));
}

// The model rung: ask haiku, give up at the deadline.
async function askModel(prompt: string): Promise<string | "none" | null> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), MODEL_DEADLINE_MS);
  try {
    const text = await aiComplete(prompt, NAME_SYSTEM_PROMPT, NAME_MODEL, {
      signal: ctl.signal,
    });
    const name = parseModelName(text);
    if (name === null)
      console.warn("[app-name] unusable reply:", JSON.stringify(text.slice(0, 200)));
    return name;
  } catch (e) {
    // Logged, never thrown: the subject words answer instead. The reason is
    // what a "naming keeps failing" report needs (signed out, binary missing,
    // the model id refused, timed out).
    console.warn(
      "[app-name] naming call failed:",
      ctl.signal.aborted ? `no answer within ${MODEL_DEADLINE_MS}ms` : e,
    );
    return null;
  } finally {
    clearTimeout(timer);
  }
}

// A kebab-case folder name for an app described by `prompt`. null only when
// neither rung produced one — the prompt names no subject — and the caller
// then asks the user.
export async function suggestAppName(prompt: string): Promise<string | null> {
  const name = await askModel(prompt.length > PROMPT_CAP ? prompt.slice(0, PROMPT_CAP) : prompt);
  if (name && name !== "none") return name;
  return nameFromPrompt(prompt) || null;
}
