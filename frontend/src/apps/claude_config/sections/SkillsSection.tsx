// Skills section: Installed (what the CLIs on this machine read) and Discover
// (skills the cloned marketplaces' plugins publish).
//
// Installed lists the user's own skills first, then the skills that arrive
// with enabled plugins, which are read-only. Each row names the CLIs that read
// the skill, but only when more than one CLI is on (see ../readers). Discover
// adds one skill to the user's directories without installing its plugin; the
// overflow menu installs the whole plugin instead.
//
// `?skill=` selects between the list and a skill's editor (SKILL.md and its
// sibling files, one textarea in one card). The value is
// `<location>/<slug>` for a user skill and `plugin:<plugin>/<slug>` for a
// plugin's. A user skill that is not linked (authored by hand rather than
// managed by the `skills` CLI) says so in its editor's header; a plugin skill
// shows its plugin there instead.
import { useCallback, useEffect, useState } from "react";
import { ChevronLeft } from "lucide-react";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { Tabs, TabsList, TabsTrigger } from "@platform/shadcn/ui/tabs";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import * as cc from "../api";
import {
  Empty,
  Head,
  KebabMenu,
  List,
  ListRow,
  ListSkeleton,
  PAGE_SIZE,
  Pager,
  Pill,
  SKELETON_ROWS,
  Seg,
  plural,
  guard,
  toastErr,
  toastOk,
  useConfirm,
  useModuleData,
} from "../bits";
import type { SectionProps } from "../bits";
import { useEnabledClis } from "../readers";
import { SkillRow } from "../skill-row";

type Tab = "installed" | "discover";

const skillKey = (s: cc.SkillRef) =>
  s.source === "plugin" ? `plugin:${s.plugin}/${s.slug}` : `${s.location}/${s.slug}`;

function readSkill(): string | null {
  return new URLSearchParams(location.search).get("skill");
}

function writeSkill(key: string | null) {
  const p = new URLSearchParams(location.search);
  if (key) p.set("skill", key);
  else p.delete("skill");
  history.replaceState(history.state, "", `${location.pathname}?${p.toString()}`);
}

function SkillDetail({
  skill,
  onBack,
  onChanged,
  onDeleted,
}: {
  skill: cc.Skill;
  onBack: () => void;
  onChanged: () => void;
  onDeleted: () => void;
}) {
  const { node: modal, ask } = useConfirm();
  const { slug, description, linked } = skill;
  const readOnly = skill.source === "plugin";
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
            {readOnly ? (
              <Pill>{skill.plugin}</Pill>
            ) : (
              <Pill tone={linked ? "on" : "neutral"}>{linked ? "linked" : "not linked"}</Pill>
            )}
          </>
        }
        blurb={
          <>
            {description || `A skill in ${skill.path}.`}
            {readOnly && (
              <span className="cc-skill-owner">
                Comes with the {skill.plugin} plugin. Updates to the plugin replace it.
              </span>
            )}
          </>
        }
        aside={
          <>
            {note && (
              <span className={note.ok ? "cc-skill-note" : "cc-skill-note is-error"} role="status">
                {note.text}
              </span>
            )}
            <Button variant="outline" size="sm" onClick={() => guard(cc.skills.open(skill))}>
              Reveal
            </Button>
            {!readOnly && (
              <>
                <Button variant="ghost" size="sm" className="cc-skill-delete" onClick={() => void remove()}>
                  Delete
                </Button>
                <Button size="sm" disabled={busy || !dirty} onClick={() => void save()}>
                  Save
                </Button>
              </>
            )}
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
            readOnly={readOnly}
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

const BLURB = "Skills your coding CLIs read.";

export default function SkillsSection({ filter = "" }: SectionProps) {
  const load = useCallback(() => cc.skills.list(), []);
  const { data, error, reload } = useModuleData(load);
  const enabledClis = useEnabledClis();
  const [skill, setSkill] = useState<string | null>(readSkill);
  const [tab, setTab] = useState<Tab>("installed");
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [page, setPage] = useState(0);
  const [busy, setBusy] = useState<string | null>(null);

  // The catalog read is deferred until Discover is asked for, and kept once
  // fetched: switching tabs back and forth must not re-read every marketplace.
  const [avail, setAvail] = useState<cc.AvailableSkills | null>(null);
  const [availError, setAvailError] = useState<string | null>(null);
  const loadAvail = useCallback(() => {
    cc.skills.available().then(
      (a) => {
        setAvail(a);
        setAvailError(null);
      },
      (e: Error) => setAvailError(e.message),
    );
  }, []);
  useEffect(() => {
    if (tab === "discover" && !avail && !availError) loadAvail();
  }, [tab, avail, availError, loadAvail]);

  const open = (key: string | null) => {
    setSkill(key);
    writeSkill(key);
  };

  const pick = (next: Tab) => {
    setTab(next);
    setPage(0);
    setAvailError(null);
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
    reload();
  };

  const addSkill = async (s: cc.AvailableSkill) => {
    setBusy(`${s.plugin}/${s.slug}`);
    try {
      const res = await cc.skills.add(s);
      if (!res.ok) {
        toastErr(res.error || "Add failed");
        return;
      }
      toastOk(`Added ${s.slug}`);
      reload();
      loadAvail();
    } catch (e) {
      toastErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const installPlugin = async (s: cc.AvailableSkill) => {
    setBusy(`${s.plugin}/${s.slug}`);
    try {
      const res = await cc.plugins.install(`${s.plugin}@${s.marketplace}`);
      if (!res.ok) {
        toastErr(res.error || "Install failed");
        return;
      }
      toastOk(`Installed ${s.plugin}`);
      reload();
      loadAvail();
    } catch (e) {
      toastErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  if (error)
    return (
      <>
        <Head title="Skills" blurb={BLURB} />
        <ErrorBanner>{error}</ErrorBanner>
      </>
    );
  if (!data)
    return (
      <>
        <Head title="Skills" blurb={BLURB} />
        <ListSkeleton rows={SKELETON_ROWS} label="Loading skills" />
      </>
    );

  const picked = skill ? data.skills.find((s) => skillKey(s) === skill) : undefined;
  if (skill && picked) {
    return (
      <SkillDetail
        key={skill}
        skill={picked}
        onBack={() => open(null)}
        onChanged={reload}
        onDeleted={() => {
          reload();
          open(null);
        }}
      />
    );
  }

  const needle = filter.trim().toLowerCase();
  const matches = (s: { name: string; slug: string; description: string; plugin?: string }) =>
    !needle || `${s.name} ${s.slug} ${s.description} ${s.plugin ?? ""}`.toLowerCase().includes(needle);
  const mine = data.skills.filter((s) => s.source === "user");
  const bundled = data.skills
    .filter((s) => s.source === "plugin")
    .sort((a, b) => (a.plugin ?? "").localeCompare(b.plugin ?? "") || a.slug.localeCompare(b.slug));
  const pluginCount = new Set(bundled.map((s) => `${s.plugin}@${s.marketplace}`)).size;
  const shownMine = mine.filter(matches);
  const shownBundled = bundled.filter(matches);
  const total = data.skills.length;
  const shown = shownMine.length + shownBundled.length;

  const discover = (avail?.skills ?? []).filter(matches);
  const pages = Math.max(1, Math.ceil(discover.length / PAGE_SIZE));
  const safePage = Math.min(page, pages - 1);
  const paged = discover.slice(safePage * PAGE_SIZE, (safePage + 1) * PAGE_SIZE);

  const row = (s: cc.Skill) => (
    <SkillRow key={skillKey(s)} skill={s} enabledClis={enabledClis} onOpen={() => open(skillKey(s))} />
  );

  return (
    <>
      <Head
        title="Skills"
        blurb={
          tab === "discover"
            ? avail
              ? `${plural(discover.length, "skill")} in your marketplaces`
              : "reading marketplace catalogs…"
            : needle
              ? `${shown} of ${plural(total, "skill")} match`
              : `${plural(total, "skill")} · ${mine.length} yours, ${bundled.length} from ${plural(pluginCount, "plugin")}`
        }
        aside={
          <>
            <Seg
              label="Skill source"
              value={tab}
              onChange={pick}
              options={[
                { id: "installed", label: "Installed", title: "Skills on this machine" },
                { id: "discover", label: "Discover", title: "Skills your marketplaces publish" },
              ]}
            />
            {tab === "installed" && (
              <Button size="sm" onClick={() => setCreating((v) => !v)}>
                {creating ? "Cancel" : "New skill"}
              </Button>
            )}
          </>
        }
      />
      {tab === "installed" && (
        <>
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
          {needle && !shown && <Empty>No skills match.</Empty>}
          {(shownMine.length > 0 || !needle) && (
            <section className="settings-section">
              <h3 className="settings-section-title">Your skills</h3>
              {shownMine.length > 0 ? (
                <div className="settings-card">{shownMine.map(row)}</div>
              ) : (
                !needle && (
                  <div className="settings-card">
                    <p className="cc-skill-empty">No skills of your own yet.</p>
                  </div>
                )
              )}
            </section>
          )}
          {shownBundled.length > 0 && (
            <section className="settings-section">
              <h3 className="settings-section-title">From plugins</h3>
              <div className="settings-card">{shownBundled.map(row)}</div>
            </section>
          )}
        </>
      )}
      {tab === "discover" && (
        <>
          {availError && <ErrorBanner>{availError}</ErrorBanner>}
          {avail && avail.skipped > 0 && (
            <div className="cc-change">
              {plural(avail.skipped, "plugin")} fetch their skills from elsewhere and are not listed.
            </div>
          )}
          {!avail && !availError && (
            <ListSkeleton rows={SKELETON_ROWS} label="Reading marketplace catalogs" />
          )}
          {paged.length > 0 && (
            <List>
              {paged.map((s) => {
                const id = `${s.plugin}/${s.slug}`;
                return (
                  <ListRow
                    key={`${s.marketplace}/${id}`}
                    stacked
                    name={s.name}
                    secondary={s.description}
                    secondaryTitle={s.description}
                    meta={
                      <span className="cc-lrow-meta">
                        {s.plugin} · {s.marketplace}
                      </span>
                    }
                    actions={
                      <KebabMenu
                        label={`Actions for ${s.name}`}
                        items={[
                          {
                            label: "Install whole plugin",
                            disabled: busy === id,
                            onClick: () => void installPlugin(s),
                          },
                        ]}
                      />
                    }
                    end={
                      s.installed ? (
                        <span className="cc-lrow-meta">Added</span>
                      ) : (
                        <button
                          type="button"
                          className="btn"
                          disabled={busy === id}
                          onClick={() => void addSkill(s)}
                        >
                          {busy === id ? "Adding…" : "Add"}
                        </button>
                      )
                    }
                  />
                );
              })}
            </List>
          )}
          <Pager
            page={safePage}
            total={discover.length}
            label="Discover results pages"
            onPage={setPage}
          />
          {avail && discover.length === 0 && (
            <Empty>
              {avail.skills.length === 0
                ? "No marketplace skills to read. Add a marketplace in Plugins and its skills show up here."
                : "No skills match."}
            </Empty>
          )}
        </>
      )}
    </>
  );
}
