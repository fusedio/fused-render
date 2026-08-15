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
import { useEffect, useMemo, useState } from "react";
import { Modal } from "@platform/ui/modal/Modal";
import {
  cancelScheduledMessage,
  getConfig,
  listDir,
  scheduleMessage,
} from "@platform/lib/api";
import type { ScheduledMessage } from "@platform/lib/api";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { describeRepeats } from "./schedule-lib";
import { ICON_CLOCK, ICON_FOLDER, ICON_REPEAT } from "./ScheduleCalendar";

// Where a new task points before the user says otherwise: ~/Desktop/fused
// (Akshil, 2026-08-14 — an empty path field was the confusing part of the
// form). Home-relative so it composes on any machine; the picker makes
// changing it a click, and a machine without the folder gets the server's
// clear 400 naming the path.
const DEFAULT_TARGET_SUFFIX = "/Desktop/fused";

// ---- Folder picker -----------------------------------------------------------
// A small in-modal directory browser: descend by clicking, one Up control,
// "Use this folder" hands the current path back. Deliberately folders-only —
// scheduling against a file is still possible by typing, but the picker's job
// is the common case, and mixing files in doubled the list for nothing.

function FolderPicker({
  start,
  onPick,
  onClose,
}: {
  start: string;
  onPick: (path: string) => void;
  onClose: () => void;
}) {
  const [path, setPath] = useState(start);
  const [dirs, setDirs] = useState<string[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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

  const up = () => {
    const trimmed = path.replace(/\/+$/, "");
    const cut = trimmed.lastIndexOf("/");
    setPath(cut > 0 ? trimmed.slice(0, cut) : "/");
  };

  return (
    <div className="schedule-picker">
      <div className="schedule-picker-head">
        <button type="button" className="btn btn-secondary" onClick={up}
                disabled={path === "/"} aria-label="Up one folder">‹</button>
        <code title={path}>{path}</code>
      </div>
      <div className={"schedule-picker-list" + (loading ? " is-loading" : "")}>
        {error && <p className="schedule-card-why">{error}</p>}
        {!error && dirs?.length === 0 && !loading && (
          <p className="schedule-card-why">No subfolders</p>
        )}
        {!error && dirs?.map((name) => (
          <button key={name} type="button" className="schedule-picker-row"
                  disabled={loading}
                  onClick={() => setPath(path.replace(/\/+$/, "") + "/" + name)}>
            {ICON_FOLDER} {name}
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

const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

// A Date as the value a <input type="datetime-local"> wants: local wall-clock,
// minute precision, no zone suffix. `toISOString` is exactly wrong here (UTC).
function toLocalInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

type Repeat = "none" | "hourly" | "daily" | "weekly" | "custom";

// Which of the derived choices a stored cron line is, so editing reopens on
// the same words the user picked. Anything the form didn't write is custom.
function repeatOf(repeats: string): Repeat {
  const m = repeats.trim().split(/\s+/);
  if (m.length !== 5 || m[2] !== "*" || m[3] !== "*" || !/^\d+$/.test(m[0]))
    return "custom";
  if (m[1] === "*" && m[4] === "*") return "hourly";
  if (!/^\d+$/.test(m[1])) return "custom";
  if (m[4] === "*") return "daily";
  return /^[0-7]$/.test(m[4]) ? "weekly" : "custom";
}

export default function NewJobModal({
  initialTime,
  initialTarget,
  editing,
  permissionModes,
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
  const [repeat, setRepeat] = useState<Repeat>(
    editing?.repeats ? repeatOf(editing.repeats) : "none",
  );
  const [customCron, setCustomCron] = useState(
    editing?.repeats && repeatOf(editing.repeats) === "custom" ? editing.repeats : "",
  );
  const [permission, setPermission] = useState(editing?.permission_mode || "auto");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  const [home, setHome] = useState("");

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
          const fallback = c.home + DEFAULT_TARGET_SUFFIX;
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
    customCron,
    permission,
  }));
  // EVERY field the form can lose arms the guard — time, repeat rule, cron
  // line and permission included. Comparing only text fields let a single ✕
  // silently discard an adjusted schedule (Bugbot, PR #538).
  const dirty =
    target !== initial.target ||
    message !== initial.message ||
    when !== initial.when ||
    repeat !== initial.repeat ||
    customCron !== initial.customCron ||
    permission !== initial.permission;

  const picked = useMemo(() => new Date(when), [when]);
  const pickedOk = !Number.isNaN(picked.getTime());

  // The derived cron line for the current choice; "" for a one-off.
  const cron = useMemo(() => {
    if (!pickedOk && repeat !== "custom") return "";
    switch (repeat) {
      case "none": return "";
      case "hourly": return `${picked.getMinutes()} * * * *`;
      case "daily": return `${picked.getMinutes()} ${picked.getHours()} * * *`;
      case "weekly": return `${picked.getMinutes()} ${picked.getHours()} * * ${picked.getDay()}`;
      case "custom": return customCron.trim();
    }
  }, [repeat, picked, pickedOk, customCron]);

  const pad = (n: number) => String(n).padStart(2, "0");
  const atTime = pickedOk ? `${pad(picked.getHours())}:${pad(picked.getMinutes())}` : "";

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
        ...(cron ? { repeats: cron } : { due: when }),
        permission_mode: permission,
        // An edit keeps what it cannot re-ask for: a composer-scheduled task
        // that continues an open chat must still continue it after a time
        // change, or the edit silently turns it into a fresh session.
        ...(editing?.session_id ? { session_id: editing.session_id } : {}),
      });
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
    repeat === "none" && pickedOk && picked.getTime() <= Date.now();

  const ready =
    !replaced &&
    message.trim() !== "" &&
    target.trim() !== "" &&
    (repeat === "custom" ? cron !== "" : pickedOk) &&
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
          className="schedule-form-title"
          rows={2}
          placeholder="Add description"
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          autoFocus
        />

        <div className="schedule-form-line">
          {ICON_FOLDER}
          <input
            type="text"
            className="field-control"
            placeholder="Add folder or file"
            value={target}
            onChange={(e) => setTarget(e.target.value)}
          />
          <button type="button" className="btn btn-secondary"
                  aria-expanded={picking}
                  onClick={() => setPicking((p) => !p)}>
            Browse
          </button>
        </div>
        {picking && (
          <div className="schedule-form-line--sub">
            <FolderPicker
              start={target.trim() || home || "/"}
              onPick={setTarget}
              onClose={() => setPicking(false)}
            />
          </div>
        )}

        {/* Google's when-row: the date-time line, with the repeat rule as the
            muted second line under it. Controls are QUIET — they read as text
            until pointed at, exactly how the reference card treats them. */}
        <div className="schedule-form-line">
          {ICON_CLOCK}
          <input
            type="datetime-local"
            className="field-control"
            value={when}
            min={toLocalInput(new Date())}
            onChange={(e) => setWhen(e.target.value)}
          />
        </div>
        <div className="schedule-form-line schedule-form-line--sub">
          <select
            className="field-control"
            value={repeat}
            aria-label="Repeats"
            onChange={(e) => setRepeat(e.target.value as Repeat)}
          >
            <option value="none">Does not repeat</option>
            <option value="hourly">Hourly at :{pickedOk ? pad(picked.getMinutes()) : "00"}</option>
            <option value="daily">Daily at {atTime || "…"}</option>
            <option value="weekly">Weekly on {pickedOk ? DAYS[picked.getDay()] : "…"}</option>
            <option value="custom">Custom (cron)…</option>
          </select>
        </div>
        {dueIsPast && (
          <span className="field-hint schedule-form-bad schedule-form-sub">
            Choose a time in the future
          </span>
        )}
        {repeat === "custom" && (
          <div className="schedule-form-line schedule-form-line--sub">
            {ICON_REPEAT}
            <input
              type="text"
              className="field-control"
              placeholder="30 9 * * 1-5"
              value={customCron}
              onChange={(e) => setCustomCron(e.target.value)}
            />
          </div>
        )}
        {/* The repeat select already SAYS the rule; restating it in a hint was
            noise. Only Custom, where the rule is a cron line, gets a reading. */}
        {repeat === "custom" && cron !== "" && (
          <span className="field-hint schedule-form-sub">
            {describeRepeats(cron) === cron ? "Minute, hour, day, month, weekday" : `Runs ${describeRepeats(cron)}`}
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
