// Settings › Instructions (an expert only): the expert's instructions.md, the whole file, in an editor that fills the
// section and scrolls inside itself. The text shown is what the expert runs with (GET /api/experts/<id>/instructions:
// the file, the default text when there is none, then a legacy Settings extra as its last section). The draft lives in
// BotSettings, so it survives a switch of section and counts as an unsaved change there: the dialog's own Save writes
// it (before the other settings), Cancel drops it, Escape and the backdrop ask first. A refused save shows its
// sentence here and keeps the draft.
import { Textarea } from "@platform/shadcn/ui/textarea";
import type { ExpertInstructions } from "../lib/api";
import { instructionsNotes, instructionsUsage } from "../lib/expert";

export interface InstructionsSectionProps {
  /** null until the read lands (or when it failed: `error` says so). */
  info: ExpertInstructions | null;
  draft: string;
  onChange: (text: string) => void;
  /** Why the read or the last save failed ("" = neither did). */
  error: string;
  saving: boolean;
}

export function InstructionsSection({ info, draft, onChange, error, saving }: InstructionsSectionProps) {
  const usage = info ? instructionsUsage(draft, info.limit) : null;
  const notes = info ? instructionsNotes(info) : [];
  return (
    <div className="flex h-full min-h-0 flex-col gap-2.5" id="xinstr">
      <div className="flex min-w-0 flex-col gap-0.5">
        <label htmlFor="bminstrfile" className="text-sm font-medium">Instructions</label>
        <p className="m-0 text-[13px] leading-5 text-muted-foreground">What this expert is told at the start of every chat and session. After a change, its chat starts a new session.</p>
      </div>
      <Textarea id="bminstrfile" value={draft} disabled={!info || saving} spellCheck={false} aria-invalid={usage?.over || undefined}
        placeholder={info ? "Write what this expert should be told." : error ? "" : "Loading…"} onChange={(e) => onChange(e.target.value)}
        className="min-h-0 flex-1 resize-none field-sizing-fixed overflow-y-auto font-mono text-[13px] leading-5 md:text-[13px]" />
      {error ? <p role="alert" className="m-0 text-[13px] leading-5 text-destructive" id="xinstrerr">{error}</p> : null}
      <div className="flex shrink-0 items-start justify-between gap-6 text-xs leading-5 text-muted-foreground">
        <span className="min-w-0">{notes.join(" ")}</span>
        {usage ? <span className={usage.over ? "shrink-0 text-destructive" : "shrink-0"} id="xinstrcount">{usage.text}</span> : null}
      </div>
    </div>
  );
}
