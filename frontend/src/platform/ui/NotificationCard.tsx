// The one row shape every status-bar panel draws. Six call sites — Models,
// Engines, Jobs, repo updates, waiting tasks, LAN pairings — all render
// through this single `.dl-row` anatomy (a title line with trailing figures
// and actions, an optional secondary line, an optional figures line, an
// optional progress bar, an optional status line, an optional dismiss),
// reusing the existing `.dl-*` classes from
// `frontend/src/styles/notifications.css` — no parallel class family. Every
// caller supplies only the parts its row has; everything here is optional
// except the title.
//
// TWO ACTION FAMILIES, KEPT SEPARATE (see notifications.css's own comments
// on `.dl-row-cancel` and `.q-all`): `liveAction` is a verb that acts on
// something live right now (Unload/Stop/Cancel) and wears the prominent
// `.dl-row-cancel` treatment; `navAction`/`extraAction` navigate
// (Update/Switch/Fix with Claude) and wear the quieter `.q-all` outline.
// Merging them into one button style would erase that distinction on
// purpose — do not.
import type { KeyboardEvent, ReactNode } from "react";

export type TerminalState = "done" | "error" | "cancelled";

/** The tick/cross that rides inline with a terminal row's status text — 11×11,
 *  the exact paths and stroke the mockups draw. Colour comes from the status
 *  tokens (`--success`/`--error`), never a literal hex, so `tests/test_theme.py`
 *  stays green. */
function TerminalGlyph({ state }: { state: TerminalState }) {
  if (state === "done") {
    return (
      <svg width="11" height="11" viewBox="0 0 12 12" aria-hidden="true">
        <path
          d="M2.2 6.3 4.7 8.8 9.8 3.4"
          fill="none"
          stroke="var(--success)"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    );
  }
  return (
    <svg width="11" height="11" viewBox="0 0 12 12" aria-hidden="true">
      <path
        d="M3.1 3.1 8.9 8.9M8.9 3.1 3.1 8.9"
        fill="none"
        stroke="var(--error)"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
    </svg>
  );
}

export interface NotificationCardAction {
  label: ReactNode;
  onClick: () => void;
  disabled?: boolean;
  title?: string;
  ariaLabel?: string;
}

export interface NotificationCardDismiss {
  onClick: () => void;
  disabled?: boolean;
  title?: string;
  ariaLabel?: string;
}

/** The whole row acting as one click target (the waiting-task row: "open the
 *  conversation" is the row's only job). A `<div>` with `role="button"`
 *  rather than a real `<button>`, because a row with `onDismiss` also carries
 *  a real `<button>` for the ✕, and a button cannot nest inside a button. */
export interface NotificationCardRowClick {
  onClick: () => void;
  title?: string;
  ariaLabel?: string;
}

export interface NotificationCardProps {
  /** `.dl-title`. The only required part. */
  title: ReactNode;
  /** `'wrap'` (default): two-line clamp, wraps anywhere — a prompt. `'id'`
   *  (`.dl-title-id`): one line, ellipsis, never wraps mid-token — a model
   *  or engine id. */
  titleMode?: "wrap" | "id";
  titleTooltip?: string;
  /** `.dl-pct`/`.dl-amount` on the head line — a percentage, a size, a count. */
  trailing?: ReactNode;
  /** `.dl-model` — its own line under the head. */
  secondary?: ReactNode;
  secondaryTooltip?: string;
  /** `.dl-row-figures` — the Models row's memory cells. */
  figures?: ReactNode;
  /** `.dl-bar`/`.dl-bar-fill`. `number` (0–1) is the fill; `null` draws the
   *  indeterminate sweep; `undefined` draws no bar at all. */
  progress?: number | null;
  /** Colours the bar's warning tone and dims the whole row (existing
   *  `.dl-row.is-stalled` behaviour) — a reporter that has gone quiet. */
  stalled?: boolean;
  /** The inline glyph on the status line for a row that has finished. */
  terminal?: TerminalState;
  /** `.dl-status`. */
  status?: ReactNode;
  /** `.dl-status-one` — one line, ellipsis, for a status line that is really
   *  a title (the waiting-task row) rather than a wrapping sentence. */
  statusOneLine?: boolean;
  statusTooltip?: string;
  /** `.dl-row-cancel` — Unload / Stop / Cancel. */
  liveAction?: NotificationCardAction;
  /** `.q-all` on the head line — Update / Switch. */
  navAction?: NotificationCardAction;
  /** A second `.q-all`, below the status line — Fix with Claude. */
  extraAction?: NotificationCardAction;
  /** Anything a row needs UNDER its status line that is not a sentence and
   *  not a button: today the jobs card's compact `TroubleCard`, for a
   *  self-fix session that could not start. It is neither `status` (which is
   *  one line of text, and the row still wants its ordinary one) nor
   *  `extraAction` (which is a label and an onClick), so it takes the one
   *  thing those two cannot express — a node the caller renders itself. */
  footer?: ReactNode;
  /** `.dl-x` (`✕`). Notifications rows only. */
  onDismiss?: NotificationCardDismiss;
  /** The whole row as one click target — see `NotificationCardRowClick`. */
  rowClick?: NotificationCardRowClick;
}

export default function NotificationCard({
  title,
  titleMode = "wrap",
  titleTooltip,
  trailing,
  secondary,
  secondaryTooltip,
  figures,
  progress,
  stalled = false,
  terminal,
  status,
  statusOneLine = false,
  statusTooltip,
  liveAction,
  navAction,
  extraAction,
  footer,
  onDismiss,
  rowClick,
}: NotificationCardProps) {
  const rowClassName = [
    "dl-row",
    stalled ? "is-stalled" : "",
    rowClick ? "dl-row-open" : "",
  ]
    .filter(Boolean)
    .join(" ");

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!rowClick) return;
    // A waiting-task row also mounts a real `<button>` for its ✕ — Enter/Space
    // fired on that focused button bubbles up here too. Without this guard,
    // Enter would fire the row's onClick alongside the button's own, and
    // Space's `preventDefault` below would eat the button's native
    // activation outright, so the ✕ would never fire at all. Ignoring any
    // keydown whose `target` isn't the row itself leaves the nested button
    // to handle its own Enter/Space exactly as a `<button>` always does.
    if (e.target !== e.currentTarget) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      rowClick.onClick();
    }
  };

  const rowClickProps = rowClick
    ? {
        role: "button" as const,
        tabIndex: 0,
        onClick: rowClick.onClick,
        onKeyDown,
        title: rowClick.title,
        "aria-label": rowClick.ariaLabel,
      }
    : {};

  const statusClassName = status != null
    ? ["dl-status", statusOneLine ? "dl-status-one" : "", terminal ? "with-glyph" : ""]
        .filter(Boolean)
        .join(" ")
    : undefined;

  return (
    <div className={rowClassName} {...rowClickProps}>
      <div className="dl-row-head">
        <span
          className={"dl-title" + (titleMode === "id" ? " dl-title-id" : "")}
          title={titleTooltip}
        >
          {title}
        </span>
        {trailing}
        {liveAction && (
          <button
            type="button"
            className="dl-row-cancel"
            onClick={(e) => {
              // A row click on `rowClick` would fire underneath a nested
              // control otherwise — no caller today combines `rowClick` with
              // an action button, but this keeps the two composable.
              e?.stopPropagation?.();
              liveAction.onClick();
            }}
            disabled={liveAction.disabled}
            title={liveAction.title}
            aria-label={liveAction.ariaLabel}
          >
            {liveAction.label}
          </button>
        )}
        {navAction && (
          <button
            type="button"
            className="q-all"
            onClick={(e) => {
              e?.stopPropagation?.();
              navAction.onClick();
            }}
            disabled={navAction.disabled}
            title={navAction.title}
            aria-label={navAction.ariaLabel}
          >
            {navAction.label}
          </button>
        )}
        {onDismiss && (
          <button
            type="button"
            className="dl-x"
            onClick={(e) => {
              e?.stopPropagation?.();
              onDismiss.onClick();
            }}
            disabled={onDismiss.disabled}
            title={onDismiss.title ?? "Dismiss"}
            aria-label={onDismiss.ariaLabel ?? "Dismiss"}
          >
            ✕
          </button>
        )}
      </div>
      {secondary != null && (
        <div className="dl-model" title={secondaryTooltip}>
          {secondary}
        </div>
      )}
      {figures != null && <div className="dl-row-figures">{figures}</div>}
      {progress !== undefined && (
        <div className={"dl-bar" + (stalled ? " is-stalled" : "")}>
          <div
            className={"dl-bar-fill" + (progress === null ? " is-indeterminate" : "")}
            data-indeterminate={progress === null ? "1" : undefined}
            style={progress === null ? undefined : { width: `${progress * 100}%` }}
          />
        </div>
      )}
      {status != null && (
        <div className={statusClassName} title={statusTooltip}>
          {terminal ? (
            <>
              <TerminalGlyph state={terminal} />
              <span className="dl-status-text">{status}</span>
            </>
          ) : (
            status
          )}
        </div>
      )}
      {extraAction && (
        <button
          type="button"
          className="q-all"
          onClick={(e) => {
            e?.stopPropagation?.();
            extraAction.onClick();
          }}
          disabled={extraAction.disabled}
          title={extraAction.title}
        >
          {extraAction.label}
        </button>
      )}
      {footer}
    </div>
  );
}
