// Small modal dialogs for the explorer's file operations, on the shadcn
// Dialog. Two shapes:
//   • PromptDialog — a single prefilled text input for New File / New Folder /
//     Rename. Enter confirms, Escape (or a backdrop click) cancels. The name is
//     validated inline: non-empty and no "/" (a rename can't move across dirs).
//   • ConfirmDialog — a message + Cancel/confirm, for Delete (recursive for a
//     non-empty directory is spelled out in the message the caller passes).
import { useEffect, useRef, useState, type ReactNode } from "react";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { useDeferredClose } from "@platform/lib/hooks";
import { OVERLAY_EXIT_MS } from "@platform/lib/exit-animation";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@platform/shadcn/ui/dialog";

// Validate a single path SEGMENT (a file/folder name, never a path). Returns an
// inline error message or null when the (already-trimmed) name is usable. Beyond
// empty and "/", it rejects "." and ".." (which would resolve to the folder or
// its parent — a directory escape), a backslash (a path separator on the server's
// OS, and confusing everywhere), and any embedded null char. Shared with
// Listing.tsx so its handlers guard before building a path.
export function nameError(trimmed: string): string | null {
  if (trimmed === "") return "Enter a name.";
  if (trimmed === "." || trimmed === "..") return "That name is reserved.";
  // Both separators fold into one message — either way the name can't hop dirs.
  if (trimmed.includes("/") || trimmed.includes("\\")) return "Names can't contain slashes.";
  // A NUL is folded in here rather than getting its own line — to a user it's
  // just an invalid character, not a distinct failure mode.
  if (trimmed.includes("\0")) return "That name contains invalid characters.";
  return null;
}

// Both dialogs are unmounted by their CALLER (`{dialog && <PromptDialog …/>}`),
// so neither can hold itself on screen for an exit animation — it defers the
// callback that makes the caller unmount it (lib/exit-animation). BOTH close
// paths go through here: an exit that plays on Cancel but not on Confirm reads
// as a bug, so the deferrer's single callback dispatches to whichever path asked
// first. `fired` is a ref, not the `closing` state, because two clicks in one
// tick would both see the state as false.
function useDialogClose(onCancel: () => void) {
  const action = useRef(onCancel);
  const fired = useRef(false);
  const { closing, requestClose } = useDeferredClose(() => action.current(), OVERLAY_EXIT_MS);
  const close = (fn: () => void) => {
    if (fired.current) return;
    fired.current = true;
    action.current = fn;
    requestClose();
  };
  return { closing, close };
}

// The shared frame: `open` flips false the moment a close is requested, so the
// Dialog plays its exit inside the deferred window before the caller unmounts
// it. Escape is caught at the window in the CAPTURE phase so it beats the
// listing's document-level key handlers (the primitive's own Escape handling
// does not stop propagation).
function Frame({
  title,
  onCancel,
  closing,
  children,
}: {
  title: string;
  onCancel: () => void;
  closing: boolean;
  children: ReactNode;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onCancel();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onCancel]);

  return (
    <Dialog
      open={!closing}
      onOpenChange={(open) => {
        if (!open) onCancel();
      }}
    >
      <DialogContent
        className="sm:max-w-sm"
        // Every key inside stays inside: the listing's document-level handlers
        // (Enter opens the selected row, Cmd+Backspace trashes it) must not
        // fire behind the dialog. Each dialog's own Enter logic runs first.
        onKeyDown={(e) => e.stopPropagation()}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
        </DialogHeader>
        {children}
      </DialogContent>
    </Dialog>
  );
}

export function PromptDialog({
  title,
  initialValue,
  confirmLabel = "OK",
  // Whether to preselect only the name (sans extension) on focus, Finder-style,
  // so a Rename edits the stem without wiping the extension.
  selectStem = false,
  onConfirm,
  onCancel,
}: {
  title: string;
  initialValue: string;
  confirmLabel?: string;
  selectStem?: boolean;
  onConfirm: (value: string) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(initialValue);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const { closing, close } = useDialogClose(onCancel);
  const cancel = () => close(onCancel);

  // Focus on open, preselecting the stem (name without extension) for a rename
  // and the whole value otherwise. Reads `initialValue`, never the live `value`,
  // so it doesn't reselect on every keystroke. Deferred a frame: the popup
  // mounts through a portal and the primitive moves focus into it on open, so
  // a synchronous focus() here would be overridden.
  useEffect(() => {
    const id = requestAnimationFrame(() => {
      const el = inputRef.current;
      if (!el) return;
      el.focus();
      const dot = initialValue.lastIndexOf(".");
      if (selectStem && dot > 0) el.setSelectionRange(0, dot);
      else el.select();
    });
    return () => cancelAnimationFrame(id);
  }, [initialValue, selectStem]);

  const trimmed = value.trim();
  const error = nameError(trimmed);

  const submit = () => {
    if (error) return;
    close(() => onConfirm(trimmed));
  };

  return (
    <Frame title={title} onCancel={cancel} closing={closing}>
      <div className="space-y-3">
        <Input
          ref={inputRef}
          value={value}
          aria-invalid={!!error && trimmed !== ""}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              // Stop the confirming Enter from reaching the listing's
              // document-level nav handler, which would otherwise treat it as
              // "open the selected row" the instant the dialog closes.
              e.stopPropagation();
              submit();
            }
          }}
        />
        {error && trimmed !== "" && <ErrorBanner>{error}</ErrorBanner>}
      </div>
      <DialogFooter>
        <Button variant="outline" onClick={cancel}>
          Cancel
        </Button>
        <Button disabled={!!error} onClick={submit}>
          {confirmLabel}
        </Button>
      </DialogFooter>
    </Frame>
  );
}

export function ConfirmDialog({
  title,
  message,
  confirmLabel = "OK",
  danger = false,
  onConfirm,
  onCancel,
}: {
  title: string;
  message: ReactNode;
  confirmLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const confirmRef = useRef<HTMLButtonElement | null>(null);
  const { closing, close } = useDialogClose(onCancel);
  const cancel = () => close(onCancel);
  const confirm = () => close(onConfirm);

  // Move focus onto the confirm button on open so it owns Enter/Space —
  // otherwise the primitive's initial focus lands on the first tabbable
  // (Cancel) and Enter would dismiss. Deferred a frame for the same portal
  // reason as PromptDialog's focus.
  useEffect(() => {
    const id = requestAnimationFrame(() => confirmRef.current?.focus());
    return () => cancelAnimationFrame(id);
  }, []);

  return (
    <Frame title={title} onCancel={cancel} closing={closing}>
      <div
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            // Contain the Enter so it can't reach the listing's document-level
            // nav handler (mirrors PromptDialog). When a button is focused its
            // own default activation decides Cancel vs Confirm — calling
            // onConfirm here too would double-fire (or override Cancel).
            e.stopPropagation();
            if (e.target instanceof HTMLButtonElement) return;
            e.preventDefault();
            confirm();
          }
        }}
      >
        <DialogDescription>{message}</DialogDescription>
      </div>
      <DialogFooter>
        <Button variant="outline" onClick={cancel}>
          Cancel
        </Button>
        <Button ref={confirmRef} variant={danger ? "destructive" : "default"} onClick={confirm}>
          {confirmLabel}
        </Button>
      </DialogFooter>
    </Frame>
  );
}
