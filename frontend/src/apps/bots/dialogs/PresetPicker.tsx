// #pmodal (OpenBot dialogs.js pickPreset): "+" asks which preset first (a site the bot knows, with its playbooks, or
// a blank bot), then the bot dialog opens with the pick filled in. Four blank starters fill the first row, then the
// presets in the order the backend returns them. The search box hides the cards that miss; Enter picks the first preset
// still showing (a blank only when nothing else is left). Escape, the backdrop and Cancel dismiss (null).
import { useEffect, useMemo, useRef, useState } from "react";
import { Face } from "../components/Face";
import { api, type Preset } from "../lib/api";
import { SUPER_BLURB, SUPER_FACE, SUPER_NAME, filterCards, firstPick, highlightRuns, matchedSkill, pickCards, queryWords, type NewBotPick } from "../lib/presets";
import { act, useBotsSelector } from "../state/store";

export function PresetPicker({ onDone }: { onDone: (pick: NewBotPick | null) => void }) {
  const [presets, setPresets] = useState<Preset[]>([]);
  const [query, setQuery] = useState("");
  const qRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    let live = true;
    void act(() => api.presets(), true).then((r) => { if (live) setPresets(r?.presets || []); });
    return () => { live = false; };
  }, []);
  useEffect(() => {
    qRef.current?.focus();
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") { e.stopPropagation(); onDone(null); } };
    document.addEventListener("keydown", key, true);
    return () => document.removeEventListener("keydown", key, true);
  }, []);

  // Super Bot card shows only while there is no Super Bot yet (one per Mac; the backend refuses a second).
  const hasSuper = useBotsSelector((s) => s.bots.some((b) => b.kind === "super"));
  const cards = useMemo(() => pickCards(presets, !hasSuper), [presets, hasSuper]);
  const { shown, none } = filterCards(cards, query);
  const visible = new Set(shown);
  const words = queryWords(query);
  // Search hits light up in the card text; a hit on a playbook title replaces the "N playbooks" line with that title.
  const hl = (text: string) => highlightRuns(text, words).map(([run, hit], i) => (hit ? <mark key={i}>{run}</mark> : run));
  return (
    <div id="pmodal" className="modal show" role="dialog" aria-modal="true" aria-label="New bot"
      onClick={(e) => { if (e.target === e.currentTarget) onDone(null); }}>
      <div className="box">
        <h3>New bot</h3>
        <p className="muted">
          A site bot comes with playbooks: ready-made tasks it already knows, like “X timeline digest”. Start blank to write your own rules
          {hasSuper ? "" : ", or make Super Bot, which works on this Mac itself"}. Name and rules come next.
        </p>
        <label className="search psearch">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
          <input id="pq" ref={qRef} type="search" placeholder="Search sites and playbooks (e.g. digest, mentions)" autoComplete="off" value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => { if (e.key !== "Enter") return; e.preventDefault(); const first = firstPick(shown); if (first) onDone(first.pick); }} />
        </label>
        <div id="pgrid" className="pgrid">
          {cards.map((c, i) => {
            const style = visible.has(c) ? undefined : { display: "none" };
            if (c.pick.kind === "super") {
              return (
                <button key="super" className="pcard super" data-key="" data-super="1" data-q={c.q} title="Claude Code's own tools on this Mac, plus the browser. One per Mac." style={style} onClick={() => onDone(c.pick)}>
                  <span className="av"><Face b={{ name: SUPER_NAME, face: SUPER_FACE }} /></span><span className="txt"><b>{hl(SUPER_NAME)}</b><small>{hl(SUPER_BLURB)}</small></span>
                </button>
              );
            }
            if (c.pick.kind === "blank") {
              const b = c.pick.blank;
              return (
                <button key={`blank:${i}`} className="pcard" data-key="" data-blank={i} data-q={c.q} title="No playbooks; you write the rules" style={style} onClick={() => onDone(c.pick)}>
                  <span className="av"><Face b={{ name: b.name, face: b.face }} /></span><b>{hl(b.name)}</b><small>From scratch</small>
                </button>
              );
            }
            const p = c.pick.preset, sk = matchedSkill(p, words);
            return (
              <button key={p.key} className="pcard" data-key={p.key} data-q={c.q} title={p.skills.join(" · ")} style={style} onClick={() => onDone(c.pick)}>
                <span className="av"><Face b={{ name: p.key, face: { icon: p.key, color: p.color } }} /></span><b>{hl(p.name)}</b>
                <small className={sk ? "hit" : undefined} title={sk || undefined}>{sk ? hl(sk) : `${p.skills.length} playbooks`}</small>
              </button>
            );
          })}
        </div>
        <p className="muted" id="pnone" style={{ display: none ? "" : "none" }}>No preset matches. Clear the search and pick a blank bot to write your own rules.</p>
        <div className="row"><button id="pcancel" onClick={() => onDone(null)}>Cancel</button></div>
      </div>
    </div>
  );
}
