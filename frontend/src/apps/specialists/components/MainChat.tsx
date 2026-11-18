// THE ONE CHAT (PRODUCT §2 "Main chat is one conversation per specialist",
// §8; PLAN A.2 "Clear"). The task chat stack (`ChatMount`) on the specialist's
// home with `{specialist: "<id>:main"}` on every start, so it gets Claude
// Code's full privileges, approval cards, Stop and resume for free.
//
// Clear (button and `/clear`) → POST …/chat/clear folds the current session,
// and the chat remounts with no session: the next send starts a fresh one in
// the same thread. Earlier sessions show above it as marker lines with Show.
// Search is a find over the turns on screen (PLAN D.2 #10).
import { useCallback, useEffect, useRef, useState, type ComponentProps, type ComponentType, type MutableRefObject } from "react";
import { ChatMount } from "@apps/claude";
import {
  clearChat,
  errorText,
  getChat,
  type SpecialistChat,
  type SpecialistDetail,
} from "@platform/lib/specialists-api";
import { Face } from "./Face";
import { FoldedSession } from "./FoldedSession";

/** What the page can do with the chat from outside it (Revert, Review and send). */
export interface ChatHandle {
  send: (text: string) => boolean;
}

const DEFAULT_API = { getChat, clearChat };

type ChatReport = { sessionId: string; running?: boolean; send?: (text: string) => void };

export function MainChat({
  specialist,
  handle,
  onOpenSettings,
  mount: Mount = ChatMount,
  api = DEFAULT_API,
}: {
  specialist: SpecialistDetail;
  handle?: MutableRefObject<ChatHandle | null>;
  onOpenSettings: () => void;
  // Seams for tests.
  mount?: ComponentType<ComponentProps<typeof ChatMount>>;
  api?: { getChat: typeof getChat; clearChat: typeof clearChat };
}) {
  const id = specialist.id;
  const [chat, setChat] = useState<SpecialistChat | null>(null);
  const [error, setError] = useState("");
  /** Bumped by Clear: a fresh mount with no session. */
  const [epoch, setEpoch] = useState(0);
  const [clearedHere, setClearedHere] = useState(false);
  const [busy, setBusy] = useState(false);
  const report = useRef<ChatReport>({ sessionId: "" });
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [hit, setHit] = useState<{ n: number; of: number } | null>(null);
  const body = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let live = true;
    setChat(null);
    setError("");
    setClearedHere(false);
    api.getChat(id).then(
      (c) => {
        if (live) setChat(c);
      },
      (err) => {
        if (live) setError(errorText(err));
      },
    );
    return () => {
      live = false;
    };
  }, [id, api]);

  const clear = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      const c = await api.clearChat(id);
      setChat({ ...c, current: null });
      setClearedHere(true);
      setEpoch((n) => n + 1);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }, [api, id]);

  const onState = useCallback((s: ChatReport) => {
    report.current = s;
  }, []);

  useEffect(() => {
    if (!handle) return;
    handle.current = {
      send: (text: string) => {
        const send = report.current.send;
        if (!send) return false;
        send(text);
        return true;
      },
    };
    return () => {
      handle.current = null;
    };
  }, [handle]);

  const runSearch = (dir: 1 | -1) => {
    const root = body.current;
    const q = query.trim().toLowerCase();
    root?.querySelectorAll(".spx-hit").forEach((el) => el.classList.remove("spx-hit"));
    if (!root || !q) {
      setHit(null);
      return;
    }
    const all = Array.from(root.querySelectorAll<HTMLElement>(".chat-log .turn, .spx-folded-turn")).filter((el) =>
      (el.textContent || "").toLowerCase().includes(q),
    );
    if (!all.length) {
      setHit({ n: 0, of: 0 });
      return;
    }
    const prev = hit && hit.of === all.length ? hit.n : 0;
    const n = prev === 0 ? (dir === 1 ? 1 : all.length) : ((prev - 1 + dir + all.length) % all.length) + 1;
    const el = all[n - 1];
    el.classList.add("spx-hit");
    el.scrollIntoView?.({ block: "center" });
    setHit({ n, of: all.length });
  };

  const current = chat?.current || "";
  const folded = chat?.folded ?? [];
  return (
    <main className="spx-main" aria-label={`${specialist.name} specialist chat`}>
      <header className="spx-main-head">
        <button type="button" className="spx-face-btn" onClick={onOpenSettings} aria-label={`${specialist.name} specialist settings`}>
          <Face id={id} name={specialist.name} face={specialist.face} size={32} />
        </button>
        <div className="spx-main-title">
          <h1>{specialist.name} specialist</h1>
          <span className="spx-muted">{specialist.status || specialist.subject}</span>
        </div>
        <button type="button" className="spx-pill" onClick={() => void clear()} disabled={busy || !chat}>
          Clear
        </button>
        <button
          type="button"
          className="spx-pill"
          aria-pressed={searchOpen}
          onClick={() => {
            setSearchOpen((v) => !v);
            setHit(null);
          }}
        >
          Search
        </button>
        <button type="button" className="spx-pill" onClick={onOpenSettings}>
          Settings
        </button>
      </header>
      {searchOpen ? (
        <form
          className="spx-search"
          role="search"
          onSubmit={(e) => {
            e.preventDefault();
            runSearch(1);
          }}
        >
          <label className="spx-sr" htmlFor={`spx-search-${id}`}>
            Find in this chat
          </label>
          <input
            id={`spx-search-${id}`}
            type="search"
            placeholder="Find in this chat"
            value={query}
            autoFocus
            onChange={(e) => {
              setQuery(e.target.value);
              setHit(null);
            }}
            onKeyDown={(e) => {
              if (e.key === "Escape") setSearchOpen(false);
            }}
          />
          <button type="button" className="spx-pill" onClick={() => runSearch(-1)}>
            Previous
          </button>
          <button type="submit" className="spx-pill">
            Next
          </button>
          {hit ? (
            <span className="spx-muted" role="status">
              {hit.of ? `${hit.n} of ${hit.of}` : "No match"}
            </span>
          ) : null}
        </form>
      ) : null}
      <div className="spx-main-body" ref={body}>
        {error ? (
          <div className="spx-error" role="alert">
            {error}
          </div>
        ) : null}
        {folded.length || clearedHere ? (
          <div className="spx-folds">
            {folded.map((f) => (
              <FoldedSession key={f.session_id} folded={f} home={specialist.home} />
            ))}
            {!current ? (
              <div className="spx-marker is-now">
                <span className="spx-marker-line" aria-hidden="true" />
                <span>Cleared · a new chat starts here</span>
                <span className="spx-marker-line" aria-hidden="true" />
              </div>
            ) : null}
          </div>
        ) : null}
        <div className="spx-chat">
          {chat ? (
            <Mount
              key={`${id}:${epoch}`}
              file={specialist.home}
              backend="claude"
              {...(current ? { sessionId: current } : {})}
              extraParams={{ specialist: `${id}:main` }}
              specialist={{ id, kind: "main" }}
              onClear={() => void clear()}
              onBreakoutState={onState}
              chatOnly
              peek
              noAnnTools
              paramsSource="memory"
            />
          ) : null}
        </div>
      </div>
    </main>
  );
}
