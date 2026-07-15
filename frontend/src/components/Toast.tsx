// The explorer's transient toast (an error, or a non-red "info" confirmation),
// shared by Listing and Preview. Purely presentational — the hosting view owns
// the toast state and its auto-dismiss timer, so this component just renders
// the pinned banner + dismiss button. Styling is .listing-toast* in shell.css.
export type ToastTone = "error" | "info";

export default function Toast({
  msg,
  tone,
  onClose,
}: {
  msg: string;
  tone: ToastTone;
  onClose: () => void;
}) {
  return (
    <div
      className={"listing-toast" + (tone === "info" ? " listing-toast-info" : "")}
      role={tone === "info" ? "status" : "alert"}
    >
      <span className="listing-toast-msg">{msg}</span>
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
