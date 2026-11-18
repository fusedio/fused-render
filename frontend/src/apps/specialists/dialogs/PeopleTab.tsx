// Settings › People (PRODUCT §1): area → owner → how to reach them (which
// connected service, which handle or channel). Edited as a table and saved
// whole (PUT …/people commits people.md).
import { useEffect, useState } from "react";
import { errorText, getPeople, putPeople, type PeopleRow } from "@platform/lib/specialists-api";

const EMPTY: PeopleRow = { area: "", owner: "", service: "", handle: "", notes: "" };
const COLS: { key: keyof PeopleRow; label: string }[] = [
  { key: "area", label: "Area" },
  { key: "owner", label: "Owner" },
  { key: "service", label: "Service" },
  { key: "handle", label: "Handle or channel" },
  { key: "notes", label: "Notes" },
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

export function PeopleTab({
  specialistId,
  api = { getPeople, putPeople },
}: {
  specialistId: string;
  api?: { getPeople: typeof getPeople; putPeople: typeof putPeople };
}) {
  const [rows, setRows] = useState<PeopleRow[] | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const get = api.getPeople;
  const put = api.putPeople;

  useEffect(() => {
    let live = true;
    get(specialistId).then(
      (r) => live && setRows(r.rows || []),
      (e) => live && setError(errorText(e)),
    );
    return () => {
      live = false;
    };
  }, [specialistId, get]);

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

  return (
    <div className="spx-tab">
      <p className="spx-muted spx-small">
        Who owns what, and how to reach them. The specialist reads this to find who to report a problem to; it never sends
        anything without your approval.
      </p>
      {error ? (
        <div className="spx-error" role="alert">
          {error}
        </div>
      ) : null}
      <div className="spx-table-wrap">
        <table className="spx-table">
          <thead>
            <tr>
              {COLS.map((c) => (
                <th key={c.key} scope="col">
                  {c.label}
                </th>
              ))}
              <th scope="col">
                <span className="spx-sr">Remove</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {(rows ?? []).map((r, i) => (
              <tr key={i}>
                {COLS.map((c) => (
                  <td key={c.key}>
                    <input
                      className="field-control"
                      aria-label={`${c.label}, row ${i + 1}`}
                      value={r[c.key]}
                      onChange={(e) => edit(i, c.key, e.target.value)}
                    />
                  </td>
                ))}
                <td>
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
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
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
          Save
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
