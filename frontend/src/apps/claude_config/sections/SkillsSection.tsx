// Skills section: the user's own skills under skills/*/SKILL.md — name and
// description from the frontmatter, source (when the `skills` CLI recorded
// one) and a `bunx skills add` command to match. A skill opens in an in-app
// editor (SKILL.md and its sibling files), can be created from a scaffold,
// and deleted. A skill that is not linked (authored by hand rather than
// managed by the `skills` CLI) carries a badge under its name.
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
      {files.length > 0 && (
        <div className="cc-fileeditor-files" role="tablist" aria-label="Files in this skill">
          {files.map((f) => (
            <Button key={f} size="xs" role="tab" aria-selected={f === path} variant={f === path ? "secondary" : "outline"} onClick={() => setPath(f)}>
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
