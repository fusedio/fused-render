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
  quantLabel,
  runModeLabel,
  scoreLabel,
  speedLabel,
  variantLabel,
} from "@apps/ai_models/lib/hubTableView";
import {
  hubSizeLabel,
  hubSizeTitle,
  knownFit,
  knownSpeedEstimate,
  knownTotalSize,
  lookupTotalSize,
} from "@apps/ai_models/lib/hubSize";
import { CancelButton } from "@apps/ai_models/shared/CancelButton";
import { DownloadGlyph } from "@apps/ai_models/shared/ModelProgress";
import { getHubModelSize, type HubModel } from "@platform/lib/api";
import { type Job } from "@platform/lib/jobs";

/** One variant's disclosure row — id, size, disk state, and its own action —
 *  rendered under a family's own row once "N variants" is opened.
 *
 *  **Why this exists at all (E, the review's "one root cause"):** the family
 *  row names and links the base model but downloads and sizes only the
 *  PRIMARY variant. Before this, a sibling on disk (say `Qwen3-8B-4bit` when
 *  the `8bit` build wins the primary pick on a fit tie) had no "✓ have"
 *  anywhere, and a download in flight on a non-primary variant drew no
 *  progress anywhere either — "N variants" was a count with nothing behind
 *  it. Each sibling gets its OWN identity, size and disk state here, with no
 *  lazy total-size lookup (unlike the family row) — that lookup exists for
 *  the row always on screen; a closed disclosure paying for a dozen Hub
 *  round trips nobody asked to see yet would be the same over-eagerness the
 *  viewport gate on the primary's own lookup exists to avoid.
 */
function HubVariantRow({
  model,
  disk,
  runner,
  busy,
  job,
  onDownload,
  onCancel,
}: {
  model: HubModel;
  disk: ResultDisk;
  runner: SectionRunner | null;
  busy: boolean;
  job: Job | undefined;
  onDownload: () => void;
  onCancel: (job: Job) => void;
}) {
  const loadable = !runner || runner.available;
  const arriving = jobFraction(job);
  const size = hubSizeLabel(model, null);
  return (
    <tr
      className={
        "am-hubtable-variant-row" +
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
      <td />
      <td />
      <td className="am-hubtable-name am-hubtable-variant-name" colSpan={3}>
        <span className="am-hubtable-name-inner">
          <a
            href={hubModelUrl(model.id)}
            target="_blank"
            rel="noopener noreferrer"
            data-hint={`Open ${model.id} on the Hub`}
          >
            {model.id}
          </a>
          {disk.state === "partial" && (
            <span className="am-hubtable-partial" data-hint={`${model.id} is a download that did not finish.`}>
              {PARTIAL_TAG}
            </span>
          )}
        </span>
      </td>
      <td className="num am-col-params">{paramsLabel(model.params)}</td>
      <td className="num am-col-quant">{quantLabel(model.quant)}</td>
      <td className="num" data-hint={hubSizeTitle(model, null)}>
        {size ?? "—"}
      </td>
      <td className="num am-col-tok">{speedLabel(model.speedEstimate)}</td>
      <td className="am-col-mode">{runModeLabel(model.fit?.runMode)}</td>
      <td className="num am-col-pop">{popLabel(model.downloads)}</td>
      <td className="num am-col-new">{ageLabel(model.created)}</td>
      <td />
      <td className="am-hubtable-action">
        {busy ? (
          <CancelButton id={model.id} job={job} onCancel={onCancel} />
        ) : disk.state === "downloaded" ? (
          <span className="am-suggest-have" data-hint={`${model.id} is already on this machine`}>
            ✓ have
          </span>
        ) : (
          (disk.state === "absent" || disk.state === "partial") && (
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
              aria-label={disk.state === "partial" ? `Resume downloading ${model.id}` : `Download ${model.id}`}
              onClick={onDownload}
            >
              <DownloadGlyph />
            </button>
          )
        )}
      </td>
    </tr>
  );
}

/** One family's row. Split out of the table so the lazy size lookup's own
 *  `useState`/`useEffect`/`IntersectionObserver` triple — which needs a real
 *  DOM node — is scoped to the one row it is about, the same boundary
 *  `HubResultCard` drew it at.
 *
 *  Resolves disk/runner/job/curated state itself, off `family.primary` for
 *  its own row and off each sibling for its own disclosure row (below) —
 *  rather than the parent precomputing one flat set of props for the primary
 *  alone, which is what left every sibling with no path to its own state at
 *  all.
 */
function HubResultRow({
  family,
  cards,
  runners,
  curated,
  jobByModel,
  pulling,
  authenticated,
  onDownload,
  onCancel,
}: {
  family: HubFamily;
  cards: ReadonlyMap<string, DiskCard> | null;
  runners: ReadonlyMap<string, SectionRunner>;
  curated: ReadonlySet<string>;
  jobByModel: Map<string, Job>;
  pulling: (id: string) => boolean;
  authenticated: boolean;
  onDownload: (id: string, capability: string) => void;
  onCancel: (job: Job) => void;
}) {
  const model = family.primary;
  const disk = resultDisk(model.id, cards);
  const runner = runners.get(model.capability) ?? null;
  const busy = pulling(model.id);
  const job = jobByModel.get(model.id);
  const [expanded, setExpanded] = useState(false);
  const row = useRef<HTMLTableRowElement>(null);
  const wantsTotal = !model.estimatedSize;
  const [total, setTotal] = useState<number | null>(
    (wantsTotal ? knownTotalSize(model.id) : null) ?? null,
  );
  // Bug chain fix: `_model_row` cannot judge fit (or speed) for a row with no
  // safetensors metadata — a GGUF repo, chiefly — during SEARCH, so
  // `model.fit`/`model.speedEstimate` arrive null. The lazy size lookup below
  // ALREADY costs one round trip once this row scrolls into view, and the
  // server rides a verdict on that same answer when it can
  // (`api_hub_size`'s own doc) — these two only ever move off `undefined`
  // for a row that actually asked (`wantsTotal`), and stay unread otherwise
  // (`effectiveFit`/`effectiveSpeed` below fall back to the search's own
  // value first).
  const [fitOverride, setFitOverride] = useState(wantsTotal ? knownFit(model.id) : undefined);
  const [speedOverride, setSpeedOverride] = useState(
    wantsTotal ? knownSpeedEstimate(model.id) : undefined,
  );

  useEffect(() => {
    if (!wantsTotal) return;
    const known = knownTotalSize(model.id);
    if (known !== undefined) {
      setTotal(known);
      setFitOverride(knownFit(model.id));
      setSpeedOverride(knownSpeedEstimate(model.id));
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
      // A capability-bound fetch, so a resolved GGUF `file` gets both the
      // file-specific size AND a fit/speed judgement in the same round trip
      // — `lookupTotalSize`'s own doc explains why the default (unbound)
      // fetch, which the page-level size SORT uses, does not ask for either.
      lookupTotalSize(model.id, model.file, (id, file) =>
        getHubModelSize(id, file, model.capability),
      ).then((bytes) => {
        if (!alive) return;
        setTotal(bytes);
        setFitOverride(knownFit(model.id));
        setSpeedOverride(knownSpeedEstimate(model.id));
        if (knownTotalSize(model.id) !== undefined) io.disconnect();
      });
    });
    io.observe(el);
    return () => {
      alive = false;
      io.disconnect();
    };
  }, [model.id, model.file, model.capability, wantsTotal]);

  // The search's own fit/speed win when they exist (a row WITH safetensors
  // metadata never even sets `wantsTotal`, so these overrides stay
  // `undefined` and never matter); the lazy ride-along only ever fills in
  // for a row that had nothing to begin with.
  const effectiveFit = model.fit ?? fitOverride ?? null;
  const effectiveSpeed = model.speedEstimate ?? speedOverride ?? null;

  const display = familyDisplay(family);
  const fit = fitCell(effectiveFit);
  const size = hubSizeLabel(model, total);
  const gate = disk.state === "downloaded" ? null : gateChrome(model.gated, authenticated);
  const loadable = !runner || runner.available;
  const arriving = jobFraction(job);
  const curatedFlag = curated.has(model.id);

  return (
    <>
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
          <span className="am-hubtable-fit-inner">
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
          </span>
        </td>
        <td className="num am-col-score">{scoreLabel(effectiveFit)}</td>
        <td className="am-hubtable-name">
          <span className="am-hubtable-name-inner">
            {/* The row's identity is the PRIMARY's own id — the same repo the
                href, the download and every other column already act on
                (`familyDisplay`'s doc). Naming the row by the base model instead
                let identity and action disagree. */}
            <a
              href={hubModelUrl(model.id)}
              target="_blank"
              rel="noopener noreferrer"
              data-hint={`Open ${model.id} on the Hub`}
            >
              {modelName(display.name)}
            </a>
            {curatedFlag && <CuratedMark />}
            {/* The gate, named, with the whole of what to do about it on hover —
                disclosed regardless of `authenticated`. `gate?.action` (below, in
                the action column) is null for a signed-in user because the
                Download button already works for them; that must not also erase
                the one thing on the row saying WHY a token-holding user's pull
                can still 403 until the owner grants access (`manual`). This is
                NOT the plain pill D313 deleted: the gate still decides the
                ACTION too, in the action column below. */}
            {gate && (
              <span className="am-card-gate" data-hint={gate.title}>
                {gate.pill}
              </span>
            )}
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
            {/* The GROUPING fact, not the acting repo — muted, secondary, and
                never the bold name (`familyDisplay`'s doc explains why). */}
            {display.baseModel && (
              <span className="am-hubtable-basemodel cc-mono" data-hint={`Grouped under ${display.baseModel}`}>
                from {display.baseModel}
              </span>
            )}
          </span>
        </td>
        <td>{model.task ?? "—"}</td>
        <td className="am-col-cap">{model.capability}</td>
        <td className="num am-col-params">{paramsLabel(model.params)}</td>
        <td className="num am-col-quant">{quantLabel(model.quant)}</td>
        <td className="num" data-hint={hubSizeTitle(model, total)}>
          {size ?? "—"}
        </td>
        <td className="num am-col-tok">{speedLabel(effectiveSpeed)}</td>
        <td className="am-col-mode">{runModeLabel(effectiveFit?.runMode)}</td>
        <td className="num am-col-pop">{popLabel(model.downloads)}</td>
        <td className="num am-col-new">{ageLabel(model.created)}</td>
        <td className="num">
          {/* A real affordance, not a static count: opening it discloses each
              sibling's own id, size and disk state below — the thing "N
              variants" only ever promised (E). No button at all for a family
              of one, same as the dash `variantLabel` already draws there. */}
          {family.variants.length > 0 ? (
            <button
              type="button"
              className="am-hubtable-varcount am-hubtable-vartoggle"
              aria-expanded={expanded}
              data-hint={
                expanded
                  ? "Hide the other repos this row folded in"
                  : `Show the ${family.variants.length} other repo(s) this row folded in`
              }
              onClick={() => setExpanded((v) => !v)}
            >
              {variantLabel(family.variants.length)} {expanded ? "▾" : "▸"}
            </button>
          ) : (
            <span className="am-hubtable-varcount">{variantLabel(0)}</span>
          )}
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
                    onClick={() => onDownload(model.id, model.capability)}
                  >
                    <DownloadGlyph />
                  </button>
                )}
            </>
          )}
        </td>
      </tr>
      {expanded &&
        family.variants.map((variant) => (
          <HubVariantRow
            key={variant.id}
            model={variant}
            disk={resultDisk(variant.id, cards)}
            runner={runners.get(variant.capability) ?? null}
            busy={pulling(variant.id)}
            job={jobByModel.get(variant.id)}
            onDownload={() => onDownload(variant.id, variant.capability)}
            onCancel={onCancel}
          />
        ))}
    </>
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
            <th scope="col" className="num am-col-score">Score</th>
            <th scope="col">Model</th>
            <th scope="col">Task</th>
            <th scope="col" className="am-col-cap">Capability</th>
            <th scope="col" className="num am-col-params">Params</th>
            <th scope="col" className="num am-col-quant">Quant</th>
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
          {families.map((family) => (
            <HubResultRow
              key={family.key}
              family={family}
              cards={cards}
              runners={runners}
              curated={curated}
              jobByModel={jobByModel}
              pulling={pulling}
              authenticated={authenticated}
              onDownload={onDownload}
              onCancel={onCancel}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}
