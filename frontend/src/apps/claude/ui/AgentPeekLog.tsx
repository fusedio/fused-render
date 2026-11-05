// THE AGENT PEEK'S LOG — one subagent drawn as a read-only chat: the prompt
// its parent sent as clamped prose, its own rows (the Agent tool segment's
// `children`) as the reply, the work before its answer folded behind one `show
// more`. The same `.chat-log` column and `Turn` reply as the chat itself, with
// no composer and no turn actions. The prompt is drawn here, not by `Turn`: a
// prompt Claude wrote is markdown, and a chat's user bubble shows typed words
// as typed. Its own module so the shell's overlay
// (`BreakoutCards`) can lazy-load it, like `SegmentView`.
import { Fragment, useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
import { cn } from "@platform/lib/utils";
import type { AssistantTurn, UserTurn } from "../protocol/controller-api";
import type { Segment, ToolSegment } from "../protocol/types";
import { viewKind } from "../protocol/segments";
import { CardPolicyProvider, createCardPolicy, useCardOpen, useHoldTail } from "./cardPolicy";
import { MarkdownView } from "./MarkdownView";
import { RunTrigger } from "./RunTrigger";
import { SegmentView } from "./SegmentView";
import { Turn } from "./Turn";

/** A subagent writes a sentence before nearly every call, so its rows split at
 *  the last call: everything up to it is the work, what follows is the answer.
 *  Calls still running stay in the answer, where they are being watched: the
 *  split lands at the first of them, so a parallel batch shows whole. */
function splitWork(seg: ToolSegment): { work: Segment[]; answer: Segment[] } {
  const rows = seg.children ?? [];
  let last = -1;
  for (let i = rows.length - 1; i >= 0 && last < 0; i--) if (viewKind(rows[i]) !== "text") last = i;
  if (last < 0) return { work: [], answer: rows };
  const running = rows.findIndex((r) => r.kind === "tool" && r.status === "running");
  const at = running >= 0 ? running : last + 1;
  return { work: rows.slice(0, at), answer: rows.slice(at) };
}

/** The rows folded behind the work trigger: the subagent's prose and calls
 *  before its answer. */
export const agentPeekWork = (seg: ToolSegment): Segment[] => splitWork(seg).work;

/** The two turns a subagent reads as. No prompt (a synthetic orphan parent,
 *  whose call was before the transcript window) means no prompt turn. The
 *  reply is the answer alone; its work is `agentPeekWork`. */
export function agentPeekTurns(seg: ToolSegment): (UserTurn | AssistantTurn)[] {
  const prompt = typeof seg.input?.prompt === "string" ? seg.input.prompt.trim() : "";
  const reply: AssistantTurn = {
    role: "assistant",
    key: "agent-reply",
    text: "",
    segments: splitWork(seg).answer,
    ...(seg.status === "running" ? { streaming: true } : {}),
  };
  return prompt ? [{ role: "user", key: "agent-prompt", text: prompt }, reply] : [reply];
}

/** The prompt as prose under a muted label, clamped to a few lines with the
 *  transcript's own `show more`; the control only appears when the text runs
 *  past the clamp. */
function PeekPrompt({ id, text }: { id: string; text: string }) {
  const [open, toggle] = useCardOpen("agent-peek-prompt:" + id);
  const [long, setLong] = useState(false);
  const body = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const el = body.current;
    if (el && !open) setLong(el.scrollHeight > el.clientHeight + 1);
  }, [text, open]);
  return (
    <div className="agent-peek-prompt">
      <div className="agent-peek-label">Prompt</div>
      <div ref={body} className={cn("agent-peek-prompt-text", !open && "is-clamped")}>
        <MarkdownView className="seg-text" text={text} />
      </div>
      {long ? <RunTrigger open={open} onToggle={toggle} /> : null}
    </div>
  );
}

/** The folded work, behind one `show more` above the answer. */
function PeekWork({ id, work }: { id: string; work: Segment[] }) {
  const [open, toggle] = useCardOpen("agent-peek-work:" + id);
  return (
    <div className="agent-peek-work">
      <RunTrigger open={open} onToggle={toggle} />
      {open ? <SegmentView segments={work} /> : null}
    </div>
  );
}

function PeekLog({ seg }: { seg: ToolSegment }) {
  const turns = useMemo(() => agentPeekTurns(seg), [seg]);
  const work = useMemo(() => agentPeekWork(seg), [seg]);
  // Follow the tail while the reader is at it: a live subagent grows at the
  // bottom, and a reader who scrolled up to read is left where they are.
  const wrap = useRef<HTMLDivElement>(null);
  const atTail = useRef(true);
  // Opening a fold grows the log; it is the reader asking to read, so it drops
  // the follow like a scroll up does (ui/cardPolicy `useHoldTail`).
  useHoldTail(useCallback(() => {
    atTail.current = false;
  }, []));
  useLayoutEffect(() => {
    const el = wrap.current;
    if (el && atTail.current) el.scrollTop = el.scrollHeight;
  }, [turns]);
  const onScroll = () => {
    const el = wrap.current;
    if (el) atTail.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  };
  return (
    <div className="chat-logwrap" ref={wrap} onScroll={onScroll}>
      <div className="chat-log">
        {turns.map((t) =>
          t.role === "user" ? (
            <PeekPrompt key={t.key} id={seg.id} text={t.text} />
          ) : (
            <Fragment key={t.key}>
              {work.length ? <PeekWork id={seg.id} work={work} /> : null}
              {t.segments?.length || t.streaming ? <Turn turn={t} /> : null}
            </Fragment>
          ),
        )}
      </div>
    </div>
  );
}

/** One policy per peek, so its folds and its tail hook are its own and not
 *  shared with another peek or chat open at the same time. */
export default function AgentPeekLog({ seg }: { seg: ToolSegment }) {
  const policy = useMemo(createCardPolicy, []);
  return (
    <CardPolicyProvider value={policy}>
      <PeekLog seg={seg} />
    </CardPolicyProvider>
  );
}
