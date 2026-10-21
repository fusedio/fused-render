// The app's one custom select: a chip that drops a listbox, drawn by the app
// rather than the OS. Written for the New task form ("custom input for
// everything", Akshil 2026-08-16) and shared since 2026-10-20 with every
// Preferences row that picks from a list — one control for one job across the
// shell. Styled by styles/schedule.css (.schedule-select / .schedule-pop /
// .schedule-menu-item), which is global.
import { useEffect, useId, useRef, useState } from "react";
import type React from "react";

// lucide "chevron-down", at the size the app's native selects draw their own
// (fields.css) — the mark that says "this opens a list", on every control that
// does.
const ICON_CHEVRON_DOWN = (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
       strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <polyline points="6 9 12 15 18 9" />
  </svg>
);

// ---- Fixed-position dropdowns ---------------------------------------------
// Every dropdown in this modal is position:fixed, measured off its trigger:
// the modal body scrolls (`.deploy-body { overflow-y: auto }`), and an
// absolutely-positioned panel gets CLIPPED at its edge — the month grid
// shipped cut off mid-row (Akshil, 2026-08-16 screenshot). Fixed escapes the
// clip; when the viewport below the trigger is shorter than the panel, it
// opens upward instead.
/**
 * HOW TALL A LIST-SHAPED PANEL MAY GET, and the gap it keeps off the window's
 * edges. The Tasks page's own popovers already answer this — `POP_MAX_HEIGHT`
 * in ScheduleTaskViews, with the same reasoning: a column of twenty-eight is
 * not a menu, it is a page. Same number, so the two lists in this app cap alike.
 */
const POP_MAX_HEIGHT = 320;
const POP_EDGE = 8;

/**
 * WHAT `position: fixed` IS ACTUALLY MEASURED FROM — the viewport, unless some
 * ancestor is transformed, and in this card one always is.
 *
 * `.modal-dialog.deploy-dialog` carries `transform: scale(.98)` from the open
 * animation, and a transformed element becomes the containing block for every
 * `fixed` descendant. So `top`/`left`/`bottom` are resolved against the CARD
 * while `getBoundingClientRect` answers in VIEWPORT coordinates, and the two
 * have been quietly disagreeing for as long as this function has existed. It
 * went unnoticed because every other menu here is short and opens downward, so
 * the error was a few pixels; a 50-row folder list opening upward put the panel
 * 150px off the top of the window (measured, 2026-09-18).
 *
 * Returns the origin to subtract and the box to fit inside. No transformed
 * ancestor — every other host of this card — answers the viewport, which is what
 * the arithmetic below has always assumed.
 */
function fixedBox(el: HTMLElement | null): { x: number; y: number; h: number } {
  for (let node = el?.parentElement ?? null; node; node = node.parentElement) {
    const cs = getComputedStyle(node);
    // `""` is what a stand-in stylesheet answers, and it is not a transform —
    // testing truthiness first keeps a test DOM from naming every ancestor.
    const transformed = (cs.transform && cs.transform !== "none")
      || (cs.filter && cs.filter !== "none")
      || (cs.perspective && cs.perspective !== "none");
    if (transformed) {
      const r = node.getBoundingClientRect();
      return { x: r.left, y: r.top, h: r.height };
    }
  }
  return { x: 0, y: 0, h: window.innerHeight };
}

export function popStyle(
  el: HTMLElement | null,
  estHeight: number,
  matchWidth = false,
  /**
   * CAP THE HEIGHT AND LET IT SCROLL, for a panel whose length is the disk's
   * business rather than the form's (Akshil, 2026-09-18: typing
   * `/Users/akshilthumar/` listed eight folders and cut off).
   *
   * Off by default, because it must be: the date grid, the time list and the
   * repeat menu are all fixed-length things this function has always placed by
   * their own height, and capping them would be a change nobody asked for. The
   * folder list is the one whose content is unbounded.
   *
   * The arithmetic is `ScheduleTaskViews.popStyle`'s, which fixed this exact
   * bug on the project menu ("sliced off at the bottom with a handful of its 28
   * folders showing"): height is never SET — a two-row list is two rows tall —
   * only capped, and capped by the room actually there so the panel can never
   * run off the card or the viewport.
   */
  scrolls = false,
): React.CSSProperties {
  const r = el?.getBoundingClientRect();
  if (!r) return {};
  // BOTH EDGES ARE ALWAYS STATED, and one of them is always `auto`. A panel that
  // flips up sets `bottom` — and the stylesheet that placed it before this
  // function existed still says `top: calc(100% + 4px)`, which a `position:
  // fixed` box resolves too. Two resolved edges do not mean "prefer the inline
  // one": they mean the height is the distance BETWEEN them, and for a panel
  // opening upward that distance is negative, so it collapsed to its padding.
  // Measured on `/Users/akshilthumar/`: a 50-row list 10px tall with 1473px of
  // scroll inside it. `tasks.css` fixes the same collision on the Tasks
  // popovers from the stylesheet's side (`top: auto`); doing it here fixes it
  // for every menu this function places, including the date, time and repeat
  // menus, which have had the same latent flip-up bug all along.
  // The box the offsets are resolved against — see `fixedBox`. The ROOM is still
  // reckoned in viewport terms, because what the reader cares about is whether
  // the panel is on their screen; only the offsets are converted.
  const o = fixedBox(el);
  const s: React.CSSProperties = {
    position: "fixed", left: r.left - o.x, right: "auto", top: "auto", bottom: "auto",
  };
  if (scrolls) {
    const below = window.innerHeight - r.bottom - 4 - POP_EDGE;
    const above = r.top - 4 - POP_EDGE;
    // Flip only when up is genuinely roomier — a panel that jumps above its
    // trigger to gain twenty pixels is a panel that moved for nothing.
    const up = above > below && below < POP_MAX_HEIGHT;
    s.maxHeight = Math.max(120, Math.min(POP_MAX_HEIGHT, up ? above : below));
    if (up) s.bottom = o.y + o.h - r.top + 4;
    else s.top = r.bottom + 4 - o.y;
  } else if (r.bottom + 4 + estHeight > window.innerHeight && r.top - 4 - estHeight > 0) {
    s.bottom = o.y + o.h - r.top + 4;
  } else {
    s.top = r.bottom + 4 - o.y;
  }
  // A menu is as wide as the control that opened it — the CSS floor of 180px
  // made the repeat menu wider than its chip and the recurrence units menu
  // three times wider than the word it was replacing (audit 2026-08-16). The
  // 140px is only there so a very narrow trigger still yields a readable list.
  if (matchWidth) {
    s.width = r.width;
    s.minWidth = 140;
  }
  return s;
}

// One custom select for EVERYTHING the form chooses from a list — repeat,
// permissions, the recurrence dialog's units. The native <select> sat beside
// the custom date/time/path dropdowns as the one control drawn by the OS
// (Akshil, 2026-08-16, "custom input for everything").
export function Dropdown({
  value,
  options,
  onPick,
  ariaLabel,
  className,
  disabled,
  labelledBy,
}: {
  value: string; // the current choice's LABEL
  options: { key: string; label: string; disabled?: boolean }[];
  onPick: (key: string) => void;
  ariaLabel?: string;
  className?: string;
  disabled?: boolean;
  /** The row label that names it (Preferences), instead of aria-label. */
  labelledBy?: string;
}) {
  const [open, setOpen] = useState(false);
  // Which option the ARROW KEYS are on. Focus never leaves the trigger — the
  // menu is a listbox, and a listbox's items are described by
  // aria-activedescendant, not focused one by one (a Tab through this form
  // otherwise walked every option of every open menu; audit 2026-08-16). The
  // options carry tabIndex={-1} for the same reason.
  const [active, setActive] = useState(-1);
  const btnRef = useRef<HTMLButtonElement>(null);
  const listId = useId();
  const menuRef = useRef<HTMLDivElement>(null);

  // Open on the current choice, so the first ArrowDown steps off it.
  // Focus the chip too: WebKit does not focus a button on click, so without
  // this the wrapper's Escape/arrow keys never reach it after a mouse open.
  const show = () => {
    setActive(options.findIndex((o) => o.label === value));
    setOpen(true);
    btnRef.current?.focus({ preventScroll: true });
  };

  // Close on a press outside. The wrapper's onBlur alone works in Chrome, but
  // WebKit never focused the chip, so nothing ever blurred and the menu stayed
  // open over the page. A press inside an app/chat iframe never reaches this
  // document, so the window losing focus closes it too.
  const wrapRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    const onDown = (e: PointerEvent) => {
      if (!wrapRef.current?.contains(e.target as Node | null)) close();
    };
    document.addEventListener("pointerdown", onDown, true);
    window.addEventListener("blur", close);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      window.removeEventListener("blur", close);
    };
  }, [open]);

  // The panel is placed from the chip's rect at render, so re-place it when
  // the chip moves — a resize, a scroll or a hidden sidebar left the menu
  // where the chip used to be (Akshil, 2026-10-21 screenshot). A frame loop,
  // because a sidebar folding moves the chip without any event on it.
  const [, replace] = useState(0);
  useEffect(() => {
    if (!open) return;
    // The window's height is in the key: it decides whether the panel flips up.
    const where = () => {
      const r = btnRef.current?.getBoundingClientRect();
      return r ? `${r.left},${r.top},${r.width},${r.height},${window.innerHeight}` : "";
    };
    // Seeded now, so a move between the opening render and the first frame
    // (the modal's scale-in, a sidebar mid-fold) still counts as a move.
    let last = where();
    let raf = 0;
    const tick = () => {
      const at = where();
      if (at !== last) replace((n) => n + 1);
      last = at;
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [open]);

  // Keep the active option in view when it is stepped past the panel's edge.
  useEffect(() => {
    if (!open) return;
    menuRef.current
      ?.querySelector<HTMLElement>(".is-active")
      ?.scrollIntoView({ block: "nearest" });
  }, [open, active]);

  const move = (delta: number) => {
    setActive((i) => {
      const n = options.length;
      if (n === 0) return -1;
      return ((i < 0 ? 0 : i + delta) + n) % n;
    });
  };

  return (
    <div
      ref={wrapRef}
      className={"schedule-pop-wrap" + (className ? " " + className : "")}
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setOpen(false);
      }}
      onKeyDown={(e) => {
        if (e.key === "Escape" && open) {
          e.stopPropagation();
          setOpen(false);
          return;
        }
        if (e.key === "ArrowDown" || e.key === "ArrowUp") {
          e.preventDefault();
          if (!open) show();
          else move(e.key === "ArrowDown" ? 1 : -1);
          return;
        }
        if (!open) return;
        if (e.key === "Home" || e.key === "End") {
          e.preventDefault();
          setActive(e.key === "Home" ? 0 : options.length - 1);
          return;
        }
        if (e.key === "Enter" && active >= 0 && active < options.length && !options[active].disabled) {
          e.preventDefault();
          setOpen(false);
          onPick(options[active].key);
        }
      }}
    >
      <button
        ref={btnRef}
        type="button"
        className="schedule-when-field schedule-select"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-activedescendant={
          open && active >= 0 ? `${listId}-${active}` : undefined
        }
        aria-label={ariaLabel}
        aria-labelledby={labelledBy}
        disabled={disabled}
        // WebKit drops focus off a pressed button, so the wrapper's blur shut
        // the menu and the click then reopened it. show() focuses the chip.
        onMouseDown={(e) => e.preventDefault()}
        onClick={() => (open ? setOpen(false) : show())}
      >
        <span className="schedule-select-label">{value}</span>
        {/* A DRAWN mark, not a "▾". The text triangle was a 10px glyph in a
            32px chip: a flex box centres the glyph's LINE BOX, and where the
            triangle's ink sits inside that box is the font's business — in the
            UI stack it rides high in the em square, so the caret hung near the
            top-right corner of the control instead of on the label's centre
            line (Akshil, 2026-08-18 screenshot). An SVG has no baseline and no
            font fallback; centred in a fixed square it lands on the middle of
            the chip in every theme and on every machine. Same mark as the
            native selects elsewhere in the app draw (fields.css) and as the
            card's own disclosure. */}
        <span className="schedule-select-caret" aria-hidden="true">
          {ICON_CHEVRON_DOWN}
        </span>
      </button>
      {open && (
        <div
          ref={menuRef}
          id={listId}
          className="schedule-pop schedule-pop--menu"
          role="listbox"
          aria-label={ariaLabel}
          style={popStyle(btnRef.current, options.length * 34 + 10, true)}
          onMouseDown={(e) => e.preventDefault()}
          onMouseLeave={() => setActive(-1)}
        >
          {options.map((o, i) => (
            <button
              key={o.key}
              id={`${listId}-${i}`}
              type="button"
              role="option"
              tabIndex={-1}
              disabled={o.disabled}
              aria-selected={o.label === value}
              className={
                "schedule-menu-item" +
                (o.label === value ? " is-selected" : "") +
                (i === active ? " is-active" : "")
              }
              onMouseEnter={() => setActive(i)}
              onClick={() => {
                if (o.disabled) return;
                setOpen(false);
                onPick(o.key);
              }}
            >
              {o.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
