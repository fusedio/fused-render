// THE ONE CHAT: one conversation per specialist (docs/specialists.md §3, §9). The task chat stack (`ChatMount`) on the specialist's
// home with `{specialist: "<id>:main"}` on every start, so it gets Claude
// Code's full privileges, approval cards, Stop and resume for free. Its model
// starts as the specialist's own; the composer's pill can change it.
//
// Clear (button and `/clear`) → POST …/chat/clear folds the current session,
// and the chat remounts with no session: the next send starts a fresh one in
// the same thread. Earlier sessions show above it as marker lines with Show.
// Search finds text in the turns on screen: a count, every match highlighted,
// Next / Previous walk them.
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ComponentProps,
  type ComponentType,
  type CSSProperties,
  type MutableRefObject,
  type ReactNode,
} from "react";
import { ChatMount } from "@apps/claude";
import {
  clearChat,
  errorText,
  getChat,
  type SpecialistChat,
  type SpecialistDetail,
} from "@platform/lib/specialists-api";
import { Face, faceCssUrl, initialsFor } from "./Face";
import { FoldedSession, Marker } from "./FoldedSession";

/** What the page can do with the chat from outside it (Revert, Review and send). */
export interface ChatHandle {
  send: (text: string) => boolean;
}

const DEFAULT_API = { getChat, clearChat };

type ChatReport = { sessionId: string; turns?: readonly unknown[]; running?: boolean; send?: (text: string) => void };

/** Every start offset of `q` in `text`, case-insensitive. */
export function findOffsets(text: string, q: string): number[] {
  const out: number[] = [];
  const needle = q.toLowerCase();
  if (!needle) return out;
  const hay = text.toLowerCase();
  for (let i = hay.indexOf(needle); i !== -1; i = hay.indexOf(needle, i + needle.length)) out.push(i);
  return out;
}

/** The greeting an empty chat shows. */
export function greeting(s: Pick<SpecialistDetail, "name" | "subject">): string {
  const subject = (s.subject || "").trim().replace(/[.\s]+$/, "");
  return subject
    ? `I'm the ${s.name} specialist. Tell me something to remember, or ask me anything about ${subject}.`
    : `I'm the ${s.name} specialist. Tell me something to remember, or ask me anything.`;
}

// CSS Custom Highlight API (Chromium, WebKit): highlights text ranges without
// touching the DOM React owns. Absent: the current match's turn is outlined.
type HighlightReg = { set: (name: string, h: unknown) => void; delete: (name: string) => void };
const highlights = (): { reg: HighlightReg; make: (...r: Range[]) => unknown } | null => {
  const css = (globalThis as { CSS?: { highlights?: HighlightReg } }).CSS;
  const H = (globalThis as { Highlight?: new (...r: Range[]) => unknown }).Highlight;
  return css?.highlights && H ? { reg: css.highlights, make: (...r) => new H(...r) } : null;
};

/** Text ranges of `q` in the chat's turns and the open earlier chats. */
function findRanges(root: HTMLElement, q: string): Range[] {
  const doc = root.ownerDocument;
  const out: Range[] = [];
  const scopes = root.querySelectorAll<HTMLElement>(".chat-log .turn, .spx-folded-turn");
  for (const scope of Array.from(scopes)) {
    const walker = doc.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const text = n.nodeValue || "";
      for (const at of findOffsets(text, q)) {
        const r = doc.createRange();
        r.setStart(n, at);
        r.setEnd(n, at + q.length);
        out.push(r);
      }
    }
  }
  return out;
}

export function MainChat({
  specialist,
  handle,
  onOpenSettings,
  onRunning,
  headExtra,
  mount: Mount = ChatMount,
  api = DEFAULT_API,
}: {
  specialist: SpecialistDetail;
  handle?: MutableRefObject<ChatHandle | null>;
  onOpenSettings: () => void;
  /** The chat's own run state, as soon as it changes (the list row's "Working…"). */
  onRunning?: (running: boolean) => void;
  /** Extra controls at the end of the header (the narrow layout's panel toggle). */
  headExtra?: ReactNode;
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
  const [running, setRunning] = useState(false);
  const [hasTurns, setHasTurns] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [hit, setHit] = useState<{ n: number; of: number } | null>(null);
  const body = useRef<HTMLDivElement>(null);
  const ranges = useRef<Range[]>([]);
  const outlined = useRef<Element | null>(null);
  const onRunningRef = useRef(onRunning);
  onRunningRef.current = onRunning;

  useEffect(() => {
    let live = true;
    setChat(null);
    setError("");
    setClearedHere(false);
    setRunning(false);
    setHasTurns(false);
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

  useEffect(() => {
    onRunningRef.current?.(running);
  }, [running]);
  useEffect(() => () => onRunningRef.current?.(false), [id]);

  const clear = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      const c = await api.clearChat(id);
      setChat({ ...c, current: null });
      setClearedHere(true);
      setHasTurns(false);
      setEpoch((n) => n + 1);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }, [api, id]);

  const onState = useCallback((s: ChatReport) => {
    report.current = s;
    setRunning(!!s.running);
    setHasTurns(!!s.turns && s.turns.length > 0);
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

  // ── search ──
  const paint = useCallback((current: number) => {
    const hl = highlights();
    outlined.current?.classList.remove("spx-hit");
    outlined.current = null;
    const all = ranges.current;
    if (hl) {
      if (all.length) hl.reg.set("spx-find", hl.make(...all));
      else hl.reg.delete("spx-find");
      if (all[current]) hl.reg.set("spx-find-current", hl.make(all[current]));
      else hl.reg.delete("spx-find-current");
    }
    const el = all[current]?.startContainer.parentElement;
    if (el) {
      if (!hl) {
        const turn = el.closest(".turn, .spx-folded-turn");
        turn?.classList.add("spx-hit");
        outlined.current = turn;
      }
      el.scrollIntoView?.({ block: "center" });
    }
  }, []);
  const unpaint = useCallback(() => {
    const hl = highlights();
    hl?.reg.delete("spx-find");
    hl?.reg.delete("spx-find-current");
    outlined.current?.classList.remove("spx-hit");
    outlined.current = null;
    ranges.current = [];
  }, []);
  const find = useCallback(
    (q: string, at: number | "first") => {
      const root = body.current;
      const needle = q.trim();
      if (!root || !needle) {
        unpaint();
        setHit(null);
        return;
      }
      ranges.current = findRanges(root, needle);
      const of = ranges.current.length;
      if (!of) {
        paint(-1);
        setHit({ n: 0, of: 0 });
        return;
      }
      const i = at === "first" ? 0 : ((at % of) + of) % of;
      paint(i);
      setHit({ n: i + 1, of });
    },
    [paint, unpaint],
  );
  // Live as the reader types.
  useEffect(() => {
    if (!searchOpen) return;
    const t = setTimeout(() => find(query, "first"), 120);
    return () => clearTimeout(t);
  }, [query, searchOpen, find]);
  useEffect(() => {
    if (!searchOpen) unpaint();
  }, [searchOpen, unpaint]);
  useEffect(() => unpaint, [unpaint]);
  const step = (dir: 1 | -1) => {
    const cur = hit && hit.of ? hit.n - 1 : -1;
    find(query, cur < 0 ? (dir === 1 ? 0 : -1) : cur + dir);
  };

  const current = chat?.current || "";
  const folded = chat?.folded ?? [];
  const showGreeting = !!chat && !current && !hasTurns && !running;
  const letters = initialsFor(specialist.name);
  const faceStyle = { "--spx-face-url": faceCssUrl(specialist.face, id, letters) } as CSSProperties;
  return (
    <main className="spx-main" aria-label={`${specialist.name} specialist chat`} style={faceStyle}>
      <header className="spx-main-head">
        <button type="button" className="spx-face-btn" onClick={onOpenSettings} aria-label={`${specialist.name} specialist settings`}>
          <Face id={id} name={specialist.name} face={specialist.face} size={32} letters={letters} />
        </button>
        <div className="spx-main-title">
          <h1>{specialist.name} specialist</h1>
          <span className={"spx-main-status" + (running ? " is-working" : "")}>
            {running ? (
              <>
                <span className="spx-dot is-working" aria-hidden="true" />
                Working…
              </>
            ) : (
              specialist.status || specialist.subject
            )}
          </span>
        </div>
        <div className="spx-main-actions">
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
          {headExtra}
        </div>
      </header>
      {searchOpen ? (
        <form
          className="spx-search"
          role="search"
          onSubmit={(e) => {
            e.preventDefault();
            step(1);
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
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                e.preventDefault();
                setSearchOpen(false);
              } else if (e.key === "Enter" && e.shiftKey) {
                e.preventDefault();
                step(-1);
              }
            }}
          />
          <span className="spx-search-count" role="status" aria-live="polite">
            {hit ? (hit.of ? `${hit.n} of ${hit.of}` : "No match") : ""}
          </span>
          <button type="button" className="spx-pill" onClick={() => step(-1)} disabled={!hit?.of} aria-label="Previous match">
            Previous
          </button>
          <button type="submit" className="spx-pill" disabled={!hit?.of} aria-label="Next match">
            Next
          </button>
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
            {!current ? <Marker label="Cleared · a new chat starts here" now /> : null}
          </div>
        ) : null}
        <div className="spx-chat">
          {showGreeting ? (
            <div className="spx-greeting">
              <Face id={id} name={specialist.name} face={specialist.face} size={40} letters={letters} />
              <p>{greeting(specialist)}</p>
            </div>
          ) : null}
          {chat ? (
            <Mount
              key={`${id}:${epoch}`}
              file={specialist.home}
              backend="claude"
              {...(current ? { sessionId: current } : {})}
              model={specialist.model || ""}
              effort={specialist.effort || ""}
              placeholder={`Message the ${specialist.name} specialist`}
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
