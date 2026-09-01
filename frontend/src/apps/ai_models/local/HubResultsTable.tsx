// The dense results table search replaces the card grid with (task 4). One
// row per model FAMILY (`hubFamilies.groupIntoFamilies`), eleven columns wide,
// following `BenchmarkTab.tsx`'s own `<table>` — `scope="col"` headers, the
// same `am-bench-*` conventions this file's `am-hubtable-*` classes sit
// beside in `ai-models.css`.
//
// **Why a table replaces the grid rather than joining it.** A card can carry
// two or three facts before it sprawls, which is why the grid it replaces
// showed popularity and size and nothing about THIS machine. Eleven columns
// only become legible in a row, and a row is only worth reading once it is
// SCORED — see `hubTableView.ts` for the cell rules and the plan this
// implements for the fuller argument.
//
// **The lazy total-size lookup stays viewport-gated**, moved here verbatim
// from `HubResultCard` (RecommendedCard.tsx) rather than rewritten: a dense
// table shows several times more rows at once than the grid did, which is
// exactly the request storm the IntersectionObserver gating exists to
// prevent — more rows on screen makes the gate MORE load-bearing, not less.
import { useEffect, useRef, useState, type CSSProperties } from "react";
import { hubModelUrl } from "./hub";
import { modelName, CuratedMark } from "./RepoCard";
import { SwitchEngines } from "./RecommendedCard";
import {
  PARTIAL_TAG,
  type DiskCard,
  type ResultDisk,
  resultDisk,
  jobFraction,
  type SectionRunner,
} from "@apps/ai_models/lib/aiModelGroups";
import { gateChrome } from "@apps/ai_models/lib/hubSearchView";
import { type HubFamily } from "@apps/ai_models/lib/hubFamilies";
import {
  ageLabel,
  familyDisplay,
  fitCell,
  paramsLabel,
  popLabel,
  runModeLabel,
  speedLabel,
  variantLabel,
} from "@apps/ai_models/lib/hubTableView";
import { hubSizeLabel, hubSizeTitle, knownTotalSize, lookupTotalSize } from "@apps/ai_models/lib/hubSize";
import { CancelButton } from "@apps/ai_models/shared/CancelButton";
import { DownloadGlyph } from "@apps/ai_models/shared/ModelProgress";
import { type HubModel } from "@platform/lib/api";
import { type Job } from "@platform/lib/jobs";

/** One family's row. Split out of the table so the lazy size lookup's own
 *  `useState`/`useEffect`/`IntersectionObserver` triple — which needs a real
 *  DOM node — is scoped to the one row it is about, the same boundary
 *  `HubResultCard` drew it at. */
function HubResultRow({
  family,
  curated,
  runner,
  disk,
  authenticated,
  busy,
  job,
  onDownload,
  onCancel,
}: {
  family: HubFamily;
  curated: boolean;
  runner: SectionRunner | null;
  disk: ResultDisk;
  authenticated: boolean;
  busy: boolean;
  job: Job | undefined;
  onDownload: () => void;
  onCancel: (job: Job) => void;
}) {
  const model = family.primary;
  const row = useRef<HTMLTableRowElement>(null);
  const wantsTotal = !model.estimatedSize;
  const [total, setTotal] = useState<number | null>(
    (wantsTotal ? knownTotalSize(model.id) : null) ?? null,
  );

  useEffect(() => {
    if (!wantsTotal) return;
    const known = knownTotalSize(model.id);
    if (known !== undefined) {
      setTotal(known);
      return;
    }
    const el = row.current;
    if (!el) return;
    let alive = true;
    let asking = false;
    const io = new IntersectionObserver((entries) => {
      if (!entries.some((e) => e.isIntersecting)) {
        asking = false;
        return;
      }
      if (asking) return;
      asking = true;
      lookupTotalSize(model.id).then((bytes) => {
        if (!alive) return;
        setTotal(bytes);
        if (knownTotalSize(model.id) !== undefined) io.disconnect();
      });
    });
    io.observe(el);
    return () => {
      alive = false;
      io.disconnect();
    };
  }, [model.id, wantsTotal]);

  const display = familyDisplay(family);
  const fit = fitCell(model.fit);
  const size = hubSizeLabel(model, total);
  const gate = disk.state === "downloaded" ? null : gateChrome(model.gated, authenticated);
  const loadable = !runner || runner.available;
  const arriving = jobFraction(job);

  return (
    <tr
      ref={row}
      // The same three washes every other card on this page wears for the
      // same three states (D436) — a row IS a claim about this disk exactly
      // like a card is, and two colour grammars for one fact would be two
      // answers to "do I have this". `am-card-arriving` needs `--am-part`
      // (below); `am-card-part-unknown` is the flat wash `HubResultCard` used
      // for the same reason: a table row has no folder of its own to measure
      // a fraction from.
      className={
        "am-hubtable-row" +
        (arriving !== null
          ? " am-card-arriving"
          : disk.state === "downloaded"
            ? " am-card-have"
            : disk.state === "partial"
              ? " am-card-part-unknown"
              : "")
      }
      style={arriving === null ? undefined : ({ "--am-part": `${arriving * 100}%` } as CSSProperties)}
    >
      <td className="am-hubtable-fit">
        {fit ? (
          <>
            <span className={`am-hubtable-dot am-hubtable-dot-${fit.dot}`} aria-hidden="true" />
            <span
              className="am-hubtable-bar"
              role="img"
              aria-label={`Fit: ${fit.dot}, ${Math.round(fit.percent)}%`}
            >
              <i className={`am-hubtable-dot-${fit.dot}`} style={{ width: `${fit.percent}%` }} />
            </span>
          </>
        ) : (
          <span className="am-hubtable-dash" title="Not enough is known about this repo to judge">—</span>
        )}
      </td>
      <td className="am-hubtable-name">
        <a
          href={hubModelUrl(display.variantId ?? model.id)}
          target="_blank"
          rel="noopener noreferrer"
          data-hint={`Open ${model.id} on the Hub`}
        >
          {modelName(display.name)}
        </a>
        {curated && <CuratedMark />}
        {/* State, not identity — the same reason `RepoCard` and `HubResultCard`
            before it kept this tag (D424): a half-fetched snapshot is not a
            model an engine can read, and it is what makes Download mean
            "resume" instead of "fetch". */}
        {disk.state === "partial" && (
          <span
            className="am-hubtable-partial"
            data-hint={
              `${model.id} is a download that did not finish. Download picks it up from the ` +
              "bytes already here rather than starting over."
            }
          >
            {PARTIAL_TAG}
          </span>
        )}
        {display.variantId && (
          <span className="am-hubtable-variant-id cc-mono" data-hint={display.variantId}>
            {display.variantId}
          </span>
        )}
      </td>
      <td>{model.task ?? "—"}</td>
      <td className="num am-col-params">{paramsLabel(model.params)}</td>
      <td className="num" data-hint={hubSizeTitle(model, total)}>
        {size ?? "—"}
      </td>
      <td className="num am-col-tok">{speedLabel(model.speedEstimate)}</td>
      <td className="am-col-mode">{runModeLabel(model.fit?.runMode)}</td>
      <td className="num am-col-pop">{popLabel(model.downloads)}</td>
      <td className="num am-col-new">{ageLabel(model.created)}</td>
      <td className="num">
        <span className="am-hubtable-varcount">{variantLabel(family.variants.length)}</span>
      </td>
      <td className="am-hubtable-action">
        {busy ? (
          <CancelButton id={model.id} job={job} onCancel={onCancel} />
        ) : disk.state === "downloaded" ? (
          <span className="am-suggest-have" data-hint={`${model.id} is already on this machine`}>
            ✓ have
          </span>
        ) : gate?.action ? (
          <a
            className="am-card-power am-card-gate-link"
            href={model.url}
            target="_blank"
            rel="noopener noreferrer"
            data-hint={gate.title}
          >
            {gate.action}
          </a>
        ) : (
          <>
            {/* The reason a dead Download is dead, on the row — the same amber
                verb `RecommendedCard`'s card and the card this table replaced
                (`HubResultCard`) both used. */}
            {!loadable && <SwitchEngines runner={runner} />}
            {(disk.state === "absent" || disk.state === "partial") &&
              (!gate || gate.canDownload) && (
                <button
                  type="button"
                  className="am-card-power"
                  disabled={!loadable}
                  data-hint={
                    !loadable
                      ? `${model.id} cannot be loaded here: ${runner?.reason ?? "unavailable"}.`
                      : disk.state === "partial"
                        ? `Resume downloading ${model.id}`
                        : `Download ${model.id}${size ? ` (${size})` : ""}`
                  }
                  aria-label={
                    disk.state === "partial" ? `Resume downloading ${model.id}` : `Download ${model.id}`
                  }
                  onClick={onDownload}
                >
                  <DownloadGlyph />
                </button>
              )}
          </>
        )}
      </td>
    </tr>
  );
}

export function HubResultsTable({
  families,
  cards,
  runners,
  curated,
  jobByModel,
  pulling,
  authenticated,
  onDownload,
  onCancel,
}: {
  families: HubFamily[];
  cards: ReadonlyMap<string, DiskCard> | null;
  runners: ReadonlyMap<string, SectionRunner>;
  curated: ReadonlySet<string>;
  jobByModel: Map<string, Job>;
  pulling: (id: string) => boolean;
  authenticated: boolean;
  onDownload: (id: string, capability: string) => void;
  onCancel: (job: Job) => void;
}) {
  return (
    <div className="am-hubtable-wrap">
      <table className="am-hubtable">
        <thead>
          <tr>
            <th scope="col">Fit</th>
            <th scope="col">Model</th>
            <th scope="col">Task</th>
            <th scope="col" className="num am-col-params">Params</th>
            <th scope="col" className="num">Size</th>
            <th scope="col" className="num am-col-tok">tok/s</th>
            <th scope="col" className="am-col-mode">Mode</th>
            <th scope="col" className="num am-col-pop">Pop.</th>
            <th scope="col" className="num am-col-new">New</th>
            <th scope="col" className="num">Var.</th>
            <th scope="col" />
          </tr>
        </thead>
        <tbody>
          {families.map((family) => {
            const model: HubModel = family.primary;
            return (
              <HubResultRow
                key={family.key}
                family={family}
                curated={curated.has(model.id)}
                runner={runners.get(model.capability) ?? null}
                disk={resultDisk(model.id, cards)}
                authenticated={authenticated}
                busy={pulling(model.id)}
                job={jobByModel.get(model.id)}
                onDownload={() => onDownload(model.id, model.capability)}
                onCancel={onCancel}
              />
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
