// The dense results table search shows instead of a card grid. One row per
// model FAMILY (`hubFamilies.groupIntoFamilies`), drawn on the shared shadcn
// `Table` primitives (`@platform/shadcn/ui/table`) so this table's chrome —
// header weight, hover tint, cell rhythm — is the same chrome every other
// table in the app gets, with `am-hubtable-*` classes in `ai-models.css`
// carrying only what is genuinely specific to Hub results.
//
// **Why a table replaces the grid rather than joining it.** A card can carry
// two or three facts before it sprawls, which is why the grid it replaces
// showed popularity and size and nothing about THIS machine. Many columns
// only become legible in a row, and a row is only worth reading once it is
// SCORED — see `hubTableView.ts` for the cell rules and D639/D640/D641 in
// DECISIONS.md for the fuller argument.
//
// **Match, Model and the action are the only unconditional columns.** Every
// data column between them earns its place twice over, on two independent
// tests owned by `hubTableView.ts`:
//
//   * UNANIMITY (`familyHoist`) — every row agrees, so the fact is stated
//     once in the summary line above the table and the column is dropped
//     rather than repeating one value down the page. A merely common (not
//     unanimous) value keeps its column and is only muted
//     (`majorityValue`/`isMajorityValue`).
//   * OCCUPANCY (`occupiedColumns`) — no row has anything to put in it, so
//     the column would be nothing but dashes. An image search does this to
//     TOK/S, which is a text-generation figure by construction.
//
// Size is deliberately exempt from the occupancy test: its cell is filled by
// the lazy lookup below, which happens after this decision is made, so a
// column dropped for emptiness would have nowhere to put the answer it is
// about to receive.
//
// **A family is one `<tbody>`, not one `<tr>`.** The rows a disclosure opens
// are part of the same group as the row that opened it, and a `<tbody>` per
// family is what says so in the markup — which is also what lets the CSS
// draw a border around an OPEN group and nothing at all around the closed
// ones, instead of the fixed every-fifth-row rule it used to count out here.
// Lines a reader cannot explain are worse than no lines.
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
  capabilityHint,
  familyDisplay,
  familyHoist,
  majorityValue,
  isMajorityValue,
  isMatchScoreStale,
  matchCell,
  matchFitBasis,
  matchTitle,
  occupiedColumns,
  paramsLabel,
  popLabel,
  quantLabel,
  resolveFit,
  resolveSpeed,
  speedLabel,
  speedTitle,
  variantLabel,
} from "@apps/ai_models/lib/hubTableView";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@platform/shadcn/ui/table";
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

/** Which data columns this render draws at all — decided ONCE in
 *  `HubResultsTable` and handed down, so the header, every family row and
 *  every disclosed variant row agree on the column COUNT and not merely on
 *  what each cell contains. A row that decided for itself would emit a
 *  different number of `<td>`s than the `<th>`s above it the moment two
 *  rows disagreed, which is the one mistake a table cannot render its way
 *  out of.
 *
 *  See this file's header for the two independent tests a column has to
 *  pass to be here, and why `size` is not one of them. */
interface HubColumns {
  /** Capability — dropped when the whole result set agrees, since the
   *  summary line above the table then says it once. */
  task: boolean;
  params: boolean;
  quant: boolean;
  tokens: boolean;
  pop: boolean;
  fresh: boolean;
}

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
  columns,
  onDownload,
  onCancel,
}: {
  model: HubModel;
  disk: ResultDisk;
  runner: SectionRunner | null;
  busy: boolean;
  job: Job | undefined;
  columns: HubColumns;
  onDownload: () => void;
  onCancel: (job: Job) => void;
}) {
  const loadable = !runner || runner.available;
  const arriving = jobFraction(job);
  const size = hubSizeLabel(model, null);
  return (
    <TableRow
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
      {/* The merged Match cell's own slot — blank for a sibling row, same as
          every other placeholder cell below. */}
      <TableCell className="am-hubtable-match" />
      <TableCell className="am-hubtable-name am-hubtable-variant-name">
        <span className="am-hubtable-name-inner">
          {/* The tick that says this row belongs to the group above it — the
              markup already says so (one `<tbody>` per family), and this is
              the same fact where a reader is actually looking. */}
          <span className="am-hubtable-branch" aria-hidden="true">
            ↳
          </span>
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
      </TableCell>
      {/* A variant row does not re-derive its own majority styling (that is
          a fact about the RESULT SET, not this one sibling) — it just states
          its own value plainly, same as an unhoisted column always would. */}
      {columns.task && <TableCell className="am-col-task">{model.capability}</TableCell>}
      {columns.params && <TableCell className="num am-col-params">{paramsLabel(model.params)}</TableCell>}
      {columns.quant && <TableCell className="num am-col-quant">{quantLabel(model.quant)}</TableCell>}
      <TableCell className="num am-col-size" data-hint={hubSizeTitle(model, null)}>
        {size ?? "—"}
      </TableCell>
      {columns.tokens && (
        <TableCell className="num am-col-tok" data-hint={speedTitle(model.params)}>
          {speedLabel(model.speedEstimate, model.params)}
        </TableCell>
      )}
      {columns.pop && <TableCell className="num am-col-pop">{popLabel(model.downloads)}</TableCell>}
      {columns.fresh && <TableCell className="num am-col-new">{ageLabel(model.created)}</TableCell>}
      <TableCell />
      <TableCell className="am-hubtable-action">
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
      </TableCell>
    </TableRow>
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
  capabilityMajority,
  quantMajority,
  columns,
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
  /** Used only for the MAJORITY-value styling hint (`isMajorityValue`) on a
   *  column that IS rendered — column presence itself is `columns.task`/
   *  `columns.quant`, computed once in `HubResultsTable` from `familyHoist`'s
   *  UNANIMOUS hoist (a separate, stricter fact — see `hubTableView.ts`),
   *  so the header, this row and `HubVariantRow` cannot disagree about how
   *  many columns there are, while this styling hint stays purely cosmetic
   *  and never implies the column's absence. */
  capabilityMajority: ReturnType<typeof majorityValue>;
  quantMajority: ReturnType<typeof majorityValue>;
  columns: HubColumns;
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
    (wantsTotal ? knownTotalSize(model.id, model.file) : null) ?? null,
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
  const [fitOverride, setFitOverride] = useState(
    wantsTotal ? knownFit(model.id, model.file) : undefined,
  );
  const [speedOverride, setSpeedOverride] = useState(
    wantsTotal ? knownSpeedEstimate(model.id, model.file) : undefined,
  );

  useEffect(() => {
    if (!wantsTotal) return;
    const known = knownTotalSize(model.id, model.file);
    if (known !== undefined) {
      setTotal(known);
      setFitOverride(knownFit(model.id, model.file));
      setSpeedOverride(knownSpeedEstimate(model.id, model.file));
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
        setFitOverride(knownFit(model.id, model.file));
        setSpeedOverride(knownSpeedEstimate(model.id, model.file));
        if (knownTotalSize(model.id, model.file) !== undefined) io.disconnect();
      });
    });
    io.observe(el);
    return () => {
      alive = false;
      io.disconnect();
    };
  }, [model.id, model.file, model.capability, wantsTotal]);

  // Code review findings 2/3: precedence (a measured verdict beats a
  // derived one) and staleness (only a REAL correction counts) are pure
  // rules, pulled into `hubTableView.ts` — `resolveFit`/`resolveSpeed`/
  // `matchFitBasis`/`isMatchScoreStale` — so this file's own test suite can
  // drive them directly. See those functions' own docs for the "why".
  const effectiveFit = resolveFit(model.fit, fitOverride, model.file);
  const effectiveSpeed = resolveSpeed(model.speedEstimate, speedOverride, model.file);
  const fitBasis = matchFitBasis(effectiveFit);
  const matchScoreStale = isMatchScoreStale(model.fit, fitOverride);

  const display = familyDisplay(family);
  const match = matchCell(effectiveFit, model.matchScore, matchScoreStale);
  const size = hubSizeLabel(model, total);
  const gate = disk.state === "downloaded" ? null : gateChrome(model.gated, authenticated);
  const loadable = !runner || runner.available;
  const arriving = jobFraction(job);
  const curatedFlag = curated.has(model.id);
  const taskHint = capabilityHint(model);
  const taskIsMajority = isMajorityValue(model.capability, capabilityMajority);
  const quantIsMajority = isMajorityValue(model.quant, quantMajority);

  return (
    <TableBody
      data-expanded={expanded && family.variants.length > 0 ? "" : undefined}
      className="am-hubtable-group"
    >
      <TableRow
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
        {/* The merged Match cell (D639/D640): bar length + number are the
            composite `matchScore`, bar colour AND glyph shape are the memory
            verdict, and a non-GPU run mode (D641) prints as a visible muted
            suffix rather than a second colour. */}
        <TableCell
          className="am-hubtable-match"
          data-hint={matchTitle(effectiveFit, model.matchScore, matchScoreStale, fitBasis)}
        >
          <span className="am-hubtable-match-inner">
            <span
              className={`am-hubtable-dot am-hubtable-dot-${match.dot}`}
              aria-hidden="true"
            >
              {match.dot === "easy" ? "●" : match.dot === "tight" ? "▲" : match.dot === "no" ? "■" : "?"}
            </span>
            <span
              className="am-hubtable-bar"
              role="img"
              aria-label={`Match ${match.scoreText}, memory fit: ${match.dot}${
                match.offloadLabel ? `, ${match.offloadLabel}` : ""
              }`}
            >
              <i className={`am-hubtable-dot-${match.dot}`} style={{ width: `${match.percent}%` }} />
            </span>
            <span className="am-hubtable-score-num">{match.scoreText}</span>
            {match.offloadLabel && <span className="am-hubtable-offload">{match.offloadLabel}</span>}
          </span>
        </TableCell>
        <TableCell className="am-hubtable-name">
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
        </TableCell>
        {/* Task/Capability, merged (D641): the value is `model.capability` —
            what the download path and runner resolution actually key on —
            with the Hub's own `task` label folded into the hint ONLY where
            it genuinely disagrees. The COLUMN itself only exists at all
            when `columns.task` says the result set is not unanimous on it
            (`columnVisible`) — a fully-hoisted column is dropped, header
            and all, rather than left present with every cell blank. When
            it IS shown, every row prints its own real value; a row
            matching the stated majority is only muted (`am-hubtable-
            majority`), never blanked — a blank cell and a real dash
            (unknown) must not look the same. */}
        {columns.task && (
          <TableCell className={"am-col-task" + (taskIsMajority ? " am-hubtable-majority" : "")} data-hint={taskHint}>
            {model.capability}
          </TableCell>
        )}
        {columns.params && <TableCell className="num am-col-params">{paramsLabel(model.params)}</TableCell>}
        {columns.quant && (
          <TableCell className={"num am-col-quant" + (quantIsMajority ? " am-hubtable-majority" : "")}>
            {quantLabel(model.quant)}
          </TableCell>
        )}
        <TableCell className="num am-col-size" data-hint={hubSizeTitle(model, total)}>
          {size ?? "—"}
        </TableCell>
        {columns.tokens && (
          <TableCell className="num am-col-tok" data-hint={speedTitle(model.params)}>
            {speedLabel(effectiveSpeed, model.params)}
          </TableCell>
        )}
        {columns.pop && <TableCell className="num am-col-pop">{popLabel(model.downloads)}</TableCell>}
        {columns.fresh && <TableCell className="num am-col-new">{ageLabel(model.created)}</TableCell>}
        <TableCell className="num">
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
        </TableCell>
        <TableCell className="am-hubtable-action">
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
        </TableCell>
      </TableRow>
      {expanded &&
        family.variants.map((variant) => (
          <HubVariantRow
            key={variant.id}
            model={variant}
            disk={resultDisk(variant.id, cards)}
            runner={runners.get(variant.capability) ?? null}
            busy={pulling(variant.id)}
            job={jobByModel.get(variant.id)}
            columns={columns}
            onDownload={() => onDownload(variant.id, variant.capability)}
            onCancel={onCancel}
          />
        ))}
    </TableBody>
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
  // Hoisting (D640/D641, amended by code review finding 4): presence and the
  // summary line must be computed off ONE value set, not two that can
  // disagree — `familyHoist` (`hubTableView.ts`) owns that computation now
  // (and this file's test suite drives it directly); see its own doc for
  // the contradiction two separate computations used to produce.
  const { capabilityMajority, quantMajority, summary, showTask, showQuant } = familyHoist(families);

  // Occupancy is checked across every row this render will actually draw —
  // a family's primary AND every one of its variants — since a disclosure
  // can surface a value the primaries alone don't carry.
  const models = families.flatMap((f) => [f.primary, ...f.variants]);
  const occupied = occupiedColumns(models);
  // `quant` is the one column both tests can hide: unanimity (`showQuant`)
  // drops it because the result set already agrees and the summary line
  // says so once, while occupancy drops it because nothing in the result
  // set measured a quantization at all — either reason alone is enough to
  // hide the column, so the two tests AND together rather than each owning
  // a disjoint slice of the decision. `size` never enters this object: its
  // cell is filled by the lazy viewport lookup further down, which runs
  // AFTER this decision, so a column dropped here for looking empty would
  // have nowhere to put the answer it is about to receive.
  const columns: HubColumns = {
    task: showTask,
    params: occupied.has("params"),
    quant: showQuant && occupied.has("quant"),
    tokens: occupied.has("tokens"),
    pop: occupied.has("pop"),
    fresh: occupied.has("new"),
  };

  return (
    <div className="am-hubtable-wrap">
      {summary && <p className="cc-caption am-hubtable-summary">{summary}</p>}
      <Table className="am-hubtable">
        <TableHeader>
          <TableRow>
            <TableHead scope="col">Match</TableHead>
            <TableHead scope="col">Model</TableHead>
            {/* Labelled "Capability", not "Task" — the cells beneath it
                render `model.capability` (D641), and a header must not
                name a different field than its own cells do. */}
            {columns.task && <TableHead scope="col" className="am-col-task">Capability</TableHead>}
            {columns.params && <TableHead scope="col" className="num am-col-params">Params</TableHead>}
            {columns.quant && <TableHead scope="col" className="num am-col-quant">Quant</TableHead>}
            <TableHead scope="col" className="num am-col-size">Size</TableHead>
            {columns.tokens && <TableHead scope="col" className="num am-col-tok">tok/s</TableHead>}
            {columns.pop && <TableHead scope="col" className="num am-col-pop">Pop.</TableHead>}
            {columns.fresh && <TableHead scope="col" className="num am-col-new">New</TableHead>}
            <TableHead scope="col" className="num">Var.</TableHead>
            <TableHead scope="col" />
          </TableRow>
        </TableHeader>
        {families.map((family) => (
          <HubResultRow
            key={family.key}
            family={family}
            capabilityMajority={capabilityMajority}
            quantMajority={quantMajority}
            columns={columns}
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
      </Table>
    </div>
  );
}
