// The chat's per-tool WORDING, as pure functions. Every string here is read off
// `fused_render/templates/claude/template.html` (`T`) so the native transcript
// and the legacy template describe the same call the same way — the two naming
// one tool differently on one screen is a bug that only ever shows up as
// confusion (T:13795-13815).
//
// Two rules the callers depend on:
//   * nothing here truncates a permission payload. An Allow hands the tool its
//     input verbatim, so a card that rendered a prefix would ask the user to
//     approve bytes they never saw (T:13805-13876);
//   * every value is stringified rather than coerced — a stray object must read
//     as its contents, never as "[object Object]".
import type {
  PermissionMode,
  PermissionRow,
  Question,
  QuestionOption,
  Segment,
  SwitchableMode,
  ToolSegment,
} from "./types";

/** T:13761-13762 — the two tools whose cards are not approvals. */
export const PLAN_TOOL = "ExitPlanMode";
export const ANSWERABLE_TOOL = "AskUserQuestion";

/** T:13743-13745 — where "allow all of these for the rest of the reply" is a
 *  proportionate offer. Everything else gets Allow/Deny only. */
export const WHOLE_TOOL_GRANTABLE: ReadonlySet<string> = new Set([
  "Edit",
  "Write",
  "Read",
  "Glob",
  "Grep",
  "NotebookEdit",
]);

/** T:13756 — modes a card may switch the RUNNING session into. */
export const SWITCHABLE_MODES: ReadonlySet<string> = new Set<SwitchableMode>(["acceptEdits", "auto"]);

/** T:11845 — the picker's words for a mode, reused by a resolved perm card. */
export const PERMISSION_LABELS: Record<string, string> = {
  plan: "plan first",
  prompt: "ask every time",
  acceptEdits: "auto-accept edits",
  auto: "Claude decides",
};

/** T:11911 — the strictest mode is the default. */
export const DEFAULT_PERMISSION: PermissionMode = "prompt";

/** T:13767 — mirrors agent.py's NOTE_LIMIT (D146). */
export const PLAN_NOTE_LIMIT = 2000;

/** T:14017 — the "Other" box's ceiling, flat (the composer's). */
export const OTHER_MAX_H = 200;

/** T:15238 — held together with `max-height: calc(14 * 1.55em)` on the diff. */
export const CHIP_DIFF_CLIP_LINES = 14;

/** agent.py SEGMENT_OUTPUT_CAP. The server caps `output` already; this is the
 *  defensive twin so an uncapped path cannot put a megabyte in a chip. */
export const CHIP_OUTPUT_CAP = 4000;

/** T:15160 — only the two states worth MARKING. `ok` draws nothing (a column of
 *  ticks is decoration); `running` is drawn by CSS as a real spinning ring. */
export const TOOL_STATUS_GLYPH: Record<string, string> = { running: "", ok: "", error: "✗" };

/** A value as text, JSON-encoded when it is not a string (never coerced). */
function str(v: unknown): string {
  if (typeof v === "string") return v;
  if (v === undefined || v === null) return "";
  return JSON.stringify(v) ?? "";
}

function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" ? (v as Record<string, unknown>) : {};
}

/** T:15234-15236 — lines in a string, counting "" as zero: an Edit that only
 *  adds text has no removed lines, and "-1" would be a lie about the change. */
export function segLineCount(v: unknown): number {
  return typeof v === "string" && v !== "" ? v.split("\n").length : 0;
}

/** T:13795-13812 — an MCP tool arrives as `mcp__<server>__<tool>`; the useful
 *  half is the last segment. Everything else is returned untouched. The RAW
 *  wire name is never lost: both call sites keep it as the element's `title`. */
export function prettyToolName(name: unknown): string {
  const raw = String(name === undefined || name === null ? "" : name);
  const m = /^mcp__(.+?)__(.+)$/.exec(raw);
  if (!m) return raw;
  return m[1].replace(/_+/g, " ") + ": " + m[2].replace(/_+/g, " ");
}

/** T:13913 — the pretty name as a BUTTON label ("Allow all X in this reply"). */
export function permCardLabel(pretty: string): string {
  return pretty.length > 28 ? pretty.slice(0, 27) + "…" : pretty;
}

/** T:13781-13786 — an Edit's `old_string`/`new_string` as one `-`/`+` marked
 *  block. TWO call sites (the approval card and the transcript's Edit chip),
 *  and the two showing the same edit differently would be a bug. */
export function formatEditDiff(input: unknown): string {
  const inp = asRecord(input);
  const mark = (v: unknown, ch: string) =>
    str(v)
      .split("\n")
      .map((line) => ch + " " + line)
      .join("\n");
  return mark(inp.old_string, "-") + "\n" + mark(inp.new_string, "+");
}

/** T:13871-13876 — whatever `summarizePermission` did not render, verbatim.
 *  `Object.fromEntries`, not `{}` + assignment: a payload DEFINES an own
 *  `__proto__` key and assignment would silently lose it (D161). */
export function leftoverInput(
  input: unknown,
  covered: readonly string[] | undefined,
): Record<string, unknown> | null {
  const inp = asRecord(input);
  const seen = covered ?? [];
  const rest = Object.keys(inp).filter((k) => seen.indexOf(k) < 0);
  return rest.length ? Object.fromEntries(rest.map((k) => [k, inp[k]])) : null;
}

export interface PermissionSummary {
  /** The one-line context under the header ("" when the tool has none). */
  sub: string;
  /** The verbatim payload for the `<pre>` ("" when there is none). */
  body: string;
  /** Input keys this summary RENDERED — drives `leftoverInput`. */
  covered: string[];
}

/** T:13816-13866 — what the user is actually being asked to allow. Nothing
 *  truncates; `covered` is derived from what was actually rendered. */
export function summarizePermission(row: Pick<PermissionRow, "tool" | "input">): PermissionSummary {
  const inp = asRecord(row.input);
  const used: string[] = [];
  const show = (k: string) => {
    const v = str(inp[k]);
    if (v) used.push(k);
    return v;
  };
  // An alternation: record only the winner.
  const pick = (...keys: string[]) => {
    for (const k of keys)
      if (inp[k]) {
        used.push(k);
        return str(inp[k]);
      }
    return "";
  };
  const out = (sub: string, body: string): PermissionSummary => ({ sub, body, covered: used });

  switch (row.tool) {
    case "Bash":
      return out(show("description"), show("command"));
    case "Edit": {
      const path = show("file_path");
      show("replace_all"); // rendered as the annotation below, so it is covered
      // Marked covered here, rendered by formatEditDiff off the raw input.
      show("old_string");
      show("new_string");
      return out(path + (inp.replace_all ? "  (every occurrence)" : ""), formatEditDiff(inp));
    }
    case "Write":
      return out(show("file_path"), show("content"));
    case "NotebookEdit":
      return out(show("notebook_path"), show("new_source"));
    case "Read":
      return out(pick("file_path", "path"), "");
    case "Glob":
    case "Grep": {
      const where = show("path");
      const glob = show("glob");
      return out([where, glob ? "in " + glob : ""].filter(Boolean).join("  "), show("pattern"));
    }
    case "WebFetch":
    case "WebSearch":
      return out(pick("url", "query"), show("prompt"));
  }
  // Unknown tool: the whole object is the body, so every key is covered.
  const keys = Object.keys(inp);
  used.push(...keys);
  return out("", keys.length ? JSON.stringify(inp, null, 2) : "");
}

export interface PermChoice {
  text: string;
  decision: "allow" | "deny";
  scope: "once" | "session";
  /** "" or the mode to switch the running session into. */
  mode: "" | SwitchableMode;
  /** The accent-filled button — exactly one, and Enter's target. */
  primary: boolean;
  /** Tooltip, on the escalation button only. */
  title?: string;
}

/** T:13887-13903 — the buttons a card offers. `liveMode` is the mode the run is
 *  ACTUALLY in as reported by poll, never the picker's param. */
export function permChoices(
  row: Pick<PermissionRow, "tool">,
  liveMode?: PermissionMode | "",
): PermChoice[] {
  const label = permCardLabel(prettyToolName(row.tool));
  const choices: PermChoice[] = [
    { text: "Allow", decision: "allow", scope: "once", mode: "", primary: true },
  ];
  if (WHOLE_TOOL_GRANTABLE.has(row.tool)) {
    choices.push({
      text: "Allow all " + label + " in this reply",
      decision: "allow",
      scope: "session",
      mode: "",
      primary: false,
    });
  }
  // Never mid-plan: this button's whole job is to loosen a strict mode, and
  // doing that from a side-door tool card would leave plan mode through a door
  // that is not the plan card.
  if ((liveMode || DEFAULT_PERMISSION) !== "auto" && liveMode !== "plan") {
    choices.push({
      text: "Allow, and let Claude decide from here",
      decision: "allow",
      scope: "once",
      mode: "auto",
      primary: false,
      title:
        "Stops asking for what Claude judges safe; it still escalates anything " +
        "it will not vouch for.",
    });
  }
  choices.push({ text: "Deny", decision: "deny", scope: "once", mode: "", primary: false });
  return choices;
}

export interface ChipSummaryParts {
  /** What the call DID. Fixed width in the row; ellipsizes from the right. */
  lead: string;
  /** The file it did it to. The one shrinkable half, clipped from the LEFT so
   *  the filename survives ("" for every tool without a path). */
  path: string;
}

/** T:15255-15325 — the one-liner beside the tool name, in TWO halves because
 *  the halves do not clip the same way. Always ONE line: this is a `<summary>`. */
export function toolChipSummaryParts(seg: Pick<ToolSegment, "name" | "input"> | null | undefined): ChipSummaryParts {
  const inp = asRecord(seg?.input);
  // First line only, with an ellipsis when there was more — the clip has to be
  // visible, or a heredoc reads as a one-line command.
  const line1 = (v: unknown) => {
    const s = str(v).split("\n");
    return s[0] + (s.length > 1 ? " …" : "");
  };
  const pick = (...keys: string[]) => {
    for (const k of keys) if (inp[k]) return line1(inp[k]);
    return "";
  };
  const where = (v: unknown) => (str(v) ? "  in " + line1(v) : "");
  const lead = (text: string): ChipSummaryParts => ({ lead: text, path: "" });

  switch (seg?.name) {
    case "Bash":
      return lead("$ " + line1(inp.command));
    case "Read":
    case "NotebookEdit":
      return { lead: "", path: pick("file_path", "path", "notebook_path") };
    case "Glob":
    case "Grep":
      return lead(line1(inp.pattern) + where(inp.path || inp.glob));
    case "Edit": {
      // +added -removed FIRST: a path is long enough to be clipped by the
      // summary's ellipsis, and a count that only shows on short paths is a
      // count you cannot rely on.
      const counts = "+" + segLineCount(inp.new_string) + " -" + segLineCount(inp.old_string);
      return { lead: counts, path: pick("file_path") };
    }
    case "Write":
      return { lead: "+" + segLineCount(inp.content), path: pick("file_path") };
    case "Task":
      return lead(pick("description", "subagent_type"));
    case "TodoWrite": {
      const todos = Array.isArray(inp.todos) ? inp.todos : [];
      const done = todos.filter((t) => asRecord(t).status === "completed").length;
      return lead(done + "/" + todos.length + " done");
    }
    case "WebFetch":
    case "WebSearch":
      return lead(pick("url", "query"));
    case PLAN_TOOL:
      // Not the plan's first line — that is a heading or a preamble, which
      // describes nothing. What the row has to say is that a plan HAPPENED.
      return lead("proposed a plan");
    case ANSWERABLE_TOOL: {
      const asked = (Array.isArray(inp.questions) ? inp.questions : []).find((q) => {
        const item = asRecord(q);
        return typeof item.question === "string" && item.question;
      });
      return lead(asked ? line1(asRecord(asked).question) : "");
    }
  }
  // Unknown tool — every MCP tool, and whatever the CLI grows next. The name is
  // already in the row beside this, so there is nothing honest to add.
  return lead("");
}

/** T:15332-15336 — the same summary as one string (the row's `title`).
 *  Double-space separated, and NOT with a trailing separator when the path half
 *  is missing: "+0 -0  " reads as a truncation. */
export function toolChipSummary(seg: Pick<ToolSegment, "name" | "input"> | null | undefined): string {
  const parts = toolChipSummaryParts(seg);
  if (!parts.path) return parts.lead;
  return parts.lead ? parts.lead + "  " + parts.path : parts.path;
}

/** T:15561-15571 — a presence check, not `||`: "" is a real glyph choice, and
 *  `hasOwnProperty` rather than `in` so "toString" cannot reach a function. */
export function toolStatusGlyph(status: string): string {
  return Object.prototype.hasOwnProperty.call(TOOL_STATUS_GLYPH, status)
    ? TOOL_STATUS_GLYPH[status]
    : TOOL_STATUS_GLYPH.running;
}

/** T:15481-15483 — "" is an empty result and null is "no result yet"; only the
 *  first has anything to show. Capped defensively (see CHIP_OUTPUT_CAP). */
export function chipOutput(output: string | null | undefined): string | null {
  if (typeof output !== "string" || output === "") return null;
  return output.length > CHIP_OUTPUT_CAP ? output.slice(0, CHIP_OUTPUT_CAP) : output;
}

/** T:15484-15493 — the media type lands in a `data:` URL, so it is validated
 *  rather than trusted. A rejected image is skipped, never coerced. */
export function chipImageUrl(media_type: unknown, data: unknown): string | null {
  const type = String(media_type ?? "");
  const raw = String(data ?? "");
  if (!/^image\/[\w.+-]+$/.test(type) || !/^[A-Za-z0-9+/=\s]+$/.test(raw)) return null;
  return "data:" + type + ";base64," + raw.replace(/\s+/g, "");
}

/** A `text`/`thinking`/`notice` segment's body, "" when it has none. */
export function segText(seg: Segment | null | undefined): string {
  return seg && "text" in seg && typeof seg.text === "string" ? seg.text : "";
}

export interface QuestionModel {
  /** The questions this window can actually answer, in order. */
  questions: Question[];
  /** All of them usable, none dropped, no duplicate texts (T:14061-14075). */
  answerable: boolean;
  /** One question, one choice → each option IS the button (T:14077-14079). */
  oneShot: boolean;
  /** Everything on the input beyond `questions`, for the leftover dump. */
  extra: Record<string, unknown> | null;
}

/** T:14056-14082 — the validation an AskUserQuestion card is built from, split
 *  out because it is the half worth testing without a DOM. */
export function questionModel(input: unknown): QuestionModel {
  const inp = asRecord(input);
  const raw = Array.isArray(inp.questions) ? inp.questions : [];
  const questions = raw.filter(isUsableQuestion);
  // Two questions with the SAME text is the quietest way a payload is
  // unanswerable: `answers` is keyed by question text, so the pair collapses.
  const answerable =
    questions.length > 0 &&
    questions.length === raw.length &&
    new Set(questions.map((q) => q.question)).size === questions.length;
  return {
    questions,
    answerable,
    oneShot: answerable && questions.length === 1 && !questions[0].multiSelect,
    extra: leftoverInput(inp, ["questions"]),
  };
}

/** T:14058-14060 — an option is usable when its label is a non-empty string. */
export function questionOptions(q: Question): QuestionOption[] {
  return (Array.isArray(q.options) ? q.options : []).filter((o) => {
    const opt = asRecord(o);
    return typeof opt.label === "string" && !!opt.label;
  });
}

function isUsableQuestion(q: unknown): q is Question {
  const item = asRecord(q);
  return (
    typeof item.question === "string" &&
    !!item.question &&
    questionOptions(item as unknown as Question).length > 0
  );
}

/** T:14477-14479 — the plan card's body, "" when the input carries no usable
 *  plan (in which case `plan` falls into the leftover dump instead). */
export function planBody(input: unknown): string {
  const plan = asRecord(input).plan;
  return typeof plan === "string" && plan ? plan : "";
}

/**
 * ExitPlanMode input keys the plan card deliberately does NOT surface, and
 * which must not fall into `leftoverInput`'s dump either.
 *
 * The disclosure rule behind that dump is "no input key the model chose is
 * invisible" — a payload the user is being asked to approve has to be readable
 * in full. `planFilePath` is not one of those: the CLI writes the plan to a
 * scratch file of its own and names the path back to itself, so what the dump
 * rendered was `{"planFilePath": "/Users/…/plans/make-a-3-step-plan-….md"}`
 * verbatim inside the card — an absolute internal path, on both the first
 * render and every "Keep planning" revision (QA round 3a, defect 3). It says
 * nothing about what is being approved and it is an id in the UI
 * (design-principles §2), so it is covered rather than dumped.
 *
 * `plan` is NOT in here: it is covered only when it is USABLE (`planBody`
 * returns it), because a card that swallowed an unrenderable plan would imply a
 * plan was read when none was.
 */
export const PLAN_HIDDEN_INPUT_KEYS: readonly string[] = ["planFilePath"];
