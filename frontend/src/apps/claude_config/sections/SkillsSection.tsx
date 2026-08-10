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
  Card,
  CardActions,
  CardSub,
  CardTitle,
  Empty,
  Pill,
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
  if (!data) return <SkeletonLines rows={3} label="Loading skills" />;
  if (!data.skills.length) return <Empty>No local skills under skills/*/SKILL.md.</Empty>;

  return (
    <>
      {data.skills.map((s) => (
        <Card key={s.slug}>
          <CardTitle>
            {s.name} {s.linked && <Pill>linked</Pill>}
          </CardTitle>
          <CardSub>{s.description}</CardSub>
          <CardActions>
            <button type="button" className="btn" onClick={() => guard(cc.skills.open(s.slug))}>
              Reveal in Finder
            </button>
            {s.shareCommand ? (
              <button
                type="button"
                className="btn"
                title={s.shareCommand}
                onClick={() => share(s.shareCommand as string)}
              >
                Copy install command
              </button>
            ) : (
              <span className="cc-unset">not shareable (no recorded source)</span>
            )}
          </CardActions>
        </Card>
      ))}
    </>
  );
}
