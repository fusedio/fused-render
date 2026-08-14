// Turning a /api/index/query or /api/index/ask response into something a table
// can render, and every failure into one string.
//
// Pure and separate from the panel because the response shapes are three, not
// one: the happy `{ok, columns, rows, truncated}`, a plain `{error}` 400 (which
// for `ask` also carries the `sql` that was refused), and the AI relay's typed
// `{error: {type, message}}` envelope passed straight through by /ask. Read
// naively, that last one renders as "[object Object]".
//
// Nothing here rewords a server message. The whole point of the server mapping
// duckdb's own text to a 400 is that "Binder Error: no such column: nope" tells
// the user exactly what to fix; a friendlier wrapper would throw that away.

export interface IndexQueryTable {
  columns: string[];
  // Pre-stringified. The table renders text, and doing the conversion here is
  // what keeps a BLOB or a struct from reaching React as an object.
  rows: string[][];
  truncated: boolean;
}

export type IndexQueryOutcome =
  // `sql` is what the SERVER compiled — non-null only for /ask, where the user
  // needs to see the statement their question turned into.
  | { ok: true; sql: string | null; table: IndexQueryTable }
  | { ok: false; sql: string | null; error: string };

/** One cell as text. */
export function cellText(v: unknown): string {
  // Spelled out, not blank: a NULL and an empty string are different answers,
  // and a column of blanks is unreadable either way.
  if (v === null || v === undefined) return "NULL";
  if (typeof v === "object") {
    try {
      return JSON.stringify(v);
    } catch {
      return String(v);
    }
  }
  return String(v);
}

function stringOrNull(v: unknown): string | null {
  return typeof v === "string" && v.length > 0 ? v : null;
}

function messageFrom(data: Record<string, unknown>, status: number): string {
  const err = data.error;
  if (typeof err === "string" && err) return err;
  // The relay envelope. `type` is for programs; `message` is the sentence.
  if (err && typeof err === "object") {
    const msg = (err as Record<string, unknown>).message;
    if (typeof msg === "string" && msg) return msg;
  }
  return `HTTP ${status}`;
}

/** The response, as either a table or a message. Never throws. */
export function outcomeFrom(status: number, data: unknown): IndexQueryOutcome {
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    return { ok: false, sql: null, error: `HTTP ${status}` };
  }
  const body = data as Record<string, unknown>;
  const sql = stringOrNull(body.sql);
  if (status < 400 && body.ok === true) {
    const columns = Array.isArray(body.columns) ? body.columns.map(cellText) : [];
    const raw = Array.isArray(body.rows) ? body.rows : [];
    return {
      ok: true,
      sql,
      table: {
        columns,
        // Padded to the column count: the table renders one cell per column, so
        // a ragged row would shift every cell after the gap one column left.
        rows: raw.map((row) => {
          const cells = Array.isArray(row) ? row.map(cellText) : [cellText(row)];
          while (cells.length < columns.length) cells.push(cellText(null));
          return cells.slice(0, Math.max(columns.length, cells.length));
        }),
        truncated: body.truncated === true,
      },
    };
  }
  return { ok: false, sql, error: messageFrom(body, status) };
}
