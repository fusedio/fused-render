// Skills section: a read-only viewer of the user's own skills under
// skills/*/SKILL.md — name + description from the frontmatter, source (when
// the `skills` CLI recorded one), and a `bunx skills add` command to match.
//
// No disclosure: the description IS a skill's whole contract with the model,
// so it gets the room to be read on the row itself (clamped to two lines so
// one long entry cannot run four times taller than its neighbours). Source,
// when recorded, sits in the meta slot. `linked` (installed via the `skills`
// CLI) is the norm, so the badge marks the EXCEPTION — a skill that is NOT
// linked, i.e. authored or dropped in by hand. Absent data renders nothing.
//
// The "not linked" badge stacks UNDER the name inside the name box rather than
// riding beside it, so it can never push the description's start x rightward
// on the one row that carries it.
import { useCallback } from "react";
import { Copy, Folder } from "lucide-react";
import { copyToClipboard } from "@platform/lib/clipboard";
import { Button } from "@platform/shadcn/ui/button";
import * as cc from "../api";
import {
  Empty,
  ErrorNote,
  List,
  ListRow,
  ListSkeleton,
  Meta,
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

  if (error) return <ErrorNote>{error}</ErrorNote>;
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
              name={
                <span className="flex flex-col items-start gap-0.5 w-44">
                  <span className="truncate max-w-full">{s.name}</span>
                  {!s.linked && <Pill>not linked</Pill>}
                </span>
              }
              secondary={s.description || undefined}
              secondaryClamp2
              secondaryTitle={s.description || undefined}
              meta={s.source ? <Meta mono className="hidden lg:inline">{s.source}</Meta> : null}
              actions={
                <>
                  <Button
                    variant="ghost"
                    size="icon-xs"
                    title="Reveal in Finder"
                    aria-label={`Reveal ${s.name} in Finder`}
                    onClick={() => guard(cc.skills.open(s.slug))}
                  >
                    <Folder />
                  </Button>
                  {s.shareCommand && (
                    <Button
                      variant="ghost"
                      size="icon-xs"
                      title={`Copy install command — ${s.shareCommand}`}
                      aria-label={`Copy the install command for ${s.name}`}
                      onClick={() => share(s.shareCommand as string)}
                    >
                      <Copy />
                    </Button>
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
