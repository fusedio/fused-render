// New skill — Draft with Claude, the only way to make a skill here. The form
// (what it should do, an optional skill to adapt, who reads it) starts a
// Claude task whose working dir is a staging dir under the state dir; while the
// task writes, this page polls the dir and shows SKILL.md live. Nothing reaches
// ~/.agents/skills or ~/.claude/skills until Save, which validates first.
import { useEffect, useRef, useState } from "react";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { Textarea } from "@platform/shadcn/ui/textarea";
import * as cc from "@apps/claude_config/api";
import { toastErr, toastOk } from "@apps/claude_config/bits";
import { CLI_LABEL, useEnabledClis } from "@apps/claude_config/readers";
import {
  tasksCancel,
  tasksCreate,
  tasksSend,
  tasksWatch,
  type TaskHandle,
  type TaskRow,
} from "@apps/bots/builds/tasks";
import { taskHref } from "../tasks-lib";
import { draftTitle, freeName, renameSkill, splitFrontmatter, useDebounced } from "./model";

const LIVE = new Set(["in_progress", "queued"]);
const POLL_MS = 2000;

type Check = { state: "idle" } | { state: "ok"; name: string; others: number } | { state: "error"; error: string };

export function NewSkill({
  initialDesc,
  existing = [],
  onClose,
  onSaved,
}: {
  initialDesc: string;
  /** Slugs already in use, to offer a free name when the draft's is taken. */
  existing?: string[];
  onClose: () => void;
  /** The saved skill's key (its path), for opening it. */
  onSaved?: (key: string) => void;
}) {
  const enabledClis = useEnabledClis();
  const [desc, setDesc] = useState(initialDesc);
  const [source, setSource] = useState("");
  const [placement, setPlacement] = useState<cc.DraftPlacement>("all");
  const [check, setCheck] = useState<Check>({ state: "idle" });
  const [starting, setStarting] = useState(false);

  const [staging, setStaging] = useState<string | null>(null);
  const [adapting, setAdapting] = useState(false);
  const handle = useRef<TaskHandle | null>(null);
  const [row, setRow] = useState<TaskRow | null>(null);
  const [finished, setFinished] = useState(false);
  const [status, setStatus] = useState<cc.DraftStatus | null>(null);

  const [change, setChange] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  // null until the user types: the input shows the suggested free name, and a
  // cleared field stays cleared ("" is a value, null is "untouched").
  const [rename, setRename] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveProblems, setSaveProblems] = useState<string[]>([]);
  const [refreshError, setRefreshError] = useState<string | null>(null);

  // "Start from": checked a moment after typing stops.
  const dsource = useDebounced(source.trim(), 400);
  useEffect(() => {
    if (!dsource) {
      setCheck({ state: "idle" });
      return;
    }
    let alive = true;
    cc.skills.draftCheck(dsource).then(
      (r) =>
        alive &&
        setCheck(
          r.ok
            ? { state: "ok", name: r.name ?? "", others: Math.max(0, (r.files?.length ?? 1) - 1) }
            : { state: "error", error: r.error || "Not a skill" },
        ),
      (e) => alive && setCheck({ state: "error", error: (e as Error).message }),
    );
    return () => {
      alive = false;
    };
  }, [dsource]);

  // The task's own row: running while it is live, and again after a follow-up.
  useEffect(() => {
    if (!staging) return;
    return tasksWatch((rows) => {
      const h = handle.current;
      if (!h) return;
      setRow(rows.find((r) => r.key === h.key || (!!h.entryId && r.entry_id === h.entryId)) ?? null);
    });
  }, [staging]);

  const running = !!staging && !finished && (row === null || LIVE.has(row.status));

  // Poll the staging dir every 2 s while Claude runs, and once after it stops.
  useEffect(() => {
    if (!staging) return;
    let alive = true;
    const read = () =>
      cc.skills.draftStatus(staging).then(
        (r) => alive && r.ok && setStatus(r as cc.DraftStatus),
        () => {},
      );
    void read();
    if (!running) return () => void (alive = false);
    const t = setInterval(read, POLL_MS);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [staging, running]);

  const draft = async () => {
    if (!desc.trim() || starting) return;
    setStarting(true);
    let dir: string | null = null;
    try {
      const withSource = check.state === "ok";
      const start = await cc.skills.draftStart(withSource ? source.trim() : "");
      if (!start.ok || !start.staging) throw new Error(start.error || "Could not start the draft");
      dir = start.staging;
      const p = await cc.skills.draftPrompt(desc.trim(), withSource);
      if (!p.ok || !p.prompt) throw new Error(p.error || "Could not build the prompt");
      const h = await tasksCreate({ prompt: p.prompt, target: dir, title: draftTitle(desc), queue: false });
      handle.current = h;
      void h.done.then(() => setFinished(true));
      setAdapting(withSource);
      setStaging(dir);
    } catch (e) {
      if (dir) void cc.skills.draftDiscard(dir).catch(() => {});
      toastErr((e as Error).message);
    } finally {
      setStarting(false);
    }
  };

  const discard = async () => {
    const h = handle.current;
    if (h && running) await tasksCancel(h.key).catch(() => {});
    if (staging) await cc.skills.draftDiscard(staging).catch(() => {});
    onClose();
  };

  const askChanges = async () => {
    const h = handle.current;
    const text = change.trim();
    if (!h || !text) return;
    try {
      await tasksSend(h.key, text);
      setChange("");
      setFinished(false);
      setRow(null);
    } catch (e) {
      toastErr((e as Error).message);
    }
  };

  const writeSkillMd = async (text: string) => {
    if (!staging) return false;
    const r = await cc.skills.draftWrite(staging, "SKILL.md", text).catch((e: Error) => ({ ok: false, error: e.message }));
    if (!r.ok) {
      toastErr(r.error || "Could not write the draft");
      return false;
    }
    setRefreshError(null);
    try {
      const s = await cc.skills.draftStatus(staging);
      if (s.ok) setStatus(s as cc.DraftStatus);
    } catch (e) {
      // The write landed; only the re-read failed. Say so inline, keep going.
      setRefreshError(`Saved the edit, but could not refresh the draft: ${(e as Error).message}`);
    }
    return true;
  };

  const save = async () => {
    if (!staging) return;
    setSaving(true);
    setSaveProblems([]);
    try {
      const r = await cc.skills.draftSave(staging, placement);
      if (!r.ok) {
        setSaveProblems(r.problems?.length ? r.problems : [r.error || "Save failed"]);
        return;
      }
      toastOk(`Saved ${r.slug ?? "skill"}`);
      onSaved?.(r.key ?? "");
    } catch (e) {
      toastErr((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const problems = status?.problems ?? [];
  const taken = problems.some((p) => p.includes("already exists"));
  const canSave = !!status?.text && problems.length === 0 && !running && !saving && editing === null;
  const locked = !!staging;
  const hasOtherClis = enabledClis.some((c) => c !== "claude");
  const readers = enabledClis.map((c) => CLI_LABEL[c]).join(", ");
  const taskLink = row ? taskHref(row as never) ?? "/tasks" : "/tasks";

  return (
    <div className="cc-new">
      <div className="cc-new-form">
        <h2 className="cc-new-title m-0">New skill</h2>

        <section className="cc-new-field">
          <label htmlFor="new-skill-desc" className="cc-new-label">
            What should the skill do?
          </label>
          <Textarea
            id="new-skill-desc"
            value={desc}
            disabled={locked}
            autoFocus
            rows={5}
            placeholder="Review a page before I ship it: check colors, spacing and type against our design tokens."
            onChange={(e) => setDesc(e.target.value)}
          />
        </section>

        <section className="cc-new-field">
          <label htmlFor="new-skill-source" className="cc-new-label">
            Start from an existing skill <span className="cc-new-opt">optional</span>
          </label>
          <Input
            id="new-skill-source"
            className="cc-mono"
            value={source}
            disabled={locked}
            placeholder="e.g. ~/.agents/skills/ui-design-review"
            spellCheck={false}
            autoComplete="off"
            onChange={(e) => setSource(e.target.value)}
          />
          {check.state === "ok" && (
            <p className="cc-new-lint is-ok m-0">
              ✓ Found SKILL.md
              {check.others > 0 ? ` + ${check.others} other file${check.others === 1 ? "" : "s"}` : ""}. Claude adapts it
              rather than starting blank.
            </p>
          )}
          {check.state === "error" && <p className="cc-new-lint is-err m-0">{check.error}</p>}
          {check.state === "idle" && (
            <p className="cc-new-lint m-0">Claude reads it and adapts it. The original isn&apos;t changed.</p>
          )}
        </section>

        <fieldset className="cc-new-field cc-new-radios" disabled={locked}>
          <legend className="cc-new-label">Who reads it</legend>
          <label className="cc-new-radio">
            <input type="radio" name="placement" checked={placement === "all"} onChange={() => setPlacement("all")} />
            <span>
              Every CLI you have on
              <span className="cc-new-sub">
                {hasOtherClis ? "~/.agents/skills, linked from ~/.claude/skills" : "~/.claude/skills"} · {readers}
              </span>
            </span>
          </label>
          <label className="cc-new-radio">
            <input type="radio" name="placement" checked={placement === "claude"} onChange={() => setPlacement("claude")} />
            <span>
              Claude Code only<span className="cc-new-sub">~/.claude/skills</span>
            </span>
          </label>
        </fieldset>

        {!staging && (
          <div className="cc-new-footer">
            <span className="cc-new-note">Runs as a Claude task. You review the files before anything is saved.</span>
            <span className="cc-new-btns">
              <Button variant="outline" onClick={onClose}>
                Cancel
              </Button>
              <Button disabled={!desc.trim() || starting} onClick={() => void draft()}>
                {starting ? "Starting…" : "Draft it"}
              </Button>
            </span>
          </div>
        )}

        {staging && (
          <section className="cc-new-draft" aria-label="Draft" data-testid="draft-card">
            <h3 className="settings-section-title">Draft</h3>
            <div className="settings-card cc-new-card">
              <p className="m-0">
                {running ? (
                  <>Claude is {adapting ? "adapting the skill" : "drafting the skill"}…</>
                ) : status?.text ? (
                  <>
                    Drafted <b>{status.name || "the skill"}</b>: {status.files.join(", ")}.
                  </>
                ) : row?.status === "needs_attention" ? (
                  <>Claude needs your answer to carry on.</>
                ) : (
                  <>Claude finished without writing a SKILL.md. Ask for it below.</>
                )}{" "}
                <a className="cc-link" href={taskLink} target="_blank" rel="noreferrer">
                  Open task
                </a>
              </p>

              {problems.length > 0 && (
                <ul className="cc-new-problems m-0" role="alert">
                  {problems.map((p) => (
                    <li key={p}>{p}</li>
                  ))}
                </ul>
              )}
              {refreshError && (
                <p className="cc-new-problems m-0" role="alert">
                  {refreshError}
                </p>
              )}
              {saveProblems.length > 0 && problems.length === 0 && (
                <ul className="cc-new-problems m-0" role="alert">
                  {saveProblems.map((p) => (
                    <li key={p}>{p}</li>
                  ))}
                </ul>
              )}

              {taken && status?.text && (
                <div className="cc-new-rename">
                  <Input
                    aria-label="A different name"
                    className="cc-mono"
                    value={rename ?? freeName(status.name, (n) => existing.includes(n))}
                    onChange={(e) => setRename(e.target.value)}
                  />
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() =>
                      void writeSkillMd(
                        renameSkill(status.text ?? "", (rename ?? freeName(status.name, (n) => existing.includes(n))).trim()),
                      )
                    }
                  >
                    Use this name
                  </Button>
                </div>
              )}

              {editing !== null ? (
                <div className="cc-new-edit">
                  <Textarea
                    aria-label="SKILL.md"
                    className="cc-skill-editor"
                    rows={16}
                    spellCheck={false}
                    value={editing}
                    onChange={(e) => setEditing(e.target.value)}
                  />
                  <span className="cc-new-btns">
                    <Button variant="outline" size="sm" onClick={() => setEditing(null)}>
                      Cancel
                    </Button>
                    <Button
                      size="sm"
                      onClick={() => void writeSkillMd(editing).then((ok) => ok && setEditing(null))}
                    >
                      Apply edits
                    </Button>
                  </span>
                </div>
              ) : (
                <>
                  <div className="cc-new-ask">
                    <Input
                      aria-label="Ask for changes"
                      placeholder="Ask for changes"
                      value={change}
                      onChange={(e) => setChange(e.target.value)}
                      onKeyDown={(e) => e.key === "Enter" && void askChanges()}
                    />
                    <Button variant="outline" disabled={!change.trim() || running} onClick={() => void askChanges()}>
                      Send
                    </Button>
                  </div>
                  <div className="cc-new-btns">
                    <Button disabled={!canSave} onClick={() => void save()}>
                      {saving ? "Saving…" : "Save skill"}
                    </Button>
                    <Button
                      variant="outline"
                      disabled={!status?.text || running}
                      onClick={() => setEditing(status?.text ?? "")}
                    >
                      Edit before saving
                    </Button>
                    <Button variant="ghost" onClick={() => void discard()}>
                      Discard draft
                    </Button>
                  </div>
                </>
              )}
            </div>
          </section>
        )}
      </div>

      <aside className="cc-skill-aside" aria-label="SKILL.md" data-testid="draft-aside">
        {status?.text ? (
          <>
            <h2 className="cc-aside-title m-0">
              SKILL.md <span className="cc-aside-src">draft</span>
            </h2>
            <p className="cc-aside-src m-0">
              {status.name || "unnamed"} · {status.files.length} file{status.files.length === 1 ? "" : "s"}
            </p>
            <pre className="cc-aside-raw m-0">
              <span className="cc-aside-fm">{splitFrontmatter(status.text)[0]}</span>
              {splitFrontmatter(status.text)[1]}
            </pre>
            <ul className="cc-aside-files m-0">
              {status.files.map((f) => (
                <li key={f} className="cc-mono">
                  {f}
                </li>
              ))}
            </ul>
          </>
        ) : (
          <>
            <h2 className="cc-aside-title m-0">What Claude writes</h2>
            <p className="cc-aside-src m-0">
              A folder with a SKILL.md: frontmatter naming the skill and saying when to use it, then the steps. It
              adds references or scripts only if the skill needs them. It shows up here as it is written.
            </p>
          </>
        )}
      </aside>
    </div>
  );
}
