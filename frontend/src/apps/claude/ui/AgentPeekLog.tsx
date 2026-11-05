// THE AGENT PEEK'S LOG — one subagent drawn as a read-only chat: the prompt
// its parent sent as clamped prose, its own rows (the Agent tool segment's
// `children`) as the reply, drawn flat and in order — prose, calls, thinking,
// notices, none of it folded. Only the prompt clamps. The same `.chat-log`
// column and `Turn` reply as the chat itself, with no composer and no turn
// actions. The prompt is drawn here, not by `Turn`: a prompt Claude wrote is
// markdown, and a chat's user bubble shows typed words as typed. Its own module
// so the shell's overlay (`BreakoutCards`) can lazy-load it, like `SegmentView`.
import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
import { cn } from "@platform/lib/utils";
import type { AssistantTurn, UserTurn } from "../protocol/controller-api";
import type { ToolSegment } from "../protocol/types";
import { CardPolicyProvider, createCardPolicy, useCardOpen, useHoldTail } from "./cardPolicy";
import { MarkdownView } from "./MarkdownView";
import { RunTrigger } from "./RunTrigger";
import { Turn } from "./Turn";

/** The two turns a subagent reads as. No prompt (a synthetic orphan parent,
 *  whose call was before the transcript window) means no prompt turn. The
 *  reply is every row the subagent produced. */
export function agentPeekTurns(seg: ToolSegment): (UserTurn | AssistantTurn)[] {
  const prompt = typeof seg.input?.prompt === "string" ? seg.input.prompt.trim() : "";
  const reply: AssistantTurn = {
    role: "assistant",
    key: "agent-reply",
    text: "",
    segments: seg.children ?? [],
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

function PeekLog({ seg }: { seg: ToolSegment }) {
  const turns = useMemo(() => agentPeekTurns(seg), [seg]);
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
            t.segments?.length || t.streaming ? <Turn key={t.key} turn={t} unfolded /> : null
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
