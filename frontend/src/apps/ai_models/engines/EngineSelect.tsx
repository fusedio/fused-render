// The Engines tab's own listbox, replacing the native <select> a capability
// row used to hold. The reason a native select does not work here is entirely
// about what an <option> can render: it is one line of plain, unstylable,
// unwrappable text, and this control needs two lines per row — a label and a
// muted, WRAPPED description — plus a whole registry sentence on a disabled
// row ("Diffusers (CUDA) — needs an NVIDIA GPU with its driver loaded — there
// is no /dev/nvidiactl or /dev/nvidia0 on this machine (this is linux/x86_64)")
// that a native menu simply clips.
//
// Follows the outside-click/Escape/roving-highlight pattern IconPicker.tsx
// already established for a popover-over-a-control on this page, cut down to a
// one-column list: open, ArrowUp/ArrowDown move an `active` index over ENABLED
// options only, Enter picks, outside click or Escape closes. The trigger is a
// <button> skinned like the app's `.field-control` select (same height/chevron)
// so the row's grid — sized around three 32px `.field-control`s — needs no
// column-width change for the swap.
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import type { EngineChoice } from "@platform/lib/api";
import { choiceReason } from "@apps/ai_models/lib/engines";

// One flattened row the popup can render uniformly, whether it came from the
// synthetic "Automatic" entry, the stranded stored code, or a real choice.
interface Option {
  code: string;
  label: string;
  description: string | null;
  disabled: boolean;
}

function buildOptions(
  auto: string,
  choices: EngineChoice[],
  stranded: string | null,
): Option[] {
  const options: Option[] = [
    {
      code: auto,
      label: "Automatic",
      description: "Picks the best engine this machine can run.",
      disabled: false,
    },
  ];
  // The stored code pinned right after Automatic, disabled — it cannot be
  // re-picked, and it sits above the real choices because it is the CURRENT
  // value rather than an alternative. See lib/engines.ts's `strandedSelection`
  // for why the raw code (never a label) is the only honest thing to show.
  if (stranded !== null) {
    options.push({
      code: stranded,
      label: stranded,
      description: "Not one of this capability's engines.",
      disabled: true,
    });
  }
  for (const choice of choices) {
    // Available: the runner's own `note` ("Transcribes on the GPU") when it
    // has one, else no second line at all. Unavailable: the registry's own
    // reason the option cannot be picked — `choiceReason` never returns null
    // for a disabled choice, so a greyed row always explains itself.
    const description = choice.available ? choice.note : choiceReason(choice);
    options.push({
      code: choice.code,
      label: choice.label,
      description,
      disabled: !choice.available,
    });
  }
  return options;
}

export interface EngineSelectProps {
  id: string;
  auto: string;
  selected: string;
  choices: EngineChoice[];
  /** From `strandedSelection(row, auto)` — the stored code when it matches no
   *  option, else null. */
  stranded: string | null;
  disabled?: boolean;
  onChange: (code: string) => void;
}

export default function EngineSelect({
  id,
  auto,
  selected,
  choices,
  stranded,
  disabled,
  onChange,
}: EngineSelectProps) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const baseId = useId();
  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);

  const options = buildOptions(auto, choices, stranded);
  const enabledIndexes = options
    .map((o, i) => (o.disabled ? -1 : i))
    .filter((i) => i !== -1);

  // The trigger shows ONLY the current choice's own label — never a reason,
  // never a second line. `selected` names either "auto", a real choice, or
  // (when stranded) a code with no option of its own — the raw code is what
  // `buildOptions` gave that entry its label as, so this lookup covers it too.
  const current = options.find((o) => o.code === selected);
  const triggerLabel = current?.label ?? selected;

  const optionId = (i: number) => `${baseId}-opt-${i}`;

  const openPanel = () => {
    if (disabled) return;
    // Opening focuses the SELECTED option when it is one of the enabled ones;
    // otherwise the first enabled option, so the highlight never lands on a
    // row Enter cannot pick.
    const selectedIdx = options.findIndex((o) => o.code === selected && !o.disabled);
    setActive(selectedIdx !== -1 ? selectedIdx : (enabledIndexes[0] ?? 0));
    setOpen(true);
  };

  const closePanel = () => {
    setOpen(false);
  };

  const pick = (code: string) => {
    closePanel();
    triggerRef.current?.focus();
    if (code !== selected) onChange(code);
  };

  // Outside click / Escape, same as IconPicker.
  useEffect(() => {
    if (!open) return;
    const onDocMouseDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (rootRef.current && rootRef.current.contains(target)) return;
      closePanel();
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        closePanel();
        triggerRef.current?.focus();
      }
    };
    document.addEventListener("mousedown", onDocMouseDown);
    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("mousedown", onDocMouseDown);
      document.removeEventListener("keydown", onKeyDown, true);
    };
  }, [open]);

  // Focus the panel so its own keydown handler (below) sees arrow/Enter keys —
  // the trigger button loses focus the moment the popup opens.
  useEffect(() => {
    if (open) panelRef.current?.focus();
  }, [open]);

  // Position the popup under the trigger: min-width = trigger width, and
  // otherwise its natural size up to the ~420px CSS cap.
  useLayoutEffect(() => {
    if (!open) return;
    const trigger = triggerRef.current;
    const panel = panelRef.current;
    if (!trigger || !panel) return;
    const r = trigger.getBoundingClientRect();
    panel.style.minWidth = `${r.width}px`;
    let top = r.bottom + 4;
    if (top + panel.offsetHeight > window.innerHeight - 8) {
      top = Math.max(8, r.top - panel.offsetHeight - 4);
    }
    panel.style.top = `${top}px`;
    panel.style.left = `${Math.min(r.left, window.innerWidth - panel.offsetWidth - 8)}px`;
  }, [open]);

  const moveActive = (delta: number) => {
    if (enabledIndexes.length === 0) return;
    const pos = enabledIndexes.indexOf(active);
    const nextPos = pos === -1
      ? (delta > 0 ? 0 : enabledIndexes.length - 1)
      : Math.max(0, Math.min(enabledIndexes.length - 1, pos + delta));
    const next = enabledIndexes[nextPos];
    setActive(next);
    document.getElementById(optionId(next))?.scrollIntoView({ block: "nearest" });
  };

  const onPanelKeyDown = (e: React.KeyboardEvent) => {
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        moveActive(1);
        break;
      case "ArrowUp":
        e.preventDefault();
        moveActive(-1);
        break;
      case "Enter":
      case " ":
        e.preventDefault();
        if (options[active] && !options[active].disabled) pick(options[active].code);
        break;
      // Escape is handled by the document-level listener above.
    }
  };

  return (
    <div className="am-engine-select-root" ref={rootRef}>
      <button
        ref={triggerRef}
        type="button"
        id={id}
        className="field-control am-engine-select am-engine-trigger"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => (open ? closePanel() : openPanel())}
      >
        <span className="am-engine-trigger-label">{triggerLabel}</span>
      </button>
      {open && (
        <div
          ref={panelRef}
          className="am-engine-popup"
          role="listbox"
          aria-labelledby={id}
          aria-activedescendant={options[active] ? optionId(active) : undefined}
          tabIndex={-1}
          onKeyDown={onPanelKeyDown}
        >
          {options.map((o, i) => {
            const isSelected = o.code === selected;
            return (
              <div
                key={o.code}
                id={optionId(i)}
                role="option"
                aria-selected={isSelected}
                aria-disabled={o.disabled}
                className={
                  "am-engine-option"
                  + (o.disabled ? " disabled" : "")
                  + (i === active ? " active" : "")
                  + (isSelected ? " selected" : "")
                }
                onMouseEnter={() => !o.disabled && setActive(i)}
                onClick={() => !o.disabled && pick(o.code)}
              >
                <span className="am-engine-option-check" aria-hidden="true">
                  {isSelected ? "✓" : ""}
                </span>
                <span className="am-engine-option-text">
                  <span className="am-engine-option-label">{o.label}</span>
                  {o.description && (
                    <span className="am-engine-option-desc">{o.description}</span>
                  )}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
