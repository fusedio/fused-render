// Skills section: a read-only viewer of the user's own skills under
// skills/*/SKILL.md — name + description from the frontmatter, whether the
// folder is a symlink, and a `bunx skills add` command when the skill's origin
// was recorded. A skill with no recorded source says so rather than offering a
// command that would install the wrong thing.
import { useCallback } from "react";
import { copyToClipboard } from "@platform/lib/clipboard";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import * as cc from "../api";
import {
  Empty,
  Icon,
  ListRow,
  Pill,
  SKELETON_ROWS,
  guard,
  toastErr,
  toastOk,
  useModuleData,
} from "../bits";

export default function SkillsSection() {
  const load = useCallback(() => cc.skills.list(), []);
  const { data, error } = useModuleData(load);

  const share = async (command: string) => {
    if (await copyToClipboard(command)) toastOk("Copied install command");
    else toastErr("Copy failed");
  };

  if (error) return <ErrorBanner>{error}</ErrorBanner>;
  if (!data) return <SkeletonLines rows={SKELETON_ROWS} label="Loading skills" />;
  if (!data.skills.length) return <Empty>No local skills under skills/*/SKILL.md.</Empty>;

  return (
    <>
      {data.skills.map((s) => (
        <ListRow
          key={s.slug}
          name={s.name}
          pills={s.linked ? <Pill>linked</Pill> : null}
          secondary={s.description}
          secondaryTitle={s.description}
          // A skill's description is its whole contract with the model and is
          // routinely a paragraph — exactly the text a one-line row must not
          // destroy, so it is repeated in full here along with where the folder
          // came from.
          details={
            s.description || s.source ? (
              <>
                {s.description && <p>{s.description}</p>}
                {s.source && (
                  <dl className="cc-lrow-dl">
                    <dt className="cc-lrow-dt">Source</dt>
                    <dd className="cc-lrow-dd cc-mono">{s.source}</dd>
                  </dl>
                )}
              </>
            ) : null
          }
          actions={
            <>
              {!s.shareCommand && (
                <span className="cc-unset">no recorded source</span>
              )}
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
    </>
  );
}
