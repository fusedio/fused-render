// The copy's NEW files (ones it has never had), offered as checkboxes when
// the user sends their changes — Propose and Update proposal alike. Every one
// starts unticked: a new file goes out only when the user ticks it (the
// action's `include`), so a stray notes file or a key never leaves by default.
import { Checkbox } from "@platform/shadcn/ui/checkbox";

export function IncludeFiles({
  files,
  picked,
  onChange,
}: {
  files: string[];
  picked: ReadonlySet<string>;
  onChange: (next: ReadonlySet<string>) => void;
}) {
  if (files.length === 0) return null;
  return (
    <fieldset className="contrib-include">
      <legend className="contrib-include-legend">
        New files <span className="font-normal text-muted-foreground">(sent only if you tick them)</span>
      </legend>
      <ul className="contrib-include-list">
        {files.map((f) => (
          <li key={f}>
            <label className="contrib-check">
              <Checkbox
                checked={picked.has(f)}
                onCheckedChange={(v) => {
                  const next = new Set(picked);
                  if (v === true) next.add(f);
                  else next.delete(f);
                  onChange(next);
                }}
              />
              <code className="contrib-include-file">{f}</code>
            </label>
          </li>
        ))}
      </ul>
    </fieldset>
  );
}

/** The ticked files, in the order listed and only those still offered. */
export function pickedOf(files: string[], picked: ReadonlySet<string>): string[] {
  return files.filter((f) => picked.has(f));
}
