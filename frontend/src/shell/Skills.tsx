// Skills page — `/skills`. One search box over the skills on this machine and
// skills.sh; `New skill` opens the Draft with Claude flow (`?new=1`); `?skill=`
// opens one skill's page. Marketplace plugins are NOT searched here, they live
// under Claude Code → Plugins. Nothing on this page shows or ranks by how many
// people installed a skill.
import { useCallback, useEffect, useRef, useState } from "react";
import { Search, Sparkles, X } from "lucide-react";
import { navigateUrl, replaceSearch } from "@platform/lib/router";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import * as cc from "@apps/claude_config/api";
import { ListSkeleton, SKELETON_ROWS, guard, toastErr, toastOk, useModuleData } from "@apps/claude_config/bits";
import { useEnabledClis } from "@apps/claude_config/readers";
import { SkillDetail } from "@apps/claude_config/skill-detail";
import { SkillRow } from "@apps/claude_config/skill-row";
import { NewSkill } from "./skills/NewSkill";
import { RemotePreview } from "./skills/RemotePreview";
import {
  REMOTE_MIN,
  matchSkills,
  readParams,
  skillKey,
  useDebounced,
  writeParams,
  type PageParams,
} from "./skills/model";

type Remote =
  | { state: "idle" }
  | { state: "loading" }
  | { state: "ok"; rows: cc.RemoteSkill[] }
  | { state: "error"; error: string };

const SEARCH_PLACEHOLDER = "Search your skills and skills.sh";

export default function Skills() {
  const load = useCallback(() => cc.skills.list(), []);
  const { data, error, reload } = useModuleData(load);
  const enabledClis = useEnabledClis();
  const [params, setParams] = useState<PageParams>(() => readParams(location.search));
  const [filter, setFilter] = useState("");
  const [remote, setRemote] = useState<Remote>({ state: "idle" });
  const [attempt, setAttempt] = useState(0);
  const [picked, setPicked] = useState<cc.RemoteSkill | null>(null);
  const [adding, setAdding] = useState<string | null>(null);
  const [added, setAdded] = useState<ReadonlySet<string>>(new Set());
  const searchRef = useRef<HTMLDivElement>(null);

  const q = filter.trim();
  const dq = useDebounced(q, 250);

  const go = (next: Partial<PageParams>) => {
    setParams((p) => ({ ...p, ...next }));
    replaceSearch(`${location.pathname}${writeParams(location.search, next)}`);
  };

  // `/` focuses the search box, unless the key is typing into something.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
      e.preventDefault();
      searchRef.current?.querySelector("input")?.focus();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  // skills.sh, debounced: nothing is asked under two letters.
  useEffect(() => {
    if (dq.length < REMOTE_MIN) {
      setRemote({ state: "idle" });
      setPicked(null);
      return;
    }
    let alive = true;
    setRemote({ state: "loading" });
    cc.skills.search(dq).then(
      (r) =>
        alive &&
        setRemote(r.ok ? { state: "ok", rows: r.skills ?? [] } : { state: "error", error: r.error || "skills.sh search failed" }),
      (e) => alive && setRemote({ state: "error", error: (e as Error).message }),
    );
    return () => {
      alive = false;
    };
  }, [dq, attempt]);

  const addRemote = async (r: cc.RemoteSkill) => {
    setAdding(r.id);
    const res = await guard(cc.skills.remoteAdd(r.source, r.skillId));
    setAdding(null);
    if (!res) return;
    if (!res.ok) {
      toastErr(res.error || "Add failed");
      return;
    }
    toastOk(`Added ${res.slug ?? r.skillId}`);
    setAdded((s) => new Set(s).add(r.id));
    reload();
  };

  const header = (withSearch: boolean) => (
    <div className="settings-header">
      <h1 className="settings-title m-0 inline-flex items-center gap-2">
        <Sparkles aria-hidden="true" className="size-5" />
        Skills
      </h1>
      {withSearch ? (
        <div className="settings-search" role="search" ref={searchRef}>
          <Search aria-hidden="true" className="settings-search-icon" />
          <Input
            type="search"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder={SEARCH_PLACEHOLDER}
            aria-label={SEARCH_PLACEHOLDER}
            className="settings-search-input"
            autoComplete="off"
            spellCheck={false}
          />
          {filter && (
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="Clear search"
              className="settings-search-clear"
              onClick={() => setFilter("")}
            >
              <X />
            </Button>
          )}
        </div>
      ) : (
        <div style={{ flex: "999 1 0" }} />
      )}
      {withSearch && (
        <Button onClick={() => go({ newSkill: true, desc: "" })}>New skill</Button>
      )}
    </div>
  );

  const frame = (main: React.ReactNode, aside?: React.ReactNode, withSearch = false) => (
    <div className="settings-page cc-page cc-skills-page">
      {header(withSearch)}
      <div className="settings-body">
        <main className="settings-main">{main}</main>
        {aside}
      </div>
    </div>
  );

  if (params.newSkill) {
    return frame(
      <NewSkill
        initialDesc={params.desc}
        existing={data?.skills.map((x) => x.slug) ?? []}
        onClose={() => go({ newSkill: false, desc: "" })}
        onSaved={(key) => {
          reload();
          go({ newSkill: false, desc: "", skill: key });
        }}
      />,
    );
  }

  if (error) return frame(<ErrorBanner>{error}</ErrorBanner>, undefined, true);
  if (!data) return frame(<ListSkeleton rows={SKELETON_ROWS} label="Loading skills" />, undefined, true);

  const open = params.skill ? data.skills.find((s) => skillKey(s) === params.skill) : undefined;
  if (open) {
    return frame(
      <SkillDetail
        key={params.skill}
        skill={open}
        enabledClis={enabledClis}
        onBack={() => go({ skill: null })}
        onChanged={reload}
        onDeleted={() => {
          reload();
          go({ skill: null });
        }}
      />,
    );
  }

  const row = (s: cc.Skill) => (
    <SkillRow key={skillKey(s)} skill={s} enabledClis={enabledClis} onOpen={() => go({ skill: skillKey(s) })} />
  );
  const searching = q.length > 0;
  const matched = matchSkills(data.skills, q);
  const mine = matched.filter((s) => s.source === "user");
  const bundled = matched
    .filter((s) => s.source === "plugin")
    .sort((a, b) => (a.plugin ?? "").localeCompare(b.plugin ?? "") || a.slug.localeCompare(b.slug));
  const remoteSettled = q.length >= REMOTE_MIN && q === dq;
  const remoteRows = remote.state === "ok" && remoteSettled ? remote.rows : [];
  const nothing =
    searching &&
    matched.length === 0 &&
    (q.length < REMOTE_MIN || (remoteSettled && (remote.state === "error" || (remote.state === "ok" && remoteRows.length === 0))));
  const isAdded = (r: cc.RemoteSkill) => r.added || added.has(r.id);

  const list = (
    <>
      {!searching && (
        <p className="cc-skills-blurb m-0">Each skill teaches your coding CLIs how to do one job.</p>
      )}
      {(mine.length > 0 || !searching) && (
        <section className="settings-section">
          <h3 className="settings-section-title">{searching ? "Installed" : "Your skills"}</h3>
          <div className="settings-card">
            {mine.length > 0 ? mine.map(row) : <p className="cc-skill-empty">No skills of your own yet.</p>}
          </div>
        </section>
      )}
      {bundled.length > 0 && (
        <section className="settings-section">
          <h3 className="settings-section-title">From Claude Code plugins</h3>
          <p className="cc-skill-note-line">
            Read-only, managed in{" "}
            <a
              href="/claude-config?tab=plugins"
              className="cc-link"
              onClick={(e) => {
                e.preventDefault();
                navigateUrl("/claude-config?tab=plugins");
              }}
            >
              Claude Code → Plugins
            </a>
            .
          </p>
          <div className="settings-card">{bundled.map(row)}</div>
        </section>
      )}
      {!searching && <div className="cc-findmore">Type {REMOTE_MIN}+ letters to search skills.sh too.</div>}
      {q.length >= REMOTE_MIN && (
        <section className="settings-section" aria-label="Add from skills.sh">
          <h3 className="settings-section-title">Add from skills.sh</h3>
          {(remote.state === "loading" || !remoteSettled) && (
            <ListSkeleton rows={3} label="Searching skills.sh" />
          )}
          {remoteSettled && remote.state === "error" && (
            <ErrorBanner>
              <b>Couldn&apos;t reach skills.sh.</b> Installed skills still show.{" "}
              <button type="button" className="cc-link" onClick={() => setAttempt((n) => n + 1)}>
                Retry
              </button>
            </ErrorBanner>
          )}
          {remoteRows.length > 0 && (
            <div className="settings-card">
              {remoteRows.map((r) => (
                <div key={r.id} className={"cc-srow" + (picked?.id === r.id ? " is-active" : "")}>
                  <button type="button" className="cc-srow-pick" onClick={() => setPicked(r)}>
                    <span className="cc-srow-name">{r.name}</span>
                    <span className="cc-srow-desc">{r.source}</span>
                  </button>
                  {isAdded(r) ? (
                    <span className="cc-lrow-meta">Added</span>
                  ) : (
                    <button
                      type="button"
                      className="cc-btn-sm"
                      disabled={adding === r.id}
                      onClick={() => void addRemote(r)}
                    >
                      {adding === r.id ? "Adding…" : "Add"}
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
        </section>
      )}
      {nothing && (
        <div className="cc-skills-none">
          <p className="m-0">
            {remote.state === "error" || q.length < REMOTE_MIN
              ? `No installed skills match "${q}".`
              : `Nothing installed and nothing on skills.sh matches "${q}".`}
          </p>
          <div className="cc-skills-chips">
            <button type="button" className="cc-chip is-on" onClick={() => go({ newSkill: true, desc: q })}>
              Write a &quot;{q}&quot; skill
            </button>
            <button type="button" className="cc-chip" onClick={() => setFilter("")}>
              Clear search
            </button>
          </div>
        </div>
      )}
    </>
  );

  const aside =
    picked && remoteRows.some((r) => r.id === picked.id) ? (
      <RemotePreview
        skill={picked}
        added={isAdded(picked)}
        busy={adding === picked.id}
        readers={enabledClis}
        onAdd={() => void addRemote(picked)}
      />
    ) : undefined;

  return frame(list, aside, true);
}
