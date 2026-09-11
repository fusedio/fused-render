// The right-hand pane: everything about one capability. Ported from the
// approved mockup's `pane()` / `offPane()` / `partsPane()` functions.
//
// Three variants, chosen by the caller (`LocalTab`) rather than switched on
// internally by a giant prop bag, except for the off/on split within a
// capability, which stays here because it is the one branch the mockup
// itself keeps inside `pane()` (`offReason` decides it): a capability this
// device has no engine for offers nothing — no suggestions, no Hub door, no
// Download — since every one of them would end in a model that cannot run.
// Anything already on disk stays listed so the space can be reclaimed.
import { useState } from "react";

import { capabilityLabel } from "@apps/ai_models/lib/engines";
import { capabilityMeta, PARTS_ICON } from "@apps/ai_models/lib/capabilityMeta";
import { formatSize } from "@platform/lib/format";
import { ModelRow, type ModelRowHandlers, type ModelRowModel, type ModelRowProgress } from "@apps/ai_models/local/ModelRow";

function pluralModel(n: number): string {
  return `model${n > 1 ? "s" : ""}`;
}

export interface CapabilityPaneProps {
  capabilityKey: string;
  /** Models on this Mac, already ordered: resident first, then most
   *  recently used. */
  have: ModelRowModel[];
  /** The id of `have`'s most-recently-used row, for the "Last used" chip. */
  lastUsedId: string | null;
  /** Curated models not yet downloaded, in the catalog's order. */
  recommended: ModelRowModel[];
  /** The id of a `recommended` row a download is in flight for, or null. */
  downloadingId: string | null;
  downloadProgress: ModelRowProgress | null;
  /** The id of the row whose (i) drawer is open, or null. */
  openInfoId: string | null;
  /** Bytes on disk across `have`. */
  totalBytes: number;
  /** The registry's own sentence for why this device has no engine for this
   *  capability, or null when it does. */
  offReason: string | null;
  handlers: ModelRowHandlers;
  /** Item A: opens the Hub search screen, handing back this pane's own
   *  capability key so the caller can seed the Task filter with it — a
   *  reader who came here FOR chat models should not have to re-pick "Chat"
   *  from the Task menu the moment the search screen opens. */
  onOpenSearch: (capabilityKey: string) => void;
}

/** The caller (`LocalTab`) must mount this with `key={capabilityKey}` — the
 *  mockup resets `expanded`/`info`/`menu` on every nav click (see its
 *  `data-nav` handler), and remounting on the key is what gives this
 *  component's local `expanded` state that same reset for free rather than
 *  needing an effect keyed on a prop change. */
export function CapabilityPane({
  capabilityKey,
  have,
  lastUsedId,
  recommended,
  downloadingId,
  downloadProgress,
  openInfoId,
  totalBytes,
  offReason,
  handlers,
  onOpenSearch,
}: CapabilityPaneProps) {
  const [expanded, setExpanded] = useState(false);
  const meta = capabilityMeta(capabilityKey);
  // The pane's own title, lowercased — threaded to every `ModelRow` below so
  // the drawer's generated "Why we suggest it" sentence (item T) can say
  // "Our pick for chat & writing" without re-deriving the label itself.
  // T-nit: must be the SAME string the `.tp-head` title (`meta.plain`) renders
  // above, not `capabilityLabel`'s internal runner-key label ("Text
  // generation") — the two diverge for every known capability (see
  // `capabilityMeta.ts`'s `plain` field vs. `engines.ts`'s `CAPABILITY_LABELS`).
  const paneLabel = meta.plain.toLowerCase();

  const head = (
    <div className="tp-head" data-part="pane.head">
      <span className="capicon" style={{ color: "var(--fg-muted)" }} dangerouslySetInnerHTML={{ __html: meta.icon }} />
      <div>
        <h4>{meta.plain}</h4>
        <p>{meta.blurb}</p>
      </div>
      <div className="act">
        {offReason ? (
          <span className="chip off-chip">Not available on this device</span>
        ) : have.length ? (
          <span className="mono">
            {have.length} on this Mac · {formatSize(totalBytes)}
          </span>
        ) : (
          <span className="chip">Needs a one-time download</span>
        )}
      </div>
    </div>
  );

  if (offReason) {
    return (
      <div className="tp-pane" data-part="pane.off">
        {head}
        <div className="offpane" data-part="pane.off.why">
          <b>This Mac has no engine that can run {capabilityLabel(capabilityKey).toLowerCase()} models</b> — it{" "}
          {offReason}. There is nothing to download or search for here, so the page does not offer it.
          {have.length > 0 && (
            <>
              <br />
              The {have.length} {pluralModel(have.length)} already on disk {have.length > 1 ? "are" : "is"} listed
              below so you can delete {have.length > 1 ? "them" : "it"}.
            </>
          )}
        </div>
        {have.length > 0 && (
          <div className="tp-group" data-part="pane.have" style={{ marginTop: 14 }}>
            <h5>
              On this Mac{" "}
              <span className="note">
                {have.length} {pluralModel(have.length)} · {formatSize(totalBytes)}
              </span>
            </h5>
            {have.map((m) => (
              <ModelRow key={m.id} model={m} paneLabel={paneLabel} opts={{ noEngine: true }} handlers={handlers} />
            ))}
          </div>
        )}
      </div>
    );
  }

  const downloadingRow = downloadingId ? recommended.find((m) => m.id === downloadingId) ?? null : null;
  const rest = downloadingRow ? recommended.filter((m) => m.id !== downloadingId) : recommended;
  const shown = expanded ? rest : rest.slice(0, 2);
  const moreCount = rest.length - 2;

  return (
    <div className="tp-pane" data-part="pane">
      {head}
      {have.length > 0 && (
        <div className="tp-group" data-part="pane.have">
          <h5>
            On this Mac{" "}
            <span className="note">
              {have.length} {pluralModel(have.length)} · {formatSize(totalBytes)}
            </span>
          </h5>
          {have.map((m) => (
            <ModelRow
              key={m.id}
              model={m}
              paneLabel={paneLabel}
              opts={{ last: m.id === lastUsedId, info: openInfoId === m.id }}
              handlers={handlers}
            />
          ))}
        </div>
      )}
      {recommended.length > 0 && (
        <div className="tp-group" data-part="pane.suggest">
          <h5>
            {have.length ? "Also worth having" : "Start with one of these"} <span className="note">chosen by us, for this Mac</span>
          </h5>
          {downloadingRow && (
            <ModelRow model={downloadingRow} paneLabel={paneLabel} opts={{ downloading: true, progress: downloadProgress ?? undefined }} handlers={handlers} />
          )}
          {shown.map((m, i) => (
            <ModelRow
              key={m.id}
              model={m}
              paneLabel={paneLabel}
              opts={{ primary: !have.length && !downloadingRow && i === 0, info: openInfoId === m.id }}
              handlers={handlers}
            />
          ))}
          {moreCount > 0 && !expanded && (
            <div style={{ marginTop: 8 }}>
              <button type="button" className="btn-link" data-expand="1" onClick={() => setExpanded(true)}>
                Show {moreCount} more suggestion{moreCount > 1 ? "s" : ""} ↓
              </button>
            </div>
          )}
        </div>
      )}
      <p className="hubdoor" data-part="pane.door">
        <button type="button" className="btn-link" data-adv="1" onClick={() => onOpenSearch(capabilityKey)}>
          Search Hugging Face for more {meta.searchNoun} →
        </button>
        <span className="why">Thousands of community uploads, ranked by what this Mac can run. Not curated by us.</span>
      </p>
    </div>
  );
}

/* ============================================================
   ENGINE FILES — the one pane that is not a capability. Bytes an
   engine fetched to do its job, belonging to no capability of
   their own. No search: you never go looking for one of these,
   you only ever come here to reclaim the space.
   ============================================================ */
export interface EngineFilePart {
  id: string;
  name: string;
  /** "part of FLUX.2" */
  partOf: string;
  sizeLabel: string;
  usedLabel: string | null;
}

export interface EngineFilesPaneProps {
  parts: EngineFilePart[];
  totalBytes: number;
  onDelete: (id: string) => void;
}

export function EngineFilesPane({ parts, totalBytes, onDelete }: EngineFilesPaneProps) {
  return (
    <div className="tp-pane" data-part="pane">
      <div className="tp-head" data-part="pane.head">
        <span className="capicon" style={{ color: "var(--fg-muted)" }} dangerouslySetInnerHTML={{ __html: PARTS_ICON }} />
        <div>
          <h4>Engine files</h4>
          <p>Downloaded automatically so a model could run. Safe to delete — they come back the next time they are needed.</p>
        </div>
        <div className="act">
          <span className="mono">{formatSize(totalBytes)}</span>
        </div>
      </div>
      <div className="tp-group">
        {parts.map((p) => (
          <div key={p.id} className="row have" data-part="row">
            <div>
              <div className="row-name">
                <b>{p.name}</b>
                <span className="chip">part of {p.partOf}</span>
              </div>
              <p className="row-note mono">{p.id}</p>
            </div>
            <span className="row-facts">
              {[p.sizeLabel, p.usedLabel ? `used ${p.usedLabel}` : null].filter(Boolean).join(" · ")}
            </span>
            <span className="row-act">
              <button type="button" className="iconbtn" title="Delete" onClick={() => onDelete(p.id)}>
                🗑
              </button>
            </span>
          </div>
        ))}
      </div>
      <p className="scoped">
        There is no search here on purpose: you never go looking for one of these, you only ever come here to
        reclaim the space.
      </p>
    </div>
  );
}
