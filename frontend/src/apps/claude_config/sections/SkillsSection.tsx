// Skills section: the user's own skills under skills/*/SKILL.md, drawn as the
// Claude Code page's whole main column. `?skill=<name>` selects between the
// two views: a list (one row per skill — name, then its description on one
// muted line) and a skill's editor (SKILL.md and its sibling files, one
// textarea in one card). A skill that is not linked (authored by hand rather
// than managed by the `skills` CLI) says so in its editor's header.
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { ChevronLeft } from "lucide-react";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { Tabs, TabsList, TabsTrigger } from "@platform/shadcn/ui/tabs";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import * as cc from "../api";
import {
  Empty,
  ListSkeleton,
  Pill,
  SKELETON_ROWS,
  guard,
  toastErr,
  toastOk,
  useChangePreview,
  useModuleData,
} from "../bits";
import type { SectionProps } from "../bits";

const SKILLS_DIR = "~/.claude/skills";

function readSkill(): string | null {
  return new URLSearchParams(location.search).get("skill");
}

function writeSkill(slug: string | null) {
  const p = new URLSearchParams(location.search);
  if (slug) p.set("skill", slug);
  else p.delete("skill");
  history.replaceState(history.state, "", `${location.pathname}?${p.toString()}`);
}

function Head({ title, blurb, lead, aside }: { title: ReactNode; blurb: ReactNode; lead?: ReactNode; aside?: ReactNode }) {
  return (
    <div className="settings-head">
      <div className="settings-head-row">
        <div className="cc-skill-headline">
          {lead}
          <h2>{title}</h2>
        </div>
        {aside && <div className="cc-skill-actions">{aside}</div>}
      </div>
      <p>{blurb}</p>
    </div>
  );
}

function SkillDetail({
  slug,
  description,
  linked,
  onBack,
  onChanged,
  onDeleted,
}: {
  slug: string;
  description: string;
  linked: boolean;
  onBack: () => void;
  onChanged: () => void;
  onDeleted: () => void;
}) {
  const { node: modal, ask } = useChangePreview();
  const [path, setPath] = useState("SKILL.md");
  const [files, setFiles] = useState<string[]>(["SKILL.md"]);
  const [saved, setSaved] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
  const dirty = saved !== null && text !== saved;

  useEffect(() => {
    let alive = true;
    setSaved(null);
    setLoadError(null);
    setNote(null);
    cc.skills.read(slug, path).then(
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
  }, [slug, path]);

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
      const res = await cc.skills.write(slug, path, text);
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
    onDeleted();
  };

  return (
    <>
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
        title={
          <>
            {slug}
            <Pill tone={linked ? "on" : "neutral"}>{linked ? "linked" : "not linked"}</Pill>
          </>
        }
        blurb={description || `A skill in ${SKILLS_DIR}/${slug}.`}
        aside={
          <>
            {note && (
              <span className={note.ok ? "cc-skill-note" : "cc-skill-note is-error"} role="status">
                {note.text}
              </span>
            )}
            <Button variant="outline" size="sm" onClick={() => guard(cc.skills.open(slug))}>
              Reveal
            </Button>
            <Button variant="ghost" size="sm" className="cc-skill-delete" onClick={() => void remove()}>
              Delete
            </Button>
            <Button size="sm" disabled={busy || !dirty} onClick={() => void save()}>
              Save
            </Button>
          </>
        }
      />
      {files.length > 1 && (
        <Tabs
          value={path}
          onValueChange={async (v) => {
            if (String(v) !== path && (await discardOk())) setPath(String(v));
          }}
          className="cc-skill-files"
        >
          <TabsList>
            {files.map((f) => (
              <TabsTrigger key={f} value={f}>
                {f}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
      )}
      {loadError ? (
        <ErrorBanner>{loadError}</ErrorBanner>
      ) : saved === null ? (
        <ListSkeleton rows={SKELETON_ROWS} label="Loading skill" />
      ) : (
        <div className="settings-card">
          <Textarea
            aria-label={`${slug}/${path}`}
            value={text}
            spellCheck={false}
            className="cc-skill-editor"
            onChange={(e) => {
              setText(e.target.value);
              setNote(null);
            }}
          />
        </div>
      )}
    </>
  );
}

export default function SkillsSection({ onChanged, filter = "" }: SectionProps) {
  const load = useCallback(() => cc.skills.list(), []);
  const { data, error, reload } = useModuleData(load);
  const [skill, setSkill] = useState<string | null>(readSkill);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");

  const open = (slug: string | null) => {
    setSkill(slug);
    writeSkill(slug);
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

  if (error)
    return (
      <>
        <Head title="Skills" blurb={`Skills in ${SKILLS_DIR}.`} />
        <ErrorBanner>{error}</ErrorBanner>
      </>
    );
  if (!data)
    return (
      <>
        <Head title="Skills" blurb={`Skills in ${SKILLS_DIR}.`} />
        <ListSkeleton rows={SKELETON_ROWS} label="Loading skills" />
      </>
    );

  const picked = skill ? data.skills.find((s) => s.slug === skill) : undefined;
  if (skill && picked) {
    return (
      <SkillDetail
        key={skill}
        slug={picked.slug}
        description={picked.description}
        linked={picked.linked}
        onBack={() => open(null)}
        onChanged={() => {
          onChanged();
          reload();
        }}
        onDeleted={() => {
          onChanged();
          reload();
          open(null);
        }}
      />
    );
  }

  const needle = filter.trim().toLowerCase();
  const shown = data.skills.filter(
    (s) => !needle || `${s.name} ${s.slug} ${s.description}`.toLowerCase().includes(needle),
  );
  const noun = (n: number) => `${n} ${n === 1 ? "skill" : "skills"}`;

  return (
    <>
      <Head
        title="Skills"
        blurb={needle ? `${shown.length} of ${noun(data.skills.length)} match` : `${noun(shown.length)} in ${SKILLS_DIR}`}
        aside={
          <Button size="sm" onClick={() => setCreating((v) => !v)}>
            {creating ? "Cancel" : "New skill"}
          </Button>
        }
      />
      {creating && (
        <div className="cc-skill-new">
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
        <div className="settings-card">
          {shown.map((s) => (
            <button key={s.slug} type="button" className="cc-skill-row" onClick={() => open(s.slug)}>
              <span className="cc-skill-name">{s.name}</span>
              {s.description && (
                <span className="cc-skill-desc" title={s.description}>
                  {s.description}
                </span>
              )}
            </button>
          ))}
        </div>
      )}
    </>
  );
}
