// Pure helpers for the app page's API tab (shell/AppApi.tsx): how a parameter
// becomes a widget, what it is prefilled with, and how the form's strings turn
// back into the JSON /api/run sends. These MIRROR templates/api/template.html
// rule for rule (PY-4: the JS side owns JSON types — coercion here must match
// what main() expects), so a run from this tab and a run from the template send
// the same body for the same inputs. No DOM here.
import type { PyEndpoint, PyParam } from "@platform/lib/api";

export type WidgetKind = "int" | "float" | "bool" | "str" | "json";

/** Widget kind from the annotation source string. Unannotated or complex
 *  (`list`, `dict`, `Optional[...]`, …) is free JSON. */
export function widgetKind(annotation: string | null): WidgetKind {
  if (annotation === "int") return "int";
  if (annotation === "float") return "float";
  if (annotation === "bool") return "bool";
  if (annotation === "str") return "str";
  return "json";
}

/** The field's starting text: the literal default as the widget shows it, or
 *  "" for a required param and for a non-literal default (shown, never
 *  prefilled — the template does the same). */
export function defaultText(p: PyParam): string {
  if (!p.has_default) return "";
  if (p.default_repr !== null) return "";
  const kind = widgetKind(p.annotation);
  if (kind === "bool") return p.default === true ? "true" : "false";
  if (kind === "str") return p.default === null ? "" : String(p.default);
  return JSON.stringify(p.default);
}

/** The default as a reader sees it in the signature. */
export function defaultLabel(p: PyParam): string | null {
  if (!p.has_default) return null;
  if (p.default_repr !== null) return p.default_repr;
  return pyLiteral(p.default);
}

/** A JSON value spelled as Python source, for the signature line. */
export function pyLiteral(v: unknown): string {
  if (v === null || v === undefined) return "None";
  if (v === true) return "True";
  if (v === false) return "False";
  if (typeof v === "string") return JSON.stringify(v).replace(/^"|"$/g, "'");
  if (Array.isArray(v)) return "[" + v.map(pyLiteral).join(", ") + "]";
  if (typeof v === "object") {
    return (
      "{" +
      Object.entries(v as Record<string, unknown>)
        .map(([k, x]) => `'${k}': ${pyLiteral(x)}`)
        .join(", ") +
      "}"
    );
  }
  return String(v);
}

export type Collected =
  | { ok: true; params: Record<string, unknown> }
  | { ok: false; field: string; message: string };

/** The form's strings → the JSON body. An empty optional field is OMITTED so
 *  the Python default applies; an empty required one is the error. Unannotated
 *  text that is not JSON goes as a string. */
export function collectParams(
  params: PyParam[],
  values: Record<string, string>,
): Collected {
  const out: Record<string, unknown> = {};
  for (const p of params) {
    const kind = widgetKind(p.annotation);
    const raw = (values[p.name] ?? defaultText(p)).trim();
    if (kind === "bool") {
      out[p.name] = raw === "true";
      continue;
    }
    if (raw === "") {
      if (!p.has_default) {
        return { ok: false, field: p.name, message: `${p.name} is required` };
      }
      continue;
    }
    if (kind === "int" || kind === "float") {
      const n = Number(raw);
      if (!Number.isFinite(n) || (kind === "int" && !Number.isInteger(n))) {
        return {
          ok: false,
          field: p.name,
          message: `${p.name} must be ${kind === "int" ? "an integer" : "a number"}`,
        };
      }
      out[p.name] = n;
    } else if (kind === "str") {
      out[p.name] = raw;
    } else {
      try {
        out[p.name] = JSON.parse(raw);
      } catch {
        out[p.name] = raw;
      }
    }
  }
  return { ok: true, params: out };
}

// ---- what kind of endpoint a file is ----------------------------------------

/** The badge on the row: which entrypoint the active engine would call.
 *  `udf` is only ever reported under the fused engine (the inspector's pick);
 *  `result` is a parameterless static script; `none` has nothing to run;
 *  `error` did not parse. */
export type EndpointKind = "udf" | "main" | "result" | "none" | "error";

export function endpointKind(ep: PyEndpoint): EndpointKind {
  if (ep.parse_error) return "error";
  if (ep.function) return ep.function.name === "main" ? "main" : "udf";
  if (ep.static_result) return "result";
  return "none";
}

/** Whether Execute makes sense for this file. */
export function isRunnable(ep: PyEndpoint): boolean {
  const k = endpointKind(ep);
  return k === "udf" || k === "main" || k === "result";
}

/** The first sentence-ish line of a docstring, for the collapsed row. */
export function summaryLine(doc: string | null | undefined): string {
  if (!doc) return "";
  const first = doc.trim().split(/\n\s*\n/)[0].replace(/\s+/g, " ").trim();
  return first.length > 140 ? first.slice(0, 137).trimEnd() + "…" : first;
}

/** A `?ep=` value fit to match against a walk's rel: relative, posix, no empty
 *  or dot segments. Same guard as the Files tab's `?file=`. */
export function safeRel(raw: string | null): string | null {
  if (!raw) return null;
  if (raw.startsWith("/") || raw.includes("\\")) return null;
  const parts = raw.split("/");
  if (parts.some((p) => !p || p === "." || p === "..")) return null;
  return raw;
}
