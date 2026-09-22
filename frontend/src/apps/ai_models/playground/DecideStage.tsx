// The decisions stage: show what a typed-decision model does, in one click.
//
// Laya (`laya-mlx`) is not a generator. It takes a STATE — a piece of text —
// and typed QUESTIONS, and answers each with a calibrated probability
// distribution: which of these labels (`choice`), how far along this rubric
// (`score`), is this true (`noul`). So the demonstration is not a reply to
// read but a set of bars to compare.
//
// **The questions are the main control, so they live in the main column**,
// not behind the settings cog (owner, 2026-09-22: "bring the main controls
// for a decision model to the front"). A reader meeting this model for the
// first time needs exactly three things in view: the text, what to ask about
// it, and what SHAPE of answer they want — and the answer is drawn right under
// the question that produced it, so asking and reading never split across the
// page. The cog holds only what a novice never touches: the keys the answers
// come back under, and the request JSON a page author would paste.
//
// One POST per run (`/api/ai/decide`): every question rides one request and
// the worker batches them through the encoder, so a run is milliseconds once
// the model is resident. Bars draw the RAW probabilities — they sum to one per
// question, so nothing is rescaled the way the embed stage rescales cosines —
// and the picked label is the one the model would act on. Questions and state
// are session state, never URL state (PlaygroundTab's rule).
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { decide, withModelReady, type DecideAnswer, type DecideQuestion } from "./client";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { Card } from "@platform/shadcn/ui/card";
import {
  useConfigOpen, ConfigPanel, CopyButton, RailField, StageHeader, StarterCards,
  type Starter,
} from "./controls";
import { StarterIcons } from "./starterIcons";

type QType = DecideQuestion["type"];

/** One editable question row. `criteria` is kept as the comma-separated
 *  string the reader types, and only parsed at run time — an editor that
 *  re-splits on every keystroke eats the comma the reader is about to type
 *  after. `idTouched` says the key was set by hand (or by a starter) and must
 *  not be re-derived from the question text. */
interface QuestionRow {
  /** React's key, never shown: rows are edited in place and removed from the
   *  middle, so an index key would hand row N's inputs to row N+1. */
  uid: number;
  id: string;
  idTouched: boolean;
  type: QType;
  instructions: string;
  criteria: string;
}

interface DecideSample extends Starter {
  questions: QuestionRow[];
}

let lastUid = 0;
const uid = () => ++lastUid;

const q = (id: string, type: QType, instructions: string, criteria = ""): QuestionRow =>
  ({ uid: uid(), id, idTouched: true, type, instructions, criteria });

// The examples (D465). Each is a whole scenario: the state AND the questions
// that make sense of it, one of each type where the scenario allows, so the
// first run shows all three answer shapes side by side.
const STARTERS: DecideSample[] = [
  {
    name: "Support ticket",
    icon: StarterIcons.mail,
    prompt: "I was billed twice. Please refund the duplicate today.",
    detail: "Route a complaint: which team, how urgent, is a refund being asked for",
    questions: [
      q("department", "choice", "Which team should handle this request?", "billing, technical, sales"),
      q("urgency", "score", "How urgent is this request?", "not urgent, soon, critical"),
      q("refund", "noul", "Does the customer ask for money back?"),
    ],
  },
  {
    name: "Bug report",
    icon: StarterIcons.code,
    prompt: "After the update the app crashes on launch every time on my iPhone 13. Reinstalling did not help.",
    detail: "Triage a report: severity, area, is it reproducible",
    questions: [
      q("area", "choice", "Which part of the product is this about?", "mobile app, web app, billing, account"),
      q("severity", "score", "How severe is the problem described?", "cosmetic, minor, major, blocking"),
      q("reproducible", "noul", "Does the reporter say it happens every time?"),
    ],
  },
  {
    name: "Restaurant review",
    icon: StarterIcons.bowl,
    prompt: "The pasta was excellent and the staff were lovely, but we waited forty minutes for a table we had booked.",
    detail: "Read a review: overall mood, star rating, would they return",
    questions: [
      q("sentiment", "choice", "What is the overall tone of this review?", "negative, mixed, positive"),
      q("stars", "score", "How many stars does this review read as?", "one, two, three, four, five"),
      q("returning", "noul", "Does the reviewer sound likely to come back?"),
    ],
  },
  {
    name: "Inbox",
    icon: StarterIcons.list,
    prompt: "Hi! Quick reminder that the quarterly numbers are due Friday — can you send me the draft by Thursday noon?",
    detail: "Sort an email: category, priority, does it need a reply",
    questions: [
      q("category", "choice", "What kind of email is this?", "work request, newsletter, personal, spam"),
      q("priority", "score", "How soon does this need attention?", "whenever, this week, today"),
      q("needs_reply", "noul", "Does the sender expect a reply?"),
    ],
  },
  {
    name: "Moderation",
    icon: StarterIcons.globe,
    prompt: "Great post, thanks for sharing! I learned a lot from the section on batteries.",
    detail: "Moderate a comment: is it safe to publish, how constructive",
    questions: [
      q("action", "choice", "What should a moderator do with this comment?", "publish, review, remove"),
      q("constructive", "score", "How constructive is this comment?", "hostile, neutral, helpful"),
      q("spam", "noul", "Is this comment spam or advertising?"),
    ],
  },
  {
    name: "Weather note",
    icon: StarterIcons.landscape,
    prompt: "Hail bounced off the car roof for ten minutes, then the sun came straight back out.",
    detail: "Read a note: season, how bad the weather was, was it raining",
    questions: [
      q("season", "choice", "Which season does this sound like?", "winter, spring, summer, autumn"),
      q("harshness", "score", "How harsh was the weather described?", "mild, unsettled, severe"),
      q("precipitation", "noul", "Did something fall from the sky?"),
    ],
  },
  {
    name: "Job application",
    icon: StarterIcons.plane,
    prompt: "I've led backend teams for six years, mostly Go and Postgres, and I'm looking for a staff role. My current base is 180k and I'd expect to match it.",
    detail: "Screen a cover letter: which role, how senior, is pay mentioned",
    questions: [
      q("role", "choice", "Which opening fits this candidate best?", "backend engineer, frontend engineer, data engineer, designer"),
      q("seniority", "score", "How senior does the candidate read?", "junior, mid, senior, staff"),
      q("salary", "noul", "Does the candidate mention pay?"),
    ],
  },
  {
    name: "Product review",
    icon: StarterIcons.bulb,
    prompt: "Battery lasts two days and the screen is gorgeous, but the strap broke in a month and support never replied. Would not buy again.",
    detail: "Read a gadget review: what kind of product, star rating, would they recommend it",
    questions: [
      q("product", "choice", "What kind of product is being reviewed?", "phone, watch, headphones, laptop"),
      q("rating", "score", "How many stars does this review read as?", "one, two, three, four, five"),
      q("recommends", "noul", "Would the reviewer recommend it?"),
    ],
  },
];

// The three answer shapes in a novice's words. The wire names (`choice`,
// `score`, `noul`) stay in the tooltip and the request JSON, where a page
// author looks for them.
const TYPES: { value: QType; label: string; title: string }[] = [
  { value: "choice", label: "Pick one", title: "choice — pick one of the options you list" },
  { value: "score", label: "Rate", title: "score — place the text on a scale, worst level first" },
  { value: "noul", label: "Yes / no", title: "noul — how likely the question is true of the text" },
];

// The editor's own ceilings, below anything the model enforces: questions per
// run, so the list stays a list, and options per question, so a pasted
// paragraph with commas in it does not become forty one-word labels.
const MAX_QUESTIONS = 12;
const MAX_CRITERIA = 24;

/** A result key derived from the question text: the first three words,
 *  snake_cased — `which_team_should` — so a reader who never opens the cog
 *  still gets a readable key in the JSON, and one that changes as they
 *  rewrite the question. */
function slugOf(instructions: string, fallback: string): string {
  const words = instructions.toLowerCase().replace(/[^a-z0-9\s]/g, " ").trim().split(/\s+/).filter(Boolean);
  return words.slice(0, 3).join("_") || fallback;
}

/** The wire question for one row, or the reason it cannot be sent. Rows are
 *  numbered for the reader (question 2), not keyed — the key is what the cog
 *  edits and a novice may never have seen it. */
function toWire(row: QuestionRow, n: number): { q: DecideQuestion } | { error: string } {
  if (!row.id.trim()) return { error: `Question ${n} needs a key (behind the settings cog).` };
  const instructions = row.instructions.trim();
  if (!instructions) return { error: `Question ${n} is empty — write what to decide.` };
  if (row.type === "noul") return { q: { type: "noul", instructions } };
  const criteria = row.criteria.split(",").map((c) => c.trim()).filter(Boolean);
  if (criteria.length < 2) {
    return {
      error: `Question ${n} needs at least two comma-separated ${row.type === "score" ? "levels" : "options"}.`,
    };
  }
  if (criteria.length > MAX_CRITERIA) {
    return { error: `Question ${n} lists ${criteria.length} options — up to ${MAX_CRITERIA} per question.` };
  }
  if (new Set(criteria).size !== criteria.length) {
    return { error: `Question ${n} lists the same option twice.` };
  }
  return { q: { type: row.type, instructions, criteria } };
}

/** The whole request, or the first reason it cannot be sent. ONE builder for
 *  both the run and the cog's "paste as is" JSON — built separately, the JSON
 *  quietly dropped rows the run then refused, so what a reader copied was not
 *  what ran. */
function buildWire(
  state: string,
  rows: QuestionRow[],
): { questions: Record<string, DecideQuestion> } | { error: string } {
  const questions: Record<string, DecideQuestion> = {};
  for (const [i, row] of rows.entries()) {
    const out = toWire(row, i + 1);
    if ("error" in out) return out;
    const key = row.id.trim();
    if (questions[key]) return { error: `Questions share the key "${key}" — change one behind the settings cog.` };
    questions[key] = out.q;
  }
  if (!Object.keys(questions).length) return { error: "Add at least one question." };
  if (!state.trim()) return { error: "Write the text to ask about." };
  return { questions };
}

/** `wanted`, or the first `wanted_2`, `wanted_3`… no other row already has:
 *  two questions that start with the same three words must not collide on a
 *  key the reader never saw. */
function uniqueKey(wanted: string, others: QuestionRow[]): string {
  const taken = new Set(others.map((r) => r.id));
  if (!taken.has(wanted)) return wanted;
  let n = 2;
  while (taken.has(`${wanted}_${n}`)) n += 1;
  return `${wanted}_${n}`;
}

function nextKey(rows: QuestionRow[]): string {
  let n = rows.length + 1;
  while (rows.some((r) => r.id === `q${n}`)) n += 1;
  return `q${n}`;
}

/** The per-label distribution as a quiet list: label left, a thin track bar,
 *  the percentage right in tabular figures. Every bar is neutral — the accent
 *  is spent on the verdict word above — and the row the verdict rests on
 *  (`picked`) is only the stronger label, so the list reads as detail under
 *  an answer rather than a second answer. */
function Distribution({
  rows,
  picked,
}: {
  rows: { key: string; label: string; p: number }[];
  picked: string | undefined;
}) {
  return (
    <ol className="pg-decide-dist">
      {rows.map(({ key, label, p }) => (
        <li
          key={key}
          className={"pg-decide-dist-row" + (key === picked ? " is-picked" : "")}
          title={`p = ${p.toFixed(4)}`}
        >
          <span className="pg-decide-dist-label">{label}</span>
          <span className="pg-decide-dist-track" aria-hidden="true">
            <span className="pg-decide-dist-fill" style={{ width: `${p * 100}%` }} />
          </span>
          <span className="pg-decide-dist-pct">{(p * 100).toFixed(1)}%</span>
        </li>
      ))}
    </ol>
  );
}

/** One answer as ONE line — the verdict, then the confidence and a Details
 *  toggle in small muted text — with everything else folded under it (owner,
 *  2026-09-23: "a single answer from the model, and then an expandable which
 *  lists details"). The verdict word is the question's one accent hit. A
 *  native <details> keeps the fold per row with no state to carry: opening
 *  one question's details leaves the others shut. */
function AnswerLine({
  verdict,
  title,
  confidence,
  children,
}: {
  verdict: string;
  title?: string;
  confidence: number;
  children: ReactNode;
}) {
  return (
    <details className="pg-decide-answer">
      <summary className="pg-decide-verdict">
        <span className="pg-decide-verdict-label" aria-hidden="true">Answer:</span>
        <span className="pg-decide-verdict-word" title={title}>{verdict}</span>
        <span className="pg-decide-verdict-conf" title="How sure the model is of this answer">
          {(confidence * 100).toFixed(0)}% confident
        </span>
        <span className="pg-decide-details-toggle">
          Details
          <svg
            viewBox="0 0 12 12"
            width="10"
            height="10"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <path d="M3 4.5 6 7.5 9 4.5" />
          </svg>
        </span>
      </summary>
      <div className="pg-decide-details">{children}</div>
    </details>
  );
}

/** "14 ms" under a second, "1.3 s" from there — the model's own time. */
function formatModelTime(seconds: number): string {
  return seconds < 1 ? `${Math.round(seconds * 1000)} ms` : `${seconds.toFixed(1)} s`;
}

/** The keys of a distribution in rubric order: a score's keys are the level
 *  indices ("0", "1", …) and must read worst-first, which `Object.keys` only
 *  promises for integer-like strings in practice. Anything non-numeric keeps
 *  its arrival order. */
function orderedKeys(probabilities: Record<string, number>): string[] {
  const keys = Object.keys(probabilities);
  return keys.every((k) => /^\d+$/.test(k)) ? keys.sort((a, b) => Number(a) - Number(b)) : keys;
}

/** One answer, drawn by its type, right under its question. */
function Answer({ answer }: { answer: DecideAnswer }) {
  if (answer.type === "noul") {
    const p = answer.noul ?? 0;
    const yes = p >= 0.5;
    return (
      <AnswerLine verdict={yes ? "Yes" : "No"} confidence={answer.confidence} title={`P(true) = ${p.toFixed(4)}`}>
        <Distribution rows={[{ key: "true", label: "P(true)", p }]} picked={yes ? "true" : undefined} />
      </AnswerLine>
    );
  }
  const probabilities = answer.probabilities ?? {};
  const keys = orderedKeys(probabilities);
  const mode = keys.reduce((best, k) => (probabilities[k] > (probabilities[best] ?? -1) ? k : best), keys[0]);
  if (answer.type === "score") {
    const top = Math.max(keys.length - 1, 0);
    const score = answer.score ?? 0;
    const levelName = (k: string) => answer.legend?.[k] ?? k;
    return (
      <AnswerLine
        verdict={mode ? levelName(mode) : ""}
        confidence={answer.confidence}
        title="The most likely level; the expected level is under Details"
      >
        {/* The scale as ordered steps with the expected level marked on it:
            a score is a position on a rubric, and the rail shows the position
            the way the bars below show the spread. */}
        <p className="pg-decide-expected" title="Zero-based — a probability-weighted average over the levels">
          expected {score.toFixed(2)} of {top}
        </p>
        <div className="pg-decide-rail" role="img" aria-label={`Expected level ${score.toFixed(2)} of ${top}`}>
          <span className="pg-decide-rail-line" aria-hidden="true" />
          {keys.map((k, i) => (
            <span
              key={k}
              className="pg-decide-rail-step"
              style={{ left: `${top ? (i / top) * 100 : 0}%` }}
              aria-hidden="true"
            />
          ))}
          <span
            className="pg-decide-rail-mark"
            style={{ left: `${top ? (Math.min(top, Math.max(0, score)) / top) * 100 : 0}%` }}
            aria-hidden="true"
          />
        </div>
        <Distribution
          rows={keys.map((k) => ({ key: k, label: levelName(k), p: probabilities[k] ?? 0 }))}
          picked={mode}
        />
      </AnswerLine>
    );
  }
  return (
    <AnswerLine verdict={answer.choice ?? ""} confidence={answer.confidence} title="The option the model would act on">
      <Distribution
        rows={keys.map((k) => ({ key: k, label: k, p: probabilities[k] ?? 0 }))}
        picked={answer.choice ?? mode}
      />
    </AnswerLine>
  );
}

export function DecideStage({
  model,
  downloaded,
}: {
  model: string;
  downloaded: boolean;
}) {
  const [state, setState] = useState(STARTERS[0].prompt);
  const [rows, setRows] = useState<QuestionRow[]>(STARTERS[0].questions);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [answers, setAnswers] = useState<Record<string, DecideAnswer> | null>(null);
  const [inputTokens, setInputTokens] = useState<number | null>(null);
  // Model time only (tokenise + forward passes, from the worker), not the
  // request's round trip — a run is milliseconds once the model is resident,
  // and this is the figure that shows it.
  const [modelSeconds, setModelSeconds] = useState<number | null>(null);
  // Which model produced the answers on screen — recorded at the run, not read
  // live, for the embed stage's reason: `model` is the sidebar's selection and
  // the answers below may belong to the previous one.
  const [answeredBy, setAnsweredBy] = useState<string | null>(null);
  const { open: configOpen, toggle: toggleConfig, touched: configTouched } = useConfigOpen();

  // The run is one quick POST, but the cold-start watch loop is not — leaving
  // the stage must stop it.
  const abortRef = useRef<AbortController | null>(null);
  useEffect(() => () => abortRef.current?.abort(), []);

  // What a page author would send — shown in the cog, verbatim, so the
  // Playground doubles as the example the skill points at.
  const request = useMemo(() => buildWire(state, rows), [rows, state]);
  const requestJson = "questions" in request ? JSON.stringify({ state, questions: request.questions }, null, 2) : null;

  // Arguments with the current state as their default: a sample card sets the
  // state AND the questions and runs in the same click.
  const run = async (stateText = state, questionRows = rows) => {
    const asked = stateText.trim();
    if (!asked || busy) return;
    const built = buildWire(asked, questionRows);
    if ("error" in built) {
      setError(built.error);
      // Stale bars under an error banner read as the answer to the broken form.
      setAnswers(null);
      return;
    }
    const wire = built.questions;
    setError(null);
    setBusy(true);
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const result = await withModelReady(() => decide(model, asked, wire), {
        signal: controller.signal,
        downloaded,
        onStatus: setStatus,
      });
      // A run superseded while it was out (an edit cleared the bars, a newer
      // run started) does not get to draw answers under questions it did not see.
      if (abortRef.current !== controller) return;
      setAnswers(result.answers);
      setInputTokens(result.usage?.inputTokens ?? null);
      setModelSeconds(result.providerMetadata?.local?.seconds ?? null);
      setAnsweredBy(result.response?.modelId ?? model);
    } catch (e) {
      if ((e as Error).name !== "AbortError") setError((e as Error).message);
    } finally {
      setStatus(null);
      setBusy(false);
      if (abortRef.current === controller) abortRef.current = null;
    }
  };

  const updateRow = (at: number, patch: Partial<QuestionRow>) => {
    setRows((current) =>
      current.map((r, i) => {
        if (i !== at) return r;
        const next = { ...r, ...patch };
        // A key nobody set by hand follows the question text.
        if (!next.idTouched && patch.instructions !== undefined) {
          const others = current.filter((_, j) => j !== at);
          next.id = uniqueKey(slugOf(next.instructions, nextKey(others)), others);
        }
        return next;
      }),
    );
    // Edited questions invalidate the answers rather than sitting beside them
    // — a bar under a question that no longer says that reads as a wrong answer.
    setAnswers(null);
  };

  const addRow = () => {
    setRows((current) => [
      ...current,
      { uid: uid(), id: nextKey(current), idTouched: false, type: "choice", instructions: "", criteria: "" },
    ]);
    setAnswers(null);
  };

  const removeRow = (at: number) => {
    setRows((current) => current.filter((_, i) => i !== at));
    setAnswers(null);
  };

  return (
    <div className={"pg-work pg-embed" + (configOpen ? " has-config" : "")}>
      <Card className="pg-work-card flex-none gap-3 px-(--card-spacing) [--card-spacing:--spacing(6)]">
        <StageHeader
          title="Ask questions about a text"
          configOpen={configOpen}
          onToggleConfig={toggleConfig}
        />
        <div className="pg-composer">
          <Textarea
            className="min-h-0 resize-y"
            rows={3}
            value={state}
            placeholder="The text to decide about — a message, a review, a ticket…"
            onChange={(e) => {
              setState(e.target.value);
              setAnswers(null);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void run();
            }}
          />
        </div>

        {/* The questions — the main control, always in view. A LIST, not a
            stack of cards: a quiet index, the question as the strongest text
            on the row (a borderless field that reads as text until focused),
            the answer shape and its options as one muted line under it, and
            after a run the answer under that. Hairlines between questions,
            no box around any of them. */}
        <div className="pg-decide-questions">
          <p className="pg-answer-label">Questions</p>
          <ol className="pg-decide-list">
            {rows.map((row, at) => {
              const answer = answers && !busy ? answers[row.id.trim()] : undefined;
              return (
                <li key={row.uid} className={"pg-decide-q" + (answer ? " has-answer" : "")}>
                  <span className="pg-decide-q-n" aria-hidden="true">{at + 1}</span>
                  <div className="pg-decide-q-body">
                    <div className="pg-decide-q-top">
                      <input
                        type="text"
                        className="pg-decide-q-text"
                        value={row.instructions}
                        placeholder="What do you want to know about the text?"
                        aria-label={`Question ${at + 1}`}
                        onChange={(e) => updateRow(at, { instructions: e.target.value })}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void run();
                        }}
                      />
                      <button
                        type="button"
                        className="pg-ghost-btn pg-decide-q-remove"
                        title="Remove this question"
                        aria-label={`Remove question ${at + 1}`}
                        disabled={rows.length <= 1}
                        onClick={() => removeRow(at)}
                      >
                        ×
                      </button>
                    </div>
                    <div className="pg-decide-q-meta">
                      {/* Three words, not three buttons: plain <button>s fully
                          reset (preflight is off, so the UA's 2px outset border
                          and buttonface fill show on anything less), the lit one
                          foreground weight 600 on a soft tint pill. The shadcn
                          ToggleGroup was tried here and its variant chrome kept
                          leaking through the scope. */}
                      <span className="pg-decide-seg-label" aria-hidden="true">Type:</span>
                      <div
                        className="pg-decide-seg"
                        role="radiogroup"
                        aria-label={`Answer shape for question ${at + 1}`}
                      >
                        {TYPES.map((t) => (
                          <button
                            key={t.value}
                            type="button"
                            role="radio"
                            aria-checked={row.type === t.value}
                            className={"pg-decide-seg-item" + (row.type === t.value ? " active" : "")}
                            title={t.title}
                            onClick={() => updateRow(at, { type: t.value })}
                          >
                            {t.label}
                          </button>
                        ))}
                      </div>
                      {row.type !== "noul" && (
                        <input
                          type="text"
                          className="pg-decide-q-criteria"
                          value={row.criteria}
                          placeholder={
                            row.type === "score"
                              ? "not urgent, soon, critical… (worst to best)"
                              : "billing, technical, sales…"
                          }
                          aria-label={row.type === "score" ? "Levels, worst to best" : "Options"}
                          onChange={(e) => updateRow(at, { criteria: e.target.value })}
                        />
                      )}
                    </div>
                    {answer && <Answer answer={answer} />}
                  </div>
                </li>
              );
            })}
          </ol>
          {/* The list's footer: Add where the next question would appear, and
              the run button at the END of what it depends on — the reading
              order is text, questions, ask (owner, 2026-09-22: a button above
              the questions "doesn't make sense"). "Ask" is what the reader
              does; deciding is the model's half. */}
          <div className="pg-decide-foot">
            <button
              type="button"
              className="pg-ghost-btn pg-decide-add"
              disabled={rows.length >= MAX_QUESTIONS}
              title={rows.length >= MAX_QUESTIONS ? `Up to ${MAX_QUESTIONS} questions per run` : "Add a question"}
              onClick={addRow}
            >
              + Add a question
            </button>
            <button
              type="button"
              className="btn btn-primary pg-send"
              disabled={busy || !state.trim() || !rows.length}
              title="⌘/Ctrl + Enter to ask"
              onClick={() => void run()}
            >
              {busy ? "Asking…" : "Ask"} <kbd className="pg-kbd">⏎</kbd>
            </button>
          </div>
        </div>

        <ConfigPanel open={configOpen} animated={configTouched.current}>
          <RailField
            label="Answer keys"
            hint="The names each answer comes back under in the result JSON. They follow the question text until you edit one."
          >
            <div className="pg-decide-keys">
              {rows.map((row, at) => (
                <label key={row.uid} className="pg-decide-key">
                  <span className="pg-decide-q-n" aria-hidden="true">{at + 1}</span>
                  <input
                    type="text"
                    className="pg-decide-key-input"
                    value={row.id}
                    aria-label={`Key for question ${at + 1}`}
                    onChange={(e) => updateRow(at, { id: e.target.value.replace(/\s+/g, "_"), idTouched: true })}
                  />
                </label>
              ))}
            </div>
          </RailField>
          <RailField
            label="Request"
            hint="What fused.ai.decide sends. Paste it into a page as is."
            action={requestJson != null && <CopyButton text={requestJson} label="Copy the request JSON" />}
          >
            {requestJson != null ? (
              <pre className="pg-decide-json">{requestJson}</pre>
            ) : (
              <p className="pg-decide-json-none">{"error" in request ? request.error : ""}</p>
            )}
          </RailField>
        </ConfigPanel>

        {/* Until there are answers to read, the examples. Each sets both halves
            of the scenario and runs it. */}
        {!answers && !busy && (
          <StarterCards
            samples={STARTERS}
            onPick={(sample) => {
              setState(sample.prompt);
              setRows(sample.questions);
              void run(sample.prompt, sample.questions);
            }}
          />
        )}

        {status && <p className="pg-status">{status}</p>}
        {error && <p className="pg-error">{error}</p>}
        {answers && !busy ? (
          <p className="pg-answer-label">
            Answered
            {answeredBy && (
              <span
                className="pg-answer-provenance"
                title={
                  `Answered by ${answeredBy}`
                  + (inputTokens != null ? ` — ${inputTokens} input tokens, 0 output tokens` : "")
                }
              >
                {answeredBy}
                {inputTokens != null && ` · ${inputTokens} tokens in`}
                {modelSeconds != null && (
                  <span title="Model time, not counting the request round trip">
                    {` · ${formatModelTime(modelSeconds)}`}
                  </span>
                )}
              </span>
            )}
            <button
              type="button"
              className="pg-ghost-btn pg-clear ml-auto"
              title="Clear the answers"
              onClick={() => setAnswers(null)}
            >
              Clear
            </button>
          </p>
        ) : null}
        {/* No idle "Answers" slot: the answers draw under their questions, so
            a dashed box down here promised a second place they never arrive. */}
      </Card>
    </div>
  );
}
