// Discover on the Skills tab: the skills the registered marketplaces' plugins
// publish, one section per plugin. A section's header installs the whole
// plugin; each skill under it can be copied in on its own.
import type { AvailableSkill } from "./api";

export interface SkillGroup {
  key: string;
  plugin: string;
  marketplace: string;
  description: string;
  pluginInstalled: boolean;
  skills: AvailableSkill[];
}

/** The skills grouped by plugin, narrowed by the filter. A plugin whose name
 *  matches keeps all its skills; otherwise only the skills that match, and a
 *  plugin with none is dropped. */
export function groupSkills(all: AvailableSkill[], filter: string): SkillGroup[] {
  const needle = filter.trim().toLowerCase();
  const groups = new Map<string, SkillGroup>();
  for (const s of all) {
    const key = `${s.plugin}@${s.marketplace}`;
    let g = groups.get(key);
    if (!g) {
      g = {
        key,
        plugin: s.plugin,
        marketplace: s.marketplace,
        description: s.pluginDescription,
        pluginInstalled: s.pluginInstalled,
        skills: [],
      };
      groups.set(key, g);
    }
    g.skills.push(s);
  }
  const keep = (g: SkillGroup): SkillGroup | null => {
    if (!needle || g.plugin.toLowerCase().includes(needle)) return g;
    const skills = g.skills.filter((s) =>
      `${s.name} ${s.slug} ${s.description}`.toLowerCase().includes(needle),
    );
    return skills.length ? { ...g, skills } : null;
  };
  return [...groups.values()]
    .map(keep)
    .filter((g): g is SkillGroup => g !== null)
    .sort((a, b) => a.plugin.localeCompare(b.plugin) || a.marketplace.localeCompare(b.marketplace));
}

export function SkillGroupSection({
  group: g,
  busy,
  onAdd,
  onInstall,
}: {
  group: SkillGroup;
  /** `plugin@marketplace` while installing it, `plugin@marketplace/slug` while adding a skill. */
  busy: string | null;
  onAdd: (s: AvailableSkill) => void;
  onInstall: (g: SkillGroup) => void;
}) {
  return (
    <section className="settings-card cc-sgroup" aria-label={g.plugin}>
      <header className="cc-sgroup-head">
        <span className="cc-sgroup-title">
          <span className="cc-sgroup-name">{g.plugin}</span>
          <span className="cc-lrow-meta">· {g.marketplace}</span>
          {g.description && (
            <span className="cc-sgroup-desc" title={g.description}>
              {g.description}
            </span>
          )}
        </span>
        {g.pluginInstalled ? (
          <span className="cc-lrow-meta">Installed</span>
        ) : (
          <button
            type="button"
            className="cc-btn-sm"
            disabled={busy === g.key}
            title={`claude plugin install ${g.key}`}
            onClick={() => onInstall(g)}
          >
            {busy === g.key ? "Installing…" : "Install plugin"}
          </button>
        )}
      </header>
      {g.skills.map((s) => {
        const id = `${g.key}/${s.slug}`;
        return (
          <div key={id} className="cc-srow">
            <span className="cc-srow-name">{s.name}</span>
            <span className="cc-srow-desc" title={s.description}>
              {s.description}
            </span>
            {s.pluginInstalled ? (
              <span className="cc-lrow-meta">Installed</span>
            ) : s.added ? (
              <span className="cc-lrow-meta">Added</span>
            ) : (
              <button
                type="button"
                className="cc-btn-sm"
                disabled={busy === id}
                onClick={() => onAdd(s)}
              >
                {busy === id ? "Adding…" : "Add"}
              </button>
            )}
          </div>
        );
      })}
    </section>
  );
}
