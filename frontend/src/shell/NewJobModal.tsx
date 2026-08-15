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
import { useEffect, useMemo, useRef, useState } from "react";
import { Modal } from "@platform/ui/modal/Modal";
import {
  cancelScheduledMessage,
  getConfig,
  listDir,
  scheduleMessage,
} from "@platform/lib/api";
import type { RecurrenceRule, ScheduledMessage } from "@platform/lib/api";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { describeRepeats, describeRule, repeatChoicesFor } from "./schedule-lib";
import { ICON_CLOCK, ICON_FOLDER } from "./ScheduleCalendar";

// Where a new task points before the user says otherwise: ~/Desktop/fused
// (Akshil, 2026-08-14 — an empty path field was the confusing part of the
// form). Home-relative so it composes on any machine; the picker makes
// changing it a click, and a machine without the folder gets the server's
// clear 400 naming the path.
const DEFAULT_TARGET_SUFFIX = "/Desktop/fused";

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

// ---- Folder picker -----------------------------------------------------------
// A small in-modal directory browser: descend by clicking, one Up control,
// "Use this folder" hands the current path back. Deliberately folders-only —
// scheduling against a file is still possible by typing, but the picker's job
// is the common case, and mixing files in doubled the list for nothing.

// Forward slashes throughout, including for Windows drive paths — the same
// normalization every other shell caller applies to `/api/config` values,
// whose `home` is a raw expanduser and arrives with backslashes there. The
// server accepts either separator; the PICKER's own string surgery (up(),
// joins) only understands one.
const normPath = (p: string) => p.replace(/\\/g, "/");


// The path as clickable crumbs: every ancestor is one tap away, which is what
// the old single "up" chevron made people hunt for (Akshil, 2026-08-15 — "not
// intuitive"). Root renders as "/" (or "C:/"), each segment jumps there.
function crumbsOf(path: string): { name: string; path: string }[] {
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

function FolderPicker({
  start,
  onPick,
  onClose,
}: {
  start: string;
  onPick: (path: string) => void;
  onClose: () => void;
}) {
  const [path, setPath] = useState(normPath(start));
  const [dirs, setDirs] = useState<string[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Type-to-narrow, cleared on every navigation — a filter that survives into
  // the next folder reads as "this folder is empty".
  const [filter, setFilter] = useState("");

  useEffect(() => {
    let stale = false;
    // The OLD listing stays up, dimmed, while the next one loads. Blanking it
    // collapsed the panel to one "Loading…" line and re-expanded it a beat
    // later, so every click made the whole modal pump (QA 2026-08-14,
    // "glitches in and out"). A local listing resolves in milliseconds — the
    // dim is usually invisible; it exists for the slow (network-mounted) case.
    setLoading(true);
    setError(null);
    listDir(path).then(
      (r) => {
        if (stale) return;
        setDirs(
          r.entries
            .filter((e) => e.is_dir && !e.name.startsWith("."))
            .map((e) => e.name)
            .sort((a, b) => a.localeCompare(b)),
        );
        setLoading(false);
      },
      (e: Error) => {
        if (stale) return;
        setError(e.message);
        setLoading(false);
      },
    );
    return () => {
      stale = true;
    };
  }, [path]);

  const go = (p: string) => {
    setPath(p);
    setFilter("");
  };
  const crumbs = crumbsOf(path);
  const shown = dirs?.filter((n) =>
    n.toLowerCase().includes(filter.trim().toLowerCase()),
  );

  return (
    <div className="schedule-picker">
      <div className="schedule-picker-crumbs" aria-label="Current folder">
        {crumbs.map((c, i) => (
          <span key={c.path} className="schedule-picker-crumb-seg">
            {i > 0 && <span className="schedule-picker-crumb-sep">/</span>}
            <button type="button" className="schedule-picker-crumb"
                    disabled={i === crumbs.length - 1}
                    title={c.path}
                    onClick={() => go(c.path)}>
              {c.name}
            </button>
          </span>
        ))}
      </div>
      <input
        type="text"
        className="field-control schedule-picker-filter"
        placeholder="Filter folders"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      />
      <div className={"schedule-picker-list" + (loading ? " is-loading" : "")}>
        {error && <p className="schedule-card-why">{error}</p>}
        {!error && shown?.length === 0 && !loading && (
          <p className="schedule-card-why">
            {filter ? "No folders match" : "No subfolders"}
          </p>
        )}
        {!error && shown?.map((name) => (
          <button key={name} type="button" className="schedule-picker-row"
                  disabled={loading} title={name}
                  onClick={() => go(path.replace(/\/+$/, "") + "/" + name)}>
            {ICON_FOLDER} <span className="schedule-picker-name">{name}</span>
            <span className="schedule-picker-enter" aria-hidden="true">›</span>
          </button>
        ))}
      </div>
      <div className="schedule-picker-foot">
        {/* "Back", not "Cancel": the picker is a level below the recents
            dropdown, and this returns there (Akshil, 2026-08-15). */}
        <button type="button" className="btn btn-secondary" onClick={onClose}>
          Back
        </button>
        <button type="button" className="btn btn-primary"
                onClick={() => { onPick(path); onClose(); }}>
          Use this folder
        </button>
      </div>
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
}: {
  selected: Date;
  onPick: (d: Date) => void;
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

  return (
    <div className="schedule-mini-cal">
      <div className="schedule-mini-cal-head">
        <span className="schedule-mini-cal-title">
          {MONTHS[view.getMonth()]} {view.getFullYear()}
        </span>
        <button type="button" className="schedule-mini-cal-nav" aria-label="Previous month"
                onClick={() => setView(new Date(view.getFullYear(), view.getMonth() - 1, 1))}>
          ‹
        </button>
        <button type="button" className="schedule-mini-cal-nav" aria-label="Next month"
                onClick={() => setView(new Date(view.getFullYear(), view.getMonth() + 1, 1))}>
          ›
        </button>
      </div>
      <div className="schedule-mini-cal-grid">
        {["S", "M", "T", "W", "T", "F", "S"].map((d, i) => (
          <span key={i} className="schedule-mini-cal-dow">{d}</span>
        ))}
        {cells.map((day, i) =>
          day === null ? (
            <span key={`b${i}`} />
          ) : (
            <button
              key={day}
              type="button"
              className={
                "schedule-mini-cal-day" +
                (same(selected, view.getFullYear(), view.getMonth(), day) ? " is-selected" : "") +
                (same(today, view.getFullYear(), view.getMonth(), day) ? " is-today" : "")
              }
              onClick={() => onPick(new Date(view.getFullYear(), view.getMonth(), day))}
            >
              {day}
            </button>
          ),
        )}
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
    <div className="schedule-time-list" ref={ref}>
      {slots.map(({ h, m }, i) => (
        <button
          key={`${h}:${m}`}
          type="button"
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
}: {
  initial: RecurrenceRule | null;
  anchor: Date;
  onDone: (rule: RecurrenceRule) => void;
  onCancel: () => void;
}) {
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
    <div className="schedule-recur" role="dialog" aria-label="Custom recurrence">
      <p className="schedule-recur-title">Custom recurrence</p>

      <div className="schedule-recur-row">
        <span>Repeat every</span>
        <input type="number" min={1} max={99} className="field-control schedule-recur-n"
               value={interval}
               onChange={(e) => setIntervalN(Math.max(1, Math.min(99, Number(e.target.value) || 1)))} />
        <select className="field-control schedule-recur-unit" value={freq}
                aria-label="Repeat unit"
                onChange={(e) => setFreq(e.target.value as RecurrenceRule["freq"])}>
          <option value="day">{interval > 1 ? "days" : "day"}</option>
          <option value="week">{interval > 1 ? "weeks" : "week"}</option>
          <option value="month">{interval > 1 ? "months" : "month"}</option>
          <option value="year">{interval > 1 ? "years" : "year"}</option>
        </select>
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
          <select className="field-control" value={monthly}
                  aria-label="Monthly on"
                  onChange={(e) => setMonthly(e.target.value as "day" | "nth-weekday")}>
            <option value="day">Monthly on day {anchor.getDate()}</option>
            <option value="nth-weekday">
              Monthly on the {nth} {DAYS[anchor.getDay()]}
            </option>
          </select>
        </div>
      )}

      <div className="schedule-recur-ends">
        <span>Ends</span>
        <label className="schedule-recur-end">
          <input type="radio" name="recur-ends" checked={ends === "never"}
                 onChange={() => setEnds("never")} />
          Never
        </label>
        <label className="schedule-recur-end">
          <input type="radio" name="recur-ends" checked={ends === "on"}
                 onChange={() => setEnds("on")} />
          On
          <input type="date" className="field-control" value={until}
                 disabled={ends !== "on"}
                 min={toLocalInput(anchor).slice(0, 10)}
                 onChange={(e) => setUntil(e.target.value)} />
        </label>
        <label className="schedule-recur-end">
          <input type="radio" name="recur-ends" checked={ends === "after"}
                 onChange={() => setEnds("after")} />
          After
          <input type="number" min={1} max={999} className="field-control schedule-recur-n"
                 disabled={ends !== "after"}
                 value={count}
                 onChange={(e) => setCount(Math.max(1, Math.min(999, Number(e.target.value) || 1)))} />
          occurrences
        </label>
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
    </div>
  );
}

const NTH_LABELS = ["first", "second", "third", "fourth", "fifth"];

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

export default function NewJobModal({
  initialTime,
  initialTarget,
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
  const [message, setMessage] = useState(editing?.message ?? "");
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
  const [repeat, setRepeat] = useState<string>(() => {
    if (editing?.rule)
      return keyOfRule(editing.rule, new Date(editing.due));
    return editing?.repeats ? "cron" : "none";
  });
  const [customRule, setCustomRule] = useState<RecurrenceRule | null>(() =>
    editing?.rule && keyOfRule(editing.rule, new Date(editing.due)) === "custom"
      ? editing.rule
      : null,
  );
  const legacyCron = editing?.repeats ?? "";
  // The recurrence dialog, and the key to fall back to if it's cancelled —
  // picking "Custom…" must not strand the select on a choice with no rule.
  const [recurOpen, setRecurOpen] = useState(false);
  const repeatBefore = useRef(repeat);
  const [permission, setPermission] = useState(editing?.permission_mode || "auto");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  const [home, setHome] = useState("");
  // The path field's recents dropdown: what this form remembers being used
  // (localStorage, first — the user's own picks outrank inference), padded
  // with the folders existing tasks point at. Read once per open — the
  // stored list only changes through this same modal.
  const [recentsOpen, setRecentsOpen] = useState(false);
  const [recents] = useState(() => {
    const seen = new Set<string>();
    return [...readRecents(), ...(recentTargets ?? [])].filter((p) => {
      if (!p || seen.has(p)) return false;
      seen.add(p);
      return true;
    });
  });

  // The description wears the title's clothes but grows like a note: with the
  // text, up to the CSS max-height (~5 lines), then scrolls. Measured on every
  // change because "auto then scrollHeight" is the one reflow-safe way to
  // shrink back when lines are deleted.
  const titleRef = useRef<HTMLTextAreaElement>(null);
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
    const el = titleRef.current;
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
    message: editing?.message ?? "",
    when,
    repeat,
    customRule: JSON.stringify(customRule),
    permission,
  }));
  // EVERY field the form can lose arms the guard — time, repeat rule and
  // permission included. Comparing only text fields let a single ✕ silently
  // discard an adjusted schedule (Bugbot, PR #538).
  const dirty =
    target !== initial.target ||
    message !== initial.message ||
    when !== initial.when ||
    repeat !== initial.repeat ||
    JSON.stringify(customRule) !== initial.customRule ||
    permission !== initial.permission;

  const picked = useMemo(() => new Date(when), [when]);
  const pickedOk = !Number.isNaN(picked.getTime());

  // The two when-dropdowns, and the time field's draft text (editable like
  // Google's: type "8:30pm" or pick from the list; an unparseable draft
  // falls back to what the field had).
  const [dateOpen, setDateOpen] = useState(false);
  const [timeOpen, setTimeOpen] = useState(false);
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
    if (repeat === "custom") return customRule;
    if (repeat === "cron" || repeat === "none") return null;
    return choices.find((c) => c.key === repeat)?.rule ?? null;
  }, [repeat, customRule, choices]);

  // The replacement was created but the original could not be withdrawn: the
  // one state where pressing Save again would mint a THIRD copy, so it
  // disables the button outright and the error says what to do by hand.
  const [replaced, setReplaced] = useState(false);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await scheduleMessage({
        target: target.trim(),
        message,
        // A rule rides WITH its anchor (`due` = the first run); the legacy
        // cron line replaces due exactly as it always did; a one-off is due
        // alone.
        ...(rule
          ? { due: when, rule }
          : repeat === "cron" && legacyCron
            ? { repeats: legacyCron }
            : { due: when }),
        permission_mode: permission,
        // An edit keeps what it cannot re-ask for: a composer-scheduled task
        // that continues an open chat must still continue it after a time
        // change, or the edit silently turns it into a fresh session.
        ...(editing?.session_id ? { session_id: editing.session_id } : {}),
      });
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

  // A one-off in the past is refused HERE, not left to the server: the server
  // accepts a slightly-past due (the catch-up bound exists for the composer's
  // "send at" racing the clock), but from a planning form a past time is only
  // ever a mistake.
  const dueIsPast =
    repeat !== "cron" && pickedOk && picked.getTime() <= Date.now();

  const ready =
    !replaced &&
    message.trim() !== "" &&
    target.trim() !== "" &&
    (repeat === "custom" ? customRule !== null : true) &&
    (repeat === "cron" ? legacyCron !== "" : pickedOk) &&
    !dueIsPast;

  return (
    <Modal
      title={editing ? "Edit task" : "New task"}
      onClose={onClose}
      busy={busy}
      width={460}
      dirty={dirty}
      footer={
        <button type="button" className="btn btn-primary schedule-save"
                disabled={busy || !ready} onClick={submit}>
          {busy ? "Saving…" : "Save"}
        </button>
      }
    >
      <div className="schedule-form">
        {/* The task text leads, wearing the title's clothes: one field is the
            whole ask — what Claude should do — and splitting a title from a
            description made people write the same thing twice (Akshil,
            2026-08-14). */}
        <textarea
          ref={titleRef}
          className="schedule-form-title"
          rows={2}
          placeholder="Add description"
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          autoFocus
        />

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
              className="field-control"
              placeholder="Add folder or file"
              role="combobox"
              aria-expanded={recentsOpen}
              value={target}
              onFocus={() => {
                if (suppressOpen.current) {
                  suppressOpen.current = false;
                  return;
                }
                setRecentsOpen(true);
              }}
              onClick={() => setRecentsOpen(true)}
              onChange={(e) => setTarget(e.target.value)}
            />
            {recentsOpen && (
              // mousedown preventDefault: keep focus ON the input while a row
              // is clicked. Safari never focuses <button> on click, so the
              // blur handler's relatedTarget is null there and the list would
              // unmount before its click fired (Bugbot, PR #541).
              <div
                className="schedule-recents"
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
                    setPicking(true);
                  }}
                >
                  Browse…
                </button>
              </div>
            )}
          </div>
        </div>
        {picking && (
          // Full row width, like every other line of the card — the sub-row
          // indent left the picker narrower than the field it serves
          // (Akshil, 2026-08-15).
          <FolderPicker
            start={target.trim() || home || "/"}
            onPick={(p) => {
              pickedFromBrowser.current = true;
              setTarget(p);
              rememberRecent(p);
            }}
            onClose={() => {
              setPicking(false);
              // Back (no pick) returns to the level above: refocusing the
              // field is what reopens the recents dropdown.
              if (!pickedFromBrowser.current) pathRef.current?.focus();
              pickedFromBrowser.current = false;
            }}
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
            >
              <button type="button" className="field-control schedule-when-field"
                      aria-expanded={dateOpen}
                      onClick={() => { setDateOpen((o) => !o); setTimeOpen(false); }}>
                {dateLabel}
              </button>
              {dateOpen && (
                <div className="schedule-pop" onMouseDown={(e) => e.preventDefault()}>
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
                type="text"
                className="field-control schedule-when-field schedule-when-time"
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
                     onMouseDown={(e) => e.preventDefault()}>
                  <TimeList
                    selected={{ h: pickedOk ? picked.getHours() : 9, m: pickedOk ? picked.getMinutes() : 0 }}
                    onPick={(h, m) => { setTimePart(h, m); setTimeOpen(false); }}
                  />
                </div>
              )}
            </div>
          </div>
        </div>
        <div className="schedule-form-line schedule-form-line--sub">
          <select
            className="field-control"
            value={repeat}
            aria-label="Repeats"
            onChange={(e) => {
              const v = e.target.value;
              if (v === "custom") {
                // The dialog answers what "Custom…" means; the select only
                // commits once Done says so.
                repeatBefore.current = repeat;
                setRecurOpen(true);
                setRepeat("custom");
              } else {
                setRepeat(v);
              }
            }}
          >
            {choices.map((c) =>
              c.key === "custom" ? (
                <option key="custom" value="custom">
                  {repeat === "custom" && customRule
                    ? describeRule(customRule, pickedOk ? picked : new Date())
                    : "Custom…"}
                </option>
              ) : (
                <option key={c.key} value={c.key}>{c.label}</option>
              ),
            )}
            {/* Legacy cron templates keep their line under a key of their own
                — the form no longer writes cron, but editing one must not
                silently rewrite the rule. */}
            {legacyCron && (
              <option value="cron">{describeRepeats(legacyCron)}</option>
            )}
          </select>
        </div>
        {recurOpen && (
          <CustomRecurrence
            initial={customRule}
            anchor={pickedOk ? picked : new Date()}
            onDone={(r) => {
              setCustomRule(r);
              setRecurOpen(false);
            }}
            onCancel={() => {
              setRecurOpen(false);
              // No rule was committed: fall back to whatever was chosen
              // before "Custom…" was tried.
              if (!customRule) setRepeat(repeatBefore.current);
            }}
          />
        )}
        {dueIsPast && (
          <span className="field-hint schedule-form-bad schedule-form-sub">
            Choose a time in the future
          </span>
        )}

        <details className="schedule-form-more">
          <summary>More options</summary>
          <label className="field">
            <span className="field-label">Permissions</span>
            <select className="field-control" value={permission}
                    onChange={(e) => setPermission(e.target.value)}>
              {(permissionModes.length ? permissionModes : ["auto"]).map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
            <span className="field-hint">
              The task runs unattended. Auto approves safe actions and holds the rest.
            </span>
          </label>
        </details>

        {error && <ErrorBanner>{error}</ErrorBanner>}
      </div>
    </Modal>
  );
}
