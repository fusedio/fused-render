// The New bot chooser (OpenBot dialogs.js pickPreset) on the Settings-sized shell: "+" asks which preset first (a site
// the bot knows, with its playbooks, or a blank bot), then the create form opens with the pick filled in. Four blank
// starters fill the first row, then the presets in the order the backend returns them, six to a row. The search box
// swaps the grid for a list: the cards whose name matched, then one row per playbook that matched, each naming its
// site, so a hit on a playbook reads as what it is. Enter picks the first row still showing. Escape clears a search
// first, then closes; the backdrop and the X dismiss (null).
import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { SearchIcon } from "lucide-react";
import { cn } from "@platform/lib/utils";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@platform/shadcn/ui/dialog";
import { Input } from "@platform/shadcn/ui/input";
import { Face } from "../components/Face";
import { api, type Preset } from "../lib/api";
import { SUPER_BLURB, SUPER_FACE, SUPER_NAME, highlightRuns, pickCards, queryWords, searchRows, type NewBotPick } from "../lib/presets";
import { act, useBotsSelector } from "../state/store";
import { DIALOG_CLASS, DIALOG_SIZE, HEADER_CLASS } from "./shell";

const CARD = "pcard flex min-w-0 cursor-pointer appearance-none flex-col items-center gap-1 rounded-2xl border-2 border-transparent bg-foreground/[0.04] px-2 pt-4 pb-3 text-center text-foreground outline-none transition-[border-color,transform] duration-150 hover:-translate-y-0.5 hover:border-primary focus-visible:border-primary";
const ROW = "prow relative flex w-full cursor-pointer appearance-none items-center gap-2.5 rounded-lg border-0 bg-transparent px-2 py-1.5 text-left text-foreground outline-none hover:bg-foreground/[0.06] focus-visible:bg-foreground/[0.06] [&_mark]:bg-transparent [&_mark]:font-semibold [&_mark]:text-primary";

export function PresetPicker({ onDone }: { onDone: (pick: NewBotPick | null) => void }) {
  const [presets, setPresets] = useState<Preset[]>([]);
  const [query, setQuery] = useState("");
  const doneRef = useRef(onDone); doneRef.current = onDone;  // the Escape listener is bound once; it must call the live prop
  const q = () => document.getElementById("pq") as HTMLInputElement | null;
  useEffect(() => {
    let live = true;
    void act(() => api.presets(), true).then((r) => { if (live) setPresets(r?.presets || []); });
    return () => { live = false; };
  }, []);
  useEffect(() => {
    const t = window.setTimeout(() => q()?.focus(), 0);  // after the dialog's own initial focus
    // Escape clears a search first (the list is a view of the grid), then closes. Capture phase: the dialog's own
    // Escape would close it before this ran.
    const key = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      if (q()?.value) { setQuery(""); return; }
      doneRef.current(null);
    };
    document.addEventListener("keydown", key, true);
    return () => { window.clearTimeout(t); document.removeEventListener("keydown", key, true); };
  }, []);

  // Super Bot card shows only while there is no Super Bot yet (one per Mac; the backend refuses a second).
  const hasSuper = useBotsSelector((s) => s.bots.some((b) => b.kind === "super"));
  const cards = useMemo(() => pickCards(presets, !hasSuper), [presets, hasSuper]);
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
    else if (i === 0) q()?.focus(); else all[i - 1]?.focus();
  };
  const sect = (label: string) => <small className="absolute -top-3 left-2 text-[10px] uppercase tracking-[.06em] text-muted-foreground">{label}</small>;
  return (
    <Dialog open modal={false} onOpenChange={(open) => { if (!open) onDone(null); }}>
      <DialogContent id="pmodal" showCloseButton className={cn(DIALOG_CLASS, DIALOG_SIZE, "[&_[data-slot=dialog-close]]:top-4 [&_[data-slot=dialog-close]]:right-4")}>
        <DialogHeader className={HEADER_CLASS}>
          <DialogTitle>New bot</DialogTitle>
          <DialogDescription>
            {hasSuper ? "Start from scratch, or pick a pre-built bot with playbooks." : "Start from scratch, a pre-built bot with playbooks, or Super Bot."}
            {" "}A playbook is a ready-made task the bot already knows, like “X timeline digest”: ask for it by name, and edit or add your own under Settings › Skills once the bot exists.
          </DialogDescription>
        </DialogHeader>
        <div className="shrink-0 px-6 pb-3">
          <div className="relative">
            <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
            <Input id="pq" type="search" placeholder="Search sites and playbooks (e.g. digest, mentions)" autoComplete="off" value={query} className="pl-8"
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") { e.preventDefault(); if (rows[0]) onDone(rows[0].pick); }
                else if (e.key === "ArrowDown") { e.preventDefault(); listRef.current?.querySelector<HTMLElement>(".prow")?.focus(); }
              }} />
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-6 pb-5">
          {searching ? (
            // Plain buttons (a list of actions, not a listbox): Tab / arrows move, Enter / click pick.
            <div id="plist" ref={listRef} className="flex flex-col gap-px" aria-label="Search results" onKeyDown={moveRow}>
              {rows.map((r, i) => (
                <button key={r.key} type="button" className={cn(ROW, r.kind, (i === firstCard || i === firstSkill) && "mt-5")} data-key={r.key} onClick={() => onDone(r.pick)}
                  title={r.kind === "skill" ? `Make a ${r.sub} bot; it knows “${r.title}”` : undefined}>
                  {i === firstCard ? sect("Bots") : i === firstSkill ? sect("Playbooks") : null}
                  <span className="size-7 shrink-0 [&>svg]:block [&>svg]:size-full"><Face b={{ name: r.name, face: r.face }} /></span>
                  <span className="flex min-w-0 flex-col gap-px"><b className="truncate text-[13px] font-medium">{hl(r.title)}</b><small className="text-[11px] text-muted-foreground">{r.kind === "skill" ? <>{hl(r.sub)} playbook</> : r.sub}</small></span>
                </button>
              ))}
              {!rows.length ? <p className="m-0 mt-2 text-sm text-muted-foreground" id="pnone">Nothing matches. Clear the search and pick a blank bot to write your own rules.</p> : null}
            </div>
          ) : (
            <div id="pgrid" className="grid grid-cols-6 gap-2.5 pt-0.5">
              {cards.map((c, i) => {
                if (c.pick.kind === "super") {
                  return (
                    <button key="super" type="button" className={cn(CARD, "super col-span-full flex-row justify-center gap-3 py-3 text-left")} data-key="" data-super="1" data-q={c.q} title="Claude Code's own tools on this Mac, plus the browser. One per Mac." onClick={() => onDone(c.pick)}>
                      <span className="size-10 shrink-0 [&>svg]:block [&>svg]:size-full"><Face b={{ name: SUPER_NAME, face: SUPER_FACE }} /></span>
                      <span className="flex flex-col items-start"><b className="text-[13px] font-medium">{SUPER_NAME}</b><small className="text-[11px] text-muted-foreground">{SUPER_BLURB}</small></span>
                    </button>
                  );
                }
                if (c.pick.kind === "blank") {
                  const b = c.pick.blank;
                  return (
                    <button key={`blank:${i}`} type="button" className={CARD} data-key="" data-blank={i} data-q={c.q} title="No playbooks; you write the rules" onClick={() => onDone(c.pick)}>
                      <span className="mb-1 size-12 [&>svg]:block [&>svg]:size-full"><Face b={{ name: b.name, face: b.face }} /></span>
                      <b className="text-[13px] font-medium">{b.name}</b><small className="text-[11px] text-muted-foreground">From scratch</small>
                    </button>
                  );
                }
                const p = c.pick.preset;
                return (
                  <button key={p.key} type="button" className={CARD} data-key={p.key} data-q={c.q} title={p.skills.join(" · ")} onClick={() => onDone(c.pick)}>
                    <span className="mb-1 size-12 [&>svg]:block [&>svg]:size-full"><Face b={{ name: p.key, face: { icon: p.key, color: p.color } }} /></span>
                    <b className="text-[13px] font-medium">{p.name}</b><small className="text-[11px] text-muted-foreground">{p.skills.length} playbooks</small>
                  </button>
                );
              })}
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
