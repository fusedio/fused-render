// THE AGENT PEEK'S LOG — one subagent drawn as a read-only chat: the prompt
// its parent sent as the user bubble, its own rows (the Agent tool segment's
// `children`) as the reply. The same `Turn` rows and `.chat-log` column as the
// chat itself, with no composer and no turn actions. Its own module so the
// shell's overlay (`BreakoutCards`) can lazy-load it, like `SegmentView`.
import { useLayoutEffect, useMemo, useRef } from "react";
import type { AssistantTurn, UserTurn } from "../protocol/controller-api";
import type { ToolSegment } from "../protocol/types";
import { Turn } from "./Turn";

/** The two turns a subagent reads as. No prompt (a synthetic orphan parent,
 *  whose call was before the transcript window) means no user bubble. */
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

export default function AgentPeekLog({ seg }: { seg: ToolSegment }) {
  const turns = useMemo(() => agentPeekTurns(seg), [seg]);
  // Follow the tail while the reader is at it: a live subagent grows at the
  // bottom, and a reader who scrolled up to read is left where they are.
  const wrap = useRef<HTMLDivElement>(null);
  const atTail = useRef(true);
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
        {turns.map((t) => (
          <Turn key={t.key} turn={t} />
        ))}
      </div>
    </div>
  );
}
