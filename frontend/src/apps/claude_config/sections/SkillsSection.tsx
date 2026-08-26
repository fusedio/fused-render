// Skills section: a read-only viewer of the user's own skills under
// skills/*/SKILL.md — name + description from the frontmatter, source (when
// the `skills` CLI recorded one), and a `bunx skills add` command to match.
//
// Round 2 fixed four defects the live page showed:
//
//   1. The expanded row repeated its own description verbatim, adding only a
//      "Source" line — one line of new information is not worth a
//      disclosure, so there is none any more. The description gets the room
//      to be read on the row itself instead (wraps rather than ellipsizing
//      mid-sentence — the row is no longer forced to one line at rest, which
//      is the deliberate exception here: a skill's description IS its whole
//      contract with the model, and cutting it off mid-clause defeated the
//      one reason this tab is worth scanning). Source, when recorded, sits in
//      the meta slot instead of behind a second click — between the row and
//      the meta slot, that is everything an expansion would have shown.
//   2. `linked` (installed via the `skills` CLI, a symlink into .agents) was a
//      badge on 9 of 10 rows, distinguishing almost nothing from its position
//      right after the name. It now marks the EXCEPTION — a skill that is
//      NOT linked, i.e. authored or dropped in by hand rather than managed —
//      because that is the one worth calling out.
//   3. "no recorded source" used to render as right-aligned italic meta on
//      the rows lacking one, creating a ragged second column that existed
//      only to say nothing was there. Absent data now renders nothing.
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
        <List>
          {data.skills.map((s) => (
            <ListRow
              key={s.slug}
              name={s.name}
              pills={!s.linked ? <Pill>not linked</Pill> : null}
              secondary={s.description || undefined}
              secondaryClass="cc-lrow-sub-wrap"
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
      )}
    </>
  );
}
