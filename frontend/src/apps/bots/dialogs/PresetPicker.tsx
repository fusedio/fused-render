// #pmodal (OpenBot dialogs.js pickPreset): "+" asks which preset first (a site the bot knows, with its playbooks, or
// a blank bot), then the bot dialog opens with the pick filled in. Four blank starters fill the first row, then the
// presets in the order the backend returns them. The search box hides the cards that miss; Enter picks the first preset
// still showing (a blank only when nothing else is left). Escape, the backdrop and Cancel dismiss (null).
import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { Face } from "../components/Face";
import { api, type Preset } from "../lib/api";
import { SUPER_BLURB, SUPER_FACE, SUPER_NAME, highlightRuns, pickCards, queryWords, searchRows, type NewBotPick } from "../lib/presets";
import { act, useBotsSelector } from "../state/store";

export function PresetPicker({ onDone }: { onDone: (pick: NewBotPick | null) => void }) {
  const [presets, setPresets] = useState<Preset[]>([]);
  const [query, setQuery] = useState("");
  const qRef = useRef<HTMLInputElement>(null);
  const doneRef = useRef(onDone); doneRef.current = onDone;  // the Escape listener is bound once; it must call the live prop
  useEffect(() => {
    let live = true;
    void act(() => api.presets(), true).then((r) => { if (live) setPresets(r?.presets || []); });
    return () => { live = false; };
  }, []);
  useEffect(() => {
    qRef.current?.focus();
    // Escape clears a search first (the list is a view of the grid), then closes.
    const key = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      if (qRef.current?.value) { setQuery(""); return; }
      doneRef.current(null);
    };
    document.addEventListener("keydown", key, true);
    return () => document.removeEventListener("keydown", key, true);
  }, []);

  // Super Bot card shows only while there is no Super Bot yet (one per Mac; the backend refuses a second).
  const hasSuper = useBotsSelector((s) => s.bots.some((b) => b.kind === "super"));
  const cards = useMemo(() => pickCards(presets, !hasSuper), [presets, hasSuper]);
  // With text in the box the grid gives way to a list: the cards whose name matched, then one row per playbook that
  // matched, each naming its site — so a hit on a playbook reads as what it is instead of a card with a changed line.
  const words = queryWords(query), searching = !!words.length;
  const rows = useMemo(() => searchRows(cards, query), [cards, query]);
  const hl = (text: string) => highlightRuns(text, words).map(([run, hit], i) => (hit ? <mark key={i}>{run}</mark> : run));
  const firstCard = rows.findIndex((r) => r.kind === "card"), firstSkill = rows.findIndex((r) => r.kind === "skill");
  const listRef = useRef<HTMLDivElement>(null);
  // Arrow keys walk the rows; Up from the first row returns to the box.
  const moveRow = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const all = Array.from(listRef.current?.querySelectorAll<HTMLElement>(".prow") || []);
    const i = all.indexOf(document.activeElement as HTMLElement);
    if (i < 0) return;
    e.preventDefault();
    if (e.key === "ArrowDown") all[Math.min(i + 1, all.length - 1)]?.focus();
    else if (i === 0) qRef.current?.focus(); else all[i - 1]?.focus();
  };
  return (
    <div id="pmodal" className="modal show" role="dialog" aria-modal="true" aria-label="New bot"
      onClick={(e) => { if (e.target === e.currentTarget) onDone(null); }}>
      <div className="box">
        <button className="xclose" aria-label="Close" title="Close (Esc)" onClick={() => onDone(null)}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>
        </button>
        <h3>New bot</h3>
        <p className="muted lead">
          {hasSuper ? "Start from scratch, or pick a pre-built bot with playbooks" : "Start from scratch, a pre-built bot with playbooks"}
          {/* The tip is CSS (::after on hover / focus), not `title`: a native tooltip takes a second to appear. */}
          <span className="info" tabIndex={0} role="img" aria-label="What a playbook is"
            data-note={"A playbook is a ready-made task the bot already knows, like “X timeline digest”. Ask for it by name; edit or add your own under Skills once the bot exists."}>i</span>
          {hasSuper ? "" : ", or Super Bot"}
        </p>
        <label className="search psearch">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
          <input id="pq" ref={qRef} type="search" placeholder="Search sites and playbooks (e.g. digest, mentions)" autoComplete="off" value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") { e.preventDefault(); if (rows[0]) onDone(rows[0].pick); }
              else if (e.key === "ArrowDown") { e.preventDefault(); listRef.current?.querySelector<HTMLElement>(".prow")?.focus(); }
            }} />
        </label>
        {searching ? (
          // Plain buttons (a list of actions, not a listbox): Tab / arrows move, Enter / click pick.
          <div id="plist" ref={listRef} className="plist" aria-label="Search results" onKeyDown={moveRow}>
            {rows.map((r, i) => (
              <button key={r.key} className={`prow ${r.kind}`} data-key={r.key} onClick={() => onDone(r.pick)}
                title={r.kind === "skill" ? `Make a ${r.sub} bot; it knows “${r.title}”` : undefined}>
                {i === firstCard ? <small className="sect">Bots</small> : i === firstSkill ? <small className="sect">Playbooks</small> : null}
                <span className="av"><Face b={{ name: r.name, face: r.face }} /></span>
                <span className="txt"><b>{hl(r.title)}</b><small>{r.kind === "skill" ? <>{hl(r.sub)} playbook</> : r.sub}</small></span>
              </button>
            ))}
          </div>
        ) : null}
        <div id="pgrid" className="pgrid" style={searching ? { display: "none" } : undefined}>
          {cards.map((c, i) => {
            const style = undefined;
            if (c.pick.kind === "super") {
              return (
                <button key="super" className="pcard super" data-key="" data-super="1" data-q={c.q} title="Claude Code's own tools on this Mac, plus the browser. One per Mac." style={style} onClick={() => onDone(c.pick)}>
                  <span className="av"><Face b={{ name: SUPER_NAME, face: SUPER_FACE }} /></span><span className="txt"><b>{SUPER_NAME}</b><small>{SUPER_BLURB}</small></span>
                </button>
              );
            }
            if (c.pick.kind === "blank") {
              const b = c.pick.blank;
              return (
                <button key={`blank:${i}`} className="pcard" data-key="" data-blank={i} data-q={c.q} title="No playbooks; you write the rules" style={style} onClick={() => onDone(c.pick)}>
                  <span className="av"><Face b={{ name: b.name, face: b.face }} /></span><b>{b.name}</b><small>From scratch</small>
                </button>
              );
            }
            const p = c.pick.preset;
            return (
              <button key={p.key} className="pcard" data-key={p.key} data-q={c.q} title={p.skills.join(" · ")} style={style} onClick={() => onDone(c.pick)}>
                <span className="av"><Face b={{ name: p.key, face: { icon: p.key, color: p.color } }} /></span><b>{p.name}</b>
                <small>{p.skills.length} playbooks</small>
              </button>
            );
          })}
        </div>
        <p className="muted" id="pnone" style={{ display: searching && !rows.length ? "" : "none" }}>Nothing matches. Clear the search and pick a blank bot to write your own rules.</p>
      </div>
    </div>
  );
}
