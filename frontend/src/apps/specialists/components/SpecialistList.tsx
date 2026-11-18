// The left column: every specialist (face, name, one line of status) and the
// round "+" that creates one (PRODUCT §8; mock-up Main.dc.html).
import type { SpecialistSummary } from "@platform/lib/specialists-api";
import { specialistHref } from "../routes";
import { Face } from "./Face";

export function SpecialistList({
  rows,
  selected,
  loading,
  error,
  onSelect,
  onCreate,
  generalCount = 0,
  onOpenGeneral,
}: {
  rows: readonly SpecialistSummary[];
  selected: string | null;
  loading: boolean;
  error: string;
  onSelect: (id: string) => void;
  onCreate: () => void;
  /** Notes in the general inbox (no specialist fits). */
  generalCount?: number;
  onOpenGeneral?: () => void;
}) {
  return (
    <nav className="spx-list" aria-label="Specialists">
      <div className="spx-list-head">
        <span className="spx-list-title">Specialists</span>
        <button type="button" className="spx-round" aria-label="New specialist" title="New specialist" onClick={onCreate}>
          +
        </button>
      </div>
      {error ? (
        <div className="spx-error" role="alert">
          {error}
        </div>
      ) : null}
      {loading && !rows.length ? <div className="spx-muted spx-pad">Loading…</div> : null}
      {rows.map((r) => (
        <a
          key={r.id}
          href={specialistHref(r.id)}
          className={"spx-list-row" + (r.id === selected ? " is-selected" : "")}
          {...(r.id === selected ? { "aria-current": "page" as const } : {})}
          onClick={(e) => {
            if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
            e.preventDefault();
            onSelect(r.id);
          }}
        >
          <Face id={r.id} name={r.name} face={r.face} />
          <span className="spx-list-text">
            <span className="spx-list-name">{r.name}</span>
            <span className="spx-list-status">{r.status || r.subject}</span>
          </span>
          {r.working ? <span className="spx-dot is-working" title="Working" aria-label="Working" role="img" /> : null}
        </a>
      ))}
      {!loading && !rows.length && !error ? (
        <p className="spx-muted spx-pad">No specialists yet. Press + to create one.</p>
      ) : null}
      <p className="spx-list-foot">Each specialist is an expert on one subject. Tasks can ask any of them for help.</p>
      {generalCount > 0 && onOpenGeneral ? (
        <button type="button" className="spx-linkbtn spx-general-line" onClick={onOpenGeneral}>
          {generalCount} {generalCount === 1 ? "note fits" : "notes fit"} no specialist
        </button>
      ) : null}
    </nav>
  );
}
