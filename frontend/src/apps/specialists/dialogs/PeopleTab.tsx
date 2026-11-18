// Settings › People (PRODUCT §1): area → owner → how to reach them (which
// connected service, which handle or channel). One row per person, saved whole
// (PUT …/people commits people.md). Service is a choice of the specialist's
// connected services (GET …/connections). Wide: a table-like grid with one
// header; narrow (container query in specialists.css): each person stacks as a
// small card with its own labels. Remove is always on the row.
import { useEffect, useState } from "react";
import {
  errorText,
  getConnections,
  getPeople,
  putPeople,
  type PeopleRow,
  type ServiceRow,
} from "@platform/lib/specialists-api";

const EMPTY: PeopleRow = { area: "", owner: "", service: "", handle: "", notes: "" };
const COLS: { key: keyof PeopleRow; label: string; placeholder: string }[] = [
  { key: "area", label: "Area", placeholder: "e.g. Sync" },
  { key: "owner", label: "Owner", placeholder: "e.g. Ana" },
  { key: "service", label: "Service", placeholder: "" },
  { key: "handle", label: "Handle or channel", placeholder: "e.g. @ana" },
  { key: "notes", label: "Notes", placeholder: "Optional" },
];

/** Rows worth saving: any cell filled, cells trimmed. */
export function cleanRows(rows: readonly PeopleRow[]): PeopleRow[] {
  return rows
    .map((r) => ({
      area: r.area.trim(),
      owner: r.owner.trim(),
      service: r.service.trim(),
      handle: r.handle.trim(),
      notes: r.notes.trim(),
    }))
    .filter((r) => r.area || r.owner || r.service || r.handle || r.notes);
}

/** The choices for Service: connected services by title, plus a value already
 *  saved that is not among them (so a save never drops it). */
export function serviceChoices(services: readonly ServiceRow[], current: string): string[] {
  const titles = services.filter((s) => s.kind !== "app" && (s.connected || s.allowed)).map((s) => s.title || s.key);
  const out = Array.from(new Set(titles));
  if (current && !out.some((t) => t.toLowerCase() === current.toLowerCase())) out.unshift(current);
  return out;
}

export function PeopleTab({
  specialistId,
  api = { getPeople, putPeople, getConnections },
}: {
  specialistId: string;
  api?: { getPeople: typeof getPeople; putPeople: typeof putPeople; getConnections?: typeof getConnections };
}) {
  const [rows, setRows] = useState<PeopleRow[] | null>(null);
  const [services, setServices] = useState<ServiceRow[]>([]);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const get = api.getPeople;
  const put = api.putPeople;
  const conns = api.getConnections;

  useEffect(() => {
    let live = true;
    get(specialistId).then(
      (r) => live && setRows(r.rows || []),
      (e) => live && setError(errorText(e)),
    );
    conns?.(specialistId).then(
      (r) => live && setServices(r.services || []),
      () => {},
    );
    return () => {
      live = false;
    };
  }, [specialistId, get, conns]);

  const edit = (i: number, key: keyof PeopleRow, value: string) => {
    setRows((rs) => (rs ?? []).map((r, j) => (j === i ? { ...r, [key]: value } : r)));
    setDirty(true);
    setNote("");
  };
  const save = async () => {
    setBusy(true);
    setError("");
    try {
      const r = await put(specialistId, cleanRows(rows ?? []));
      setRows(r.rows || []);
      setDirty(false);
      setNote("Saved.");
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const list = rows ?? [];
  return (
    <div className="spx-tab spx-people">
      <p className="spx-muted spx-small">
        Who owns what, and how to reach them. The specialist reads this to find who to report a problem to; it never sends
        anything without your approval.
      </p>
      {error ? (
        <div className="spx-error" role="alert">
          {error}
        </div>
      ) : null}
      {rows === null && !error ? <div className="spx-muted">Loading…</div> : null}
      {rows && !list.length ? (
        <div className="spx-empty-note">
          No people yet. Add who owns each area, so the specialist knows who to write to when it finds a problem there.
        </div>
      ) : null}
      {list.length ? (
        <div className="spx-people-grid" role="table" aria-label="People">
          <div className="spx-people-row spx-people-head" role="row">
            {COLS.map((c) => (
              <span key={c.key} role="columnheader">
                {c.label}
              </span>
            ))}
            <span role="columnheader">
              <span className="spx-sr">Remove</span>
            </span>
          </div>
          {list.map((r, i) => (
            <div key={i} className="spx-people-row" role="row">
              {COLS.map((c) => {
                const id = `spx-people-${specialistId}-${i}-${c.key}`;
                return (
                  <div key={c.key} className="spx-people-cell" role="cell">
                    <label className="spx-people-label" htmlFor={id}>
                      {c.label}
                    </label>
                    {c.key === "service" ? (
                      <select
                        id={id}
                        className="field-control"
                        aria-label={`${c.label}, row ${i + 1}`}
                        value={r.service}
                        onChange={(e) => edit(i, "service", e.target.value)}
                      >
                        <option value="">{services.length ? "Choose…" : "No service connected"}</option>
                        {serviceChoices(services, r.service).map((t) => (
                          <option key={t} value={t}>
                            {t}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <input
                        id={id}
                        className="field-control"
                        aria-label={`${c.label}, row ${i + 1}`}
                        placeholder={c.placeholder}
                        value={r[c.key]}
                        onChange={(e) => edit(i, c.key, e.target.value)}
                      />
                    )}
                  </div>
                );
              })}
              <div className="spx-people-cell is-remove" role="cell">
                <button
                  type="button"
                  className="btn btn-secondary"
                  aria-label={`Remove row ${i + 1}`}
                  onClick={() => {
                    setRows((rs) => (rs ?? []).filter((_, j) => j !== i));
                    setDirty(true);
                  }}
                >
                  Remove
                </button>
              </div>
            </div>
          ))}
        </div>
      ) : null}
      <div className="spx-row-gap">
        <button
          type="button"
          className="btn btn-secondary"
          onClick={() => {
            setRows((rs) => [...(rs ?? []), { ...EMPTY }]);
            setDirty(true);
          }}
        >
          Add a person
        </button>
        <button type="button" className="btn btn-primary" onClick={() => void save()} disabled={busy || !dirty}>
          {busy ? "Saving…" : "Save"}
        </button>
        {note ? (
          <span className="spx-muted spx-small" role="status">
            {note}
          </span>
        ) : null}
      </div>
    </div>
  );
}
