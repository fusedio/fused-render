// Skills section: Installed (what the CLIs on this machine read) and Discover
// (skills the registered marketplaces' plugins publish).
//
// Installed lists the user's own skills first, then the skills that arrive
// with enabled plugins, which are read-only. Each row names the CLIs that read
// the skill, but only when more than one CLI is on (see ../readers). Discover
// groups the skills by plugin: a section's header installs the whole plugin,
// and each skill under it is copied into the user's directories on its own.
//
// `?skill=` selects between the list and a skill's page (../skill-detail). The
// value is `<location>/<slug>` for a user skill and `plugin:<plugin>/<slug>`
// for a plugin's.
import { useCallback, useEffect, useState } from "react";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import * as cc from "../api";
import {
  Empty,
  Head,
  ListSkeleton,
  PAGE_SIZE,
  Pager,
  SKELETON_ROWS,
  Seg,
  plural,
  guard,
  toastErr,
  toastOk,
  useModuleData,
} from "../bits";
import type { SectionProps } from "../bits";
import { useEnabledClis } from "../readers";
import { SkillDetail } from "../skill-detail";
import { SkillGroupSection, groupSkills, type SkillGroup } from "../skill-discover";
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

const BLURB = "Skills your coding CLIs read.";

export default function SkillsSection({
  filter = "",
  onOpenPluginsDiscover,
}: SectionProps & {
  /** Switch the page to the Plugins tab with its Discover list selected. */
  onOpenPluginsDiscover?: () => void;
}) {
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
    setBusy(`${s.plugin}@${s.marketplace}/${s.slug}`);
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

  const installPlugin = async (g: SkillGroup) => {
    setBusy(g.key);
    try {
      const res = await cc.plugins.install(g.key);
      if (!res.ok) {
        toastErr(res.error || "Install failed");
        return;
      }
      toastOk(`Installed ${g.plugin}`);
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
        enabledClis={enabledClis}
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

  const groups = groupSkills(avail?.skills ?? [], filter);
  const skillCount = groups.reduce((n, g) => n + g.skills.length, 0);
  const pages = Math.max(1, Math.ceil(groups.length / PAGE_SIZE));
  const safePage = Math.min(page, pages - 1);
  const paged = groups.slice(safePage * PAGE_SIZE, (safePage + 1) * PAGE_SIZE);

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
              ? `${plural(skillCount, "skill")} in ${plural(groups.length, "plugin")}`
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
            <p className="cc-skill-note-line">
              {plural(avail.skipped, "plugin")} fetch their skills from elsewhere and are not listed.{" "}
              {onOpenPluginsDiscover && (
                <button type="button" className="cc-link" onClick={onOpenPluginsDiscover}>
                  Browse them in Plugins → Discover
                </button>
              )}
            </p>
          )}
          {!avail && !availError && (
            <ListSkeleton rows={SKELETON_ROWS} label="Reading marketplace catalogs" />
          )}
          {paged.map((g) => (
            <SkillGroupSection
              key={g.key}
              group={g}
              busy={busy}
              onAdd={(sk) => void addSkill(sk)}
              onInstall={(grp) => void installPlugin(grp)}
            />
          ))}
          <Pager
            page={safePage}
            total={groups.length}
            label="Discover results pages"
            onPage={setPage}
          />
          {avail && groups.length === 0 && (
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
