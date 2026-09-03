// The (i) at a row's end, and the popover it opens: everything about a model
// that is IDENTITY rather than STATE — engine, parameters, quantization,
// format, when it was added. Each is read once, by somebody deciding about ONE
// model; none by sweeping a list, which is what the row's face is for.
//
// A shadcn Popover (portalled, positioned by the primitive), so opening it
// cannot change the row's box. One grey for everything in it: this is a fact
// sheet, and a fact sheet in five colours reads as five kinds of urgency.
import { useState } from "react";
import { InfoIcon } from "lucide-react";
import { type AiModelRepo } from "@platform/lib/api";
import { formatParams, timeAgo } from "@platform/lib/format";
import { navigate, urlForFsPath } from "@platform/lib/router";
import { Button } from "@platform/shadcn/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@platform/shadcn/ui/popover";
import { PropertyList, PropertyRow } from "@platform/ui/flow/PropertyRow";
import { noEngineReason } from "@apps/ai_models/lib/aiModelGroups";

/** One line of the panel. */
export interface InfoRow {
  label: string;
  /** `null` for a row that has nothing to say — the row then does not exist. */
  value: string | null;
  /** The long version, on the app's instant hint rather than a native `title`. */
  hint?: string;
}

/** What the footer link does, for a row that has somewhere local to send you.
 *  A recommendation has nowhere — the model is not on this disk yet. */
interface InfoMore {
  label: string;
  href: string;
  onOpen: () => void;
}

/** The (i) and its panel, owned together. Takes rows rather than a repo,
 *  because three different rows open one and only one has an `AiModelRepo`
 *  behind it — a recommendation and a Hub result know their engine and nothing
 *  else about the weights. */
export function InfoButton({ name, rows, more }: { name: string; rows: InfoRow[]; more?: InfoMore }) {
  const [open, setOpen] = useState(false);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        render={
          <Button variant="ghost" size="icon-xs" data-hint={`About ${name}`} aria-label={`About ${name}`} />
        }
      >
        <InfoIcon />
      </PopoverTrigger>
      <PopoverContent align="end" aria-label={`About ${name}`}>
        <PropertyList>
          {rows
            .filter((row) => row.value)
            .map((row) => (
              <PropertyRow key={row.label} label={row.label}>
                <span data-hint={row.hint}>{row.value}</span>
              </PropertyRow>
            ))}
        </PropertyList>
        {more && (
          <a
            className="text-xs text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
            href={more.href}
            onClick={(e) => {
              if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
                return;
              e.preventDefault();
              setOpen(false);
              more.onOpen();
            }}
          >
            {more.label}
          </a>
        )}
      </PopoverContent>
    </Popover>
  );
}

/** The same control, for a repo that is actually ON this disk — the only row
 *  that can answer more than "which engine". */
export function ModelInfoButton({ repo }: { repo: AiModelRepo }) {
  const added = timeAgo(repo.added);
  return (
    <InfoButton
      name={repo.id}
      rows={[
        // The ENGINE first and always: the one fact this page could not answer
        // from the model's name, and "None" is its most useful state.
        {
          label: "Engine",
          value: repo.engine ? repo.engine.shortLabel : "None",
          hint: repo.engine ? undefined : noEngineReason(repo),
        },
        {
          label: "Parameters",
          value: repo.params === null ? null : `${repo.paramsEstimated ? "≈" : ""}${formatParams(repo.params)}`,
          hint: repo.paramsEstimated
            ? "Recovered from packed weights, so it rests on the width the checkpoint declares"
            : undefined,
        },
        { label: "Quantization", value: repo.quantization },
        // Only where no engine claimed the repo: an engine tag IS a format claim.
        { label: "Format", value: repo.engine ? null : repo.library },
        { label: "Added", value: added ? added : null },
      ]}
      /* The local door: this folder's own model card (SPEC §38). The row's NAME
         still goes to the Hub — two destinations, two controls. */
      more={{
        label: "Know more",
        href: urlForFsPath(repo.path, "?_mode=model_card"),
        onOpen: () => navigate(repo.path, { isDir: true, mode: "model_card" }),
      }}
    />
  );
}
