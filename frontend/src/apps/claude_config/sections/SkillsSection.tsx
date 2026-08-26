// Skills section: a read-only viewer of the user's own skills under
// skills/*/SKILL.md — name + description from the frontmatter, source (when
// the `skills` CLI recorded one), and a `bunx skills add` command to match.
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
import { useCallback } from "react";
import { copyToClipboard } from "@platform/lib/clipboard";
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
  useModuleData,
} from "../bits";

export default function SkillsSection() {
  const load = useCallback(() => cc.skills.list(), []);
  const { data, error, reload } = useModuleData(load);

  const share = async (command: string) => {
    if (await copyToClipboard(command)) toastOk("Copied install command");
    else toastErr("Copy failed");
  };

  if (error) return <ErrorBanner>{error}</ErrorBanner>;
  if (!data) return <ListSkeleton rows={SKELETON_ROWS} label="Loading skills" />;

  return (
    <>
      <SectionToolbar summary={`${data.skills.length} skill(s)`} onRefresh={reload} />
      {!data.skills.length && <Empty>No local skills under skills/*/SKILL.md.</Empty>}
      {data.skills.length > 0 && (
        <div className="cc-skills-list">
          <List>
            {data.skills.map((s) => (
              <ListRow
                key={s.slug}
                name={s.name}
                pills={!s.linked ? <Pill>not linked</Pill> : null}
                secondary={s.description || undefined}
                secondaryClass="cc-lrow-sub-clamp2"
                secondaryTitle={s.description || undefined}
                meta={
                  s.source ? <span className="cc-lrow-meta cc-mono">{s.source}</span> : null
                }
                actions={
                  <>
                    <button
                      type="button"
                      className="cc-iconbtn"
                      title="Reveal in Finder"
                      aria-label={`Reveal ${s.name} in Finder`}
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
