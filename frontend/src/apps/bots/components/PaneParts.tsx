// Pieces of a bot's preview column that shows files instead of a browser (ExpertPane.tsx): the tab strip, and a
// memory diff read as a document.
import type { KeyboardEvent } from "react";
import { diffDoc } from "../lib/memdiff";
import { MdDoc } from "./MdDoc";

/** The pane's tab strip in the Browser | Terminal strip's style (`id`s `<uid>-<tab>`, the panel `<uid>-panel`); the
 *  arrow keys step through the tabs. */
export function PaneTabs<T extends string>({ label, tabs, tab, onTab, uid }: {
  label: string; tabs: [T, string][]; tab: T; onTab: (t: T) => void; uid: string;
}) {
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const i = tabs.findIndex(([t]) => t === tab), next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length][0];
    onTab(next);
    document.getElementById(`${uid}-${next}`)?.focus();
  };
  return (
    <div className="viewsw" role="tablist" aria-label={label} onKeyDown={onKey}>
      {tabs.map(([t, l]) => (
        <button key={t} type="button" id={`${uid}-${t}`} role="tab" className={`satab${tab === t ? " on" : ""}`} aria-selected={tab === t}
          aria-controls={`${uid}-panel`} tabIndex={tab === t ? 0 : -1} onClick={() => onTab(t)}>{l}</button>
      ))}
    </div>
  );
}

/** One diff line's markdown on its background, with a + / − gutter mark. */
function DiffLine({ tone, text }: { tone: "add" | "del" | "ctx"; text: string }) {
  const mark = tone === "add" ? "+" : tone === "del" ? "−" : "";
  // A code fence alone parses as an empty code block: the line would show its mark and no text. Its source instead.
  const fence = /^\s*(`{3,}|~{3,})/.test(text);
  return (
    <div className={`xp-dr ${tone}${text.trim() ? "" : " blank"}`}>
      <span className="xp-g" aria-hidden="true">{mark}</span>
      {mark ? <span className="xp-sr">{tone === "add" ? "Added: " : "Removed: "}</span> : null}
      <div className="xp-dc xp-md">{fence ? <code>{text.trim()}</code> : text.trim() ? <MdDoc text={text} /> : "\u00a0"}</div>
    </div>
  );
}

/** A memory diff read as a document (lib/memdiff.ts diffDoc): no headers, each line's markdown rendered, added lines on
 *  green with +, removed on red with −, struck; a removed line answered by an added one as a pair; context muted.
 *  "No change to memory." for an empty one. */
export function DiffView({ diff }: { diff: string | null | undefined }) {
  const rows = diffDoc(diff);
  if (!rows.length) return <p className="xp-none">{(diff || "").trim() ? "Only blank lines changed." : "No change to memory."}</p>;
  return (
    <div className="xp-diff" role="group" aria-label="Memory diff">
      {rows.map((r, i) => r.tone === "gap" ? <div key={i} className="xp-dgap" aria-hidden="true">⋯</div>
        : r.tone === "pair" ? (
          <div key={i} className="xp-dpair"><DiffLine tone="del" text={r.del} /><DiffLine tone="add" text={r.add} /></div>
        ) : <DiffLine key={i} tone={r.tone} text={r.text} />)}
    </div>
  );
}
