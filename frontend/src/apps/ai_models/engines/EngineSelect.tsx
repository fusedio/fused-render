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
// already established for a popover-over-a-control on this page, and the
// scroll/resize/blur dismissal ContextMenu.tsx adds beside it — a popup
// pinned `position: fixed` against a one-shot trigger rect detaches from that
// rect the moment the page moves under it, so it has to close (or move) on
// anything that could move the trigger. Cut down to a one-column list: open,
// ArrowUp/ArrowDown move an `active` index over ENABLED options only, Enter
// picks, outside click, Escape, Tab/focusout, scroll, resize or window blur
// all close it. The trigger is a <button> skinned like the app's
// `.field-control` select (same height/chevron) so the row's grid — sized
// around three 32px `.field-control`s — needs no column-width change for the
// swap.
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import type { EngineChoice } from "@platform/lib/api";
import { choiceReason } from "@apps/ai_models/lib/engines";
import { capitalise } from "@apps/ai_models/lib/aiModelGroups";

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
  strandedLabel: string | null,
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
  // for why the CODE is what identifies this option; `strandedLabel` — the
  // registry's own name for it, null when there is none to give (a withdrawn
  // engine) — is what LABELS it, so a wrong-capability code reads as "MLX
  // Whisper" rather than "mlx-whisper" here too, the same name the warning
  // below already uses.
  if (stranded !== null) {
    options.push({
      code: stranded,
      label: strandedLabel ?? stranded,
      description: "Not one of this capability's engines.",
      disabled: true,
    });
  }
  for (const choice of choices) {
    // Available: the runner's own `note` ("Transcribes on the GPU") when it
    // has one, else no second line at all. Unavailable: the registry's own
    // reason the option cannot be picked — `choiceReason` never returns null
    // for a disabled choice, so a greyed row always explains itself.
    //
    // `choiceReason` stays LOWERCASE in `registry.py`: the same sentence is
    // spliced mid-clause elsewhere (`ignoredWarning`'s "X is not used here —
    // {reason}"), so the registry cannot capitalise it for us. Here it stands
    // alone as an option's own description line, next to notes and the
    // stranded-row copy that are already full sentences — so only the reason
    // needs the fix, and only at render time.
    const reason = choiceReason(choice);
    const description = choice.available ? choice.note : (reason && capitalise(reason));
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
  /** Id of the VISIBLE capability label this control belongs to ("Speech to
   *  text") — no longer wired through `htmlFor` (see the accessible-name
   *  comment below); this component builds its own `aria-labelledby` from it. */
  labelId: string;
  auto: string;
  selected: string;
  choices: EngineChoice[];
  /** From `strandedSelection(row, auto)` — the stored code when it matches no
   *  option, else null. */
  stranded: string | null;
  /** From `row.strandedLabel` — the registry's display name for `stranded`,
   *  or null when there is none to give (a withdrawn engine). Meaningless
   *  when `stranded` is null. */
  strandedLabel: string | null;
  /** True while a PUT for this row is in flight. Deliberately NOT the native
   *  `disabled` attribute on the trigger — see that comment below. */
  disabled?: boolean;
  onChange: (code: string) => void;
}

export default function EngineSelect({
  labelId,
  auto,
  selected,
  choices,
  stranded,
  strandedLabel,
  disabled,
  onChange,
}: EngineSelectProps) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const baseId = useId();
  const valueId = `${baseId}-value`;
  const labelledBy = `${labelId} ${valueId}`;
  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);

  const options = buildOptions(auto, choices, stranded, strandedLabel);
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

  // Outside click, Escape, scroll, resize and window blur all close the
  // popup — IconPicker.tsx's capture-phase document scroll listener and
  // ContextMenu.tsx's resize+blur, both here for the same reason: the popup
  // is `position: fixed` against a rect read ONCE (the layout effect below),
  // so any of these can move the trigger out from under it or take the whole
  // window away, and a popup that stays put then floats over whatever the
  // page put where it used to be — with its rows still clickable.
  useEffect(() => {
    if (!open) return;
    const onDocMouseDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (rootRef.current && rootRef.current.contains(target)) return;
      closePanel();
      // Restore focus to the trigger, mirroring IconPicker's opener-restore —
      // but only when the click did not already hand focus to something else
      // on its own (another button, a link). `mousedown` fires before the
      // browser's default focus change for the element that was clicked, so
      // check on the next tick once that has had a chance to happen; forcing
      // the trigger to take focus unconditionally would steal it right back
      // from the very control the user just clicked.
      window.setTimeout(() => {
        if (document.activeElement === document.body) {
          triggerRef.current?.focus();
        }
      }, 0);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        closePanel();
        triggerRef.current?.focus();
      }
    };
    // Capture phase: the scrollable ancestor a card sits in doesn't bubble
    // scroll to `window`, the same reason IconPicker's listener is captured.
    const onScroll = (e: Event) => {
      if (rootRef.current && rootRef.current.contains(e.target as Node)) return;
      closePanel();
    };
    document.addEventListener("mousedown", onDocMouseDown);
    document.addEventListener("keydown", onKeyDown, true);
    document.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", closePanel);
    window.addEventListener("blur", closePanel);
    return () => {
      document.removeEventListener("mousedown", onDocMouseDown);
      document.removeEventListener("keydown", onKeyDown, true);
      document.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", closePanel);
      window.removeEventListener("blur", closePanel);
    };
  }, [open]);

  // Focus the panel so its own keydown handler (below) sees arrow/Enter keys —
  // the trigger button loses focus the moment the popup opens.
  useEffect(() => {
    if (open) panelRef.current?.focus();
  }, [open]);

  // Position the popup under the trigger — min-width = trigger width,
  // otherwise its natural size up to the ~420px CSS cap — and scroll the
  // active row into view. Both wait for this layout effect rather than
  // running in `openPanel` because neither the popup nor its rows exist in
  // the DOM until AFTER this render: `openPanel` only flips `open` to true.
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
    // A stored LAST option opens off-screen against the ~320px max-height
    // without this — six wrapped rows run well past it, and the list then
    // reads as though "Automatic" (the always-visible first row) were the
    // current choice.
    document.getElementById(optionId(active))?.scrollIntoView({ block: "nearest" });
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
      // Escape is handled by the document-level listener above. Tab is not
      // handled here at all — it is left to move focus normally, and the
      // `onBlur` below notices the panel no longer holds it and closes.
    }
  };

  // The ARIA listbox-popup pattern's own close condition: focus leaving the
  // panel for anything outside it (Tab forward, Shift+Tab back, a script
  // moving focus) closes the popup — otherwise it stays mounted and painted
  // over whatever control focus just moved to, with `aria-expanded="true"`
  // still claiming it is the thing in focus. `relatedTarget` is the element
  // gaining focus; null when focus leaves the document entirely (window
  // blur), which the effect above already handles.
  const onPanelBlur = (e: React.FocusEvent) => {
    if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
      closePanel();
    }
  };

  return (
    <div className="am-engine-select-root" ref={rootRef}>
      <button
        ref={triggerRef}
        type="button"
        className={"field-control am-engine-select am-engine-trigger" + (disabled ? " disabled" : "")}
        // `aria-disabled` + `aria-busy`, NOT the native `disabled` attribute:
        // a disabled button is pulled out of the tab order and can't hold
        // focus, so `pick()`'s `triggerRef.current?.focus()` — called just
        // before this prop flips true for the PUT it kicks off — would lose
        // focus to <body> for the whole request and never get it back. This
        // way the trigger stays focused and merely ignores input.
        aria-disabled={disabled || undefined}
        aria-busy={disabled || undefined}
        aria-haspopup="listbox"
        aria-expanded={open}
        // The accessible name has to be BOTH the capability's own label and
        // the current value, or a screen reader hears only one of them:
        // `htmlFor`/`for` on a `<label>` makes the label the control's WHOLE
        // name (HTML-AAM: label-from-content outranks the button's own
        // content), so a reader heard "Speech to text, button" and never the
        // engine actually chosen — the one thing `servingLine` may no longer
        // repeat once the trigger is the only place naming it. Composing both
        // ids here is what a native `<select>` gets for free from its
        // `<option>` text; this control has to say it explicitly.
        aria-labelledby={labelledBy}
        onClick={() => {
          if (disabled) return;
          open ? closePanel() : openPanel();
        }}
      >
        <span id={valueId} className="am-engine-trigger-label">{triggerLabel}</span>
      </button>
      {open && (
        <div
          ref={panelRef}
          className="am-engine-popup context-menu"
          role="listbox"
          aria-labelledby={labelledBy}
          aria-activedescendant={options[active] ? optionId(active) : undefined}
          tabIndex={-1}
          onKeyDown={onPanelKeyDown}
          onBlur={onPanelBlur}
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
