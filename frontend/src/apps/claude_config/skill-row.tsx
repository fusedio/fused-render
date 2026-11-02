// One installed skill in the Skills list: its name and the CLIs that read it
// over its description, and at the right edge the plugin it came with or, for
// the user's own skill, the dir it lives in.
import * as cc from "./api";
import { CLI_LABEL, visibleReaders } from "./readers";
import { tildify } from "./skill-detail";

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
      <span className="cc-skill-main">
        <span className="cc-skill-title">
          <span className="cc-skill-name">{s.name}</span>
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
      </span>
      <span className="cc-lrow-meta">{s.plugin ?? tildify(s.location)}</span>
    </button>
  );
}
