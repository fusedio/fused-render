// The explorer's transient toast (an error, or a non-red "info" confirmation).
// Purely presentational — the hosting view (or the global ToastHost) owns the
// toast state and its auto-dismiss timer, so this component just renders the
// pinned banner + optional action + dismiss button. Styling is .listing-toast*
// in shell.css.
export type ToastTone = "error" | "info";

// An optional call-to-action rendered before the dismiss button (e.g. the
// mount-health "Reconnect" affordance). The host owns what onClick does.
export interface ToastAction {
  label: string;
  onClick: () => void;
}

export default function Toast({
  msg,
  tone,
  action,
  onClose,
}: {
  msg: string;
  tone: ToastTone;
  action?: ToastAction;
  onClose: () => void;
}) {
  return (
    <div
      className={"listing-toast" + (tone === "info" ? " listing-toast-info" : "")}
      role={tone === "info" ? "status" : "alert"}
    >
      <span className="listing-toast-msg">{msg}</span>
      {action && (
        <button
          type="button"
          className="listing-toast-action"
          onClick={action.onClick}
        >
          {action.label}
        </button>
      )}
      <button
        type="button"
        className="listing-toast-close"
        onClick={onClose}
        aria-label="Dismiss"
      >
        ✕
      </button>
    </div>
  );
}
