// The shared model row — one skeleton for both species a capability pane
// shows: a model already on this Mac, and one the catalog only recommends.
// Ported verbatim from the approved mockup's `row()`/`drawer()` functions.
//
// The only thing that distinguishes the two species on the face is the tick:
// a model we suggested (curated) carries it, one the user fetched themselves
// does not. Everything else — chips, facts, actions, the disclosure drawer —
// is driven by the caller's already-merged view model (`ModelRowModel`),
// which is deliberately NOT `AiModelRepo` or `AiCatalogModel`: the mockup's
// `row(m)` took one flat object for a disk row and a recommendation alike,
// and reproducing that here means the caller (`CapabilityPane`) does the one
// merge, instead of this file re-deriving "what counts as this row's size"
// twice.
import type { ReactNode } from "react";

import { hubModelUrl } from "@apps/ai_models/local/hub";

export interface ModelRowModel {
  id: string;
  /** Nickname, or the model half of the repo id — never the bare repo id. */
  name: string;
  /** True for a row the catalog suggested (carries the tick and a note). */
  curated: boolean;
  /** "Our pick" chip. */
  ourPick: boolean;
  /** A warning chip's text (e.g. "needs 32GB"), or null for none. */
  warnChip: string | null;
  have: boolean;
  /** Formatted size, or "size not checked yet" when nothing is known. */
  sizeLabel: string;
  /** "17d ago", or null when never used / not on this Mac. */
  usedLabel: string | null;
  /** "MLX", or null when not recorded. */
  engine: string | null;
  params: string | null;
  quant: string | null;
  /** Always "safetensors" today — a field rather than a literal because a
   *  second format is a matter of when, not if. */
  format: string;
  /** File count on disk, for the drawer's "On disk" line. Null when `!have`. */
  fileCount: number | null;
  /** Cache path, for the drawer's "Path" line. Null when `!have`. */
  path: string | null;
  /** Why we suggest it — curated rows only. */
  note: string | null;
}

export interface ModelRowProgress {
  doneLabel: string;
  totalLabel: string;
  etaLabel: string;
  /** 0…1 */
  fraction: number;
}

export interface ModelRowOptions {
  /** This is the most-recently-used model on this Mac for the pane's
   *  capability — carries the "Last used" chip. */
  last?: boolean;
  /** A download job is in flight for this row right now. */
  downloading?: boolean;
  progress?: ModelRowProgress;
  /** No engine on this device can run it — Try is disabled and there is no
   *  Download (a no-engine row is never a recommendation). */
  noEngine?: boolean;
  /** Give the Download button the primary treatment (the first suggestion
   *  in a group). */
  primary?: boolean;
  /** The disclosure drawer is open. */
  info?: boolean;
}

export interface ModelRowHandlers {
  onTry?: (id: string) => void;
  onDownload?: (id: string) => void;
  onStop?: (id: string) => void;
  onDelete?: (id: string) => void;
  onToggleInfo?: (id: string) => void;
  onShowInFinder?: (id: string) => void;
}

function Unknown() {
  return <span className="unknown">not recorded</span>;
}

function fact(value: string | null): ReactNode {
  return value ? value : <Unknown />;
}

function Drawer({ model, handlers }: { model: ModelRowModel; handlers: ModelRowHandlers }) {
  return (
    <div className="drawer" data-part="row.drawer">
      <dl>
        <dt>Repository</dt>
        <dd>{model.id}</dd>
        <dt>Engine</dt>
        <dd className="plain">{fact(model.engine)}</dd>
        <dt>Parameters</dt>
        <dd className="plain">{fact(model.params)}</dd>
        <dt>Quantization</dt>
        <dd className="plain">{fact(model.quant)}</dd>
        <dt>Format</dt>
        <dd className="plain">{model.format}</dd>
        <dt>On disk</dt>
        <dd className="plain">
          {model.sizeLabel}
          {model.have ? ` · ${model.fileCount ?? 0} files` : " (not downloaded)"}
        </dd>
        {model.have && (
          <>
            <dt>Last used</dt>
            <dd className="plain">{model.usedLabel || "never"}</dd>
            <dt>Path</dt>
            <dd>{model.path || <Unknown />}</dd>
          </>
        )}
      </dl>
      {model.curated ? (
        <p className="why">
          <b>Why we suggest it.</b> {model.note}
        </p>
      ) : (
        <p className="why">
          Not one of our suggestions — you downloaded this one, so everything above is read off the
          cache directory.
        </p>
      )}
      <div className="acts">
        <a className="btn-link" href={hubModelUrl(model.id)} target="_blank" rel="noreferrer">
          View on Hugging Face ↗
        </a>
        {model.have && (
          <button type="button" className="btn-link" onClick={() => handlers.onShowInFinder?.(model.id)}>
            Show in Finder
          </button>
        )}
        <button type="button" className="btn-link" onClick={() => handlers.onToggleInfo?.(model.id)}>
          Close
        </button>
      </div>
    </div>
  );
}

function Actions({
  model,
  opts,
  handlers,
}: {
  model: ModelRowModel;
  opts: ModelRowOptions;
  handlers: ModelRowHandlers;
}) {
  if (opts.downloading) {
    return (
      <button type="button" className="btn" onClick={() => handlers.onStop?.(model.id)}>
        Stop
      </button>
    );
  }
  if (model.have && opts.noEngine) {
    return (
      <>
        <button type="button" className="btn" disabled title="No engine on this device can run it">
          Try
        </button>
        <button
          type="button"
          className="iconbtn"
          title="Details"
          data-info={model.id}
          onClick={() => handlers.onToggleInfo?.(model.id)}
        >
          ⓘ
        </button>
        <button type="button" className="iconbtn" title="Delete" onClick={() => handlers.onDelete?.(model.id)}>
          🗑
        </button>
      </>
    );
  }
  if (model.have) {
    return (
      <>
        <button type="button" className="btn btn-primary" onClick={() => handlers.onTry?.(model.id)}>
          Try
        </button>
        <button
          type="button"
          className="iconbtn"
          title="Details"
          data-info={model.id}
          onClick={() => handlers.onToggleInfo?.(model.id)}
        >
          ⓘ
        </button>
        <button type="button" className="iconbtn" title="Delete" onClick={() => handlers.onDelete?.(model.id)}>
          🗑
        </button>
      </>
    );
  }
  return (
    <>
      <button
        type="button"
        className={`btn${opts.primary ? " btn-primary" : ""}`}
        onClick={() => handlers.onDownload?.(model.id)}
      >
        Download
      </button>
      <button
        type="button"
        className="iconbtn"
        title="Details"
        data-info={model.id}
        onClick={() => handlers.onToggleInfo?.(model.id)}
      >
        ⓘ
      </button>
    </>
  );
}

export function ModelRow({
  model,
  opts = {},
  handlers = {},
}: {
  model: ModelRowModel;
  opts?: ModelRowOptions;
  handlers?: ModelRowHandlers;
}) {
  const chips: ReactNode[] = [];
  if (opts.last) chips.push(
    <span key="last" className="chip last-chip">
      Last used
    </span>,
  );
  if (model.ourPick) chips.push(
    <span key="rec" className="chip rec-chip">
      Our pick
    </span>,
  );
  if (model.warnChip) chips.push(
    <span key="warn" className="chip warn-chip">
      {model.warnChip}
    </span>,
  );

  return (
    <div className={`rowwrap${opts.info ? " open" : ""}`}>
      <div className={`row ${model.have ? "have" : ""}`} data-part="row">
        <div>
          <div className="row-name">
            <b>{model.name}</b>
            {model.curated && (
              <span className="tick" title="One of our suggestions">
                ✔
              </span>
            )}
            {chips}
          </div>
          <p className="row-note mono">{model.id}</p>
          {opts.downloading && opts.progress && (
            <>
              <div className="bar-prog">
                <i style={{ width: `${Math.round(opts.progress.fraction * 100)}%` }} />
              </div>
              <p className="row-note mono" style={{ marginTop: 5 }}>
                {opts.progress.doneLabel} of {opts.progress.totalLabel} · about {opts.progress.etaLabel} left
              </p>
            </>
          )}
        </div>
        <span className="row-facts">
          {opts.downloading ? "downloading" : [model.sizeLabel, model.usedLabel ? `used ${model.usedLabel}` : null].filter(Boolean).join(" · ")}
        </span>
        <span className="row-act">
          <Actions model={model} opts={opts} handlers={handlers} />
        </span>
      </div>
      {opts.info && <Drawer model={model} handlers={handlers} />}
    </div>
  );
}
