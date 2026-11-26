// An expert's chat (docs/experts.md): the task chat (ChatMount) on its home, every start keyed
// `<id>:main` so the server knows it is this expert's main chat. Clear (the header's, or `/clear`) folds the
// current session away and remounts with none; the Agent pill is free again then (Claude or Copilot), and fixed
// once the new chat has a turn. Earlier chats sit above it as a cleared bot thread shows them
// (Thread.tsx ClearMark): a "Context cleared" rule with "Show earlier conversation", which opens them read-only.
import { Fragment, useCallback, useEffect, useState, type MutableRefObject } from "react";
import type { Expert } from "@platform/lib/experts";
import { ChatMount } from "@apps/claude";
import { fetchHistory } from "@apps/claude/protocol/history";
import { screenContext } from "@platform/lib/screen-context";
import { isBotsPath } from "../routes";
import { expertApi, type ExpertChat as Chat } from "../lib/api";
import { fmtTime, fmtWhen } from "../lib/format";
import { actExpert, errMsg, showBanner } from "../state/store";
import { MdDoc } from "./MdDoc";

type Line = { role: string; text: string };
/** The agents that can run an expert's chat, for a server that does not say (docs/experts.md §17). */
const EXPERT_AGENTS = ["claude", "copilot"];

/** The page behind the chat, when it floats over one: on the Bots page itself the chat IS the page. */
const screenNote = (): string => (isBotsPath(location.pathname) ? "" : screenContext());

export function ExpertChat({ s, clearRef }: { s: Expert; clearRef: MutableRefObject<(() => void) | null> }) {
  const [chat, setChat] = useState<Chat | null>(null);
  const [epoch, setEpoch] = useState(0);  // a Clear remounts the chat with no session
  const [open, setOpen] = useState(false);
  const [earlier, setEarlier] = useState<Record<string, Line[]> | null>(null);
  useEffect(() => { void expertApi.chat(s.id).then(setChat, (e) => showBanner(errMsg(e))); }, [s.id]);

  const clear = useCallback(() => {
    void actExpert(() => expertApi.clear(s.id)).then((c) => {
      if (!c) return;
      setChat(c); setEarlier(null); setOpen(false); setEpoch((n) => n + 1);
    });
  }, [s.id]);
  useEffect(() => { clearRef.current = clear; return () => { clearRef.current = null; }; }, [clear, clearRef]);

  const folded = chat?.folded || [];
  const toggle = () => {
    setOpen(!open);
    if (open || earlier) return;
    void Promise.all(folded.map((f) => fetchHistory(s.home, f.session_id))).then((rs) => setEarlier(Object.fromEntries(rs.map((r, i) => [
      folded[i].session_id,
      (r.turns || []).filter((t) => (t.role === "user" || t.role === "assistant") && t.text.trim()).map((t) => ({ role: t.role, text: t.text })),
    ]))), (e) => showBanner(errMsg(e)));
  };
  const marks = open ? folded : folded.slice(-1);
  return (
    <>
      {marks.length ? (
        <div className="thread earlier">
          {/* Hide where Show was: on top, not under the conversation it opened. */}
          {open ? <div className="cleared"><button aria-expanded onClick={toggle}>Hide earlier conversation</button></div> : null}
          {marks.map((f) => (
            <Fragment key={f.session_id}>
              {open ? (earlier?.[f.session_id] || []).map((t, j) => t.role === "user"
                ? <div key={j} className="msg user">{t.text}</div>
                : <div key={j} className="msg done md"><MdDoc text={t.text} /></div>) : null}
              <div className="cleared" title={fmtWhen(f.at)}>
                <span className="lbl">Context cleared · {fmtTime(f.at)}</span>
                {open ? null : <button aria-expanded={false} onClick={toggle}>Show earlier conversation</button>}
              </div>
            </Fragment>
          ))}
        </div>
      ) : null}
      {chat ? (
        <ChatMount key={epoch} file={s.home} backend={chat.backend || "claude"} agentChoices={chat.backends || EXPERT_AGENTS} {...(chat.session_id ? { sessionId: chat.session_id } : {})}
          extraParams={{ expert: `${s.id}:main` }} permission="auto" onClear={clear} chatOnly peek noAnnTools paramsSource="memory" screenNote={screenNote} />
      ) : null}
    </>
  );
}
