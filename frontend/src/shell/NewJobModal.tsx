// "New task" — the /scheduled page's own way to create a scheduled message,
// alongside the chat composer's Send now pill (which stays the convenient path
// when a chat is already open on the right folder). This form serves the
// calendar-first direction, so it has to ask for the folder too.
//
// The layout is Google Calendar's new-event card, copied deliberately (Akshil,
// 2026-08-14 — the first cut's labelled-field stack read as "a bit too much"):
// a big borderless title, one when-row, one where-row, and everything else
// behind a collapsed More options. The trick that keeps it that small is also
// Google's: the REPEAT choices are derived from the picked date-time ("Weekly
// on Monday" because the date IS a Monday), so recurrence needs no fields of
// its own — only "Custom (cron)…" reveals one extra input.
import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Modal } from "@platform/ui/modal/Modal";
import {
  cancelScheduledMessage,
  getConfig,
  listDir,
  scheduleMessage,
} from "@platform/lib/api";
import type { RecurrenceRule, ScheduledMessage } from "@platform/lib/api";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { navigateUrl } from "@platform/lib/router";
import { describeRepeats, describeRule, repeatChoicesFor } from "./schedule-lib";
import { ICON_CLOCK, ICON_FOLDER } from "./ScheduleCalendar";
// This card's own rules live in styles/new-task.css, imported from the
// shell.css barrel like every other section — no shell component imports its
// own CSS (tests/test_theme.py pins the barrel against the styles/ directory).

// Where a new task points before the user says otherwise: ~/Desktop/fused
// (Akshil, 2026-08-14 — an empty path field was the confusing part of the
// form). Home-relative so it composes on any machine; the picker makes
// changing it a click, and a machine without the folder gets the server's
// clear 400 naming the path.
const DEFAULT_TARGET_SUFFIX = "/Documents/Fused";

// ---- Recent paths --------------------------------------------------------
// The path field's dropdown offers the last folders the user actually used —
// picked in the browser or saved on a task — newest first, five shown. Stored
// in localStorage so "the folder I always schedule against" survives reloads.
// try/catch throughout: storage can be denied (private mode), and a corrupt
// value must read as "no recents", never crash the modal (Bugbot, PR #538
// pattern).
const RECENTS_KEY = "fused-render:recent-paths";
const RECENTS_SHOWN = 5;

function readRecents(): string[] {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(RECENTS_KEY) ?? "[]");
    return Array.isArray(parsed)
      ? parsed.filter((p): p is string => typeof p === "string" && p !== "")
      : [];
  } catch {
    return [];
  }
}

function rememberRecent(path: string) {
  const p = path.trim();
  if (!p) return;
  try {
    const next = [p, ...readRecents().filter((r) => r !== p)].slice(0, 8);
    localStorage.setItem(RECENTS_KEY, JSON.stringify(next));
  } catch {
    // Storage denied — recents just don't persist.
  }
}

// ---- Browse: a slide-in explorer panel ---------------------------------------
// Browsing happens BESIDE the card, not inside it: the in-modal picker was
// "too small to see anything" (Akshil, 2026-08-16), so Browse slides an
// explorer-shaped panel in on the modal's right — the card shifts left to
// make room (see .schedule-explorer / the :has() rule in schedule.css) — with
// the room to show folders AND files. A folder click descends; a file click
// IS the pick (a task can target a file); "Use this folder" picks where you
// stand.

// Forward slashes throughout, including for Windows drive paths — the same
// normalization every other shell caller applies to `/api/config` values,
// whose `home` is a raw expanduser and arrives with backslashes there. The
// server accepts either separator; the PICKER's own string surgery (up(),
// joins) only understands one.
const normPath = (p: string) => p.replace(/\\/g, "/");


interface Crumb {
  name: string;
  path: string;
}

// The path as clickable crumbs: every ancestor is one tap away, which is what
// the old single "up" chevron made people hunt for (Akshil, 2026-08-15 — "not
// intuitive"). Root renders as "/" (or "C:/"), each segment jumps there.
function crumbsOf(path: string): Crumb[] {
  const trimmed = path.replace(/\/+$/, "");
  const drive = trimmed.match(/^[A-Za-z]:/)?.[0];
  const rootPath = drive ? drive + "/" : "/";
  const rest = (drive ? trimmed.slice(drive.length) : trimmed)
    .split("/")
    .filter(Boolean);
  const out = [{ name: drive ?? "/", path: rootPath }];
  let acc = drive ?? "";
  for (const seg of rest) {
    acc += "/" + seg;
    out.push({ name: seg, path: acc });
  }
  return out;
}

// A real path is deeper than a 460px panel is wide, and the trail used to wrap
// onto three lines — which moved the filter, the listing and the foot down with
// it, so the panel's whole geometry hung off how long the current path happened
// to be (audit 2026-08-16). Past four segments the middle collapses to one "…",
// which is NOT a control: there is no single folder it could stand for.
const CRUMBS_SHOWN = 4;

function collapseCrumbs(crumbs: Crumb[]): (Crumb | null)[] {
  if (crumbs.length <= CRUMBS_SHOWN) return crumbs;
  return [crumbs[0], null, crumbs[crumbs.length - 2], crumbs[crumbs.length - 1]];
}

const ICON_FILE = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
       strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
    <path d="M14 2v6h6" />
  </svg>
);

// lucide "type" — the serif T of a title field. Inline, like every other glyph
// in this file: no icon package in this repo.
const ICON_TITLE = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
       strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <polyline points="4 7 4 4 20 4 20 7" />
    <line x1="9" y1="20" x2="15" y2="20" />
    <line x1="12" y1="4" x2="12" y2="20" />
  </svg>
);

// lucide "check", at tick scale.
const ICON_CHECK = (
  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor"
       strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <polyline points="20 6 9 17 4 12" />
  </svg>
);

// A quiet checkbox: a real <input> (focusable, space-toggled, announced as a
// checkbox) wrapped in the <label> that names it, with the box itself drawn in
// CSS so it resolves in both themes. Used twice — Repeat, and the flag behind
// it.
function CheckField({
  checked,
  onChange,
  label,
  className,
  describedBy,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  className?: string;
  // A hint printed under the box that says what ticking it MEANS. Attached to
  // the real input, not merely placed near it, so a screen reader reads the
  // consequence with the control rather than as a stray line below it (the
  // discipline pastHintId already follows).
  describedBy?: string;
}) {
  return (
    <label className={"new-task-check" + (className ? " " + className : "")}>
      <input
        type="checkbox"
        className="new-task-check-input"
        checked={checked}
        aria-describedby={describedBy}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="new-task-check-box" aria-hidden="true">{ICON_CHECK}</span>
      <span className="new-task-check-text">{label}</span>
    </label>
  );
}

// Both side panels (Browse, Custom recurrence) borrow the dialog's own rect
// so they read as siblings of the card in geometry — see the comment inside.
function useDialogBox() {
  const [box, setBox] = useState<{ top: number; height: number } | null>(null);
  useLayoutEffect(() => {
    const measure = () => {
      const dialog = document.querySelector<HTMLElement>(".modal-dialog");
      if (!dialog) return;
      const r = dialog.getBoundingClientRect();
      setBox({ top: r.top, height: Math.max(r.height, 480) });
    };
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, []);
  return box;
}

function ExplorerPanel({
  start,
  onPick,
  onClose,
  closing,
}: {
  start: string;
  onPick: (path: string) => void;
  onClose: () => void;
  // Mounted-but-leaving: paints the exit animation while the parent waits to
  // unmount, so the way out mirrors the way in.
  closing?: boolean;
}) {
  // A file target starts the panel in its PARENT — listing a file's "children"
  // is a guaranteed error banner.
  const [path, setPath] = useState(() => {
    const p = normPath(start).replace(/\/+$/, "");
    return p || "/";
  });
  const [rows, setRows] = useState<{ name: string; dir: boolean }[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Type-to-narrow, cleared on every navigation — a filter that survives into
  // the next folder reads as "this folder is empty".
  const [filter, setFilter] = useState("");
  // One free climb: the client cannot tell a file path from a folder path
  // until listDir refuses it, so the FIRST refusal walks to the parent
  // instead of showing the error banner — Browse on an already-picked file
  // must open where that file lives (Bugbot, PR #548). One only, so a
  // genuinely missing tree still errors instead of climbing to "/".
  const climbed = useRef(false);

  useEffect(() => {
    let stale = false;
    // The OLD listing stays up, dimmed, while the next one loads — blanking
    // it made the panel pump on every click (QA 2026-08-14).
    setLoading(true);
    setError(null);
    listDir(path).then(
      (r) => {
        if (stale) return;
        setRows(
          r.entries
            .filter((e) => !e.name.startsWith("."))
            .map((e) => ({ name: e.name, dir: e.is_dir }))
            // Folders first, then files, each alphabetical — the explorer's
            // own ordering, so the panel reads like the app it stands in for.
            .sort((a, b) =>
              a.dir !== b.dir ? (a.dir ? -1 : 1) : a.name.localeCompare(b.name),
            ),
        );
        setLoading(false);
      },
      (e: Error) => {
        if (stale) return;
        const cut = path.replace(/\/+$/, "").lastIndexOf("/");
        if (!climbed.current && cut >= 0) {
          climbed.current = true;
          const parent = path.replace(/\/+$/, "").slice(0, cut);
          // A drive root keeps its slash — bare "C:" reads as cwd-relative
          // elsewhere in the shell, not as the root (Bugbot, PR #548; the
          // same trap the old picker's up() fixed in PR #541).
          setPath(/^[A-Za-z]:$/.test(parent) ? parent + "/" : parent || "/");
          return;
        }
        setError(e.message);
        setLoading(false);
      },
    );
    return () => {
      stale = true;
    };
  }, [path]);

  // The panel reads as a sibling of the card, so it has to BE one in geometry:
  // its top and height come from the dialog's own rect, not from the viewport.
  // Centred on the viewport (the first cut) the two lined up only when their
  // heights happened to match — a 520px card beside a 600px panel shared no
  // edge at all (audit 2026-08-16). The floor keeps a short card (an Edit with
  // nothing expanded) from shrinking the listing back to the "too small to see
  // anything" it was rescued from. Modal exposes no ref for its dialog, hence
  // the querySelector; recomputed while open because a resize moves both.
  const box = useDialogBox();

  const go = (p: string) => {
    setPath(p);
    setFilter("");
  };
  const crumbs = collapseCrumbs(crumbsOf(path));
  const shown = rows?.filter((r) =>
    r.name.toLowerCase().includes(filter.trim().toLowerCase()),
  );

  // Escape dismisses the PANEL, not the modal behind it — captured before the
  // modal chassis' own document-level Escape listener can see it.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopImmediatePropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className={"schedule-explorer" + (closing ? " is-closing" : "")} role="dialog" aria-label="Choose a folder or file"
         style={box ? { top: box.top, height: box.height } : undefined}>
      <div className="schedule-explorer-head">
        <span className="schedule-explorer-title">Choose a folder or file</span>
      </div>
      <div className="schedule-picker-crumbs" aria-label="Current folder">
        {crumbs.map((c, i) =>
          c === null ? (
            <span key="gap" className="schedule-picker-crumb-ellipsis">…</span>
          ) : (
            <span key={c.path} className="schedule-picker-crumb-seg">
              {/* The root crumb IS "/", so a separator in front of the first
                  real segment prints it twice — "//Users" (audit 2026-08-16).
                  A drive root ("C:") still takes one. */}
              {i > 0 && !(i === 1 && crumbs[0]?.name === "/") && (
                <span className="schedule-picker-crumb-sep">/</span>
              )}
              <button type="button" className="schedule-picker-crumb"
                      disabled={i === crumbs.length - 1}
                      title={c.path}
                      onClick={() => go(c.path)}>
                {c.name}
              </button>
            </span>
          ),
        )}
      </div>
      <input
        type="text"
        className="field-control schedule-picker-filter"
        placeholder="Filter this folder"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      />
      <div className={"schedule-picker-list" + (loading ? " is-loading" : "")}>
        {error && <p className="schedule-card-why">{error}</p>}
        {!error && shown?.length === 0 && !loading && (
          <p className="schedule-card-why">
            {filter ? "Nothing matches" : "Empty folder"}
          </p>
        )}
        {!error && shown?.map(({ name, dir }) => (
          <button key={name} type="button"
                  className={"schedule-picker-row" + (dir ? "" : " schedule-picker-row--file")}
                  disabled={loading} title={name}
                  onClick={() => {
                    const full = path.replace(/\/+$/, "") + "/" + name;
                    // A folder is a place to go; a file is an ANSWER — picking
                    // one finishes the errand.
                    if (dir) go(full);
                    else {
                      onPick(full);
                      onClose();
                    }
                  }}>
            {dir ? ICON_FOLDER : ICON_FILE}
            <span className="schedule-picker-name">{name}</span>
            {dir && <span className="schedule-picker-enter" aria-hidden="true">›</span>}
          </button>
        ))}
      </div>
      <div className="schedule-picker-foot">
        <button type="button" className="btn btn-secondary" onClick={onClose}>
          Cancel
        </button>
        <button type="button" className="btn btn-primary"
                onClick={() => { onPick(path); onClose(); }}>
          Use this folder
        </button>
      </div>
    </div>
  );
}

// ---- Fixed-position dropdowns ---------------------------------------------
// Every dropdown in this modal is position:fixed, measured off its trigger:
// the modal body scrolls (`.deploy-body { overflow-y: auto }`), and an
// absolutely-positioned panel gets CLIPPED at its edge — the month grid
// shipped cut off mid-row (Akshil, 2026-08-16 screenshot). Fixed escapes the
// clip; when the viewport below the trigger is shorter than the panel, it
// opens upward instead.
function popStyle(
  el: HTMLElement | null,
  estHeight: number,
  matchWidth = false,
): React.CSSProperties {
  const r = el?.getBoundingClientRect();
  if (!r) return {};
  const s: React.CSSProperties = { position: "fixed", left: r.left, right: "auto" };
  if (r.bottom + 4 + estHeight > window.innerHeight && r.top - 4 - estHeight > 0) {
    s.bottom = window.innerHeight - r.top + 4;
  } else {
    s.top = r.bottom + 4;
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
function Dropdown({
  value,
  options,
  onPick,
  ariaLabel,
  className,
}: {
  value: string; // the current choice's LABEL
  options: { key: string; label: string }[];
  onPick: (key: string) => void;
  ariaLabel: string;
  className?: string;
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
  const show = () => {
    setActive(options.findIndex((o) => o.label === value));
    setOpen(true);
  };

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
        if (e.key === "Enter" && active >= 0 && active < options.length) {
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
        onClick={() => (open ? setOpen(false) : show())}
      >
        <span className="schedule-select-label">{value}</span>
        <span className="schedule-select-caret" aria-hidden="true">▾</span>
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
              aria-selected={o.label === value}
              className={
                "schedule-menu-item" +
                (o.label === value ? " is-selected" : "") +
                (i === active ? " is-active" : "")
              }
              onMouseEnter={() => setActive(i)}
              onClick={() => {
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

const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

// ---- Google-style date + time dropdowns ----------------------------------
// The native datetime-local control is one opaque box; Google splits it into
// a date field that drops a month grid and a time field that drops a 15-min
// list (Akshil, 2026-08-15, "have custom dropdown like gmail does"). Both are
// dumb views over the modal's single `when` string.

function MiniCalendar({
  selected,
  onPick,
  minDate,
}: {
  selected: Date;
  onPick: (d: Date) => void;
  // A hard floor, and the ONLY one left: the recurrence section's end date,
  // which cannot precede the anchor it ends. The when-row's grid no longer
  // floors at today — scheduling into the past is now a legitimate way to say
  // "run this as soon as you can" (design §9), so the `minToday` this
  // component used to take is gone with the refusal it enforced.
  minDate?: Date;
}) {
  // The month being LOOKED AT, which is not the month selected — paging
  // through months must not move the selection.
  const [view, setView] = useState(
    () => new Date(selected.getFullYear(), selected.getMonth(), 1),
  );
  const today = new Date();
  const firstDow = view.getDay();
  const daysInMonth = new Date(view.getFullYear(), view.getMonth() + 1, 0).getDate();
  const cells: (number | null)[] = [
    ...Array.from({ length: firstDow }, () => null),
    ...Array.from({ length: daysInMonth }, (_, i) => i + 1),
  ];
  const same = (d: Date, y: number, m: number, day: number) =>
    d.getFullYear() === y && d.getMonth() === m && d.getDate() === day;

  // The earliest day this grid will hand back, as a midnight stamp; -Infinity
  // when nothing constrains it.
  const floor = minDate
    ? new Date(minDate.getFullYear(), minDate.getMonth(), minDate.getDate()).getTime()
    : -Infinity;

  return (
    <div className="schedule-mini-cal">
      <div className="schedule-mini-cal-head">
        <span className="schedule-mini-cal-title">
          {MONTHS[view.getMonth()]} {view.getFullYear()}
        </span>
        <button type="button" className="schedule-mini-cal-nav" tabIndex={-1}
                aria-label="Previous month"
                onClick={() => setView(new Date(view.getFullYear(), view.getMonth() - 1, 1))}>
          ‹
        </button>
        <button type="button" className="schedule-mini-cal-nav" tabIndex={-1}
                aria-label="Next month"
                onClick={() => setView(new Date(view.getFullYear(), view.getMonth() + 1, 1))}>
          ›
        </button>
      </div>
      <div className="schedule-mini-cal-grid">
        {["S", "M", "T", "W", "T", "F", "S"].map((d, i) => (
          <span key={i} className="schedule-mini-cal-dow">{d}</span>
        ))}
        {cells.map((day, i) => {
          if (day === null) return <span key={`b${i}`} />;
          const d = new Date(view.getFullYear(), view.getMonth(), day);
          return (
            <button
              key={day}
              type="button"
              // The grid is one control reached from its chip, not 31 tab
              // stops in the middle of the form.
              tabIndex={-1}
              disabled={d.getTime() < floor}
              className={
                "schedule-mini-cal-day" +
                (same(selected, view.getFullYear(), view.getMonth(), day) ? " is-selected" : "") +
                (same(today, view.getFullYear(), view.getMonth(), day) ? " is-today" : "")
              }
              onClick={() => onPick(d)}
            >
              {day}
            </button>
          );
        })}
      </div>
    </div>
  );
}

// "8:30pm" — Google's compact clock wording, used by the field and its list.
function fmtTime(h: number, m: number): string {
  const ap = h < 12 ? "am" : "pm";
  const hh = h % 12 === 0 ? 12 : h % 12;
  return `${hh}:${String(m).padStart(2, "0")}${ap}`;
}

// Parse what a person types into a time field: "8", "8:30", "8:30pm", "20:15".
// null = not a time; the field then falls back to what it had.
function parseTime(text: string): { h: number; m: number } | null {
  const m = text.trim().toLowerCase().match(/^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$/);
  if (!m) return null;
  let h = Number(m[1]);
  const mins = Number(m[2] ?? 0);
  if (mins > 59) return null;
  if (m[3] === "pm" && h < 12) h += 12;
  if (m[3] === "am" && h === 12) h = 0;
  if (m[3] && Number(m[1]) > 12) return null;
  return h > 23 ? null : { h, m: mins };
}

function TimeList({
  selected,
  onPick,
}: {
  selected: { h: number; m: number };
  onPick: (h: number, m: number) => void;
}) {
  // The NEAREST slot carries the highlight — a typed 10:19pm is not on the
  // 15-minute grid, and matching exactly left the list unmarked and parked
  // at midnight (QA 2026-08-15). Scrolled by container arithmetic, not
  // scrollIntoView: the latter also scrolls the modal behind the dropdown.
  const ref = useRef<HTMLDivElement>(null);
  const nearest = Math.min(95, Math.round((selected.h * 60 + selected.m) / 15));
  useEffect(() => {
    const list = ref.current;
    const hit = list?.querySelector<HTMLElement>(".is-selected");
    if (list && hit) {
      list.scrollTop = hit.offsetTop - list.clientHeight / 2 + hit.offsetHeight / 2;
    }
  }, []);
  const slots = Array.from({ length: 96 }, (_, i) => ({
    h: Math.floor(i / 4),
    m: (i % 4) * 15,
  }));
  return (
    // A listbox, said out loud: 96 slots that announced themselves as plain
    // buttons left a screen reader no way to know one of them was the current
    // time, and Tab walked all 96 (audit 2026-08-16).
    <div className="schedule-time-list" ref={ref} role="listbox" aria-label="Time">
      {slots.map(({ h, m }, i) => (
        <button
          key={`${h}:${m}`}
          type="button"
          role="option"
          tabIndex={-1}
          aria-selected={i === nearest}
          className={"schedule-time-slot" + (i === nearest ? " is-selected" : "")}
          onClick={() => onPick(h, m)}
        >
          {fmtTime(h, m)}
        </button>
      ))}
    </div>
  );
}

// ---- Custom recurrence (Google's dialog, copied deliberately) --------------
// Repeat every [n] [unit], weekday circles for weeks, Ends never/on/after.
function CustomRecurrence({
  initial,
  anchor,
  onDone,
  onCancel,
  closing,
}: {
  initial: RecurrenceRule | null;
  anchor: Date;
  onDone: (rule: RecurrenceRule) => void;
  onCancel: () => void;
  closing?: boolean;
}) {
  const box = useDialogBox();
  // Escape dismisses the PANEL (as Cancel), captured before the modal's own
  // document-level Escape — same contract as the explorer beside it.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopImmediatePropagation();
        onCancel();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const [freq, setFreq] = useState<RecurrenceRule["freq"]>(initial?.freq ?? "week");
  const [interval, setIntervalN] = useState(initial?.interval ?? 1);
  const [byday, setByday] = useState<number[]>(
    initial?.byday?.length ? initial.byday : [anchor.getDay()],
  );
  const [monthly, setMonthly] = useState<"day" | "nth-weekday">(initial?.monthly ?? "day");
  const [ends, setEnds] = useState<"never" | "on" | "after">(
    initial?.until ? "on" : initial?.count ? "after" : "never",
  );
  const [until, setUntil] = useState(initial?.until ?? "");
  const [count, setCount] = useState(initial?.count ?? 13);
  // The end date is picked from the SAME month grid the when-row uses, dropped
  // from a chip — the `<input type="date">` it replaces was the last OS-drawn
  // control in a form of custom chips (audit 2026-08-16).
  const [untilOpen, setUntilOpen] = useState(false);
  const untilRef = useRef<HTMLButtonElement>(null);
  // The section opens on the question it is asking: how often. Without this
  // the reveal landed focus nowhere and a keyboard user had to Tab in from the
  // repeat menu they had just left (audit 2026-08-16).
  const intervalRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    intervalRef.current?.focus();
    intervalRef.current?.select();
  }, []);

  const toggleDay = (d: number) =>
    setByday((prev) => {
      const has = prev.includes(d);
      // Never empty: a weekly rule with no days is a rule that never fires.
      if (has && prev.length === 1) return prev;
      return has ? prev.filter((x) => x !== d) : [...prev, d].sort((a, b) => a - b);
    });

  const done = () => {
    const rule: RecurrenceRule = { freq };
    if (interval > 1) rule.interval = interval;
    if (freq === "week") rule.byday = byday;
    if (freq === "month") rule.monthly = monthly;
    if (ends === "on" && until) rule.until = until;
    if (ends === "after") rule.count = count;
    onDone(rule);
  };

  const nth = NTH_LABELS[Math.floor((anchor.getDate() - 1) / 7)];

  return (
    // A SECTION of the form, not a dialog: it has no scrim, no focus trap and
    // the card behind it stays live, so announcing role="dialog" promised a
    // modality that does not exist (audit 2026-08-16).
    <section className={"schedule-recur" + (closing ? " is-closing" : "")}
             aria-label="Custom recurrence"
             style={box ? { top: box.top, maxHeight: box.height + 140 } : undefined}>
      <p className="schedule-recur-title">Custom recurrence</p>

      <div className="schedule-recur-row">
        <span>Repeat every</span>
        <input ref={intervalRef} type="number" min={1} max={99}
               className="schedule-recur-n" aria-label="Repeat interval"
               value={interval}
               onChange={(e) => setIntervalN(Math.max(1, Math.min(99, Number(e.target.value) || 1)))} />
        <Dropdown
          ariaLabel="Repeat unit"
          className="schedule-recur-unit"
          value={interval > 1 ? `${freq}s` : freq}
          options={(["hour", "day", "week", "month", "year"] as const).map((u) => ({
            key: u,
            label: interval > 1 ? `${u}s` : u,
          }))}
          onPick={(u) => setFreq(u as RecurrenceRule["freq"])}
        />
      </div>

      {freq === "week" && (
        <div className="schedule-recur-row schedule-recur-days">
          <span>Repeat on</span>
          <span className="schedule-recur-circles">
            {["S", "M", "T", "W", "T", "F", "S"].map((label, d) => (
              <button key={d} type="button"
                      className={"schedule-recur-day" + (byday.includes(d) ? " is-on" : "")}
                      aria-pressed={byday.includes(d)}
                      aria-label={DAYS[d]}
                      onClick={() => toggleDay(d)}>
                {label}
              </button>
            ))}
          </span>
        </div>
      )}

      {freq === "month" && (
        <div className="schedule-recur-row">
          <Dropdown
            ariaLabel="Monthly on"
            value={
              monthly === "day"
                ? `Monthly on day ${anchor.getDate()}`
                : `Monthly on the ${nth} ${DAYS[anchor.getDay()]}`
            }
            options={[
              { key: "day", label: `Monthly on day ${anchor.getDate()}` },
              { key: "nth-weekday", label: `Monthly on the ${nth} ${DAYS[anchor.getDay()]}` },
            ]}
            onPick={(v) => setMonthly(v as "day" | "nth-weekday")}
          />
        </div>
      )}

      {/* Three mutually exclusive answers to one question = a segmented
          control, the same one the page's view toggle is built from. Three
          stacked native radios (two of whose fields were rendered DISABLED
          rather than hidden) was the one place this form still looked like a
          settings page (audit 2026-08-16). Only the chosen branch's field is
          rendered — a greyed-out control is a question you cannot answer. */}
      <div className="schedule-recur-ends">
        <span className="schedule-recur-ends-label">Ends</span>
        <div className="schedule-form-seg" role="radiogroup" aria-label="Ends">
          {ENDS_CHOICES.map(({ key, label }) => (
            <button key={key} type="button"
                    className={"btn btn-secondary" + (ends === key ? " is-active" : "")}
                    aria-pressed={ends === key}
                    onClick={() => setEnds(key)}>
              {label}
            </button>
          ))}
        </div>

        {ends === "on" && (
          <div className="schedule-recur-detail">
            <div
              className="schedule-pop-wrap"
              onBlur={(e) => {
                if (!e.currentTarget.contains(e.relatedTarget as Node | null))
                  setUntilOpen(false);
              }}
              onKeyDown={(e) => {
                if (e.key === "Escape" && untilOpen) {
                  e.stopPropagation();
                  setUntilOpen(false);
                }
              }}
            >
              <button ref={untilRef} type="button"
                      className="schedule-when-field schedule-recur-until"
                      aria-expanded={untilOpen}
                      aria-label="End date"
                      onClick={() => setUntilOpen((o) => !o)}>
                {untilLabel(until)}
              </button>
              {untilOpen && (
                <div className="schedule-pop" style={popStyle(untilRef.current, 300)}
                     onMouseDown={(e) => e.preventDefault()}>
                  {/* minToday is deliberately off: an end date is bounded by
                      its own anchor, not by today. */}
                  <MiniCalendar
                    selected={untilDate(until) ?? anchor}
                    minDate={anchor}
                    onPick={(d) => { setUntil(ymdOf(d)); setUntilOpen(false); }}
                  />
                </div>
              )}
            </div>
          </div>
        )}

        {ends === "after" && (
          <div className="schedule-recur-detail">
            <input type="number" min={1} max={999} className="schedule-recur-n"
                   aria-label="Number of occurrences"
                   value={count}
                   onChange={(e) => setCount(Math.max(1, Math.min(999, Number(e.target.value) || 1)))} />
            <span>occurrences</span>
          </div>
        )}
      </div>

      <div className="schedule-picker-foot">
        <button type="button" className="btn btn-secondary" onClick={onCancel}>
          Cancel
        </button>
        <button type="button" className="btn btn-primary"
                disabled={ends === "on" && !until}
                onClick={done}>
          Done
        </button>
      </div>
    </section>
  );
}

const ENDS_CHOICES = [
  { key: "never", label: "Never" },
  { key: "on", label: "On" },
  { key: "after", label: "After" },
] as const;

// A date the recurrence section stores as "YYYY-MM-DD", both ways. Parsed by
// hand rather than through `new Date(ymd)` — that reads a bare date string as
// UTC and lands a day early west of Greenwich.
const ymdOf = (d: Date) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

function untilDate(ymd: string): Date | null {
  const [y, m, d] = ymd.split("-").map(Number);
  return y && m && d ? new Date(y, m - 1, d) : null;
}

function untilLabel(ymd: string): string {
  const d = untilDate(ymd);
  return d ? `${MONTHS[d.getMonth()].slice(0, 3)} ${d.getDate()}, ${d.getFullYear()}` : "Pick a date";
}

const NTH_LABELS = ["first", "second", "third", "fourth", "fifth"];

// How a permission mode is SAID. The keys are the server's contract and stay
// exactly as they are on the wire; only the reading changes. A mode this map
// has never heard of shows its key, which is still better than hiding it.
const PERMISSION_LABELS: Record<string, string> = {
  auto: "Auto",
  acceptEdits: "Accept edits",
  plan: "Plan only",
  prompt: "Ask every time",
};

const permissionLabel = (key: string) => PERMISSION_LABELS[key] ?? key;

// A Date as the value a <input type="datetime-local"> wants: local wall-clock,
// minute precision, no zone suffix. `toISOString` is exactly wrong here (UTC).
function toLocalInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// Which derived choice a stored RULE is, so editing reopens on the words the
// user picked; anything the preset list can't say is "custom". Legacy cron
// templates get a "cron" key of their own — the form no longer writes cron,
// but editing an old entry must not silently rewrite its rule.
function keyOfRule(rule: RecurrenceRule, anchor: Date): string {
  const choices = repeatChoicesFor(anchor);
  const canon = (r: RecurrenceRule) =>
    JSON.stringify({
      freq: r.freq,
      interval: r.interval ?? 1,
      byday: r.freq === "week" ? (r.byday?.length ? [...r.byday].sort((a, b) => a - b) : [anchor.getDay()]) : undefined,
      monthly: r.freq === "month" ? (r.monthly ?? "day") : undefined,
      until: r.until,
      count: r.count,
    });
  const hit = choices.find((c) => c.rule && canon(c.rule) === canon(rule));
  return hit?.key ?? "custom";
}

// The repeat choice a form OPENS on. "none" is the one value that means the
// Repeat checkbox is unticked, so this is also what decides whether editing a
// repeating task opens checked (design §6).
export function initialRepeatKey(entry?: ScheduledMessage | null): string {
  if (entry?.rule) return keyOfRule(entry.rule, new Date(entry.due));
  return entry?.repeats ? "cron" : "none";
}

// What the big field opens with — the one prose field the form has, standing
// for both of the two values the server stores. A new task opens on the chat
// composer's draft, if it came from one; an Edit opens on what the entry has.
//
// `message` first, because every entry has one and it is what Claude was
// actually sent; `description` as the fallback, so a task whose prose lives
// only there still fills the field instead of opening blank and re-creating
// itself empty. `||`, not `??`: "" is a missing answer here, not an answer.
export function initialAskOf(
  entry?: ScheduledMessage | null,
  chatDraft?: string | null,
): string {
  return entry?.message || entry?.description || chatDraft || "";
}

// The thread a task ALREADY OWNS, if any. A repeating template LEARNS one: its
// first run reports the session it ran in and the server writes that id back
// onto the template, so run 2 resumes it (a task IS a session — design §6).
// That id has to survive an edit, because an edit is cancel + re-create and
// dropping it orphans everything the task built.
//
// An UNMARKED id is not that. It is a chat handoff kept from when the task was
// scheduled, and it keeps a handoff's rules: continued while the task stays a
// one-off, refused the moment it starts repeating — otherwise ticking Repeat on
// a chat-scheduled task quietly signs the user's open conversation up to be
// appended to forever, the exact thing the repeat rule exists to refuse.
//
// The two are told apart by `session_learned`, which the server writes at the
// moment it learns the id and which travels through the cancel-and-re-create an
// edit is. This used to be INFERRED — an id counted as learned if the entry
// repeated — and that reading cannot survive a round trip: demote a chaining
// task to a one-off (its learned id deliberately rides along) and promote it
// back, and the learned thread reads as a chat handoff and is dropped
// (Bugbot, PR #555). An absent marker means NOT learned, which is the reading
// that keeps a chat's id refused by a repeat.
export function learnedSessionOf(entry?: ScheduledMessage | null): string {
  if (!entry?.session_id || entry.session_learned !== true) return "";
  return entry.session_id;
}

// What the Repeat checkbox does to the repeat state. Unticking CLEARS: the key
// goes back to "none" AND the custom rule is dropped, so nothing stays armed
// behind a dropdown that is no longer on screen — a hidden rule would still be
// submitted by `rule` below. Ticking an unset form lands on the commonest
// answer rather than on a blank menu; ticking a form that already carries a
// rule (an Edit) leaves it exactly where it was.
export const DEFAULT_REPEAT_KEY = "daily";

export function applyRepeatToggle(
  on: boolean,
  current: { repeat: string; customRule: RecurrenceRule | null },
): { repeat: string; customRule: RecurrenceRule | null } {
  if (!on) return { repeat: "none", customRule: null };
  if (current.repeat === "none") return { repeat: DEFAULT_REPEAT_KEY, customRule: current.customRule };
  return current;
}

// The body POSTed to /api/schedule — api.ts's own parameter type, nothing
// added to it. That type models `title`, `description` and `new_task_each_run`
// itself, so this alias only names what the builder returns.
export type SchedulePayload = Parameters<typeof scheduleMessage>[0];

export function buildSchedulePayload(form: {
  target: string;
  // The big field, and the ONLY prose the form asks for: what Claude is sent,
  // and — same text, no second field — the task's description (Akshil,
  // 2026-08-17: "the big field is the description, that is the first message").
  // It rides the wire twice, once as each, because the server stores them as
  // two things and a task page that showed nothing under a task would be the
  // only alternative.
  message: string;
  // Optional, and empty is the ordinary case: the server fills a missing title
  // in from the transcript's `ai-title` (design §4).
  title: string;
  when: string;
  // The structured rule the current choice means; null for a one-off and for
  // the legacy cron key, whose line is submitted verbatim instead.
  rule: RecurrenceRule | null;
  repeat: string;
  legacyCron: string;
  permission: string;
  // A CHAT HANDOFF's session: the conversation the composer was in when it
  // deep-linked here (?new=1&session_id=…). A one-off continues it; a repeat
  // refuses it, because a task that runs every day must not hijack the user's
  // open chat and compound its context forever.
  sessionId: string;
  // The task's OWN session, and the opposite case: the thread the entry being
  // edited LEARNED when its first run reported the session it ran in, which the
  // server marks as learned at that moment (`session_learned`). Editing is
  // cancel + re-create, so dropping this is how a chaining task silently
  // abandons everything it had built. It outranks a chat's id and survives a
  // repeat — unless the task forks every run below.
  learnedSessionId?: string;
  // Ticked: mint a fresh task — a fresh Claude session — per occurrence,
  // instead of the default, which is every run landing in this task's own
  // thread (design §6).
  newTaskEachRun: boolean;
}): SchedulePayload {
  const repeating = form.rule !== null || form.repeat === "cron";
  const trimmedTitle = form.title.trim();
  // The description IS the ask. Trimmed, because the padding a textarea
  // collects is not part of what the task is about; `message` itself is sent
  // verbatim, since that is what Claude actually receives.
  const trimmedDescription = form.message.trim();
  // WHICH session, if any, the re-created entry continues. The two sources are
  // treated oppositely:
  //   · the task's own (learned) id survives everything except a template that
  //     is meant to fork — an edit that dropped it would orphan the thread the
  //     task had been building, with nothing in the UI saying so;
  //   · a chat's id is continued only while the task stays a one-off.
  // A ticked "new task each run" refuses BOTH: that template mints a fresh
  // session per occurrence, so any id on it is a thread it must not resume.
  const carriesLearned =
    Boolean(form.learnedSessionId) && !(repeating && form.newTaskEachRun);
  const continued = carriesLearned
    ? (form.learnedSessionId ?? "")
    : form.learnedSessionId || repeating
      ? ""
      : form.sessionId;
  return {
    target: form.target.trim(),
    message: form.message,
    // A rule rides WITH its anchor (`due` = the first run); the legacy cron
    // line replaces due exactly as it always did; a one-off is due alone.
    ...(form.rule
      ? { due: form.when, rule: form.rule }
      : form.repeat === "cron" && form.legacyCron
        ? { repeats: form.legacyCron }
        : { due: form.when }),
    permission_mode: form.permission,
    // An edit keeps what it cannot re-ask for — see `continued` above.
    ...(continued ? { session_id: continued } : {}),
    // …and re-states WHERE that id came from, so the re-created entry is still
    // marked as owning a learned thread. Without this the marker would die on
    // the first edit and the next one would read the id as a chat handoff —
    // which is the bug this replaced. Never sent for a chat's id: nothing has
    // learned anything yet.
    ...(continued && carriesLearned ? { session_learned: true } : {}),
    // Empty means "the server decides" for the title and "there isn't one" for
    // the description — in both cases the key is better left off the wire than
    // sent as "". A blank description only happens if `message` is blank, which
    // the Save button already refuses.
    ...(trimmedTitle ? { title: trimmedTitle } : {}),
    ...(trimmedDescription ? { description: trimmedDescription } : {}),
    // Only ever sent on a repeating task: on a one-off there is no "each run"
    // for it to mean anything about.
    ...(repeating && form.newTaskEachRun ? { new_task_each_run: true } : {}),
  };
}

export default function NewJobModal({
  initialTime,
  initialTarget,
  initialMessage,
  chatSessionId,
  chatBack,
  editing,
  permissionModes,
  recentTargets,
  onClose,
  onCreated,
}: {
  // From a calendar slot click, or null from the New task button.
  initialTime: Date | null;
  // From a deep link that ALREADY knows the folder: the chat composer's
  // Schedule button, which is bound to one target (/scheduled?new=1&target=…).
  // It outranks the DEFAULT_TARGET_SUFFIX guess below — a guess is what you
  // offer when nobody said — and an Edit outranks both, having a stored target.
  initialTarget?: string | null;
  // The chat composer's handoff (Akshil, 2026-08-16): the draft the user had
  // typed arrives as the ask — which is the message AND the description…
  initialMessage?: string | null;
  // …the open conversation arrives as a session to CONTINUE — but only a
  // one-off resumes it; a repeating task always opens fresh chats, because
  // resuming the same conversation every day compounds context forever.
  chatSessionId?: string | null;
  // And the chat's own URL, so the form can offer the way back — the whole
  // point is a round trip (chat → schedule → back → adjust → again).
  chatBack?: string | null;
  // An existing task to change. The server has no update: saving schedules the
  // replacement first, then withdraws this one — see submit().
  editing?: ScheduledMessage | null;
  permissionModes: string[];
  // Folders existing tasks already point at, newest first — the parent reads
  // them off the schedule it has anyway. They pad the dropdown out on a
  // machine whose localStorage hasn't seen this form yet (QA 2026-08-15 —
  // the first open showed nothing but Browse).
  recentTargets?: string[];
  onClose: () => void;
  onCreated: () => void;
}) {
  // One field, two stored values — see initialAskOf. Held in a const because
  // the BASELINE below (`initial`) has to be the identical value, or an Edit
  // opens looking dirty and its ✕ arms the close-twice guard on an untouched
  // modal (QA 2026-08-14).
  const initialAsk = initialAskOf(editing, initialMessage);
  const [message, setMessage] = useState(initialAsk);
  // Optional and new (design §4), and normally left blank — Claude Code writes
  // its own one-liner into the transcript and the server prefers that — so the
  // field asks for nothing and the placeholder says so. Absent on the stored
  // entry reads as empty, which is what an entry written before this field
  // existed means.
  const [title, setTitle] = useState(editing?.title ?? "");
  const [target, setTarget] = useState(editing?.target ?? initialTarget ?? "");
  // ONE date-time drives everything: a one-off runs at it, and every derived
  // repeat choice reads its parts (minute, time, weekday) — Google's model.
  const [when, setWhen] = useState(() =>
    toLocalInput(
      editing?.due ? new Date(editing.due)
        : (initialTime ?? new Date(Date.now() + 3600_000)),
    ),
  );
  // The repeat CHOICE (a key into repeatChoicesFor) plus the one choice that
  // carries its own data: a custom rule from the recurrence dialog. Legacy
  // cron templates edit under the "cron" key and keep their line verbatim.
  const [repeat, setRepeat] = useState<string>(() => initialRepeatKey(editing));
  const [customRule, setCustomRule] = useState<RecurrenceRule | null>(() =>
    editing?.rule && keyOfRule(editing.rule, new Date(editing.due)) === "custom"
      ? editing.rule
      : null,
  );
  // Repeat is a CHECKBOX now, and the dropdown only exists while it is ticked
  // (design §6). Editing a repeating task therefore opens ticked, with the
  // stored rule already loaded — which is exactly "the key is not none".
  const [repeatOn, setRepeatOn] = useState(() => initialRepeatKey(editing) !== "none");
  // The opt-out behind it: every run of a repeating task lands in this task's
  // own thread — a task IS a session — unless this says to mint a fresh one
  // per occurrence.
  const [newTaskEachRun, setNewTaskEachRun] = useState(
    () => editing?.new_task_each_run ?? false,
  );
  const legacyCron = editing?.repeats ?? "";
  // The thread this task has already been building, if it has one — read once
  // and used twice: it goes on the wire (or the edit orphans it) and it is what
  // the note under the repeat row is able to say out loud.
  const learnedSession = learnedSessionOf(editing);
  // The recurrence dialog, and the key to fall back to if it's cancelled —
  // picking "Custom…" must not strand the select on a choice with no rule.
  const [recurOpen, setRecurOpen] = useState(false);
  const repeatBefore = useRef(repeat);
  const [permission, setPermission] = useState(editing?.permission_mode || "auto");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  // Leaving states: the panel stays mounted for its 180ms exit animation —
  // popping off while the card glided back read as a glitch (Akshil,
  // 2026-08-16). Guarded so a double-close cannot double-arm the timer.
  const [pickingOut, setPickingOut] = useState(false);
  const [recurOut, setRecurOut] = useState(false);
  // The exit timers are HELD, not fire-and-forget: reopening a panel during
  // its 180ms exit must cancel the pending unmount, or the reopened panel
  // stays is-closing and then vanishes when the stale timer lands (Bugbot,
  // PR #548 — same discipline as backTimer above).
  const pickerTimer = useRef<number | null>(null);
  const recurTimer = useRef<number | null>(null);
  useEffect(
    () => () => {
      if (pickerTimer.current !== null) window.clearTimeout(pickerTimer.current);
      if (recurTimer.current !== null) window.clearTimeout(recurTimer.current);
    },
    [],
  );
  const closePicker = () => {
    if (pickingOut) return;
    setPickingOut(true);
    pickerTimer.current = window.setTimeout(() => {
      pickerTimer.current = null;
      setPicking(false);
      setPickingOut(false);
    }, 180);
  };
  const openPicker = () => {
    if (pickerTimer.current !== null) window.clearTimeout(pickerTimer.current);
    pickerTimer.current = null;
    setPickingOut(false);
    // Displacing an open Custom panel runs its CANCEL semantics, not a bare
    // close: "Custom…" chosen but never Done'd would otherwise strand the
    // select on a choice with no rule and Save disabled for no visible
    // reason (Bugbot, PR #548).
    if (recurTimer.current !== null) window.clearTimeout(recurTimer.current);
    recurTimer.current = null;
    setRecurOut(false);
    setRecurOpen((open) => {
      if (open && !customRule)
        setRepeat((r) => (r === "custom" ? repeatBefore.current : r));
      return false;
    });
    setPicking(true);
  };
  const closeRecur = () => {
    if (recurOut) return;
    setRecurOut(true);
    recurTimer.current = window.setTimeout(() => {
      recurTimer.current = null;
      setRecurOpen(false);
      setRecurOut(false);
    }, 180);
  };
  const openRecur = () => {
    if (recurTimer.current !== null) window.clearTimeout(recurTimer.current);
    recurTimer.current = null;
    setRecurOut(false);
    if (pickerTimer.current !== null) window.clearTimeout(pickerTimer.current);
    pickerTimer.current = null;
    setPickingOut(false);
    setPicking(false);
    setRecurOpen(true);
  };
  // The Repeat tick. Unticking is the case worth being explicit about: it puts
  // the key back to "none" AND drops the custom rule, so the rule the form
  // submits really is gone rather than merely hidden — an armed rule behind an
  // unticked box would repeat a task nobody asked to repeat. The flag under it
  // goes with it, and an open recurrence panel is dismissed (it is asking about
  // a rule that no longer exists).
  const toggleRepeat = (on: boolean) => {
    const next = applyRepeatToggle(on, { repeat, customRule });
    setRepeat(next.repeat);
    setCustomRule(next.customRule);
    setRepeatOn(on);
    if (!on) {
      setNewTaskEachRun(false);
      if (recurOpen) closeRecur();
    }
  };
  const [home, setHome] = useState("");
  // The path field's recents dropdown: what this form remembers being used
  // (localStorage, first — the user's own picks outrank inference), padded
  // with the folders existing tasks point at.
  //
  // RE-READ every time the list opens, not once per modal: the store changes
  // through this very modal (Browse writes the folder you pick), so a
  // read-once state showed the list as it was BEFORE you went browsing —
  // "I just went through a bunch of folders but recents didn't update"
  // (Akshil, 2026-08-16). The read is a single localStorage hit on a user
  // gesture, so doing it per open costs nothing worth saving.
  const [recentsOpen, setRecentsOpen] = useState(false);
  const [recents, setRecents] = useState<string[]>([]);
  const readRecentList = useCallback(() => {
    const seen = new Set<string>();
    return [...readRecents(), ...(recentTargets ?? [])].filter((p) => {
      if (!p || seen.has(p)) return false;
      seen.add(p);
      return true;
    });
  }, [recentTargets]);
  const openRecents = () => {
    setRecents(readRecentList());
    setRecentsOpen(true);
  };

  // Early path validation (Akshil, 2026-08-16 — "detect it before me
  // scanning the input"): a beat after typing stops, ask the server whether
  // the path exists. A folder answers listDir directly; a FILE fails it, so
  // the parent is listed and the basename looked up — a file target is legal.
  // null = fine (or still checking); a string is the red line under the row.
  const [pathError, setPathError] = useState<string | null>(null);
  useEffect(() => {
    const p = target.trim();
    if (!p) {
      setPathError(null);
      return;
    }
    let stale = false;
    const timer = window.setTimeout(() => {
      listDir(p).then(
        () => {
          if (!stale) setPathError(null);
        },
        () => {
          const norm = normPath(p).replace(/\/+$/, "");
          const cut = norm.lastIndexOf("/");
          const parent = cut > 0 ? norm.slice(0, cut) : "/";
          const base = norm.slice(cut + 1);
          listDir(parent).then(
            (r) => {
              if (stale) return;
              setPathError(
                r.entries.some((e) => e.name === base)
                  ? null
                  : "This folder or file doesn't exist",
              );
            },
            () => {
              if (!stale) setPathError("This folder or file doesn't exist");
            },
          );
        },
      );
    }, 400);
    return () => {
      stale = true;
      window.clearTimeout(timer);
    };
  }, [target]);

  // The ask wears the title's clothes but grows like a note: with the text, up
  // to the CSS max-height (~5 lines), then scrolls. Measured on every change
  // because "auto then scrollHeight" is the one reflow-safe way to shrink back
  // when lines are deleted.
  const askRef = useRef<HTMLTextAreaElement>(null);
  const pathRef = useRef<HTMLInputElement>(null);
  // Escape-from-a-row hands focus back to the field WITHOUT reopening the
  // list it just dismissed — the input's onFocus otherwise undoes the close
  // in the same tick.
  const suppressOpen = useRef(false);
  // Whether the picker is closing because a folder was chosen (done — stay
  // closed) or backed out of (return to the recents dropdown). onPick runs
  // just before onClose, so a ref is enough to tell the two closes apart.
  const pickedFromBrowser = useRef(false);
  useEffect(() => {
    const el = askRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [message]);

  // The default target, filled once the server says where home is — and only
  // into a still-empty field, so it never clobbers an edit or the user's own
  // typing that raced the fetch. The BASELINE moves with it (setInitial): the
  // default is what the form opened with, not something the user typed, and
  // counting it as dirty armed the close-twice guard on a fresh untouched
  // modal — the "✕ intermittently does nothing" bug (QA 2026-08-14, second
  // sighting; the first was Edit's prefill).
  useEffect(() => {
    getConfig().then(
      (c) => {
        setHome(c.home);
        if (!editing) {
          const fallback = normPath(c.home) + DEFAULT_TARGET_SUFFIX;
          setTarget((prev) => (prev === "" ? fallback : prev));
          setInitial((prev) =>
            prev.target === "" ? { ...prev, target: fallback } : prev,
          );
        }
      },
      () => undefined,
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // What the form OPENED with. The chassis' dirty guard (close once, confirm
  // within 2s) must fire on "the user typed something", not "the fields are
  // non-empty" — an Edit modal opens full, and treating prefill as dirty made
  // its ✕ appear broken (QA 2026-08-14).
  const [initial, setInitial] = useState(() => ({
    // Same expressions as the states above, and they have to be: a prefill
    // the user did not type must not read as dirty, or the ✕ arms its
    // close-twice guard on an untouched modal (the bug the getConfig effect's
    // setInitial exists for — this is the same one, one prefill earlier).
    target: editing?.target ?? initialTarget ?? "",
    message: initialAsk,
    title,
    when,
    repeat,
    repeatOn,
    newTaskEachRun,
    customRule: JSON.stringify(customRule),
    permission,
  }));
  // EVERY field the form can lose arms the guard — time, repeat rule and
  // permission included. Comparing only text fields let a single ✕ silently
  // discard an adjusted schedule (Bugbot, PR #538).
  const dirty =
    target !== initial.target ||
    message !== initial.message ||
    title !== initial.title ||
    when !== initial.when ||
    repeat !== initial.repeat ||
    repeatOn !== initial.repeatOn ||
    newTaskEachRun !== initial.newTaskEachRun ||
    JSON.stringify(customRule) !== initial.customRule ||
    permission !== initial.permission;

  const picked = useMemo(() => new Date(when), [when]);
  const pickedOk = !Number.isNaN(picked.getTime());

  // Ids so the two refusals this form can print are ATTACHED to the controls
  // they refuse, not merely near them: a screen reader announcing the date chip
  // otherwise read a bare label with an unrelated red line somewhere below
  // (audit 2026-08-16).
  const pastHintId = useId();
  const pathErrorId = useId();
  // …and the third: what the repeat does to this task's thread, attached to
  // the checkbox that decides it.
  const threadHintId = useId();

  // The two when-dropdowns, and the time field's draft text (editable like
  // Google's: type "8:30pm" or pick from the list; an unparseable draft
  // falls back to what the field had).
  const [dateOpen, setDateOpen] = useState(false);
  const [timeOpen, setTimeOpen] = useState(false);
  const dateBtnRef = useRef<HTMLButtonElement>(null);
  const timeRef = useRef<HTMLInputElement>(null);
  const [timeText, setTimeText] = useState(() =>
    fmtTime(new Date(when).getHours() || 0, new Date(when).getMinutes() || 0),
  );

  const dateLabel = pickedOk
    ? `${DAYS[picked.getDay()]}, ${MONTHS[picked.getMonth()]} ${picked.getDate()}` +
      (picked.getFullYear() === new Date().getFullYear() ? "" : `, ${picked.getFullYear()}`)
    : "Pick a date";

  const setDatePart = (d: Date) => {
    const t = pickedOk ? picked : new Date();
    setWhen(toLocalInput(new Date(
      d.getFullYear(), d.getMonth(), d.getDate(), t.getHours(), t.getMinutes(),
    )));
  };
  const setTimePart = (h: number, m: number) => {
    const d = pickedOk ? picked : new Date();
    setWhen(toLocalInput(new Date(
      d.getFullYear(), d.getMonth(), d.getDate(), h, m,
    )));
    setTimeText(fmtTime(h, m));
  };
  const commitTimeText = () => {
    const parsed = parseTime(timeText);
    if (parsed) setTimePart(parsed.h, parsed.m);
    else if (pickedOk) setTimeText(fmtTime(picked.getHours(), picked.getMinutes()));
  };

  // The structured rule the current choice means; null for a one-off (and for
  // "cron", whose legacy line is submitted verbatim instead).
  const choices = useMemo(
    () => repeatChoicesFor(pickedOk ? picked : new Date()),
    [picked, pickedOk],
  );
  const rule: RecurrenceRule | null = useMemo(() => {
    // The checkbox is the outer gate: an unticked Repeat submits no rule, full
    // stop, whatever the (hidden) dropdown last said.
    if (!repeatOn) return null;
    if (repeat === "custom") return customRule;
    if (repeat === "cron" || repeat === "none") return null;
    return choices.find((c) => c.key === repeat)?.rule ?? null;
  }, [repeatOn, repeat, customRule, choices]);

  // Back to chat honours the SAME two-step dirty guard as the ✕ — one click
  // must not silently abandon an adjusted form just because the exit points
  // at the chat instead of nowhere (Bugbot, PR #548). First click re-labels
  // the button for 2s; the second within that window really leaves.
  const [backConfirm, setBackConfirm] = useState(false);
  const backTimer = useRef<number | null>(null);
  useEffect(
    () => () => {
      if (backTimer.current !== null) window.clearTimeout(backTimer.current);
    },
    [],
  );
  const backToChat = () => {
    if (!chatBack) return;
    if (dirty && !backConfirm) {
      setBackConfirm(true);
      if (backTimer.current !== null) window.clearTimeout(backTimer.current);
      backTimer.current = window.setTimeout(() => setBackConfirm(false), 2000);
      return;
    }
    navigateUrl(chatBack);
  };

  // The replacement was created but the original could not be withdrawn: the
  // one state where pressing Save again would mint a THIRD copy, so it
  // disables the button outright and the error says what to do by hand.
  const [replaced, setReplaced] = useState(false);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      // One pure function builds the whole body, so what actually goes on the
      // wire can be asserted without a DOM (new-task-form.test.ts). A rule
      // rides with its anchor, a legacy cron line replaces `due`, a CHAT's
      // session is continued only while the task stays a one-off (resuming one
      // conversation on every run compounds its context forever — Akshil,
      // 2026-08-16; Bugbot, PR #548), and the task's OWN thread is carried
      // through the re-create an edit really is.
      await scheduleMessage(
        buildSchedulePayload({
          target,
          message,
          title,
          when,
          rule,
          repeat,
          legacyCron,
          permission,
          // The two sources are kept APART here, because the payload treats
          // them oppositely: the task's OWN thread (learned, on the entry)
          // survives a repeat, a CHAT's does not. A one-off entry's stored id
          // travels in the chat slot — see learnedSessionOf — and still
          // outranks the deep link's, as it always did.
          sessionId: (!learnedSession && editing?.session_id) || chatSessionId || "",
          learnedSessionId: learnedSession,
          newTaskEachRun,
        }),
      );
      rememberRecent(target);
      if (editing) {
        // Replacement first, THEN withdraw — a failed create must never leave
        // the user with neither task. A 404 here is the fine race: the old
        // run fired (or was cancelled elsewhere) while the form was open.
        // Anything ELSE is the bad case — old and new both armed, an
        // unattended double-run — so it is said out loud instead of closed
        // over (the first cut swallowed every error here).
        try {
          await cancelScheduledMessage(editing.id);
        } catch (e) {
          if ((e as Error & { status?: number }).status !== 404) {
            onCreated();
            setReplaced(true);
            setBusy(false);
            setError(
              "The new task is saved, but the old one couldn't be withdrawn — " +
              "cancel it from the list so it doesn't also run.",
            );
            return;
          }
        }
      }
      onCreated();
      onClose();
    } catch (e) {
      // The server's 400s are written for a human (bad path, bad cron, past
      // due time) — show them verbatim rather than translating.
      setError((e as Error).message);
      setBusy(false);
    }
  };

  // A past time is no longer refused — by this form or by the server (design
  // §9): missed work is queued and runs when the app next opens, so picking
  // yesterday is a legitimate way to say "run this as soon as you can". What
  // is left is a NOTE saying so, and only for a one-off: for a rule the picked
  // time is the series' anchor — "Daily at 9am" saved in the afternoon starts
  // tomorrow, which is not "as soon as it can" — and cron never reads `due` at
  // all (Bugbot, PR #541).
  const dueIsPast = !repeatOn && pickedOk && picked.getTime() <= Date.now();

  const ready =
    !replaced &&
    message.trim() !== "" &&
    target.trim() !== "" &&
    pathError === null &&
    (repeatOn && repeat === "custom" ? customRule !== null : true) &&
    (repeat === "cron" ? legacyCron !== "" : pickedOk);

  return (
    <Modal
      title={editing ? "Edit task" : "New task"}
      onClose={onClose}
      busy={busy}
      width={460}
      dirty={dirty}
      footer={
        <>
          {/* The way back completes the chat's round trip: chat → schedule →
              adjust the draft → schedule again. Only shown when a chat sent
              us here — from anywhere else there is no "back". */}
          {chatBack && (
            <button type="button" className="btn btn-secondary schedule-back-chat"
                    disabled={busy}
                    onClick={backToChat}>
              {backConfirm ? "Discard changes?" : "Back to chat"}
            </button>
          )}
          <button type="button" className="btn btn-primary schedule-save"
                  disabled={busy || !ready} onClick={submit}>
            {busy ? "Saving…" : "Save"}
          </button>
        </>
      }
    >
      <div className="schedule-form">
        {/* The ask leads, wearing the title's clothes, and it is now the ONLY
            prose the form collects: what the user types here is what Claude is
            sent AND what the task is described as (Akshil, 2026-08-17 — three
            text fields for one thought did not make sense). The separate
            "Description (optional)" textarea that used to sit a row below is
            gone; Title, still optional, is the one field left under it.
            The placeholder stays "What should Claude do?": the field's job did
            not change — you type the instruction — and "Description" is what
            the server calls that text, not a second thing to write. */}
        <textarea
          ref={askRef}
          className="schedule-form-title"
          rows={2}
          aria-label="What should Claude do?"
          placeholder="What should Claude do?"
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          autoFocus
        />

        {/* Optional, and normally left alone: Claude Code writes its own
            one-line title into the transcript and the server prefers that,
            falling back to the first line of the message. The placeholder says
            so, because a blank field with a "Title" label reads as something
            you owe the form. */}
        <div className="schedule-form-line">
          {ICON_TITLE}
          <input
            type="text"
            className="field-control new-task-title"
            aria-label="Title"
            placeholder="Title — optional, filled in automatically"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
        </div>

        {/* The path is a combobox, Google-style: focusing it drops the last
            few folders the user scheduled against, with Browse as the
            dropdown's last row (Akshil, 2026-08-15 — the standalone Browse
            button next to the field moved in here). Blur closes it, but only
            when focus truly leaves the wrap — clicking a row moves focus INTO
            the dropdown, and closing on that blur would eat the click. */}
        <div className="schedule-form-line">
          {ICON_FOLDER}
          <div
            className="schedule-recents-wrap"
            onBlur={(e) => {
              if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
                setRecentsOpen(false);
              }
            }}
            // On the WRAP, not the input: a row reached by Tab is focusable
            // too, and Escape from it must dismiss the list, not bubble to
            // the modal's close handler (Bugbot, PR #541).
            onKeyDown={(e) => {
              if (e.key === "Escape" && recentsOpen) {
                e.stopPropagation();
                setRecentsOpen(false);
                if (document.activeElement !== pathRef.current) {
                  suppressOpen.current = true;
                  pathRef.current?.focus();
                }
              }
            }}
          >
            <input
              ref={pathRef}
              type="text"
              className={"field-control" + (pathError ? " is-invalid" : "")}
              aria-invalid={pathError !== null}
              aria-describedby={pathError ? pathErrorId : undefined}
              placeholder="Add folder or file"
              role="combobox"
              aria-expanded={recentsOpen}
              value={target}
              onFocus={() => {
                if (suppressOpen.current) {
                  suppressOpen.current = false;
                  return;
                }
                openRecents();
              }}
              onClick={openRecents}
              onChange={(e) => setTarget(e.target.value)}
            />
            {recentsOpen && (
              // mousedown preventDefault: keep focus ON the input while a row
              // is clicked. Safari never focuses <button> on click, so the
              // blur handler's relatedTarget is null there and the list would
              // unmount before its click fired (Bugbot, PR #541).
              <div
                className="schedule-recents"
                style={popStyle(pathRef.current, 240, true)}
                onMouseDown={(e) => e.preventDefault()}
              >
                {recents.slice(0, RECENTS_SHOWN).map((p) => (
                  <button
                    key={p}
                    type="button"
                    className="schedule-picker-row"
                    onClick={() => {
                      setTarget(p);
                      setRecentsOpen(false);
                    }}
                  >
                    {ICON_FOLDER} <span className="schedule-recents-path" title={p}>{p}</span>
                  </button>
                ))}
                <button
                  type="button"
                  className="schedule-picker-row schedule-recents-browse"
                  onClick={() => {
                    setRecentsOpen(false);
                    openPicker();
                  }}
                >
                  {/* The empty icon column, so the verb's label starts on the
                      same edge as every folder above it (audit 2026-08-16). */}
                  <span className="schedule-picker-gutter" aria-hidden="true" />
                  Browse…
                </button>
              </div>
            )}
          </div>
        </div>
        {pathError && (
          <span id={pathErrorId} className="field-hint schedule-form-bad schedule-form-sub"
                role="alert">
            {pathError}
          </span>
        )}
        {picking && (
          // Slides in BESIDE the card (position:fixed; the card shifts left
          // via the :has() rule in schedule.css) — inside the card it was
          // "too small to see anything" (Akshil, 2026-08-16).
          <ExplorerPanel
            start={target.trim() || home || "/"}
            onPick={(p) => {
              pickedFromBrowser.current = true;
              setTarget(p);
              rememberRecent(p);
            }}
            onClose={() => {
              closePicker();
              pickedFromBrowser.current = false;
            }}
            closing={pickingOut}
          />
        )}

        {/* Google's when-row, its controls included: a date field that drops
            a month grid and a time field that drops a 15-minute list (Akshil,
            2026-08-15). Both write into the single `when` string. */}
        <div className="schedule-form-line">
          {ICON_CLOCK}
          <div className="schedule-when">
            <div
              className="schedule-pop-wrap"
              onBlur={(e) => {
                if (!e.currentTarget.contains(e.relatedTarget as Node | null))
                  setDateOpen(false);
              }}
              // Escape dismisses the GRID, not the modal around it — same
              // contract as every other dropdown here.
              onKeyDown={(e) => {
                if (e.key === "Escape" && dateOpen) {
                  e.stopPropagation();
                  setDateOpen(false);
                }
              }}
            >
              <button ref={dateBtnRef} type="button"
                      className="schedule-when-field"
                      aria-describedby={dueIsPast ? pastHintId : undefined}
                      aria-expanded={dateOpen}
                      onClick={() => { setDateOpen((o) => !o); setTimeOpen(false); }}>
                {dateLabel}
              </button>
              {dateOpen && (
                <div className="schedule-pop" style={popStyle(dateBtnRef.current, 300)}
                     onMouseDown={(e) => e.preventDefault()}>
                  {/* No floor at all now. A past day is a one-off saying "run
                      this as soon as you can" (design §9), and for a rule it
                      was always legitimate — the date is the series' anchor,
                      and the server materializes from the next future run. */}
                  <MiniCalendar
                    selected={pickedOk ? picked : new Date()}
                    onPick={(d) => { setDatePart(d); setDateOpen(false); }}
                  />
                </div>
              )}
            </div>
            <div
              className="schedule-pop-wrap"
              onBlur={(e) => {
                if (!e.currentTarget.contains(e.relatedTarget as Node | null))
                  setTimeOpen(false);
              }}
            >
              <input
                ref={timeRef}
                type="text"
                className="schedule-when-field schedule-when-time"
                aria-describedby={dueIsPast ? pastHintId : undefined}
                aria-expanded={timeOpen}
                aria-label="Time"
                value={timeText}
                onFocus={(e) => { setTimeOpen(true); setDateOpen(false); e.target.select(); }}
                onChange={(e) => setTimeText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") { commitTimeText(); setTimeOpen(false); }
                  if (e.key === "Escape" && timeOpen) { e.stopPropagation(); setTimeOpen(false); }
                }}
                onBlur={commitTimeText}
              />
              {timeOpen && (
                <div className="schedule-pop schedule-pop--time"
                     style={popStyle(timeRef.current, 208)}
                     onMouseDown={(e) => e.preventDefault()}>
                  <TimeList
                    selected={{ h: pickedOk ? picked.getHours() : 9, m: pickedOk ? picked.getMinutes() : 0 }}
                    onPick={(h, m) => { setTimePart(h, m); setTimeOpen(false); }}
                  />
                </div>
              )}
            </div>
          </div>
          {/* Repeat is a tick on the when-row, not a dropdown that is always
              open (design §6): most tasks run once, and the menu they never
              use was the loudest thing under the time. Unticking clears the
              rule outright — see toggleRepeat. */}
          <CheckField
            className="new-task-check--repeat"
            label="Repeat"
            checked={repeatOn}
            onChange={toggleRepeat}
          />
        </div>
        {/* Not a refusal any more: past-due work is queued and runs when the
            app next opens (design §9), so this says what will happen instead
            of asking for a different answer. Still directly under the row it
            is about — printed after the repeat row it read as a complaint
            about the recurrence rule (audit 2026-08-16). */}
        {dueIsPast && (
          <span id={pastHintId} className="field-hint new-task-past schedule-form-sub"
                role="status">
            This time has passed — the task will run as soon as it can.
          </span>
        )}
        {repeatOn && (
        <>
        <div className="schedule-form-line schedule-form-line--sub">
          <Dropdown
            ariaLabel="Repeats"
            className="schedule-repeat"
            value={
              repeat === "custom" && customRule
                ? describeRule(customRule, pickedOk ? picked : new Date())
                : repeat === "cron"
                  ? describeRepeats(legacyCron)
                  : choices.find((c) => c.key === repeat)?.label ?? "Does not repeat"
            }
            options={[
              // "Does not repeat" is gone from the menu: the tick above IS
              // that answer now, and a dropdown that can contradict the
              // checkbox it hangs from is two controls for one question.
              ...choices
                .filter((c) => c.key !== "none")
                .map((c) =>
                  c.key === "custom" && repeat === "custom" && customRule
                    ? { key: "custom", label: describeRule(customRule, pickedOk ? picked : new Date()) }
                    : { key: c.key, label: c.label },
                ),
              // Legacy cron templates keep their line under a key of their
              // own — the form no longer writes cron, but editing one must
              // not silently rewrite the rule.
              ...(legacyCron ? [{ key: "cron", label: describeRepeats(legacyCron) }] : []),
            ]}
            onPick={(v) => {
              if (v === "custom") {
                // The dialog answers what "Custom…" means; the choice only
                // commits once Done says so. One side panel at a time — the
                // recurrence panel takes Browse's spot beside the card.
                repeatBefore.current = repeat;
                openRecur();
                setRepeat("custom");
              } else {
                setRepeat(v);
                // A non-custom pick makes an open recurrence panel moot.
                if (recurOpen) closeRecur();
              }
            }}
          />
          {/* A task IS a Claude session, so a repeating task sends every run
              into its own thread by construction — that is the default and
              needs no flag. This is the opt-OUT: tick it and each occurrence
              mints a fresh task, with a session and a TASK-nnn of its own
              (design §6). */}
          <CheckField
            label="New task each run"
            checked={newTaskEachRun}
            onChange={setNewTaskEachRun}
            describedBy={threadHintId}
          />
        </div>
        {/* The thread this repeat writes into, said out loud. It is the one
            thing about a repeating task that was invisible: a task IS a
            session, so every run lands in the same chat — and an edit that
            silently dropped that chat cost the user everything it had built
            with nothing on screen to notice. Editing a task that already has
            a thread says so in particular, because THAT is the sentence worth
            reading before you change anything. */}
        <span id={threadHintId} className="field-hint schedule-form-sub new-task-thread">
          {newTaskEachRun
            ? "Each run starts a new chat."
            : learnedSession
              ? "Every run adds to the chat this task has already started."
              : "Every run adds to the same chat."}
        </span>
        </>
        )}
        {recurOpen && (
          <CustomRecurrence
            initial={customRule}
            anchor={pickedOk ? picked : new Date()}
            onDone={(r) => {
              setCustomRule(r);
              closeRecur();
            }}
            onCancel={() => {
              closeRecur();
              // No rule was committed: fall back — but only if the select
              // still says "Custom…". The card stays live while the panel is
              // open, and a newer pick made meanwhile must not be wiped by
              // the panel's cancel (Bugbot, PR #548).
              if (!customRule)
                setRepeat((r) => (r === "custom" ? repeatBefore.current : r));
            }}
            closing={recurOut}
          />
        )}

        <details className="schedule-form-more">
          <summary>More options</summary>
          {/* Inside the same 26px icon gutter every other control hangs from —
              the details block was flush with the card edge, so the one control
              behind it was the only one in the form that did not line up
              (audit 2026-08-16). */}
          <div className="field schedule-form-sub">
            <span className="field-label">Permissions</span>
            <Dropdown
              ariaLabel="Permissions"
              value={permissionLabel(permission)}
              // The KEY is what gets submitted; the label is only how the mode
              // is said. The raw keys ("acceptEdits") were the server's
              // vocabulary printed at the user (audit 2026-08-16), and an
              // unknown key still shows itself rather than being hidden.
              options={(permissionModes.length ? permissionModes : ["auto"]).map((m) => ({
                key: m,
                label: permissionLabel(m),
              }))}
              onPick={setPermission}
            />
            <span className="field-hint">
              The task runs unattended. Auto approves safe actions and holds the rest.
            </span>
          </div>
        </details>

        {error && <ErrorBanner>{error}</ErrorBanner>}
      </div>
    </Modal>
  );
}
