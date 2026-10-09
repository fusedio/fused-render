// Model usage (OpenBot dialogs.js renderUsage) on the Settings-sized shell: today / last hour / from routines / failed
// as four tiles, calls per hour (24 h), the per-bot table ranked by model-weighted spend (live bots open on click),
// calls per day (7 days). Single-series accent bars, exact value on hover; plain divs, no chart library.
import type { CSSProperties } from "react";
import { cn } from "@platform/lib/utils";
import { Button } from "@platform/shadcn/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@platform/shadcn/ui/dialog";
import { fmtAgo } from "../lib/format";
import { modelChips, rankUsage, weighted } from "../lib/live";
import { select, useBotsSelector } from "../state/store";
import { DIALOG_CLASS, DIALOG_SIZE, FOOTER_CLASS, HEADER_CLASS } from "./shell";

function Bars({ id, vals, label, style }: { id: string; vals: { n: number }[]; label: (v: { n: number }, i: number) => string; style?: CSSProperties }) {
  const max = Math.max(1, ...vals.map((v) => v.n));
  return (
    <div className="flex h-16 items-end gap-0.5" id={id} style={style}>
      {vals.map((v, i) => <i key={i} className={cn("min-h-0.5 flex-1 rounded-t", v.n ? "bg-primary" : "bg-foreground/10")} style={{ height: `${v.n ? Math.max(4, Math.round(v.n / max * 100)) : 2}%` }} title={label(v, i)} />)}
    </div>
  );
}

const Heading = ({ children }: { children: string }) => <h4 className="mt-5 mb-1.5 text-xs font-medium text-muted-foreground">{children}</h4>;
const calls = (n: number) => `${n} call${n === 1 ? "" : "s"}`;
const ROW = "grid grid-cols-[minmax(120px,1fr)_56px_56px_minmax(120px,1.2fr)_56px_80px] items-center gap-3 rounded-lg px-2 py-1.5 text-[13px]";

export function UsageDialog({ onClose }: { onClose: () => void }) {
  const u = useBotsSelector((s) => s.usage);
  const o = u?.origin || { routine: 0, manual: 0 };
  const now = Date.now(), hourOf = (i: number) => new Date(now - (23 - i) * 3600e3).toLocaleTimeString([], { hour: "2-digit" });
  const bots = rankUsage(u?.bots || []);
  return (
    <Dialog open modal={false} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent showCloseButton={false} className={cn(DIALOG_CLASS, DIALOG_SIZE)}>
        <DialogHeader className={HEADER_CLASS}>
          <DialogTitle>Model usage</DialogTitle>
          <DialogDescription>One call is one agent step, counted across every worker process.</DialogDescription>
        </DialogHeader>
        <div className="min-h-0 flex-1 overflow-y-auto px-6 pb-5">
          <div className="grid grid-cols-4 gap-2" id="ustats">
            {u ? ([[u.today, "today"], [u.hour, "last hour"], [o.routine || 0, "from routines"], [u.errors, "failed"]] as [number, string][])
              .map(([n, l]) => <div key={l} className="rounded-xl bg-foreground/[0.04] px-3 py-2.5"><b className="block text-2xl font-medium tabular-nums">{n}</b><span className="text-xs text-muted-foreground">{l}</span></div>) : null}
          </div>
          <Heading>Calls per hour · last 24 hours</Heading>
          <Bars id="uhours" vals={(u?.hours || []).map((n) => ({ n }))} label={(v, i) => `${calls(v.n)} · ${hourOf(i)}`} />
          <Heading>By bot · this week</Heading>
          <div id="ubots">
            {bots.length ? (
              <>
                <div className={cn(ROW, "mb-0.5 rounded-none border-b border-foreground/10 pb-1 text-[10px] uppercase tracking-[.06em] text-muted-foreground")}>
                  <span>Bot</span><span className="text-right">Today</span><span className="text-right">7 days</span><span>Models</span><span className="text-right">Failed</span><span>Last call</span>
                </div>
                {bots.map((b) => (
                  <div key={b.id} className={cn(ROW, b.live && "cursor-pointer hover:bg-foreground/[0.04]")} data-id={b.live ? b.id : ""}
                    title={`${b.live ? "Open this bot" : "No longer exists"} · weighted ${weighted(b).toFixed(1)} sonnet-calls`}
                    onClick={b.live ? () => { onClose(); select(b.id); } : undefined}>
                    <span className={cn("truncate", !b.live && "italic text-muted-foreground")}>{b.name}</span>
                    <span className="text-right tabular-nums text-muted-foreground">{b.today || "·"}</span>
                    <span className="text-right tabular-nums text-muted-foreground">{b.week}</span>
                    <span className="flex flex-wrap gap-1">{modelChips(b.models).map(([m, n]) => <i key={m} className={`chip ${m}`} title={`${n} on ${m}`}>{m}{n > 1 ? ` ${n}` : ""}</i>)}</span>
                    <span className={cn("text-right tabular-nums", b.errors ? "text-destructive" : "text-muted-foreground")}>{b.errors || "·"}</span>
                    <span className="truncate text-xs text-muted-foreground">{fmtAgo(b.last)}</span>
                  </div>
                ))}
              </>
            ) : <p className="m-0 text-sm text-muted-foreground">No calls this week.</p>}
          </div>
          <Heading>Calls per day · last 7 days</Heading>
          <Bars id="udays" vals={u?.days || []} label={(v) => `${calls(v.n)} · ${(v as { day?: string }).day || ""}`} style={{ height: 40 }} />
          <p className="mt-4 mb-0 text-[13px] text-muted-foreground">The log lives in ~/.fused-render-app/bots/data/usage.jsonl.</p>
        </div>
        <DialogFooter className={FOOTER_CLASS}>
          <Button id="uclose" variant="outline" onClick={onClose}>Close</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
