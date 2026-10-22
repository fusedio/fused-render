// One skill's page: its header (path or owning plugin, readers, actions), its
// description, and its files. A user skill's Markdown files open as a rendered
// preview, with an Edit toggle to the textarea; a plugin's skill is read-only
// and only previews. The file list sits left of the pane when there is more
// than one file, and the pane fills the height the page has left.
import { useEffect, useState } from "react";
import { ChevronLeft } from "lucide-react";
import { Button } from "@platform/shadcn/ui/button";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { MarkdownView } from "../claude/ui/MarkdownView";
import * as cc from "./api";
import { Head, ListSkeleton, SKELETON_ROWS, Seg, guard, toastErr, toastOk, useConfirm } from "./bits";
import { CLI_LABEL, visibleReaders } from "./readers";

type Mode = "preview" | "edit";

/** SKILL.md's leading `---` block is metadata the header already shows. */
export function stripFrontmatter(text: string): string {
  return text.replace(/^---[ \t]*\r?\n[\s\S]*?\r?\n---[ \t]*(?:\r?\n|$)/, "").replace(/^\s*\n/, "");
}

/** A path with the home directory shortened to `~`. */
export function tildify(p: string): string {
  return p.replace(/^(?:\/Users|\/home)\/[^/]+/, "~");
}

const isMarkdown = (path: string) => /\.(md|markdown)$/i.test(path);

export function SkillDetail({
  skill,
  enabledClis,
  onBack,
  onChanged,
  onDeleted,
}: {
  skill: cc.Skill;
  enabledClis: cc.CliId[];
  onBack: () => void;
  onChanged: () => void;
  onDeleted: () => void;
}) {
  const { node: modal, ask } = useConfirm();
  const { slug, description } = skill;
  const readOnly = skill.source === "plugin";
  const [path, setPath] = useState("SKILL.md");
  const [files, setFiles] = useState<string[]>(["SKILL.md"]);
  const [saved, setSaved] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [mode, setMode] = useState<Mode>("preview");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
  const dirty = saved !== null && text !== saved;
  const md = isMarkdown(path);
  const previewing = md && (readOnly || mode === "preview");
  const readers = readOnly ? [] : visibleReaders(skill.readers, enabledClis);

  useEffect(() => {
    let alive = true;
    setSaved(null);
    setLoadError(null);
    setNote(null);
    setMode("preview");
    cc.skills.read(skill, path).then(
      (res) => {
        if (!alive) return;
        if (!res.ok) {
          setLoadError(res.error || "Could not read the skill");
          return;
        }
        setFiles(res.files);
        setSaved(res.text);
        setText(res.text);
      },
      (e) => alive && setLoadError((e as Error).message),
    );
    return () => {
      alive = false;
    };
  }, [skill, path]);

  // The "Saved" note fades out on its own; an error stays until the next edit.
  useEffect(() => {
    if (!note?.ok) return;
    const t = setTimeout(() => setNote(null), 2500);
    return () => clearTimeout(t);
  }, [note]);

  const discardOk = async () =>
    !dirty ||
    (await ask<boolean>({
      title: "Discard unsaved changes?",
      note: `${path} has edits that are not saved.`,
      buttons: [
        { label: "Keep editing", value: false },
        { label: "Discard", value: true, primary: true, danger: true },
      ],
    }));

  const save = async () => {
    setBusy(true);
    setNote(null);
    try {
      const res = await cc.skills.write(skill, path, text);
      if (!res.ok) {
        setNote({ ok: false, text: res.error || "Save failed" });
        return;
      }
      setSaved(text);
      setNote({ ok: true, text: "Saved" });
      onChanged();
    } catch (e) {
      setNote({ ok: false, text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    const ok = await ask<boolean>({
      title: `Delete skill "${slug}"?`,
      note: (
        <>
          {skill.paths.length > 1 ? "These are deleted" : "This is deleted"}, and it cannot be
          undone:
          {skill.paths.map((p) => (
            <span key={p} className="cc-mono" style={{ display: "block" }}>
              {p}
            </span>
          ))}
        </>
      ),
      buttons: [
        { label: "Cancel", value: false },
        { label: "Delete", value: true, primary: true, danger: true },
      ],
    });
    if (!ok) return;
    const res = await guard(cc.skills.delete(skill));
    if (!res) return;
    if (!res.ok) {
      toastErr(res.error || "Delete failed");
      return;
    }
    toastOk(`Deleted ${slug}`);
    onDeleted();
  };

  return (
    <div className="cc-skill-detail">
      {modal}
      <Head
        lead={
          <Button
            variant="ghost"
            size="sm"
            onClick={async () => {
              if (await discardOk()) onBack();
            }}
          >
            <ChevronLeft data-icon="inline-start" />
            Skills
          </Button>
        }
        title={slug}
        blurb={
          <span className="cc-skill-meta">
            {readOnly ? (
              <span>from {skill.plugin} · updates to the plugin replace it</span>
            ) : (
              <>
                <span className="cc-mono" title={skill.path}>
                  {tildify(skill.path)}
                </span>
                {readers.map((r) => (
                  <span key={r} className="cc-reader">
                    {CLI_LABEL[r]}
                  </span>
                ))}
              </>
            )}
          </span>
        }
        aside={
          <>
            {note && (
              <span className={note.ok ? "cc-skill-note" : "cc-skill-note is-error"} role="status">
                {note.text}
              </span>
            )}
            <button type="button" className="btn" onClick={() => guard(cc.skills.open(skill))}>
              Reveal
            </button>
            {!readOnly && (
              <>
                <button type="button" className="btn cc-btn-danger-ghost" onClick={() => void remove()}>
                  Delete
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={busy || !dirty}
                  onClick={() => void save()}
                >
                  Save
                </button>
              </>
            )}
          </>
        }
      />
      {description && (
        <p className="cc-skill-blurb" title={description}>
          {description}
        </p>
      )}
      <div className="cc-skill-work">
        {files.length > 1 && (
          <nav className="cc-skill-filelist" aria-label="Skill files">
            {files.map((f) => (
              <button
                key={f}
                type="button"
                className={"cc-skill-file" + (f === path ? " active" : "")}
                aria-current={f === path ? "true" : undefined}
                title={f}
                onClick={async () => {
                  if (f !== path && (await discardOk())) setPath(f);
                }}
              >
                {f}
              </button>
            ))}
          </nav>
        )}
        <div className="cc-skill-pane">
          {md && !readOnly && (
            <Seg
              label="Skill view"
              value={mode}
              onChange={setMode}
              options={[
                { id: "preview", label: "Preview" },
                { id: "edit", label: "Edit" },
              ]}
            />
          )}
          {loadError ? (
            <ErrorBanner>{loadError}</ErrorBanner>
          ) : saved === null ? (
            <ListSkeleton rows={SKELETON_ROWS} label="Loading skill" />
          ) : (
            <div className="settings-card cc-skill-card">
              {previewing ? (
                <div className="cc-md" aria-label={`${slug}/${path} preview`}>
                  <MarkdownView text={stripFrontmatter(text)} enhance={false} />
                </div>
              ) : (
                <Textarea
                  aria-label={`${slug}/${path}`}
                  value={text}
                  spellCheck={false}
                  readOnly={readOnly}
                  className="cc-skill-editor"
                  onChange={(e) => {
                    setText(e.target.value);
                    setNote(null);
                  }}
                />
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
