// Skills section: the user's own skills under
// skills/*/SKILL.md — name + description from the frontmatter, source (when
// the `skills` CLI recorded one), and a `bunx skills add` command to match.
// A skill opens in an in-app editor (SKILL.md and its sibling files), can be
// created from a scaffold, and deleted.
//
// Round 2 fixed four defects the live page showed:
//
//   1. The expanded row repeated its own description verbatim, adding only a
//      "Source" line — one line of new information is not worth a
//      disclosure, so there is none any more. The description gets the room
//      to be read on the row itself instead of ellipsizing mid-sentence —
//      a skill's description IS its whole contract with the model. Source,
//      when recorded, sits in the meta slot instead of behind a second click.
//   2. `linked` (installed via the `skills` CLI, a symlink into .agents) was a
//      badge on 9 of 10 rows, distinguishing almost nothing from its position
//      right after the name. It now marks the EXCEPTION — a skill that is
//      NOT linked, i.e. authored or dropped in by hand rather than managed —
//      because that is the one worth calling out.
//   3. "no recorded source" used to render as right-aligned italic meta on
//      the rows lacking one, creating a ragged second column that existed
//      only to say nothing was there. Absent data now renders nothing.
//
// Round 2 FOLLOW-UP after a live look at the page fixed three more: the name
// column had no shared left edge (it shrink-wrapped against the description
// instead of sitting in a fixed column — .cc-skills-list gives it one, and
// tops the row instead of centering it, since the description can now be two
// lines); the description was let wrap UNBOUNDED, so one long entry ran ~4x
// taller than its neighbours (clamped to 2 lines, `.cc-lrow-sub-clamp2` —
// shared with Memory's file rows so the two tabs agree); and at a narrow
// width the description was the thing that disappeared while the source
// (lower-value, repetitive across a marketplace) stayed — inverted below.
//
// A SECOND follow-up: the "not linked" badge rode as a `pills` sibling of the
// name inside the row's flex flow, so on cocoindex — the one row that has it
// — it pushed the description's start x rightward, the exact same class of
// defect the fixed name column had just fixed for the other nine. It moves
// into the name box itself now (stacked under the name, `.cc-skills-namebox`
// below), so it can never again be the thing that breaks the shared left
// edge, no matter how many rows end up carrying it.
import { useCallback, useState } from "react";
import { copyToClipboard } from "@platform/lib/clipboard";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import * as cc from "../api";
import {
  Empty,
  Icon,
  List,
  ListRow,
  ListSkeleton,
  Pill,
  SKELETON_ROWS,
  SectionToolbar,
  guard,
  toastErr,
  toastOk,
  useChangePreview,
  useModuleData,
} from "../bits";
import type { SectionProps } from "../bits";
import { FileEditor } from "../FileEditor";

function SkillEditor({ slug, onDelete }: { slug: string; onDelete: () => void }) {
  const [path, setPath] = useState("SKILL.md");
  const [files, setFiles] = useState<string[]>([]);
  const load = useCallback(async () => {
    const res = await cc.skills.read(slug, path);
    if (!res.ok) throw new Error(res.error || "Could not read the skill");
    setFiles(res.files);
    return res.text;
  }, [slug, path]);
  const save = useCallback(
    async (text: string) => {
      const res = await cc.skills.write(slug, path, text);
      return res.ok ? null : res.error || "Save failed";
    },
    [slug, path],
  );
  return (
    <div className="cc-fileeditor">
      {files.length > 1 && (
        <div className="cc-fileeditor-files">
          {files.map((f) => (
            <Button key={f} size="xs" variant={f === path ? "secondary" : "outline"} onClick={() => setPath(f)}>
              {f}
            </Button>
          ))}
        </div>
      )}
      <FileEditor
        key={path}
        label={`${slug}/${path}`}
        load={load}
        save={save}
        extra={
          <Button size="sm" variant="destructive" className="ml-auto" onClick={onDelete}>
            Delete skill
          </Button>
        }
      />
    </div>
  );
}

export default function SkillsSection({ onChanged, filter = "" }: SectionProps) {
  const load = useCallback(() => cc.skills.list(), []);
  const { data, error, reload } = useModuleData(load);
  const { node: modal, ask } = useChangePreview();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");

  const share = async (command: string) => {
    if (await copyToClipboard(command)) toastOk("Copied install command");
    else toastErr("Copy failed");
  };

  const create = async () => {
    const res = await guard(cc.skills.create(name.trim()));
    if (!res) return;
    if (!res.ok) {
      toastErr(res.error || "Create failed");
      return;
    }
    toastOk(`Created ${name.trim()}`);
    setName("");
    setCreating(false);
    onChanged();
    reload();
  };

  const remove = async (slug: string) => {
    const ok = await ask<boolean>({
      title: `Delete skill "${slug}"?`,
      note: "The skill folder is deleted and the deletion is committed — recoverable from History.",
      buttons: [
        { label: "Cancel", value: false },
        { label: "Delete", value: true, primary: true, danger: true },
      ],
    });
    if (!ok) return;
    const res = await guard(cc.skills.delete(slug));
    if (!res) return;
    if (!res.ok) {
      toastErr(res.error || "Delete failed");
      return;
    }
    toastOk(`Deleted ${slug}`);
    onChanged();
    reload();
  };

  if (error) return <ErrorBanner>{error}</ErrorBanner>;
  if (!data) return <ListSkeleton rows={SKELETON_ROWS} label="Loading skills" />;

  const needle = filter.trim().toLowerCase();
  const shown = data.skills.filter(
    (s) => !needle || `${s.name} ${s.slug} ${s.description}`.toLowerCase().includes(needle),
  );

  return (
    <>
      {modal}
      <SectionToolbar summary={`${shown.length} skill(s)`} onRefresh={reload}>
        <Button size="sm" variant="outline" onClick={() => setCreating((v) => !v)}>
          {creating ? "Cancel" : "New skill"}
        </Button>
      </SectionToolbar>
      {creating && (
        <div className="cc-fileeditor-bar" style={{ marginBottom: 10 }}>
          <Input
            aria-label="New skill name"
            placeholder="skill-name"
            value={name}
            autoFocus
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && name.trim() && void create()}
            className="max-w-64"
          />
          <Button size="sm" disabled={!name.trim()} onClick={() => void create()}>
            Create
          </Button>
        </div>
      )}
      {!shown.length && <Empty>{needle ? "No skills match." : "No local skills under skills/*/SKILL.md."}</Empty>}
      {shown.length > 0 && (
        <div className="cc-skills-list">
          <List>
            {shown.map((s) => (
              <ListRow
                key={s.slug}
                name={
                  <span className="cc-skills-namebox">
                    <span className="cc-skills-namebox-text">{s.name}</span>
                    {!s.linked && <Pill>not linked</Pill>}
                  </span>
                }
                secondary={s.description || undefined}
                secondaryClass="cc-lrow-sub-clamp2"
                secondaryTitle={s.description || undefined}
                meta={
                  s.source ? <span className="cc-lrow-meta cc-mono">{s.source}</span> : null
                }
                details={<SkillEditor slug={s.slug} onDelete={() => void remove(s.slug)} />}
                actions={
                  <>
                    <button
                      type="button"
                      className="cc-iconbtn"
                      title="Reveal in file manager"
                      aria-label={`Reveal ${s.name} in the file manager`}
                      onClick={() => guard(cc.skills.open(s.slug))}
                    >
                      <Icon name="folder" />
                    </button>
                    {s.shareCommand && (
                      <button
                        type="button"
                        className="cc-iconbtn"
                        title={`Copy install command — ${s.shareCommand}`}
                        aria-label={`Copy the install command for ${s.name}`}
                        onClick={() => share(s.shareCommand as string)}
                      >
                        <Icon name="copy" />
                      </button>
                    )}
                  </>
                }
              />
            ))}
          </List>
        </div>
      )}
    </>
  );
}
