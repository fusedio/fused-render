// A specialist's chat (docs/specialists-v2.md): the task chat (ChatMount) on its home, every start keyed
// `<id>:main` so the server knows it is this specialist's main chat. Clear (the header's, or `/clear`) folds the
// current session away and remounts with none. Earlier chats sit above it as a cleared bot thread shows them
// (Thread.tsx ClearMark): a "Context cleared" rule with "Show earlier conversation", which opens them read-only.
import { Fragment, useCallback, useEffect, useState, type MutableRefObject } from "react";
import type { Specialist } from "@platform/lib/specialists";
import { ChatMount } from "@apps/claude";
import { fetchHistory } from "@apps/claude/protocol/history";
import { specialistApi, type SpecialistChat as Chat } from "../lib/api";
import { fmtTime, fmtWhen } from "../lib/format";
import { actSpecialist, errMsg, showBanner } from "../state/store";
import { MdDoc } from "./MdDoc";

type Said = { role: string; text: string };

export function SpecialistChat({ s, clearRef }: { s: Specialist; clearRef: MutableRefObject<(() => void) | null> }) {
  const [chat, setChat] = useState<Chat | null>(null);
  const [epoch, setEpoch] = useState(0);  // a Clear remounts the chat with no session
  const [open, setOpen] = useState(false);
  const [earlier, setEarlier] = useState<Record<string, Said[]> | null>(null);
  useEffect(() => { void specialistApi.chat(s.id).then(setChat, (e) => showBanner(errMsg(e))); }, [s.id]);

  const clear = useCallback(() => {
    void actSpecialist(() => specialistApi.clear(s.id)).then((c) => {
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
          {marks.map((f, i) => (
            <Fragment key={f.session_id}>
              {open ? (earlier?.[f.session_id] || []).map((t, j) => t.role === "user"
                ? <div key={j} className="msg user">{t.text}</div>
                : <div key={j} className="msg done md"><MdDoc text={t.text} /></div>) : null}
              <div className="cleared" title={fmtWhen(f.at)}>
                <span className="lbl">Context cleared · {fmtTime(f.at)}</span>
                {i === marks.length - 1 ? <button aria-expanded={open} onClick={toggle}>{open ? "Hide" : "Show"} earlier conversation</button> : null}
              </div>
            </Fragment>
          ))}
        </div>
      ) : null}
      {chat ? (
        <ChatMount key={epoch} file={s.home} backend="claude" {...(chat.session_id ? { sessionId: chat.session_id } : {})}
          extraParams={{ specialist: `${s.id}:main` }} onClear={clear} chatOnly peek noAnnTools paramsSource="memory" />
      ) : null}
    </>
  );
}
