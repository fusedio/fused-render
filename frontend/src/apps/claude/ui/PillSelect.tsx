// One composer pill: OUR OWN menu over a real <select> (T:12498-12610).
//
// The <select> stays the state holder — it owns `.value`, it fires `change`,
// and `fitSelect` keeps working on it untouched. What is replaced is only the
// PLATFORM POPUP, by cancelling the pointerdown that would open it: the OS menu
// is the one surface in this composer the template cannot skin.
//
// KEYBOARD is deliberately still the select's: a focused pill moves through its
// values on the arrows and fires `change` for each, and Space still gets the
// platform popup (themed via `.c-pill option`). This menu is the POINTER path
// only, which is why the cancelled pointerdown does not hand focus over either.
//
// shadcn's Popover carries the portal, the placement (above by default, the
// composer is pinned to the bottom of a pane) and the dismissal contract
// (outside press, Escape). Its trigger is an inert overlay on the pill's own
// box rather than the select, so nothing intercepts the select's keys.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@platform/shadcn/ui/popover";
import { useDismissOnWindow } from "./useDismissOnWindow";

export interface PillOption {
  value: string;
  /** What the MENU shows — always the full spelling (T:12537). */
  label: string;
}

export interface PillSelectProps {
  /** Marks the pill for the fit pass (`c-model-sel` / `c-effort-sel` /
   *  `c-perm-sel`, mirroring T's `.model-sel` &c). */
  kind: string;
  ariaLabel: string;
  /** The optgroup label, and the menu's heading: the list's NAME (T:12520). */
  group: string;
  value: string;
  options: readonly PillOption[];
  /** The pill's own label, which may be the shortened one (T:12228). Defaults
   *  to the menu's. */
  pillLabel?: (option: PillOption) => string;
  onChange(value: string): void;
  disabled?: boolean;
  selectRef?: React.MutableRefObject<HTMLSelectElement | null>;
  /** permPopupGuard's three hooks, on the SELECT and not on a wrapper: a
   *  wrapper element would take a seat in the control row's fit sum. */
  onSelectKeyDown?(ev: React.KeyboardEvent<HTMLSelectElement>): void;
  onSelectMouseDown?(): void;
  onSelectBlur?(): void;
}

export function PillSelect({
  kind,
  ariaLabel,
  group,
  value,
  options,
  pillLabel,
  onChange,
  disabled,
  selectRef,
  onSelectKeyDown,
  onSelectMouseDown,
  onSelectBlur,
}: PillSelectProps) {
  const ownRef = useRef<HTMLSelectElement | null>(null);
  const ref = selectRef ?? ownRef;
  const [open, setOpen] = useState(false);
  const openRef = useRef(false);
  /** Was the menu already up when this press started? The press that closes it
   *  is the popover's own outside-press, so the click must not reopen — T's
   *  "a second press on the same pill closes" (T:12574). */
  const wasOpen = useRef(false);
  const [minWidth, setMinWidth] = useState<number | undefined>();

  useEffect(() => {
    openRef.current = open;
  }, [open]);

  const closeMenu = useCallback(() => setOpen(false), []);

  const onPointerDown = useCallback((ev: React.PointerEvent) => {
    ev.preventDefault(); // kills the native popup AND the focus it would take
    wasOpen.current = openRef.current;
  }, []);

  // The two dismissals Base UI's outside-press cannot reach — above all a click
  // that lands INSIDE THE PREVIEW IFRAME, which never reaches this document at
  // all (T:12602, T:12605). See `useDismissOnWindow`.
  useDismissOnWindow(open, closeMenu);

  const onClick = useCallback(() => {
    if (wasOpen.current) return;
    // Floor the menu to the pill it hangs off; content may push it wider
    // (T:12556).
    const rect = ref.current?.getBoundingClientRect();
    setMinWidth(rect ? Math.round(rect.width) : undefined);
    setOpen(true);
  }, [ref]);

  return (
    <span className="c-pillwrap">
      <select
        ref={ref}
        className={`c-pill ${kind}`}
        aria-label={ariaLabel}
        value={value}
        disabled={disabled}
        onChange={(ev) => onChange(ev.currentTarget.value)}
        onPointerDown={onPointerDown}
        onKeyDown={onSelectKeyDown}
        onBlur={onSelectBlur}
        // Belt and braces: on engines where the popup is a MOUSEDOWN default
        // action and a cancelled pointerdown does not suppress the
        // compatibility event, this is what stops the platform menu (T:12583).
        onMouseDown={(ev) => {
          ev.preventDefault();
          onSelectMouseDown?.();
        }}
        onClick={onClick}
      >
        <optgroup label={group}>
          {options.map((option) => (
            <option
              key={option.value}
              value={option.value}
              data-full={option.label}
            >
              {pillLabel ? pillLabel(option) : option.label}
            </option>
          ))}
        </optgroup>
      </select>
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger
          render={
            <span className="c-pill-anchor" aria-hidden="true" tabIndex={-1} />
          }
        />
        <PopoverContent
          side="top"
          align="start"
          sideOffset={6}
          aria-label={ariaLabel}
          style={{ minWidth }}
          className="c-overlay c-selpop w-auto min-w-0 flex-col gap-0 rounded-[10px] bg-[var(--c-panel)] p-1 text-[var(--c-fg)] shadow-none ring-0"
        >
          <div className="c-selpop-head">{group}</div>
          {options.map((option) => (
            <button
              key={option.value}
              type="button"
              className={`c-selpop-opt${option.value === value ? " is-on" : ""}`}
              onClick={() => {
                setOpen(false);
                // Fired even when the value is unchanged: picking the value
                // detection guessed is how the user PINS it (T:12540).
                onChange(option.value);
              }}
            >
              <span>{option.label}</span>
            </button>
          ))}
        </PopoverContent>
      </Popover>
    </span>
  );
}
