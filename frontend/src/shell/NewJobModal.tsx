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
  getTasks,
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

// There was a lucide "type" glyph here — the serif T that led the Title row
// while Title was one of the form's quiet icon rows. Title is a prominent
// peer of the ask now and neither of the two takes an icon, so the glyph went
// with the row (see the form's markup for why).

// lucide "check", at tick scale.
const ICON_CHECK = (
  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor"
       strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <polyline points="20 6 9 17 4 12" />
  </svg>
);

// lucide "trash-2", at the footer buttons' glyph scale. Carries the destructive
// reading before the label is read, and stays through both press states so the
// button does not change shape under the cursor.
const ICON_TRASH = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
       strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <polyline points="3 6 5 6 21 6" />
    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
    <line x1="10" y1="11" x2="10" y2="17" />
    <line x1="14" y1="11" x2="14" y2="17" />
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
  // How often, as a list of units. `year` is NOT one of them any more (Akshil,
  // 2026-08-17 — the annual preset went with it), with one exception: a rule
  // that is ALREADY yearly keeps the row while it is being edited. That rule
  // opens here (keyOfRule finds no preset for a yearly freq, so the modal opens
  // it as Custom), and a unit dropdown that could not say "year" would show a
  // value with no matching row — and would turn any other edit on the panel,
  // the interval or the end date, into a silent change of frequency. Read off
  // `initial` rather than the live `freq` so the row does not vanish mid-edit.
  const units = initial?.freq === "year" ? LEGACY_RECUR_UNITS : RECUR_UNITS;
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
          options={units.map((u) => ({
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

// The custom panel's "repeat every N ___" units. Shortest first, the order
// recur.FREQUENCIES is written in — and without `year`, which is off the menu
// for anything new (see `units` in CustomRecurrence for the one exception, and
// repeatChoicesFor for the preset row that went with it).
const RECUR_UNITS: readonly RecurrenceRule["freq"][] = ["hour", "day", "week", "month"];
const LEGACY_RECUR_UNITS: readonly RecurrenceRule["freq"][] = [...RECUR_UNITS, "year"];

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

// ---- Deleting a task -----------------------------------------------------
// The one way to STOP a repeating task. Everything else on the page cancels an
// OCCURRENCE — the list's per-message cancel and the calendar popover's row
// cancel both mean "skip this run", deliberately, and a rule whose runs you
// skip one at a time keeps minting more forever (Akshil, 2026-08-17). The
// server has always been able to do it: `schedule.cancel` on a TEMPLATE id
// cancels the template AND its pending occurrence, which is exactly "no further
// runs". Nothing in the UI had ever called it with a template id.
//
// The modal is where it belongs because the modal is already the one place a
// template is addressable: an occurrence's Edit resolves `template_id ||
// entry_id` (Scheduled.editEntry), so opening "tomorrow's run" of a repeating
// task opens the RULE. The button just had to exist.
//
// What is cancellable is decided here rather than at the press, so a control
// that would 404 is never drawn: `sending` is deliberately not cancellable (the
// helper is away and the turn may have started — schedule.cancel's docstring),
// and a terminal entry (`sent`/`missed`/`error`/`cancelled`) has nothing left
// to stop. Only `pending` and `recurring` can be withdrawn.
export interface DeleteAction {
  // The id to cancel — a template's id when this is a rule, which is what
  // makes it stop the series rather than skip one run.
  id: string;
  // Whether cancelling ends a SERIES. Drives every sentence below, and the
  // reading of a 404.
  series: boolean;
  label: string;
  // The second press. It names the consequence rather than asking "are you
  // sure?", because the consequence is the whole difference between the two
  // cases and it is not undoable from this page.
  confirm: string;
  title: string;
}

export function deleteActionFor(entry?: ScheduledMessage | null): DeleteAction | null {
  if (!entry) return null;
  const series = entry.state === "recurring";
  if (!series && entry.state !== "pending") return null;
  return {
    id: entry.id,
    series,
    // One label for both cases — the user is deleting the task either way, and
    // a rule that called itself "Delete schedule" would read as a third noun
    // the page never uses. The difference is spelled out on the second press.
    label: "Delete task",
    confirm: series
      ? "Delete and stop all future runs?"
      : "Delete and cancel this run?",
    title: series
      ? "Deletes this task and stops all future runs. Runs it has already made are kept."
      : "Deletes this task. It will not run.",
  };
}

// What a press of that button decides, as a value rather than as a branch
// buried in a handler — so "the first press cannot reach the server" is a thing
// that can be asserted. `arm` carries no id at all; only the second press
// produces one.
export type DeletePress = { do: "arm" } | { do: "delete"; id: string };

export function deletePress(
  action: DeleteAction | null,
  armed: boolean,
): DeletePress | null {
  if (!action) return null;
  if (!armed) return { do: "arm" };
  return { do: "delete", id: action.id };
}

// What the error area says when the cancel does not land. A 404 is the honest
// race, not a failure: the run fired, or someone cancelled it in another tab —
// so it is translated instead of showing the server's id-bearing sentence,
// which reads as a bug. Every other status keeps the server's own words: those
// are written for a human (see the router's 400s).
export function deleteFailureText(err: unknown, series: boolean): string {
  const status = (err as { status?: number } | null)?.status;
  if (status === 404) {
    return series
      ? "This task is already stopped — nothing is scheduled to run from it any more."
      : "This task is already gone — it has run, or it was cancelled somewhere else.";
  }
  return (err as Error | null)?.message || "The task could not be deleted.";
}

// ---- Naming the task -----------------------------------------------------
// Title is REQUIRED (Akshil, 2026-08-17), and it opens prefilled wherever the
// app honestly knows a name — which is any path with a SESSION behind it (see
// the precedence below). Where it does not, the field opens blank and the
// requirement is what asks for a name. That is the trade, stated plainly: a
// blank required field costs the user one line of typing, while a field
// prefilled with a guess costs them a task named after its own description.
//
// The placeholder is left with only the job a placeholder can honestly do —
// saying which field this is. It used to say "optional, filled in
// automatically", and it used to double as a PREVIEW of the chat's own name;
// both went with the requirement. A previewed name that Save then refuses to
// accept is the worst of the three states.
export const TITLE_PLACEHOLDER = "Title";

// One line of a block of prose, trimmed. Used to reduce a multi-line value to
// something an <input> can hold — it would strip the newlines anyway. It also
// used to answer "is this string the message I am about to send?" for
// sessionTitleOf; that question is the server's now, and answered by provenance
// rather than by comparing strings.
export function firstLine(text: string): string {
  return text.trim().split("\n")[0]?.trim() ?? "";
}

// -- The title names the SESSION, never the message ---------------------------
// The bug this replaced (Akshil, 2026-08-17): scheduling from a Claude chat
// prefilled Title with `firstLine(ask)` — the very message being scheduled — so
// a long message came out duplicated, once as the title and once as the
// description. "The description is what we type in the chat box"; the title is
// what the CONVERSATION is called.
//
// So the precedence is now, in order:
//   1. the task's own stored title, if a user ever set one;
//   2. the SESSION's resolved title — Claude Code's `ai-title` record, which is
//      the "cloud summarised it" case, served on /api/tasks as `title` with
//      `title_source: "ai"`;
//   3. the session's FIRST user message, shortened — "the first message that we
//      had". Also /api/tasks, as `title_source: "message"`: the first line of the
//      transcript's first user prompt (tasks.py `_title` reading
//      `tasks_store.head`). Its sibling `title_source: "entry"` — a row named
//      from a message merely SCHEDULED at the session, because the transcript
//      could not be read — is not a step here at all; see sessionTitleOf;
//   4. nothing. The field opens blank and the user types a name.
// Never the composed message, at any step. Steps 2 and 3 need a fetch, so they
// live in the /api/tasks effect; 1 and 4 are what `initialTitleOf` decides
// synchronously.

// How long a title derived from a first message is allowed to be. A name, not a
// summary: this is the whole point of step 3 — a 200-char first line (the
// server's own cap) is the duplication bug again in a longer field.
export const TITLE_MAX = 60;

// A first message reduced to a name: one line, clamped, cut on a word boundary.
// No ellipsis — the field is a NAME the user can edit, and "…" is punctuation
// they would have to delete. A single word longer than the clamp has no boundary
// to cut on, so it is cut hard; that is the only mid-word cut here.
export function shortTitle(text: string, max = TITLE_MAX): string {
  const line = firstLine(text);
  if (line.length <= max) return line;
  // max + 1 so a value whose max'th character is the space gets the whole word
  // before it rather than losing it.
  const boundary = line.slice(0, max + 1).lastIndexOf(" ");
  return (boundary > 0 ? line.slice(0, boundary) : line.slice(0, max)).trimEnd();
}

// What Title OPENS on, synchronously. Only step 1 and step 4: a stored title
// wins outright (an edit that quietly replaced it would be data loss), and
// otherwise the field is BLANK until the /api/tasks lookup answers.
//
// It used to derive `firstLine(initialAskOf(...))` here, which is what put the
// scheduled message in the title. Blank is the honest synchronous answer instead
// — the form has nothing to say about the session yet — and blank is safe even
// though Title is required: the requirement bites at Save, by which time either
// the lookup has filled the field or the user has.
export function initialTitleOf(entry?: ScheduledMessage | null): string {
  return (entry?.title ?? "").trim();
}

// Steps 2 and 3, which only /api/tasks can answer: the name the session this
// form was opened from already carries. A session IS a task there, so the row
// keyed on it has both the resolved `title` and the `title_source` saying which
// branch produced it — and that provenance is the whole reason this reads the
// API instead of a string in the deep link.
//
// The server's step 3 has TWO sources and only one of them is a name, so the
// row says which it read (tasks.py `_title`):
//
//   * `message` — the session's own first prompt, out of the transcript. Step 3
//     itself, "the first message that we had", and taken as a name (shortened).
//   * `entry` — no readable transcript, so the row is named from the earliest
//     message SCHEDULED at that session. On a task made in this form that is the
//     ask itself, which is the duplication bug arriving by way of the server, so
//     it is refused and Title stays blank for the user to fill.
//
// This used to be one value, and the composed ask was passed in so the client
// could GUESS which of the two it had: a `message` title was dropped whenever
// the ask's first line began with it. A guess cannot tell an echo from a
// continuation — "pull today's news and file it" begins with the session's real
// first prompt "pull today's news" — so a session lost the name the app already
// knew and Save sat disabled until the user retyped it. The server knows the
// answer for certain, so it says it, and no draft is an input here at all.
//
// Steps 1 and 2 are taken verbatim — a name a human typed and a name Claude
// wrote are both already names, and shortening either would edit someone's
// words. Only step 3 is a message being reduced to one.
export function sessionTitleOf(
  tasks: readonly { session_id: string; title: string; title_source: string }[],
  sessionId: string,
): string {
  if (!sessionId) return "";
  const task = tasks.find((t) => t.session_id === sessionId);
  const title = (task?.title ?? "").trim();
  if (!title) return "";
  if (task?.title_source === "entry") return "";
  if (task?.title_source === "message") return shortTitle(title);
  return title;
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

// -- What a time already gone actually MEANS ---------------------------------
// A past time is not refused, by this form or by the server (design §9). What
// the form owes instead is a sentence naming which of the TWO things will
// happen, because a one-off and a rule answer differently:
//
//   * one-off — the queue sorts it to the head and sends it (SCH-3b). Once.
//   * rule    — SCH-13b. A rule template with nothing materialized yet walks
//     anchor → now and creates ONE occurrence, on the latest slot at or before
//     now, marked `catch_up`; it is overdue the instant it exists, so it goes
//     on the next tick. Every slot it stepped past is never materialized and
//     never runs — the same collapse `_coalesce` applies to a backlog
//     (SCH-13) — so an anchor a year back is still exactly one run, not a
//     year of them. The series then continues from now in the ordinary way.
//
// Two sentences rather than one, because "runs as soon as it can" is a promise
// a repeat does not keep: it runs once now AND then keeps its pattern.
export const PAST_NOTE_ONE_OFF =
  "This time has passed — the task will run as soon as it can.";
export const PAST_NOTE_CATCH_UP =
  "This time has passed — one catch-up run goes now, then the task keeps to "
  + "its schedule. Just the one, however many have gone by.";

// The series' FIRST slot, given the picked date as its anchor — null once the
// rule's `until` has already cut the series off before it began.
//
// Mirrors recur._walk's opening step, and for four of the five frequencies
// there is nothing to mirror: hourly, daily, monthly and annually all include
// the anchor itself (a monthly nth-weekday reads "the second Wednesday" OFF
// the anchor, so the anchor's own month always has it; a Feb 29 anchor is in a
// leap year by construction). Only a WEEKLY rule can start later than its
// anchor, and only because the chosen days are free of it: the anchor's week
// is a partial one (`when >= anchor` in _walk_week), so a Tuesday anchor with
// only Thursday ticked starts on that Thursday, and a Tuesday anchor with only
// Monday ticked starts `interval` weeks on.
export function firstRuleSlot(rule: RecurrenceRule, anchor: Date): Date | null {
  let first = anchor;
  if (rule.freq === "week" && rule.byday?.length) {
    const days = [...rule.byday].sort((a, b) => a - b);
    // Sunday-anchored blocks, counted from the anchor's OWN week — the unit
    // that repeats is the week, not "7·interval days from each run".
    const sunday = anchor.getDate() - anchor.getDay();
    const slot = (day: number, weeks: number) =>
      new Date(anchor.getFullYear(), anchor.getMonth(), sunday + day + weeks * 7,
               anchor.getHours(), anchor.getMinutes());
    const thisWeek = days
      .map((d) => slot(d, 0))
      .find((d) => d.getTime() >= anchor.getTime());
    first = thisWeek ?? slot(days[0], rule.interval ?? 1);
  }
  if (rule.until) {
    // INCLUSIVE, and compared on the DATE, so the time of day cannot decide
    // it — recur._walk's rule exactly.
    const [y, m, d] = rule.until.split("-").map(Number);
    if (first.getTime() > new Date(y, m - 1, d, 23, 59, 59, 999).getTime())
      return null;
  }
  return first;
}

// The note the when-row prints, or null for silence. Silence is the answer for
// a future time, and also for the two repeats with no anchor to catch up FROM:
// a legacy cron template (`create` computes its first run from now by
// construction, and cron never reads `due` at all — Bugbot, PR #541) and a
// half-finished Custom, which Save refuses anyway. Never a refusal: it does
// not touch `ready`, because "start this pattern, and run the one I missed" is
// a legitimate thing to ask for.
export function pastNoteFor(
  picked: Date | null,
  repeatOn: boolean,
  rule: RecurrenceRule | null,
  now: Date,
): string | null {
  if (!picked || Number.isNaN(picked.getTime())) return null;
  if (picked.getTime() > now.getTime()) return null;
  if (!repeatOn) return PAST_NOTE_ONE_OFF;
  if (!rule) return null;
  const first = firstRuleSlot(rule, picked);
  return first !== null && first.getTime() <= now.getTime()
    ? PAST_NOTE_CATCH_UP
    : null;
}

// The body POSTed to /api/schedule — api.ts's own parameter type, nothing
// added to it. That type models `title`, `description` and `new_task_each_run`
// itself, so this alias only names what the builder returns.
export type SchedulePayload = Parameters<typeof scheduleMessage>[0];

export function buildSchedulePayload(form: {
  target: string;
  // The SECOND field on the card and the required one: what Claude is sent,
  // and — same text, no third field — the task's description (Akshil,
  // 2026-08-17: "the big field is the description, that is the first message").
  // It rides the wire twice, once as each, because the server stores them as
  // two things and a task page that showed nothing under a task would be the
  // only alternative. Never blank by the time it gets here: `saveEnabled`
  // refuses Save on an empty one.
  message: string;
  // The FIRST field on the card, and REQUIRED as of 2026-08-17: `saveEnabled`
  // refuses Save on a blank one, so what reaches here is a name a human accepted
  // or typed. The wire contract is unchanged — the server still names an untitled
  // task from the transcript's `ai-title` (design §4) — so the empty branch below
  // stays as the honest fallback for a caller this form's gate never saw.
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
    // sent as "". Neither branch is reachable from the form any more: Save
    // refuses a blank on either field. They stay because the wire contract still
    // allows both to be absent, and a builder that sent "" would be lying.
    ...(trimmedTitle ? { title: trimmedTitle } : {}),
    ...(trimmedDescription ? { description: trimmedDescription } : {}),
    // Only ever sent on a repeating task: on a one-off there is no "each run"
    // for it to mean anything about.
    ...(repeating && form.newTaskEachRun ? { new_task_each_run: true } : {}),
  };
}

// WHAT SAVE REFUSES. Pulled out of the component (it was an inline `ready`
// expression) so the rules are assertable: BOTH prose fields are required — the
// description because it is the text Claude is actually sent and a task with
// nothing to do is not a task, and Title because a task nobody can name in a
// list is not much better (Akshil, 2026-08-17).
//
// Title being required is not softened by the field sometimes opening blank —
// that is the point of the requirement rather than a hole in it. The form fills
// the field from the session wherever a session has a name (initialTitleOf plus
// the /api/tasks lookup behind it); where nothing honest is available it asks,
// which is strictly better than the alternative it replaced — naming the task
// after the message it is scheduling.
//
// Both use `.trim()`, because a textarea full of newlines is not an instruction
// and a title of spaces is not a name. Save is `disabled` on a false — nothing
// here is a submit handler that could be reached another way — and both fields
// carry `aria-required` so the disabled button is not the only hint.
export function saveEnabled(f: {
  // The ask / description. Required.
  message: string;
  // The task's name. Required, and prefilled rather than asked for.
  title: string;
  // Where it runs. Required, and the async existence check must not be failing.
  target: string;
  pathError: string | null;
  // A "custom" repeat is only a choice once the recurrence dialog produced a
  // rule; a legacy cron template needs its line; everything else needs a
  // parseable date-time.
  repeatOn: boolean;
  repeat: string;
  customRule: RecurrenceRule | null;
  legacyCron: string;
  pickedOk: boolean;
  // The entry has already been re-created by this modal — saving twice would
  // schedule it twice.
  replaced: boolean;
}): boolean {
  return (
    !f.replaced &&
    f.message.trim() !== "" &&
    f.title.trim() !== "" &&
    f.target.trim() !== "" &&
    f.pathError === null &&
    (f.repeatOn && f.repeat === "custom" ? f.customRule !== null : true) &&
    (f.repeat === "cron" ? f.legacyCron !== "" : f.pickedOk)
  );
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
  // The FIRST field on the card, and REQUIRED (Akshil, 2026-08-17).
  // `initialTitleOf` is only the synchronous half of the precedence: a stored
  // title, else blank. The two SESSION steps — the thread's `ai-title`, then its
  // first user message — need a fetch and land in the /api/tasks effect below.
  // Blank on the first paint is deliberate now: the alternative was deriving a
  // name from the ask, which is exactly how a long scheduled message ended up
  // duplicated into the title.
  //
  // Held in a const for the same reason `initialAsk` is: the BASELINE (`initial`)
  // has to be the identical value or an untouched Edit reads as dirty.
  const derivedTitle = initialTitleOf(editing);
  const [title, setTitle] = useState(derivedTitle);
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

  // The ask shares Title's borderless surface but not its face, and it grows
  // like a note: with the text, from the CSS floor (`.new-task-ask`'s
  // min-height, which is what hands it the card's slack) up to the CSS
  // max-height, then it scrolls. Both clamps hold against the inline height set
  // here — min-/max-height bound the used value whatever `style.height` says —
  // so this measuring only ever picks the size BETWEEN them. It is the whole
  // difference between the two fields: Title is an <input> and has no height to
  // measure. Measured on every change because "auto then scrollHeight" is the
  // one reflow-safe way to shrink back when lines are deleted.
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

  // Ids so the lines this form prints are ATTACHED to the controls they are
  // about, not merely near them: a screen reader announcing the date chip
  // otherwise read a bare label with an unrelated line somewhere below (audit
  // 2026-08-16). Only pathError is a refusal; the past-time note states a
  // consequence and never blocks Save.
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

  // ---- The session's own name, prefilled into Title ----------------------
  // Steps 2 and 3 of the precedence (see sessionTitleOf): what the CONVERSATION
  // is called — its `ai-title`, else its first user message — never the message
  // being scheduled.
  //
  // Sourced from /api/tasks rather than from the deep link or a new endpoint:
  // a session IS a task there (tasks.py `_collect`), so the row keyed on it
  // already carries the resolved title AND `title_source`, which is what says
  // WHICH of the two steps produced it — and the first user message is only
  // reachable that way, since the server resolves it (`tasks_store.head`) but
  // does not put it on the wire under a name of its own. The alternative —
  // having the chat template put its title in the URL beside `message`,
  // `target`, `session_id` and `back` — would hand us a string with no
  // provenance, and one that is stale from the moment the link is built.
  //
  // BOTH paths that have a session use it, and for the same reason: the chat
  // this form was deep-linked from (?new=1&session_id=…), and whatever session
  // an edited entry carries — the thread it learned, or the chat it was
  // scheduled from. The provenance that matters elsewhere (learnedSessionOf)
  // does not matter here: either way that conversation is where this task's name
  // comes from, and an untitled task is exactly the case where `ai-title` is the
  // only name that exists. Refusing to look it up would leave the user retyping
  // a name the app already knows.
  //
  // It only ever replaces the SYNCHRONOUS title (usually the empty string),
  // never a typed one and never a stored one — same discipline as the getConfig
  // effect above, and for the same bug: `initial` moves with it, because a value
  // the user did not type must not read as dirty and arm the close-twice guard.
  // That pairing matters more now that the field starts blank: without it, every
  // form opened from a chat would arrive already dirty and its ✕ would need two
  // presses before the user had touched anything.
  const nameSession = (editing?.session_id || chatSessionId) ?? "";
  useEffect(() => {
    // A stored title is the top of the precedence — nothing may outrank it.
    if ((editing?.title ?? "").trim() || !nameSession) return;
    let alive = true;
    getTasks()
      .then(({ tasks }) => {
        const resolved = sessionTitleOf(tasks, nameSession);
        if (!alive || !resolved) return;
        setTitle((prev) => (prev === derivedTitle ? resolved : prev));
        setInitial((prev) =>
          prev.title === derivedTitle ? { ...prev, title: resolved } : prev,
        );
      })
      // A failed lookup is not worth reporting: the field is either already
      // showing a stored title or blank with Save asking for one, and neither
      // state is improved by an error banner about a name.
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [editing, nameSession, derivedTitle]);

  // ---- Delete ------------------------------------------------------------
  // Only when EDITING, and only for something the server will actually
  // withdraw — see deleteActionFor. null means no button at all, which is the
  // refusal: a control that 404s on press is worse than no control.
  const del = deleteActionFor(editing);
  // The same two-press idiom the ✕ and Back to chat use, for the same reason
  // and with the same 2s window — except the second label names the
  // CONSEQUENCE rather than asking, because stopping a series is not undoable
  // from this page and "Are you sure?" is not what the user needs to read.
  const [delConfirm, setDelConfirm] = useState(false);
  const delTimer = useRef<number | null>(null);
  useEffect(
    () => () => {
      if (delTimer.current !== null) window.clearTimeout(delTimer.current);
    },
    [],
  );
  const remove = async () => {
    const press = deletePress(del, delConfirm);
    if (press === null || del === null) return;
    if (press.do === "arm") {
      setDelConfirm(true);
      if (delTimer.current !== null) window.clearTimeout(delTimer.current);
      delTimer.current = window.setTimeout(() => setDelConfirm(false), 2000);
      return;
    }
    if (delTimer.current !== null) window.clearTimeout(delTimer.current);
    setDelConfirm(false);
    setBusy(true);
    setError(null);
    try {
      // A TEMPLATE id here is the whole point: the server cancels the rule AND
      // its materialized next run, which is what "stop this recurring job"
      // means. An occurrence id would only skip one run — which the list and
      // the calendar popover already offer, and which is the opposite thing.
      await cancelScheduledMessage(press.id);
      // `onCreated` is the page's "something changed, re-read" callback
      // (Scheduled passes `reload`), and a delete is exactly that. A separate
      // `onDeleted` prop would read better in isolation but would need the
      // parent to pass it, and the reload it would trigger is the identical
      // one — so this reuses the callback rather than growing the contract.
      onCreated();
      onClose();
    } catch (e) {
      // A 404 is not a failure: the entry really is gone, so the page is
      // re-read (its row must not linger) and the modal stays open only long
      // enough to say why nothing happened.
      if ((e as { status?: number }).status === 404) onCreated();
      setBusy(false);
      setError(deleteFailureText(e, del.series));
    }
  };

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
  // is left is a NOTE saying which of the two things happens.
  //
  // It used to be scoped to a one-off, on the reasoning that a rule's picked
  // time is only the series' ANCHOR — it sets the pattern and nothing runs
  // until the next future slot, so "as soon as it can" would have been a lie.
  // SCH-13b ended that: a past-anchored rule now materializes a catch-up on
  // its latest past slot and fires on the next tick, so a repeat kept silent
  // here fired with nothing on the form saying so (Bugbot, PR #555). The
  // anchor's pattern role is untouched — a monthly rule anchored on a past
  // second Wednesday still means the second Wednesday — which is exactly why
  // the two wordings differ rather than one covering both. See pastNoteFor.
  const pastNote = pastNoteFor(pickedOk ? picked : null, repeatOn, rule, new Date());

  // See saveEnabled: both prose fields are required now, Title included.
  const ready = saveEnabled({
    message,
    title,
    target,
    pathError,
    repeatOn,
    repeat,
    customRule,
    legacyCron,
    pickedOk,
    replaced,
  });

  return (
    <Modal
      title={editing ? "Edit task" : "New task"}
      onClose={onClose}
      busy={busy}
      width={460}
      dirty={dirty}
      footer={
        <>
          {/* Destructive, so it sits at the far left of the footer, away from
              Save — `.btn-danger-text` carries the margin-right:auto that
              anchors it there. Present only on an Edit, and only when the
              entry is actually withdrawable. `type="button"`, like every
              control in this footer: the form has no submit, so Enter never
              reaches it. */}
          {del && (
            <button
              type="button"
              className={"btn btn-danger-text new-task-delete"
                + (delConfirm ? " is-armed" : "")}
              title={del.title}
              disabled={busy}
              onClick={remove}
            >
              {ICON_TRASH}
              {delConfirm ? del.confirm : del.label}
            </button>
          )}
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
        {/* ONE WRITING SURFACE, not two controls (Akshil, 2026-08-17, reference
            image): the title and the description share a single borderless
            area running from the header rule to the rows below it, the title
            set large and the description quieter beneath it. Neither field
            looks like an input — no border, no underline, no filled box — so
            the top of the card reads as a document you type into rather than a
            form with two fields in it. The wrapper is what makes that ONE
            surface: it owns the block's vertical space and hands the slack to
            the description, so a two-word entry does not leave the card
            top-heavy. See `.new-task-write` in new-task.css for the whole
            treatment, including what focus looks like when there is no box to
            recolour.

            TITLE FIRST, and the prominent one (Akshil, 2026-08-17: "title will
            be the first field and then description will be the second field").
            The folder and the time are facts ABOUT the task and stay quiet
            beneath in the 26px icon gutter; neither of these two carries a
            leading icon, because a 14px glyph beside a 20px face read as
            debris and the gutter is what says "this is a detail".

            REQUIRED, and prefilled from the SESSION where there is one to read
            (Akshil, 2026-08-17): its `ai-title`, else its first user message —
            see sessionTitleOf for the whole precedence and for what it refuses.
            What it will never be again is the first line of the ask: that named
            every chat-scheduled task after the message it was scheduling, so a
            long message arrived duplicated into both fields. Opened from the New
            task button, or from a session with nothing to say for itself, the
            field is blank and the requirement is what asks for a name —
            `aria-required` says so rather than leaving a disabled Save as the
            only hint.

            An <input>, not a textarea, and that is the whole overflow answer:
            one line that never wraps and never grows. Long text scrolls
            horizontally under the caret while the field has focus and ellipses
            when it does not — see `.new-task-title` in new-task.css. */}
        <div className="new-task-write">
          <input
            type="text"
            className="new-task-field new-task-title"
            aria-label="Title"
            aria-required="true"
            placeholder={TITLE_PLACEHOLDER}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            autoFocus
          />

          {/* …and the ask SECOND, still the only other prose the form collects:
              what the user types here is what Claude is sent AND what the task
              is described as (design §4 — three text fields for one thought did
              not make sense, so `description` and `message` are one field).
              REQUIRED, as Title above now is, and for a sharper reason: an empty
              one is a task with nothing to do. `ready` refuses Save on it, and
              aria-required says so to a screen reader rather than leaving a
              disabled button as the only hint. Unlike Title there is nothing
              honest to prefill it from, so this is the one field the user really
              does have to write.

              Quieter and smaller than the title, and it keeps the growth Title
              deliberately does not have: multi-line, autogrowing with the text
              from the floor `.new-task-ask` sets up to its max-height, then
              scrolling. */}
          <textarea
            ref={askRef}
            className="new-task-field new-task-ask"
            rows={2}
            aria-label="What should Claude do?"
            aria-required="true"
            placeholder="What should Claude do?"
            value={message}
            onChange={(e) => setMessage(e.target.value)}
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
                      aria-describedby={pastNote ? pastHintId : undefined}
                      aria-expanded={dateOpen}
                      onClick={() => { setDateOpen((o) => !o); setTimeOpen(false); }}>
                {dateLabel}
              </button>
              {dateOpen && (
                <div className="schedule-pop" style={popStyle(dateBtnRef.current, 300)}
                     onMouseDown={(e) => e.preventDefault()}>
                  {/* No floor at all now. A past day is a one-off saying "run
                      this as soon as you can" (design §9), and for a rule it
                      is legitimate too — it says "start this pattern, and run
                      the one I missed". The date is still the series' ANCHOR,
                      which is what makes "monthly on the second Wednesday"
                      expressible by picking a past second Wednesday; what
                      changed is that the server no longer waits for the next
                      future slot to materialize from. It catches up on the
                      latest past one first (SCH-13b), which is why picking a
                      past day under a standing Repeat prints a note of its own
                      rather than nothing. */}
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
                aria-describedby={pastNote ? pastHintId : undefined}
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
            of asking for a different answer. WHICH of the two things it says is
            pastNoteFor's decision — a repeat's past anchor gets its own
            sentence, because SCH-13b makes it one catch-up run and then the
            pattern, not "as soon as it can" full stop.

            One element for both wordings, so it keeps the id the date and time
            fields point `aria-describedby` at, and stays directly under the row
            it is about: printed after the repeat row it read as a complaint
            about the recurrence rule (audit 2026-08-16). `role="status"` earns
            its keep twice over now — ticking Repeat rewrites this line in
            place, and a silent swap is the one thing worse than no line. */}
        {pastNote && (
          <span id={pastHintId} className="field-hint new-task-past schedule-form-sub"
                role="status">
            {pastNote}
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
