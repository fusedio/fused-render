// One tool call, as a receipt for something that already happened (T:15496-15589
// for the row, T:15342-15494 for the body).
//
// Deliberately quieter than a permission card — a card is a question and has to
// be seen, a chip is a record, and a transcript of forty loud cards is
// unreadable. Collapsed by default; the reader's click is the only thing that
// opens one and it sticks (see cardPolicy).
//
// EVERY string below goes in as a text node. The one exception is a plan
// (D248), which is markdown the model wrote for a human and goes through
// MarkdownView like the reply itself.
import { memo } from "react";

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@platform/shadcn/ui/collapsible";
import { cn } from "@platform/lib/utils";

import {
  ANSWERABLE_TOOL,
  CHIP_DIFF_CLIP_LINES,
  chipImageUrl,
  chipOutput,
  formatEditDiff,
  leftoverInput,
  PLAN_TOOL,
  prettyToolName,
  toolChipSummary,
  toolChipSummaryParts,
  toolStatusGlyph,
} from "../protocol/summaries";
import type { ToolSegment } from "../protocol/types";
import { useCardOpen } from "./cardPolicy";
import { MarkdownView } from "./MarkdownView";

function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" ? (v as Record<string, unknown>) : {};
}

function text(v: unknown): string {
  if (typeof v === "string") return v;
  if (v === undefined || v === null) return "";
  return JSON.stringify(v) ?? "";
}

/** T:15368-15393 — one span per LINE and no "\n" text nodes between them: the
 *  spans are `display: block` (that is what makes the +/- colour a full-width
 *  band), so a newline character between two of them renders as an extra empty
 *  line inside every band. The breaks a copy needs are put back by
 *  `enhanceCodeBlocks`'s `copyText` (which joins `:scope > span`). */
function EditDiff({ input }: { input: Record<string, unknown> }) {
  const lines = formatEditDiff(input).split("\n");
  // `clipped` only when there IS something below the fold: the mask paints the
  // box, not the overflow, so an unconditional fade dissolves the last line of
  // a two-line diff.
  const cls = lines.length > CHIP_DIFF_CLIP_LINES ? "diff clipped" : "diff";
  return (
    <pre className={cls}>
      {lines.map((line, i) => (
        <span
          key={i}
          className={line.charAt(0) === "+" ? "diff-add" : line.charAt(0) === "-" ? "diff-del" : ""}
        >
          {line}
        </span>
      ))}
    </pre>
  );
}

/** The keys each branch RENDERS. Anything else lands in the leftover dump —
 *  a key that changes what the tool did (`run_in_background`, `timeout`, a flag
 *  added after this was written) must not be invisible because our renderer
 *  predates it (T:15476-15477). */
function usedKeys(seg: ToolSegment, inp: Record<string, unknown>): string[] {
  switch (seg.name) {
    case "Edit":
      return ["file_path", "old_string", "new_string", "replace_all"];
    case "Write":
      return ["file_path", "content"];
    case "Bash":
      return ["command", "description"];
    case "TodoWrite":
      return ["todos"];
    case PLAN_TOOL:
      // ...and when `plan` is not a usable string it stays UNUSED, so it falls
      // into the dump: a chip must never imply a plan was read.
      return typeof inp.plan === "string" && inp.plan ? ["plan"] : [];
    case ANSWERABLE_TOOL:
      return Array.isArray(inp.questions) ? ["questions"] : [];
    default:
      return Object.keys(inp);
  }
}

function ChipBody({ seg }: { seg: ToolSegment }) {
  const inp = asRecord(seg.input);
  const extra = leftoverInput(inp, usedKeys(seg, inp));
  const out = chipOutput(seg.output);
  return (
    <>
      {renderInput(seg, inp)}
      {extra ? <pre>{JSON.stringify(extra, null, 2)}</pre> : null}
      {out === null ? null : <pre className="chip-out">{out}</pre>}
      {(Array.isArray(seg.images) ? seg.images : []).map((img, i) => {
        const url = chipImageUrl(img?.media_type, img?.data);
        // A rejected image is skipped, not coerced: inventing `image/png` for
        // something that said it was text/html would be guessing about bytes we
        // did not read.
        if (!url) return null;
        return <img key={i} className="chip-img" src={url} alt={seg.name + " result image"} />;
      })}
    </>
  );
}

function renderInput(seg: ToolSegment, inp: Record<string, unknown>) {
  switch (seg.name) {
    case "Edit":
      return (
        <>
          {/* The target path, WHOLE and wrapped: the summary row clips it from
              the left to keep the filename, but a 120-character path still
              loses its middle there — and the middle is which repo, which
              worktree, which package. */}
          <PathLabel value={inp.file_path} />
          {inp.replace_all ? <div className="chip-flag">every occurrence</div> : null}
          <EditDiff input={inp} />
        </>
      );
    case "Write":
      return (
        <>
          <PathLabel value={inp.file_path} />
          <pre>{typeof inp.content === "string" ? inp.content : ""}</pre>
        </>
      );
    case "Bash":
      return (
        <>
          {inp.description ? <div className="chip-label">{String(inp.description)}</div> : null}
          <pre>{typeof inp.command === "string" ? inp.command : ""}</pre>
        </>
      );
    case "TodoWrite":
      return (
        <>
          {(Array.isArray(inp.todos) ? inp.todos : []).map((raw, i) => {
            const t = asRecord(raw);
            const done = t.status === "completed";
            return (
              <div key={i} className={cn("chip-todo", done && "done")}>
                {(done ? "☑ " : "☐ ") + String(t.content || t.activeForm || "")}
              </div>
            );
          })}
        </>
      );
    case PLAN_TOOL: {
      // On a restored transcript the plan CARD is gone and this chip is the only
      // record that a plan was ever proposed, so a raw JSON dump would be a
      // record of the bytes rather than of the plan (D248).
      const plan = typeof inp.plan === "string" && inp.plan ? inp.plan : "";
      return plan ? <MarkdownView className="plan-body chip-plan" text={plan} /> : null;
    }
    case ANSWERABLE_TOOL: {
      // Structured plain text, never markdown: the labels are what the answer
      // is keyed by, and an option that renders as anything other than its
      // literal text is an option the user cannot check against their answer.
      const questions = Array.isArray(inp.questions) ? inp.questions : null;
      if (!questions) return null;
      return (
        <>
          {questions.map((raw, qi) => {
            const q = asRecord(raw);
            return (
              <div key={qi}>
                <div className="chip-label chip-ask-q">{text(q.question)}</div>
                {(Array.isArray(q.options) ? q.options : []).map((rawOpt, oi) => {
                  const o = asRecord(rawOpt);
                  const label = text(o.label);
                  const desc = text(o.description);
                  return (
                    <div key={oi} className="chip-label chip-ask-o">
                      {"  ○ " + label + (desc ? " — " + desc : "")}
                    </div>
                  );
                })}
              </div>
            );
          })}
        </>
      );
    }
    default:
      // No renderer for this tool, so the input IS the body: a chip that showed
      // part of an unknown call would be a chip that misdescribed what ran.
      return Object.keys(inp).length ? <pre>{JSON.stringify(inp, null, 2)}</pre> : null;
  }
}

function PathLabel({ value }: { value: unknown }) {
  const s = text(value);
  return s ? <div className="chip-label chip-label-path">{s}</div> : null;
}

export interface ToolChipProps {
  seg: ToolSegment;
  /** Collapse-policy key (cardPolicy.cardKey). */
  cardKey: string;
}

/** MEMOIZED: a replayed tool call is the same segment object poll after poll,
 *  and a chip's body is the most expensive thing in a long turn. */
export const ToolChip = memo(function ToolChip({ seg, cardKey }: ToolChipProps) {
  const [open, toggle] = useCardOpen(cardKey);
  const raw = String(seg.name || "tool");
  const pretty = prettyToolName(raw);
  const parts = toolChipSummaryParts(seg);
  const full = toolChipSummary(seg);
  const status = String(seg.status || "running");
  return (
    <Collapsible open={open} onOpenChange={toggle} className={cn("toolchip", open && "is-open")}>
      <CollapsibleTrigger
        className="chip-summary"
        {...(pretty !== raw ? { title: raw } : {})}
      >
        <span className="chip-marker" aria-hidden="true">
          ▶
        </span>
        <span className="chip-row">
          {/* `is-` prefixed, and not the bare status word: `error` alone
              collided with `.turn.error`'s red pill, which repainted a
              one-character glyph as a badge. */}
          <span className={"chip-status is-" + status}>{toolStatusGlyph(status)}</span>
          <span className="chip-name">{pretty}</span>
          {/* Two spans, not one string: `lead` keeps the row's fixed width and
              `path` is the one item allowed to shrink, so it ellipsizes from
              the LEFT and the filename survives. The <bdi> keeps the path's own
              characters in logical order inside that rtl box. */}
          <span
            className={cn("chip-sub", parts.path && "has-path")}
            {...(full ? { title: full } : {})}
          >
            <span className="chip-lead">{parts.lead}</span>
            <span className="chip-path">
              <bdi>{parts.path}</bdi>
            </span>
          </span>
        </span>
      </CollapsibleTrigger>
      <CollapsibleContent className="chip-body">
        <ChipBody seg={seg} />
      </CollapsibleContent>
    </Collapsible>
  );
});

export default ToolChip;
