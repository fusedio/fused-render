// The left column: every specialist (face, name, one line of status) and the
// round "+" that creates one (PRODUCT §8; mock-up Main.dc.html). A specialist
// whose main chat is running says "Working…" with a dot. Notes that fit no
// specialist show as a quiet row of their own, with a count.
import type { SpecialistSummary } from "@platform/lib/specialists-api";
import { specialistHref } from "../routes";
import { Face, initialsFor } from "./Face";

export function SpecialistList({
  rows,
  selected,
  loading,
  error,
  onSelect,
  onCreate,
  generalCount = 0,
  onOpenGeneral,
  runningHere = false,
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
  /** The selected specialist's chat says it is running (sooner than the list's poll). */
  runningHere?: boolean;
}) {
  const names = rows.map((r) => r.name);
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
      {rows.map((r) => {
        const working = r.working || (r.id === selected && runningHere);
        const status = working ? (r.working && /^working /i.test(r.status) ? r.status : "Working…") : r.status || r.subject;
        return (
          <a
            key={r.id}
            href={specialistHref(r.id)}
            className={"spx-list-row" + (r.id === selected ? " is-selected" : "")}
            // The narrow layout shows only the face: the name and status stay in the row's name.
            aria-label={status ? `${r.name}, ${status}` : r.name}
            title={r.name}
            {...(r.id === selected ? { "aria-current": "page" as const } : {})}
            onClick={(e) => {
              if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
              e.preventDefault();
              onSelect(r.id);
            }}
          >
            <Face id={r.id} name={r.name} face={r.face} letters={initialsFor(r.name, names)} />
            <span className="spx-list-text">
              <span className="spx-list-name">{r.name}</span>
              <span className="spx-list-status">{status}</span>
            </span>
            {working ? <span className="spx-dot is-working" aria-hidden="true" /> : null}
          </a>
        );
      })}
      {!loading && !rows.length && !error ? (
        <p className="spx-muted spx-pad">No specialists yet. Press + to create one.</p>
      ) : null}
      {generalCount > 0 && onOpenGeneral ? (
        <button type="button" className="spx-list-row spx-general-row" onClick={onOpenGeneral}>
          <span className="spx-general-icon" aria-hidden="true">
            ?
          </span>
          <span className="spx-list-text">
            <span className="spx-list-name">Fits no specialist</span>
            <span className="spx-list-status">
              {generalCount} {generalCount === 1 ? "note" : "notes"} to give to one
            </span>
          </span>
          <span className="spx-count-badge" aria-label={`${generalCount} ${generalCount === 1 ? "note" : "notes"}`}>
            {generalCount}
          </span>
        </button>
      ) : null}
      <p className="spx-list-foot">Each specialist knows one subject in depth. Tasks can ask any of them for help.</p>
    </nav>
  );
}
