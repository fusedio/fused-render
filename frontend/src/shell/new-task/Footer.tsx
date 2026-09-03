// The card's footer: Delete (Edit only, far left, two-press), Back to chat
// (only when a chat sent us here), and the primary button wearing the hotkey.
import { Trash2Icon } from "lucide-react";
import { Button } from "@platform/shadcn/ui/button";
import { Kbd, KbdGroup } from "@platform/shadcn/ui/kbd";
import { ENTER_LABEL, MOD_LABEL } from "@platform/lib/platform";
import type { DeleteAction } from "./form-logic";

export function Footer({
  del,
  delArmed,
  onDelete,
  chatBack,
  backArmed,
  onBack,
  busy,
  ready,
  actionLabel,
  onSubmit,
}: {
  del: DeleteAction | null;
  delArmed: boolean;
  onDelete: () => void;
  chatBack?: string | null;
  backArmed: boolean;
  onBack: () => void;
  busy: boolean;
  ready: boolean;
  actionLabel: "Schedule" | "Create";
  onSubmit: () => void;
}) {
  return (
    <>
      {/* Destructive, so it sits at the far left, away from Save. Present
          only on an Edit, and only when the entry is actually withdrawable.
          The second press names the CONSEQUENCE rather than asking. */}
      {del && (
        <Button
          type="button"
          variant={delArmed ? "destructive" : "ghost"}
          size="sm"
          className={"mr-auto" + (delArmed ? "" : " text-destructive hover:text-destructive")}
          title={del.title}
          disabled={busy}
          onClick={onDelete}
        >
          <Trash2Icon data-icon="inline-start" />
          {delArmed ? del.confirm : del.label}
        </Button>
      )}
      {/* The way back completes the chat's round trip: chat → schedule →
          adjust the draft → schedule again. */}
      {chatBack && (
        <Button type="button" variant="outline" size="sm" disabled={busy} onClick={onBack}>
          {backArmed ? "Discard changes?" : "Back to chat"}
        </Button>
      )}
      {/* NOT disabled on `!ready`: a dead button is not a hint. A press on a
          form that cannot be saved SAYS which field is missing and puts the
          caret in it (trySubmit / saveBlockedReason). `aria-disabled` keeps the
          state announced while the button stays pressable; only `busy` truly
          disables it — a second press mid-save would schedule twice. */}
      <Button
        type="button"
        size="sm"
        // `schedule-save` is a DOM hook for the tasks tour, not a style.
        className="schedule-save"
        disabled={busy}
        aria-disabled={!ready}
        onClick={onSubmit}
      >
        {busy ? `${actionLabel === "Create" ? "Creating" : "Scheduling"}…` : actionLabel}
        {/* THE HOTKEY, ON THE BUTTON (Akshil, 2026-08-27). Hidden while busy: a
            live-looking hotkey on a dead button is a lie. */}
        {!busy && (
          <KbdGroup aria-hidden className="ml-1">
            <Kbd className="bg-primary-foreground/15 text-primary-foreground">{MOD_LABEL}</Kbd>
            <Kbd className="bg-primary-foreground/15 text-primary-foreground">{ENTER_LABEL}</Kbd>
          </KbdGroup>
        )}
      </Button>
    </>
  );
}
