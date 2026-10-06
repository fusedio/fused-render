// #umodal (OpenBot dialogs.js renderUsage): today / last hour / from routines / failed, calls per hour (24 h),
// the per-bot table ranked by model-weighted spend (live bots open on click), calls per day (7 days).
// Single-series accent bars, exact value on hover.
import type { CSSProperties } from "react";
import { fmtAgo } from "../lib/format";
import { modelChips, rankUsage, weighted } from "../lib/live";
import { select, useBotsSelector } from "../state/store";

function Bars({ id, vals, label, style }: { id: string; vals: { n: number }[]; label: (v: { n: number }, i: number) => string; style?: CSSProperties }) {
  const max = Math.max(1, ...vals.map((v) => v.n));
  return (
    <div className="ubars" id={id} style={style}>
      {vals.map((v, i) => <i key={i} className={v.n ? "" : "zero"} style={{ height: `${v.n ? Math.max(4, Math.round(v.n / max * 100)) : 2}%` }} title={label(v, i)} />)}
    </div>
  );
}

const calls = (n: number) => `${n} call${n === 1 ? "" : "s"}`;

export function UsageDialog({ onClose }: { onClose: () => void }) {
  const u = useBotsSelector((s) => s.usage);
  const o = u?.origin || { routine: 0, manual: 0 };
  const now = Date.now(), hourOf = (i: number) => new Date(now - (23 - i) * 3600e3).toLocaleTimeString([], { hour: "2-digit" });
  const bots = rankUsage(u?.bots || []);
  return (
    <div id="umodal" className="modal show" role="dialog" aria-modal="true" aria-label="Model usage" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="box">
        <h3>Model usage</h3>
        <div className="ustats" id="ustats">
          {u ? ([[u.today, "today"], [u.hour, "last hour"], [o.routine || 0, "from routines"], [u.errors, "failed"]] as [number, string][])
            .map(([n, l]) => <div key={l}><b>{n}</b><span>{l}</span></div>) : null}
        </div>
        <div className="uh">Calls per hour · last 24 hours</div>
        <Bars id="uhours" vals={(u?.hours || []).map((n) => ({ n }))} label={(v, i) => `${calls(v.n)} · ${hourOf(i)}`} />
        <div className="uh">By bot · this week</div>
        <div className="ulist" id="ubots">
          {bots.length ? (
            <>
              <div className="u uhd"><span>Bot</span><span className="n">Today</span><span className="n">7 days</span><span>Models</span><span className="n">Failed</span><span>Last call</span></div>
              {bots.map((b) => (
                <div key={b.id} className={`u ${b.live ? "" : "gone"}`} data-id={b.live ? b.id : ""}
                  title={`${b.live ? "Open this bot" : "No longer exists"} · weighted ${weighted(b).toFixed(1)} sonnet-calls`}
                  onClick={b.live ? () => { onClose(); select(b.id); } : undefined}>
                  <span className="name">{b.name}</span><span className="n">{b.today || "·"}</span><span className="n">{b.week}</span>
                  <span className="models">{modelChips(b.models).map(([m, n]) => <i key={m} className={`chip ${m}`} title={`${n} on ${m}`}>{m}{n > 1 ? ` ${n}` : ""}</i>)}</span>
                  <span className={`n ${b.errors ? "warn" : ""}`}>{b.errors || "·"}</span><span className="last">{fmtAgo(b.last)}</span>
                </div>
              ))}
            </>
          ) : <div className="muted">No calls this week.</div>}
        </div>
        <div className="uh">Calls per day · last 7 days</div>
        <Bars id="udays" vals={u?.days || []} label={(v) => `${calls(v.n)} · ${(v as { day?: string }).day || ""}`} style={{ height: 40 }} />
        <p className="muted" style={{ marginTop: 14 }}>One call is one agent step. Counted across every worker process; the log lives in ~/.fused-render-app/bots/data/usage.jsonl.</p>
        <div className="row"><button id="uclose" onClick={onClose}>Close</button></div>
      </div>
    </div>
  );
}
