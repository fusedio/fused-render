// The app page's API tab: every .py in the app folder as an endpoint, the way
// a Swagger page lists routes — one line per file, a badge for what kind of
// entrypoint it has, its signature in mono; open a row and the parameters
// become a form with Execute under it and the response beneath that.
//
// The description of each file is the `api` template's (inspector.py, served
// for the whole folder by GET /api/apps/py — routers/app_api.py), and the run is
// the same POST /api/run the template makes, with the same coercion rules
// (app-api-lib.ts mirrors template.html). So a file behaves identically here
// and in its own api view; this tab only puts all of them on one page.
//
// The badge is the one loud thing on the row. It names the entrypoint the
// ACTIVE engine will call: UDF (a `@fused.udf` function, fused engine only),
// MAIN (`main()`), RESULT (a static `result = …` script, fused only), or a
// muted "no entrypoint" for a helper module — those stay listed rather than
// hidden, because "this file cannot be called" is an answer too.
//
// Which row is open lives in the QUERY (`?ep=<rel>`), written in place
// (replaceSearch — opening a row is not a navigation Back should retrace) and
// carried across a tab switch untouched by the page, so a Tasks detour and back
// finds the same endpoint open. Form values and responses are component state:
// they are a session's scratch, not an address.
import { useEffect, useMemo, useState } from "react";
import {
  getAppPy,
  runPy,
  type AppPyResult,
  type PyEndpoint,
  type PyParam,
  type RunResult,
} from "@platform/lib/api";
import { useUrlVersion } from "@platform/lib/hooks";
import { replaceSearch } from "@platform/lib/router";
import { cn } from "@platform/lib/utils";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import { Badge } from "@platform/shadcn/ui/badge";
import { Button } from "@platform/shadcn/ui/button";
import { Checkbox } from "@platform/shadcn/ui/checkbox";
import { Input } from "@platform/shadcn/ui/input";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { ChevronRight, FileCode2, Play } from "lucide-react";
import {
  collectParams,
  defaultLabel,
  defaultText,
  endpointKind,
  isRunnable,
  safeRel,
  summaryLine,
  widgetKind,
  type EndpointKind,
} from "./app-api-lib";

type Load =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ok"; data: AppPyResult };

type Run =
  | { kind: "running"; startedAt: number }
  | { kind: "done"; result: RunResult; ms: number; sent: Record<string, unknown> }
  | { kind: "failed"; message: string };

// The badge's words and colours, one per kind. Semantic tokens only, so both
// themes come for free; the two callable kinds share the primary tint because
// they are the same promise ("Execute works here"), the static script is
// quieter, and a helper module is outlined — present, not callable.
const KIND_BADGE: Record<EndpointKind, { label: string; className: string }> = {
  udf: { label: "UDF", className: "bg-primary/10 text-primary" },
  main: { label: "MAIN", className: "bg-primary/10 text-primary" },
  result: { label: "RESULT", className: "bg-secondary text-secondary-foreground" },
  none: { label: "MODULE", className: "border-border text-muted-foreground" },
  error: { label: "SYNTAX", className: "bg-destructive/10 text-destructive" },
};

const MONO = "font-mono text-[12.5px]";

export default function AppApi({
  dir,
  folderHref,
}: {
  /** The app folder, absolute forward-slash. */
  dir: string;
  folderHref: string;
}) {
  useUrlVersion();
  const openRel = safeRel(new URLSearchParams(location.search).get("ep"));

  const [load, setLoad] = useState<Load>({ kind: "loading" });
  // Per-endpoint scratch, keyed by rel: what the form holds, what the last run
  // said. Kept across open/close so a reader can compare two files' outputs.
  const [values, setValues] = useState<Record<string, Record<string, string>>>({});
  const [runs, setRuns] = useState<Record<string, Run>>({});
  const [invalid, setInvalid] = useState<Record<string, string | null>>({});

  useEffect(() => {
    let live = true;
    setLoad({ kind: "loading" });
    getAppPy(dir)
      .then((data) => live && setLoad({ kind: "ok", data }))
      .catch((e) => live && setLoad({ kind: "error", message: (e as Error).message }));
    return () => {
      live = false;
    };
  }, [dir]);

  const toggle = (rel: string) => {
    const next = new URLSearchParams(location.search);
    if (openRel === rel) next.delete("ep");
    else next.set("ep", rel);
    const q = next.toString();
    replaceSearch(location.pathname + (q ? "?" + q : ""));
  };

  const setValue = (rel: string, name: string, v: string) => {
    setValues((prev) => ({ ...prev, [rel]: { ...(prev[rel] ?? {}), [name]: v } }));
    setInvalid((prev) => (prev[rel] ? { ...prev, [rel]: null } : prev));
  };

  const execute = async (ep: PyEndpoint) => {
    const params = ep.function?.params ?? [];
    const collected = collectParams(params, values[ep.rel] ?? {});
    if (!collected.ok) {
      setInvalid((prev) => ({ ...prev, [ep.rel]: collected.field }));
      setRuns((prev) => ({ ...prev, [ep.rel]: { kind: "failed", message: collected.message } }));
      return;
    }
    const startedAt = performance.now();
    setRuns((prev) => ({ ...prev, [ep.rel]: { kind: "running", startedAt } }));
    try {
      const result = await runPy(ep.path, collected.params);
      setRuns((prev) => ({
        ...prev,
        [ep.rel]: {
          kind: "done",
          result,
          ms: Math.round(performance.now() - startedAt),
          sent: collected.params,
        },
      }));
    } catch (e) {
      setRuns((prev) => ({
        ...prev,
        [ep.rel]: { kind: "failed", message: (e as Error).message || "request failed" },
      }));
    }
  };

  const data = load.kind === "ok" ? load.data : null;
  const endpoints = data?.endpoints ?? [];
  const callable = endpoints.filter(isRunnable).length;
  // The project's declared dependencies are the FOLDER's, so every file reports
  // the same list; shown once, above the list, rather than on every row.
  const deps = useMemo(
    () => endpoints.find((e) => e.dependencies && e.dependencies.length)?.dependencies ?? [],
    [endpoints],
  );

  return (
    <div className="app-api flex min-h-0 flex-1 flex-col gap-3">
      {load.kind === "loading" && <SkeletonLines rows={3} label="Reading Python files" />}
      {load.kind === "error" && <ErrorBanner>Could not read the folder: {load.message}</ErrorBanner>}

      {data && (
        <>
          {/* The header line: how many routes, which engine will run them, and
              the environment they run in. Facts, set small. */}
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-0.5 text-[12.5px] text-muted-foreground">
            <span>
              <span className="text-foreground tabular-nums">{callable}</span>{" "}
              {callable === 1 ? "endpoint" : "endpoints"}
              {endpoints.length > callable && (
                <>
                  {" "}
                  · {endpoints.length - callable}{" "}
                  {endpoints.length - callable === 1 ? "module" : "modules"}
                </>
              )}
            </span>
            <span aria-hidden>·</span>
            <span>
              runs with the <span className="text-foreground">{data.engine}</span> engine
            </span>
            {deps.length > 0 && (
              <>
                <span aria-hidden>·</span>
                <span className="flex flex-wrap items-center gap-1">
                  {deps.map((d) => (
                    <Badge key={d} variant="outline" className={cn(MONO, "h-[18px] font-normal")}>
                      {d}
                    </Badge>
                  ))}
                </span>
              </>
            )}
          </div>

          {endpoints.length === 0 && (
            <div className="flex flex-1 flex-col items-center justify-center gap-2 rounded-xl border border-border py-16 text-[13px] text-muted-foreground">
              <FileCode2 className="size-7 opacity-60" aria-hidden />
              <p className="m-0">No Python files in this app yet.</p>
              <p className="m-0">
                Add a <code className={MONO}>.py</code> with a <code className={MONO}>main()</code>{" "}
                and it shows up here as an endpoint.
              </p>
            </div>
          )}

          {endpoints.length > 0 && (
            <div className="flex min-h-0 flex-1 flex-col overflow-auto rounded-xl border border-border">
              <ul className="m-0 list-none divide-y divide-border p-0">
                {endpoints.map((ep) => (
                  <EndpointRow
                    key={ep.rel}
                    ep={ep}
                    open={openRel === ep.rel}
                    values={values[ep.rel] ?? {}}
                    run={runs[ep.rel]}
                    invalidField={invalid[ep.rel] ?? null}
                    onToggle={() => toggle(ep.rel)}
                    onChange={(name, v) => setValue(ep.rel, name, v)}
                    onExecute={() => execute(ep)}
                  />
                ))}
              </ul>
              {data.truncated && (
                <p className="m-0 px-4 py-3 text-[12px] text-muted-foreground">
                  Showing the first {endpoints.length} files.{" "}
                  <a href={folderHref} className="text-inherit underline">
                    Open the folder
                  </a>{" "}
                  for the rest.
                </p>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

// ---- one endpoint ------------------------------------------------------------

function EndpointRow({
  ep,
  open,
  values,
  run,
  invalidField,
  onToggle,
  onChange,
  onExecute,
}: {
  ep: PyEndpoint;
  open: boolean;
  values: Record<string, string>;
  run: Run | undefined;
  invalidField: string | null;
  onToggle: () => void;
  onChange: (name: string, v: string) => void;
  onExecute: () => void;
}) {
  const kind = endpointKind(ep);
  const badge = KIND_BADGE[kind];
  const fn = ep.function ?? null;
  const params = fn?.params ?? [];
  const runnable = isRunnable(ep);
  const summary = summaryLine(fn?.docstring ?? ep.module_docstring);
  const cut = ep.rel.lastIndexOf("/");
  const crumb = cut >= 0 ? ep.rel.slice(0, cut + 1) : "";
  const name = ep.rel.slice(cut + 1);
  const bodyId = `app-api-body-${ep.rel.replace(/[^a-zA-Z0-9_-]/g, "_")}`;

  return (
    <li className={cn("app-api-row", open && "bg-muted/30")}>
      {/* The route line. Badge, path, signature, chevron — one row, one click. */}
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        aria-controls={bodyId}
        className={cn(
          "flex w-full items-center gap-3 border-0 bg-transparent px-3 py-2.5 text-left text-foreground",
          "cursor-pointer hover:bg-muted/40 focus-visible:outline-2 focus-visible:outline-ring focus-visible:-outline-offset-2",
          !runnable && "text-muted-foreground",
        )}
      >
        <Badge
          variant="outline"
          className={cn(
            MONO,
            "h-[20px] w-[64px] justify-center rounded-md border-transparent px-0 text-[10.5px] font-semibold tracking-[0.06em]",
            badge.className,
          )}
        >
          {badge.label}
        </Badge>
        <span className={cn(MONO, "min-w-0 flex-none truncate")} title={ep.path}>
          {crumb && <span className="text-muted-foreground">{crumb}</span>}
          <span className={cn(runnable ? "font-medium" : "font-normal")}>{name}</span>
        </span>
        {fn && (
          <span className={cn(MONO, "min-w-0 flex-1 truncate text-muted-foreground")}>
            <Signature fn={fn} />
          </span>
        )}
        {!fn && summary && (
          <span className="min-w-0 flex-1 truncate text-[12.5px] text-muted-foreground">
            {summary}
          </span>
        )}
        {!fn && !summary && <span className="flex-1" />}
        <ChevronRight
          aria-hidden
          className={cn(
            "size-4 flex-none text-muted-foreground transition-transform duration-150 motion-reduce:transition-none",
            open && "rotate-90",
          )}
        />
      </button>

      {open && (
        <div id={bodyId} className="flex flex-col gap-4 border-t border-border px-4 pt-3 pb-4">
          {kind === "error" && (
            <pre className={cn(MONO, "m-0 whitespace-pre-wrap text-destructive")}>
              Syntax error — {ep.parse_error}
            </pre>
          )}

          {(ep.module_docstring || fn?.docstring) && (
            <div className="flex flex-col gap-2 text-[13px] leading-relaxed text-foreground/85">
              {ep.module_docstring && (
                <p className="m-0 whitespace-pre-wrap">{ep.module_docstring.trim()}</p>
              )}
              {fn?.docstring && fn.docstring !== ep.module_docstring && (
                <p className="m-0 whitespace-pre-wrap">{fn.docstring.trim()}</p>
              )}
            </div>
          )}

          {kind === "none" && (
            <p className="m-0 text-[12.5px] text-muted-foreground">
              No <code className={MONO}>main()</code> here — a helper module, imported by the
              others rather than called on its own. Define <code className={MONO}>main()</code> to
              make it an endpoint.
            </p>
          )}

          {kind === "result" && (
            <p className="m-0 text-[12.5px] text-muted-foreground">
              Static script — assigns <code className={MONO}>result</code> at the top level, no
              parameters.
            </p>
          )}

          {fn && (
            <section className="flex flex-col gap-2">
              <Eyebrow>Parameters</Eyebrow>
              {params.length === 0 && (
                <p className="m-0 text-[12.5px] text-muted-foreground">None.</p>
              )}
              {params.length > 0 && (
                <div className="grid grid-cols-[minmax(120px,max-content)_minmax(80px,max-content)_1fr] items-start gap-x-5 gap-y-2.5">
                  {params.map((p) => (
                    <ParamRow
                      key={p.name}
                      p={p}
                      value={values[p.name] ?? defaultText(p)}
                      invalid={invalidField === p.name}
                      onChange={(v) => onChange(p.name, v)}
                      idPrefix={bodyId}
                    />
                  ))}
                </div>
              )}
            </section>
          )}

          {runnable && (
            <div className="flex items-center gap-3">
              <Button
                size="sm"
                onClick={onExecute}
                disabled={run?.kind === "running"}
                aria-busy={run?.kind === "running"}
              >
                <Play data-icon="inline-start" />
                {run?.kind === "running" ? "Running…" : "Execute"}
              </Button>
              {run?.kind === "failed" && (
                <span className="text-[12.5px] text-destructive">{run.message}</span>
              )}
              {run?.kind === "done" && (
                <span className="text-[12px] text-muted-foreground tabular-nums">
                  {run.result.ok ? "Returned" : "Failed"} in{" "}
                  {formatMs(run.result.duration_ms ?? run.ms)}
                </span>
              )}
            </div>
          )}

          {run?.kind === "running" && <SkeletonLines rows={2} label="Running" />}
          {run?.kind === "done" && <Response run={run} py={ep.path} />}
        </div>
      )}
    </li>
  );
}

/** `main(city: str, limit: int = 10)` — the signature as the row's subtitle. */
function Signature({ fn }: { fn: NonNullable<PyEndpoint["function"]> }) {
  return (
    <>
      <span className="text-foreground/80">{fn.name}</span>(
      {fn.params.map((p, i) => (
        <span key={p.name}>
          {i > 0 && ", "}
          <span className="text-foreground/80">{p.name}</span>
          {p.annotation && <span>: {p.annotation}</span>}
          {p.has_default && <span> = {defaultLabel(p)}</span>}
        </span>
      ))}
      )
    </>
  );
}

function ParamRow({
  p,
  value,
  invalid,
  onChange,
  idPrefix,
}: {
  p: PyParam;
  value: string;
  invalid: boolean;
  onChange: (v: string) => void;
  idPrefix: string;
}) {
  const kind = widgetKind(p.annotation);
  const required = !p.has_default;
  const id = `${idPrefix}-${p.name}`;
  const dflt = defaultLabel(p);
  return (
    <>
      <label htmlFor={id} className={cn(MONO, "flex items-baseline gap-1 pt-1.5 leading-snug")}>
        <span className="text-foreground">{p.name}</span>
        {required && (
          <span className="text-destructive" title="required" aria-label="required">
            *
          </span>
        )}
      </label>
      <div className={cn(MONO, "flex flex-col gap-0.5 pt-1.5 text-[11.5px] leading-snug")}>
        <span className="text-primary">{p.annotation ?? "any"}</span>
        {dflt !== null && <span className="text-muted-foreground">= {dflt}</span>}
      </div>
      <div className="min-w-0">
        {kind === "bool" && (
          <div className="flex h-8 items-center">
            <Checkbox
              id={id}
              checked={value === "true"}
              onCheckedChange={(c) => onChange(c ? "true" : "false")}
            />
          </div>
        )}
        {(kind === "int" || kind === "float") && (
          <Input
            id={id}
            type="number"
            step={kind === "int" ? 1 : "any"}
            value={value}
            aria-invalid={invalid || undefined}
            onChange={(e) => onChange(e.target.value)}
            className={cn(MONO, "h-8 max-w-[260px]")}
          />
        )}
        {kind === "str" && (
          <Input
            id={id}
            type="text"
            value={value}
            aria-invalid={invalid || undefined}
            onChange={(e) => onChange(e.target.value)}
            className={cn(MONO, "h-8")}
          />
        )}
        {kind === "json" && (
          <Textarea
            id={id}
            rows={1}
            value={value}
            placeholder="JSON"
            aria-invalid={invalid || undefined}
            onChange={(e) => onChange(e.target.value)}
            className={cn(MONO, "min-h-8 resize-y py-1.5")}
          />
        )}
      </div>
    </>
  );
}

/** The response card: status, the result (or traceback), stdout, and the
 *  request that produced it — Swagger's curl block, for the fused bridge. */
function Response({
  run,
  py,
}: {
  run: Extract<Run, { kind: "done" }>;
  py: string;
}) {
  const { result } = run;
  const body = JSON.stringify({ py, params: run.sent }, null, 2);
  return (
    <section className="flex flex-col gap-2">
      <Eyebrow>Response</Eyebrow>
      <div className="overflow-hidden rounded-lg border border-border">
        <div className="flex items-center gap-2 border-b border-border bg-muted/40 px-3 py-1.5">
          <Badge
            variant="outline"
            className={cn(
              MONO,
              "h-[18px] rounded-md border-transparent px-1.5 text-[10.5px] font-semibold tracking-[0.06em]",
              result.ok ? "bg-primary/10 text-primary" : "bg-destructive/10 text-destructive",
            )}
          >
            {result.ok ? "OK" : "ERROR"}
          </Badge>
          {!result.ok && result.error?.type && (
            <span className={cn(MONO, "text-muted-foreground")}>{result.error.type}</span>
          )}
          <span className="ml-auto text-[11.5px] text-muted-foreground tabular-nums">
            {formatMs(result.duration_ms ?? run.ms)}
          </span>
        </div>
        <pre
          className={cn(
            MONO,
            "m-0 max-h-[420px] overflow-auto px-3 py-2.5 leading-relaxed",
            !result.ok && "text-destructive",
          )}
        >
          {result.ok
            ? JSON.stringify(result.result, null, 2)
            : result.error?.traceback || result.error?.message || "run failed"}
        </pre>
        {result.stdout && (
          <Fold label="stdout" open>
            {result.stdout}
          </Fold>
        )}
        {result.stderr && <Fold label="stderr">{result.stderr}</Fold>}
        <Fold label="Request">{`POST /api/run\n${body}`}</Fold>
      </div>
    </section>
  );
}

function Fold({
  label,
  open,
  children,
}: {
  label: string;
  open?: boolean;
  children: string;
}) {
  return (
    <details className="border-t border-border" open={open}>
      <summary className="cursor-pointer select-none px-3 py-1.5 text-[12px] text-muted-foreground hover:text-foreground">
        {label}
      </summary>
      <pre className={cn(MONO, "m-0 max-h-[280px] overflow-auto px-3 pb-2.5 leading-relaxed")}>
        {children}
      </pre>
    </details>
  );
}

function Eyebrow({ children }: { children: string }) {
  return (
    <h3 className="m-0 text-[11px] font-semibold tracking-[0.08em] text-muted-foreground uppercase">
      {children}
    </h3>
  );
}

function formatMs(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)} s`;
}
