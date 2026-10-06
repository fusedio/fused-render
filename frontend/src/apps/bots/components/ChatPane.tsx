// The middle column (OpenBot index.html section.chat + chat.js render()'s header): the header (face → Settings, status
// dot, name and the "something is happening" subtitle, thread search, the preview toggle), the thread with its
// to-bottom pill, and the composer. Owns the state those share: the thread element, the reply quote, the search bar
// and the reaction picker.
import { useCallback, useEffect, useRef, useState } from "react";
import { statusLabel } from "../lib/derive";
import { toggleRight } from "../lib/layout";
import { searchQuery } from "../lib/thread";
import { cur, onSelectChange, openDialog, useBots } from "../state/store";
import { Composer, type ReplyTo } from "./Composer";
import { Face } from "./Face";
import { ReactionPicker, type ReactionReq } from "./ReactionPicker";
import { Thread } from "./Thread";
import { ThreadSearch } from "./ThreadSearch";
import { ToBottom } from "./ToBottom";

export function ChatPane() {
  useBots();
  const b = cur();
  const threadRef = useRef<HTMLDivElement>(null);
  const [reply, setReply] = useState<ReplyTo | null>(null);
  const [search, setSearch] = useState({ open: false, q: "" });
  const [searchN, setSearchN] = useState("");
  const [pick, setPick] = useState<ReactionReq | null>(null);
  // A reply and an open picker belong to one bot.
  useEffect(() => onSelectChange(() => { setReply(null); setPick(null); }), []);
  const closePick = useCallback(() => setPick(null), []);

  // Subtitle only while something is happening; an idle bot is just its name.
  const sub = !b || b.status === "idle" ? "" : (b.status === "running" && b.note ? b.note : statusLabel(b)) + (b.title ? " · " + b.title : "");
  return (
    <section className="chat">
      <header>
        <span className="av sm" id="cav" title="Settings" onClick={() => { if (b) openDialog({ kind: "settings", id: b.id }); }}>
          <span id="cavt">{b ? <Face b={b} svgId="hface" /> : null}</span>
          <span className={`dot${b ? " " + b.status : ""}`} id="cdot" />
        </span>
        <div className="title"><b id="cname">{b ? b.name : "Select a bot"}</b><small id="csub">{sub}</small></div>
        <ThreadSearch open={search.open} q={search.q} count={searchN} disabled={!b}
          onOpen={() => setSearch((s) => ({ ...s, open: true }))}
          onClose={() => setSearch({ open: false, q: "" })}
          onQuery={(q) => setSearch((s) => ({ ...s, q }))} />
        <button id="rcol" className="ptog" title="Show the browser preview" onClick={toggleRight}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="12" rx="2" /><path d="M8 20h8M12 16v4" /></svg>
        </button>
      </header>
      <div className="tw">
        <Thread b={b} threadRef={threadRef} searchQ={search.open ? searchQuery(search.q) : ""} onSearchCount={setSearchN}
          onReact={(anchor, seq) => setPick({ anchor, seq })} onReply={setReply} />
        <ToBottom threadRef={threadRef} />
      </div>
      <Composer b={b} reply={reply} setReply={setReply} threadRef={threadRef} />
      <ReactionPicker req={pick} onClose={closePick} />
    </section>
  );
}
