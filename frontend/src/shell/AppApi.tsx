// The app page's API tab: every .py in the app folder as an endpoint, the way
// a Swagger page lists routes — one line per file, a glyph for what kind of
// entrypoint it has, one line about it; open a row and the parameters
// become a form with Execute under it and the response beneath that.
//
// The description of each file is the `api` template's (inspector.py, served
// for the whole folder by GET /api/apps/py — routers/app_api.py), and the run is
// the same POST /api/run the template makes, with the same coercion rules
// (app-api-lib.ts mirrors template.html). So a file behaves identically here
// and in its own api view; this tab only puts all of them on one page.
//
// The list is meant to read as a LIST, not as code: a glyph for what the file
// is (a play mark if Execute works on it, a file mark for a helper module, a
// warning if it will not parse), the filename in mono, and one sentence about
// it in prose — the docstring's first line, or failing that just the parameter
// names. Types, defaults and the full signature wait inside the open row. The
// entrypoint kind the ACTIVE engine will call (`@fused.udf` under fused, else
// `main()`, or a static `result = …`) only gets a word when it is unusual.
// Helper modules stay listed rather than hidden, dimmer: "this file cannot be
// called" is an answer too.
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
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia } from "@platform/shadcn/ui/empty";
import { EntityList } from "@platform/ui/flow/EntityRow";
import { PropertyList, PropertyRow } from "@platform/ui/flow/PropertyRow";
import { StatusBadge } from "@platform/ui/flow/StatusIcon";
import { SectionHeading, Tiny } from "@platform/ui/flow/Typography";
import { Checkbox } from "@platform/shadcn/ui/checkbox";
import { Input } from "@platform/shadcn/ui/input";
import { Textarea } from "@platform/shadcn/ui/textarea";
import {
  Check,
  ChevronRight,
  Copy,
  FileCode2,
  Play,
  TriangleAlert,
} from "lucide-react";
import {
  collectParams,
  curlCommand,
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
  | {
      kind: "done";
      result: RunResult;
      ms: number;
      sent: Record<string, unknown>;
    }
  | { kind: "failed"; message: string };

// The row's trailing chip — the Swagger "method" slot: what kind of entrypoint
// the file has, in one word. The common case (a `main()`) still gets its chip
// so the column reads as a column; broken files take the red status bucket.
const KIND_CHIP: Record<EndpointKind, string> = {
  udf: "UDF",
  main: "MAIN",
  result: "RESULT",
  none: "MODULE",
  error: "ERROR",
  unreadable: "UNREADABLE",
};

function KindChip({ kind }: { kind: EndpointKind }) {
  const label = KIND_CHIP[kind];
  if (kind === "error" || kind === "unreadable") {
    return (
      <StatusBadge bucket="red" className="font-mono tracking-wide">
        {label}
      </StatusBadge>
    );
  }
  return (
    <Badge
      variant="outline"
      className={cn(
        "font-mono tracking-wide text-muted-foreground",
        kind === "none" && "border-dashed",
      )}
    >
      {label}
    </Badge>
  );
}

/** The row's leading glyph: a play mark for anything Execute works on, a file
 *  mark for a helper module, a warning for a file that does not parse. Muted at
 *  rest; the play mark takes the primary colour when the row is hovered, which
 *  is the only colour on the list. */
function KindGlyph({ kind }: { kind: EndpointKind }) {
  if (kind === "error" || kind === "unreadable") {
    return (
      <TriangleAlert
        aria-hidden
        className="size-4 flex-none text-destructive"
      />
    );
  }
  if (kind === "none") {
    return (
      <FileCode2
        aria-hidden
        className="size-4 flex-none text-muted-foreground/60"
      />
    );
  }
  return (
    <Play
      aria-hidden
      className="size-3.5 flex-none fill-current text-muted-foreground/70 transition-colors group-hover/row:text-primary motion-reduce:transition-none"
    />
  );
}

const MONO = "font-mono text-xs";

export default function AppApi({
  dir,
  folderHref,
}: {
  /** The app folder, absolute forward-slash. */
  dir: string;
  folderHref: string;
}) {
  useUrlVersion();
  // Absent `ep` means "nothing chosen yet" — the first endpoint opens so the
  // page lands on a form, not a list of closed rows (resolved below once the
  // list is loaded). Present-but-empty `ep=` is the reader having closed
  // that row: nothing open, and it stays that way.
  const rawEp = new URLSearchParams(location.search).get("ep");
  const chosenRel = safeRel(rawEp);

  const [load, setLoad] = useState<Load>({ kind: "loading" });
  // Per-endpoint scratch, keyed by rel: what the form holds, what the last run
  // said. Kept across open/close so a reader can compare two files' outputs.
  const [values, setValues] = useState<Record<string, Record<string, string>>>(
    {},
  );
  const [runs, setRuns] = useState<Record<string, Run>>({});
  const [invalid, setInvalid] = useState<Record<string, string | null>>({});

  useEffect(() => {
    let live = true;
    setLoad({ kind: "loading" });
    getAppPy(dir)
      .then((data) => live && setLoad({ kind: "ok", data }))
      .catch(
        (e) =>
          live && setLoad({ kind: "error", message: (e as Error).message }),
      );
    return () => {
      live = false;
    };
  }, [dir]);

  const toggle = (rel: string) => {
    const next = new URLSearchParams(location.search);
    if (openRel === rel) next.set("ep", "");
    else next.set("ep", rel);
    const q = next.toString();
    replaceSearch(location.pathname + (q ? "?" + q : ""));
  };

  const setValue = (rel: string, name: string, v: string) => {
    setValues((prev) => ({
      ...prev,
      [rel]: { ...(prev[rel] ?? {}), [name]: v },
    }));
    setInvalid((prev) => (prev[rel] ? { ...prev, [rel]: null } : prev));
  };

  const execute = async (ep: PyEndpoint) => {
    const params = ep.function?.params ?? [];
    const collected = collectParams(params, values[ep.rel] ?? {});
    if (!collected.ok) {
      setInvalid((prev) => ({ ...prev, [ep.rel]: collected.field }));
      setRuns((prev) => ({
        ...prev,
        [ep.rel]: { kind: "failed", message: collected.message },
      }));
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
        [ep.rel]: {
          kind: "failed",
          message: (e as Error).message || "request failed",
        },
      }));
    }
  };

  const data = load.kind === "ok" ? load.data : null;
  const endpoints = data?.endpoints ?? [];
  const openRel = rawEp === null ? (endpoints[0]?.rel ?? null) : chosenRel;
  const callable = endpoints.filter(isRunnable).length;
  // Three counts, not two: a file that will not parse or cannot be read is not
  // a helper module, and calling it one would hide exactly the files a reader
  // most needs to notice.
  const broken = endpoints.filter((e) => {
    const k = endpointKind(e);
    return k === "error" || k === "unreadable";
  }).length;
  const helpers = endpoints.length - callable - broken;
  // The project's declared dependencies are the FOLDER's, so every file reports
  // the same list; shown once, above the list, rather than on every row.
  const deps = useMemo(
    () =>
      endpoints.find((e) => e.dependencies && e.dependencies.length)
        ?.dependencies ?? [],
    [endpoints],
  );

  // The tab body is the scroller (the shell is a fixed-height frame whose
  // document never scrolls): a long list, or an open row with a tall response,
  // scrolls here. The list inside hugs its rows (flex-none).
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto scrollbar-auto-hide">
      {load.kind === "loading" && (
        <SkeletonLines rows={3} label="Reading Python files" />
      )}
      {load.kind === "error" && (
        <ErrorBanner>Could not read the folder: {load.message}</ErrorBanner>
      )}

      {data && (
        <>
          {/* The header line: one plain sentence about what is here, and — set
              apart on the right — what the folder needs installed. The engine
              is not named: nobody chooses it on this page. */}
          {endpoints.length > 0 && (
            <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 px-1 text-sm text-muted-foreground">
              <p className="m-0">
                {callable === 0
                  ? "Nothing to run yet"
                  : `${callable} ${callable === 1 ? "endpoint" : "endpoints"} you can run`}
                {helpers > 0 &&
                  ` · ${helpers} helper ${helpers === 1 ? "module" : "modules"}`}
                {broken > 0 && (
                  <>
                    {" · "}
                    <span className="text-destructive">
                      {broken} {broken === 1 ? "file" : "files"} with problems
                    </span>
                  </>
                )}
              </p>
              {deps.length > 0 && (
                <p className="m-0 flex flex-wrap items-baseline gap-x-1.5 gap-y-1">
                  <span>Needs</span>
                  {deps.map((d) => (
                    <Badge
                      key={d}
                      variant="outline"
                      className="font-mono font-normal text-muted-foreground"
                    >
                      {d}
                    </Badge>
                  ))}
                </p>
              )}
            </div>
          )}

          {endpoints.length === 0 && (
            <Empty className="border border-border py-16">
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <FileCode2 />
                </EmptyMedia>
                <EmptyDescription>
                  No Python files in this app yet. Add a <code className={MONO}>.py</code> with a{" "}
                  <code className={MONO}>main()</code> and it shows up here as an endpoint.
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
          )}

          {/* Hugs its rows — no flex-1: four endpoints should not be stretched
              into a full-height frame with a void under them. */}
          {endpoints.length > 0 && (
            <EntityList className="flex flex-none flex-col">
              <ul className="m-0 list-none p-0">
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
                <p className="m-0 border-t border-border px-4 py-3 text-xs text-muted-foreground">
                  Showing the first {endpoints.length} files.{" "}
                  <a href={folderHref} className="text-inherit underline">
                    Open the folder
                  </a>{" "}
                  for the rest.
                </p>
              )}
            </EntityList>
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
  const fn = ep.function ?? null;
  const params = fn?.params ?? [];
  const runnable = isRunnable(ep);
  const summary = summaryLine(fn?.docstring ?? ep.module_docstring);
  // A missing docstring falls back to the parameter NAMES — enough to tell two
  // endpoints apart, without the types and defaults.
  const paramHint =
    fn && params.length > 0
      ? params.map((p) => p.name).join(" · ")
      : fn
        ? "No parameters"
        : kind === "none"
          ? "Helper module — nothing to call"
          : "";
  const cut = ep.rel.lastIndexOf("/");
  const crumb = cut >= 0 ? ep.rel.slice(0, cut + 1) : "";
  const name = ep.rel.slice(cut + 1);
  const bodyId = `app-api-body-${ep.rel.replace(/[^a-zA-Z0-9_-]/g, "_")}`;
  // "Copy as curl": the form as it stands, or an empty body when it does not
  // validate — a snippet with a blank required field is still worth pasting.
  const [copied, setCopied] = useState(false);
  const copyCurl = async () => {
    const collected = collectParams(params, values);
    const sent = collected.ok ? collected.params : {};
    await navigator.clipboard.writeText(
      curlCommand(location.origin, ep.path, sent),
    );
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };

  return (
    <li className={cn("group/row border-b border-border last:border-b-0", open && "bg-accent/20")}>
      {/* The route line — an entity row: a glyph for what the file is, its name
          (mono identifier), one sentence about it; then a right cluster — "Copy
          as cURL" (on hover, or always once open), the kind chip, the chevron.
          The signature is NOT here: types and defaults belong to the open form,
          and a wall of them made the list read as code. The toggle is its own
          <button> (not EntityRow: the row needs aria-expanded/aria-controls,
          which the shared composite does not pass) and the cluster sits BESIDE
          it, because a button cannot nest a button; the chip + chevron half of
          the cluster forwards its click to the toggle so the whole line still
          opens the row. */}
      <div
        className={cn(
          "flex w-full min-w-0 items-stretch text-sm",
          "hover:bg-accent/50 has-[button[aria-expanded]:focus-visible]:bg-accent/50",
        )}
      >
        <button
          type="button"
          data-slot="entity-row"
          onClick={onToggle}
          aria-expanded={open}
          aria-controls={bodyId}
          className={cn(
            "flex min-w-0 flex-1 cursor-pointer items-center gap-3 border-0 bg-transparent py-2 pl-4 text-left text-foreground focus-visible:outline-none",
            !runnable && "text-muted-foreground",
          )}
        >
          <KindGlyph kind={kind} />
          <span className="flex min-w-0 flex-1 flex-col gap-0.5 sm:flex-row sm:items-baseline sm:gap-3">
            <span
              className={cn(
                MONO,
                "flex-none truncate",
                runnable && "font-medium",
              )}
              title={ep.path}
            >
              {crumb && <span className="text-muted-foreground">{crumb}</span>}
              {name}
            </span>
            <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
              {summary || paramHint}
            </span>
          </span>
        </button>
        <span className="flex flex-none items-center gap-2 pr-4 pl-2">
          {runnable && (
            <Button
              size="xs"
              variant="ghost"
              onClick={copyCurl}
              title="Copy this call as a curl command"
              className={cn(
                "border-border bg-transparent px-1.5 font-normal text-muted-foreground transition-opacity hover:bg-transparent hover:text-foreground dark:hover:bg-transparent",
                "group-hover/row:pointer-events-auto group-hover/row:opacity-100 focus-visible:pointer-events-auto focus-visible:opacity-100 motion-reduce:transition-none",
                // Hidden means hidden: no hit-testing, so a tap on a collapsed
                // row (touch has no hover) reaches the toggle beside it.
                open ? "opacity-100" : "pointer-events-none opacity-0",
              )}
            >
              {copied ? (
                <Check data-icon="inline-start" />
              ) : (
                <Copy data-icon="inline-start" />
              )}
              {copied ? "Copied" : "Copy as cURL"}
            </Button>
          )}
          {/* Not a second button (one toggle per row for assistive tech): a
              plain span that hands its click to the toggle. */}
          <span
            className="flex cursor-pointer items-center gap-2"
            onClick={onToggle}
            aria-hidden
          >
            <KindChip kind={kind} />
            <ChevronRight
              className={cn(
                "size-3.5 flex-none text-muted-foreground/70 transition-transform duration-150 motion-reduce:transition-none",
                open && "rotate-90",
              )}
            />
          </span>
        </span>
      </div>

      {open && (
        <div
          id={bodyId}
          className="flex flex-col gap-4 border-t border-border px-4 pt-3 pb-4"
        >
          <PropertyList className="max-w-2xl">
            <PropertyRow label="File">
              <span className={MONO} title={ep.path}>
                {ep.rel}
              </span>
            </PropertyRow>
            {fn && (
              <PropertyRow label="Signature">
                <span className={cn(MONO, "text-muted-foreground")} title={fn.name}>
                  <Signature fn={fn} />
                </span>
              </PropertyRow>
            )}
            <PropertyRow label="Entrypoint">
              <span className="text-xs">
                {kind === "udf"
                  ? "@fused.udf"
                  : kind === "main"
                    ? "main()"
                    : kind === "result"
                      ? "static result ="
                      : kind === "none"
                        ? "none — helper module"
                        : kind === "error"
                          ? "won't parse"
                          : "can't read"}
              </span>
            </PropertyRow>
          </PropertyList>
          {kind === "error" && (
            <pre
              className={cn(MONO, "m-0 whitespace-pre-wrap text-destructive")}
            >
              Syntax error — {ep.parse_error}
            </pre>
          )}
          {kind === "unreadable" && (
            <pre
              className={cn(MONO, "m-0 whitespace-pre-wrap text-destructive")}
            >
              Could not read this file — {ep.read_error}
            </pre>
          )}

          {(ep.module_docstring || fn?.docstring) && (
            <div className="flex flex-col gap-2 text-sm leading-relaxed text-foreground/85">
              {ep.module_docstring && (
                <p className="m-0 whitespace-pre-wrap">
                  {ep.module_docstring.trim()}
                </p>
              )}
              {fn?.docstring && fn.docstring !== ep.module_docstring && (
                <p className="m-0 whitespace-pre-wrap">{fn.docstring.trim()}</p>
              )}
            </div>
          )}

          {kind === "none" && (
            <p className="m-0 text-xs text-muted-foreground">
              No <code className={MONO}>main()</code> here — a helper module,
              imported by the others rather than called on its own. Define{" "}
              <code className={MONO}>main()</code> to make it an endpoint.
            </p>
          )}

          {kind === "result" && (
            <p className="m-0 text-xs text-muted-foreground">
              Static script — assigns <code className={MONO}>result</code> at
              the top level, no parameters.
            </p>
          )}

          {fn && (
            <section className="flex flex-col gap-2">
              <SectionHeading className="text-xs">Parameters</SectionHeading>
              {params.length === 0 && (
                <p className="m-0 text-xs text-muted-foreground">None.</p>
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
                <span className="text-xs text-destructive">
                  {run.message}
                </span>
              )}
              {run?.kind === "done" && (
                <Tiny className="tabular-nums">
                  {run.result.ok ? "Returned" : "Failed"} in{" "}
                  {formatMs(run.result.duration_ms ?? run.ms)}
                </Tiny>
              )}
            </div>
          )}

          {run?.kind === "running" && (
            <SkeletonLines rows={2} label="Running" />
          )}
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
      <label
        htmlFor={id}
        className={cn(MONO, "flex items-baseline gap-1 pt-1.5 leading-snug")}
      >
        <span className="text-foreground">{p.name}</span>
        {required && (
          <span
            className="text-destructive"
            title="required"
            aria-label="required"
          >
            *
          </span>
        )}
      </label>
      <div
        className={cn(
          MONO,
          "flex flex-col gap-0.5 pt-1.5 text-xs leading-snug",
        )}
      >
        <span className="text-primary">{p.annotation ?? "any"}</span>
        {dflt !== null && (
          <span className="text-muted-foreground">= {dflt}</span>
        )}
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
      <SectionHeading className="text-xs">Response</SectionHeading>
      <div className="overflow-hidden rounded-lg border border-border">
        <div className="flex items-center gap-2 border-b border-border bg-muted/40 px-3 py-1.5">
          {/* Status colour from the one map: green = ok, red = error. */}
          <StatusBadge
            status={result.ok ? "ok" : "error"}
            className="font-mono font-semibold tracking-wide"
          >
            {result.ok ? "OK" : "ERROR"}
          </StatusBadge>
          {!result.ok && result.error?.type && (
            <span className={cn(MONO, "text-muted-foreground")}>
              {result.error.type}
            </span>
          )}
          <Tiny className="ml-auto tabular-nums">{formatMs(result.duration_ms ?? run.ms)}</Tiny>
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
      <summary className="cursor-pointer select-none px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground">
        {label}
      </summary>
      <pre
        className={cn(
          MONO,
          "m-0 max-h-[280px] overflow-auto px-3 pb-2.5 leading-relaxed",
        )}
      >
        {children}
      </pre>
    </details>
  );
}

function formatMs(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)} s`;
}
