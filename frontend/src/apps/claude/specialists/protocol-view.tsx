// PROTOCOL NEVER SHOWS (docs/specialists R1, CONTRACT C3). Inside a specialist
// session (its main chat, a breakout card) the app writes some messages for
// the model: the breakout's seed, the close-out request and its marked reply,
// a revert request, a "review and send" request. The thread recognises them by
// their fixed first characters and draws the product view instead:
//
//   seed               a brief box: Problem / Tried / Look at
//   close-out request  nothing (the card's header says "Writing the summary…")
//   close-out reply    a tidy summary; the NOTE block is not shown
//   revert request     "Asked to undo: <text>"
//   send-draft request "Review and send: <subject>" with the text quoted
//
// Pure parsing first (exported for tests and the summary draft), then the views.
import type { ReactNode } from "react";
import {
  CLOSE_OUT_PREFIX,
  REVERT_PREFIX,
  SEED_PREFIX,
  SEND_DRAFT_PREFIX,
  type Brief,
} from "@platform/lib/specialists-api";
import type { Turn } from "../protocol/controller-api";

/** An older server's revert wording, still in transcripts written before C4. */
const OLD_REVERT_PREFIX = "Revert last night's change: ";

export type ProtocolKind = "seed" | "close-request" | "close-reply" | "revert" | "send-draft";

export function protocolKind(turn: Turn): ProtocolKind | null {
  if (turn.role === "user") {
    const t = (turn.text || "").trimStart();
    if (t.startsWith(SEED_PREFIX)) return "seed";
    if (t.startsWith(CLOSE_OUT_PREFIX)) return "close-request";
    if (t.startsWith(REVERT_PREFIX) || t.startsWith(OLD_REVERT_PREFIX)) return "revert";
    if (t.startsWith(SEND_DRAFT_PREFIX)) return "send-draft";
    return null;
  }
  if (turn.role === "assistant" && (turn.text || "").includes("<<<SUMMARY")) return "close-reply";
  return null;
}

/** The brief a seed was written from (`breakouts.BREAKOUT_SEED`): the task's
 *  title and the three fields. Used when the breakout record is not at hand. */
export function parseSeed(text: string): { title: string; brief: Brief } {
  const t = (text || "").trimStart();
  const title = /^Brief from the task "(.*?)":/.exec(t)?.[1] ?? "";
  // The fields run until the next field or the blank line before the
  // instructions ("The task is still running in …").
  const body = t.split(/\n\s*\nThe task is still running/)[0] ?? t;
  const at = (name: string) => body.indexOf(`\n${name}:`);
  const field = (name: string, next: string[]): string => {
    const start = at(name);
    if (start < 0) return "";
    const from = start + name.length + 2;
    const ends = next.map(at).filter((i) => i > start);
    return body.slice(from, ends.length ? Math.min(...ends) : body.length).trim();
  };
  return {
    title,
    brief: {
      problem: field("Problem", ["Tried", "Look at"]),
      tried: field("Tried", ["Look at"]),
      look_at: field("Look at", []),
    },
  };
}

/** The text between `<<<NAME` and `NAME>>>` (the last such block), as
 *  `harness.extract_block` reads it; `open` when the block has begun but not
 *  ended (a reply still streaming). */
export function extractBlock(text: string, name: string): { body: string; open: boolean } | null {
  const opener = "<<<" + name;
  const start = (text || "").lastIndexOf(opener);
  if (start < 0) return null;
  const from = start + opener.length;
  const end = text.indexOf(name + ">>>", from);
  if (end < 0) return { body: text.slice(from).trim(), open: true };
  return { body: text.slice(from, end).trim(), open: false };
}

export interface SummaryField {
  label: string;
  text: string;
}

const SUMMARY_LABELS = ["Outcome", "Found", "Do next", "Files changed", "Full session"];

/** "Outcome: …\nFound: …\n…" as labelled rows, in the order the template gives
 *  them. A line that starts no known label continues the row above it; text
 *  before the first label is kept as an "Outcome" row so nothing is lost. */
export function parseSummary(text: string): SummaryField[] {
  const out: SummaryField[] = [];
  const labelRe = new RegExp(`^\\s*(?:[-*]\\s*)?\\**(${SUMMARY_LABELS.join("|")})\\**\\s*:\\s*(.*)$`, "i");
  for (const raw of (text || "").split("\n")) {
    const m = labelRe.exec(raw);
    if (m) {
      const label = SUMMARY_LABELS.find((l) => l.toLowerCase() === m[1]!.toLowerCase()) ?? m[1]!;
      out.push({ label, text: m[2]!.trim() });
      continue;
    }
    const line = raw.trim();
    if (!line) continue;
    const last = out[out.length - 1];
    if (last) last.text = last.text ? `${last.text}\n${line}` : line;
    else out.push({ label: "Outcome", text: line });
  }
  return out;
}

/** What a revert request asks to undo: its first quoted string, else the rest
 *  of its first sentence. */
export function revertWhat(text: string): string {
  const t = (text || "").trimStart();
  const rest = t.startsWith(REVERT_PREFIX)
    ? t.slice(REVERT_PREFIX.length)
    : t.startsWith(OLD_REVERT_PREFIX)
      ? t.slice(OLD_REVERT_PREFIX.length)
      : t;
  const quoted = /["“']([^"”\n]{1,400}?)["”']/.exec(rest);
  if (quoted) return quoted[1]!.trim();
  const first = rest.split("\n")[0] ?? "";
  const cut = first.search(/\.\s/);
  return (cut > 0 ? first.slice(0, cut) : first).trim();
}

/** A send-draft request's subject and text (`drafts.SEND_DRAFT`). */
export function sendDraftParts(text: string): { to: string; subject: string; body: string } {
  const t = (text || "").trimStart();
  const to = /^Review and send this draft to (.+?) through /.exec(t)?.[1] ?? "";
  const m = /\n\s*\nSubject:[ \t]*(.*)\n\s*\n([\s\S]*)$/.exec(t);
  if (m) return { to, subject: m[1]!.trim(), body: m[2]!.trim() };
  const subj = /\nSubject:[ \t]*(.*)/.exec(t);
  return { to, subject: subj?.[1]?.trim() ?? "", body: "" };
}

// ── views ──────────────────────────────────────────────────────────────────

export function BriefBox({ brief, title }: { brief: Brief; title?: string }) {
  const row = (label: string, text: string) => (
    <>
      <span className="spx-offer-key">{label}</span>
      <span className="spx-proto-val">{text || "—"}</span>
    </>
  );
  return (
    <section className="spx-proto spx-proto-brief" aria-label="Brief from the task">
      <div className="spx-proto-head">{title ? `Brief from ${title}` : "Brief from the task"}</div>
      <div className="spx-offer-brief">
        {row("Problem", brief.problem)}
        {row("Tried", brief.tried)}
        {row("Look at", brief.look_at)}
      </div>
    </section>
  );
}

export function SummaryRows({ fields }: { fields: readonly SummaryField[] }) {
  return (
    <div className="spx-summary-rows">
      {fields.map((f, i) => (
        <div className="spx-summary-row" key={`${f.label}:${i}`}>
          <span className="spx-offer-key">{f.label}</span>
          <span className="spx-proto-val">{f.text || "—"}</span>
        </div>
      ))}
    </div>
  );
}

export function CloseReply({ text, streaming }: { text: string; streaming?: boolean }) {
  const block = extractBlock(text, "SUMMARY");
  const fields = block ? parseSummary(block.body) : [];
  if (!block || (block.open && streaming) || !fields.length) {
    return (
      <div className="spx-proto spx-proto-quiet" role="status">
        {streaming || block?.open ? "Writing the summary…" : "The summary was written."}
      </div>
    );
  }
  return (
    <section className="spx-proto spx-proto-summary" aria-label="Summary for the task">
      <div className="spx-proto-head">Summary for the task</div>
      <SummaryRows fields={fields} />
      <div className="spx-proto-foot">Its full note is saved with the specialist.</div>
    </section>
  );
}

/** The product view of one app-written turn, or `undefined` for an ordinary
 *  turn. `brief`/`taskTitle` come from the breakout record when the host has
 *  it; the seed's own text is the fallback. */
export function protocolTurnView(
  turn: Turn,
  facts: { brief?: Brief | undefined; taskTitle?: string | undefined } = {},
): ReactNode | undefined {
  const kind = protocolKind(turn);
  if (!kind) return undefined;
  const text = turn.role === "user" || turn.role === "assistant" ? turn.text || "" : "";
  switch (kind) {
    case "seed": {
      const parsed = parseSeed(text);
      const brief = facts.brief && facts.brief.problem ? facts.brief : parsed.brief;
      const title = facts.taskTitle || parsed.title;
      return <BriefBox brief={brief} {...(title ? { title } : {})} />;
    }
    case "close-request":
      return null;
    case "close-reply":
      return <CloseReply text={text} streaming={turn.role === "assistant" && !!turn.streaming} />;
    case "revert":
      return (
        <div className="spx-proto spx-proto-quiet" role="note">
          Asked to undo: {revertWhat(text) || "a change from the latest update"}
        </div>
      );
    case "send-draft": {
      const d = sendDraftParts(text);
      return (
        <div className="spx-proto spx-proto-quiet" role="note">
          <div>Review and send: {d.subject || (d.to ? `a message to ${d.to}` : "a draft")}</div>
          {d.body ? <blockquote className="spx-proto-quote">{d.body}</blockquote> : null}
        </div>
      );
    }
  }
}
