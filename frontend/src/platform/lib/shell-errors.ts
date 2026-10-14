/**
 * The shell's own error reporter: what the page saw, written where the server
 * log can show it next to the request lines.
 *
 * Why this exists: a blank window is the one failure the server cannot see.
 * index.html answered 200, then either a chunk failed to import, a render
 * threw, or the config fetch never settled — and from the server's side all
 * three are "GET / -> 200" followed by silence (the asset lines are not even
 * logged; `_LOG_SKIP_PREFIXES` drops `/static/`). The view runtime
 * (static/runtime.js `reportPageError`) already does this for a RENDERED page,
 * under `kind: "page-error"`, into the call store keyed by the page's file.
 * The shell has no file to key on and no interest in the store, so its events
 * take `kind: "shell-error"` and the endpoint writes them to the log only.
 *
 * Capped per document — a broken render loop throws thousands of times and
 * the first few are the diagnosis. Fire-and-forget with a swallowed
 * rejection: a failed report must never itself hit the unhandledrejection
 * path it is listening on.
 *
 * What a report carries is the minimum that names a failure: the PATH (not
 * the query — a `?path=` or a token in the search string has no business in
 * a session log that ends up in diagnostic bundles), the message, the source
 * position and the stack. A rejection value that is not an Error is reduced
 * to a short string, never serialised whole: an object rejected by some
 * fetch wrapper may be a response body.
 */

const SHELL_ERROR_CAP = 5;
let sent = 0;

export interface ShellErrorFields {
  /** Where it was caught: "window.onerror", "unhandledrejection", "boundary", "boot". */
  where: string;
  message: string;
  stack?: string;
  source?: string;
}

export function reportShellError(fields: ShellErrorFields): void {
  if (sent >= SHELL_ERROR_CAP) return;
  sent += 1;
  try {
    fetch("/api/calls/event", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Fused": "1" },
      body: JSON.stringify({
        kind: "shell-error",
        page: location.pathname,
        ...fields,
      }),
      // keepalive, NOT navigator.sendBeacon: a beacon cannot set the X-Fused
      // header this endpoint requires, so it would simply 403.
      keepalive: true,
    }).catch(() => {});
  } catch {
    /* reporting is best-effort; never let it break the page */
  }
}

const NON_ERROR_CAP = 200;

function describe(err: unknown): { message: string; stack?: string } {
  if (err instanceof Error) return { message: err.message || String(err), stack: err.stack };
  if (typeof err === "string") return { message: err.slice(0, NON_ERROR_CAP) };
  let text: string;
  try {
    text = String(err);
  } catch {
    text = "(unprintable rejection value)";
  }
  return { message: text.slice(0, NON_ERROR_CAP) };
}

/**
 * Installed once, before anything else in main.tsx runs — a throw during
 * module evaluation of the entry graph is exactly the case with no React, no
 * boundary and no fetch of its own to show up in a log, and only these two
 * window listeners can see it.
 */
export function installShellErrorReporter(): void {
  window.addEventListener("error", (ev) => {
    const d = ev.error ? describe(ev.error) : { message: ev.message };
    reportShellError({
      where: "window.onerror",
      message: d.message,
      stack: d.stack,
      source: ev.filename ? `${ev.filename}:${ev.lineno}:${ev.colno}` : undefined,
    });
  });
  window.addEventListener("unhandledrejection", (ev) => {
    const d = describe(ev.reason);
    reportShellError({ where: "unhandledrejection", message: d.message, stack: d.stack });
  });
}
