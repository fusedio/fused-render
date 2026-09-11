// The approvals pill — the row's one long label, and so the only one with a
// short form (T:11844-11876). Every mode is offered, always: the disabling
// that used to live here belonged to a DEFERRED send and went with the pill
// (T:12172-12180).
//
// `permPopupGuard` (T:12309-12326) is the one reader the compact rewrite cannot
// fool: pointer opens the custom menu, which spells everything out, but
// KEYBOARD (Space, Enter, the arrows) opens the select's own list, and that
// list paints the options' live textContent — so a compact row offered
// `ask / edits / decides` as the actual choices (bugbot #685). The full
// spellings are restored on the keydown that is about to open the popup —
// synchronously, before the default action, which is why this is a DOM write
// and not a state flip — and the short forms come back on change/blur, the two
// ways a popup ends. `fitSelect` is deliberately NOT re-run: the pill keeps its
// fitted width, so nothing in the row moves.
import { useCallback, useEffect, useRef } from "react";
import type { PermissionMode } from "../protocol/types";
import {
  GROUP_LABELS,
  PERMISSION_LABELS,
  PERMISSION_MODES,
  PERMISSION_SHORT,
  PILL_ARIA,
} from "./composer-defaults";
import { PillSelect, type PillOption } from "./PillSelect";

const OPTIONS: PillOption[] = PERMISSION_MODES.map((value) => ({
  value,
  label: PERMISSION_LABELS[value],
}));

export interface PermissionSelectProps {
  value: PermissionMode;
  onChange(value: PermissionMode): void;
  /** The control row ran out of width (`fitComposerRow` stage 2). */
  compact?: boolean;
  disabled?: boolean;
}

export function PermissionSelect({
  value,
  onChange,
  compact,
  disabled,
}: PermissionSelectProps) {
  const ref = useRef<HTMLSelectElement | null>(null);

  const write = useCallback((short: boolean) => {
    const select = ref.current;
    if (!select) return;
    for (const option of Array.from(select.options)) {
      const full = option.dataset.full || option.textContent || "";
      option.textContent = short
        ? PERMISSION_SHORT[option.value as PermissionMode] || full
        : full;
    }
  }, []);

  // React owns the resting label; this only has to undo a guard write that a
  // popup left behind when the verdict itself changed.
  useEffect(() => {
    write(!!compact);
  }, [compact, write]);

  const guardKeys = useCallback(
    (ev: React.KeyboardEvent<HTMLSelectElement>) => {
      if (
        ev.key === " " ||
        ev.key === "Enter" ||
        ev.key === "ArrowDown" ||
        ev.key === "ArrowUp"
      ) {
        write(false);
      }
    },
    [write],
  );
  const short = useCallback(() => {
    if (compact) write(true);
  }, [compact, write]);

  return (
    <PillSelect
      kind="c-perm-sel"
      ariaLabel={PILL_ARIA.permission}
      group={GROUP_LABELS.permission}
      value={value}
      options={OPTIONS}
      selectRef={ref}
      onSelectKeyDown={guardKeys}
      onSelectMouseDown={() => write(false)}
      onSelectBlur={short}
      pillLabel={(option) =>
        compact
          ? PERMISSION_SHORT[option.value as PermissionMode] || option.label
          : option.label
      }
      onChange={(next) => {
        short();
        onChange(next as PermissionMode);
      }}
      disabled={disabled}
    />
  );
}
