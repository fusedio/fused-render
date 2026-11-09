// THE COMPOSER'S ATTACH BUTTON (owner, 2026-11-09: "add a file attach icon
// similar to the bots composer"). A paperclip in the seat immediately left of
// Schedule — the group of things that act on THIS draft (attach, schedule,
// send), apart from the pickers on the left that say how the turn is made.
// It opens the system file picker and hands the picks to the same road a
// paste or a drop takes (`useAttachments.addFiles`), so a picked file becomes
// the same chip in the tray above the box, with the same size and kind rules.
import { Paperclip } from "lucide-react";
import { useRef } from "react";

export const ATTACH_LABEL = "Attach files (or paste / drop them here)";

export function AttachButton({
  onFiles,
  disabled,
  disabledReason,
  className = "c-pill c-attachbtn",
  hidden,
  skipTab,
}: {
  onFiles: (files: File[]) => void;
  disabled?: boolean;
  disabledReason?: string;
  /** The folded card's own seat (`c-attach-idle`, beside Send) instead of
   *  the shelf's pill. */
  className?: string;
  /** Faded out and out of reach (the folded seat while the card is open). */
  hidden?: boolean;
  /** Pointer-only seat: Tab would focus it, which opens the folded card and
   *  hides it under the focus. The shelf's clip is the keyboard's road. */
  skipTab?: boolean;
}) {
  const input = useRef<HTMLInputElement>(null);
  return (
    <>
      <button
        type="button"
        className={hidden ? `${className} is-hidden` : className}
        aria-label="Attach files"
        title={disabled && disabledReason ? disabledReason : ATTACH_LABEL}
        disabled={disabled}
        {...(hidden || skipTab ? { tabIndex: -1 } : {})}
        {...(hidden ? { "aria-hidden": true } : {})}
        // A PRESS TAKES NO FOCUS: in the folded card, focus moving onto this
        // button opens the card (Composer `onFormFocus`), which hides this
        // seat between the press and its release — and the click was lost.
        // The picker is the press's whole answer; the card stays as it was.
        onMouseDown={(e) => e.preventDefault()}
        onClick={() => input.current?.click()}
      >
        <Paperclip size={14} strokeWidth={2} aria-hidden="true" />
      </button>
      <input
        ref={input}
        type="file"
        multiple
        hidden
        tabIndex={-1}
        aria-hidden="true"
        onChange={(e) => {
          const files = Array.from(e.target.files ?? []);
          // Cleared so picking the same file again still fires `change`.
          e.target.value = "";
          if (files.length) onFiles(files);
        }}
      />
    </>
  );
}
