// One installed skill in the Skills list: its name, the plugin it came with,
// the CLIs that read it, and its description on a muted line beneath.
import * as cc from "./api";
import { Pill } from "./bits";
import { CLI_LABEL, visibleReaders } from "./readers";

export function SkillRow({
  skill: s,
  enabledClis,
  onOpen,
}: {
  skill: cc.Skill;
  enabledClis: cc.CliId[];
  onOpen: () => void;
}) {
  const readers = visibleReaders(s.readers, enabledClis);
  return (
    <button type="button" className="cc-skill-row" onClick={onOpen}>
      <span className="cc-skill-title">
        <span className="cc-skill-name">{s.name}</span>
        {s.plugin && <Pill>{s.plugin}</Pill>}
        {readers.map((r) => (
          <span key={r} className="cc-reader">
            {CLI_LABEL[r]}
          </span>
        ))}
      </span>
      {s.description && (
        <span className="cc-skill-desc" title={s.description}>
          {s.description}
        </span>
      )}
    </button>
  );
}
