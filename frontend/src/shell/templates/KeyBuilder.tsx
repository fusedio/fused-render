import { useEffect, useState } from "react";
import type { KeyKind } from "@platform/lib/api";
import { buildKey, KEY_KINDS } from "@shell/templates/helpers";
import { FilterGroup } from "@shell/templates/chips";
import { InputGroup, InputGroupAddon, InputGroupInput, InputGroupText } from "@platform/shadcn/ui/input-group";
import { Identifier, Tiny } from "@platform/ui/flow/Typography";

export function KeyBuilder({
  onChange,
  inputId,
  inputRef,
}: {
  onChange: (key: string, valid: boolean) => void;
  inputId?: string;
  inputRef?: React.RefObject<HTMLInputElement | null>;
}) {
  const [kind, setKind] = useState<KeyKind>("simple");
  const [raw, setRaw] = useState("");
  const { key, error } = buildKey(kind, raw);

  // Report the derived key up on every change.
  useEffect(() => {
    onChange(key, error === null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, error]);

  const prefix = kind === "wildcard" ? ".*." : ".";
  const suffix = kind === "directory" ? "/" : "";
  return (
    <div className="flex flex-col gap-2">
      <FilterGroup<KeyKind>
        ariaLabel="Key shape"
        value={kind}
        onChange={setKind}
        options={KEY_KINDS.map((k) => ({ value: k.kind, label: k.label, title: k.hint }))}
      />
      <InputGroup className="font-mono">
        <InputGroupAddon align="inline-start">
          <InputGroupText className="font-mono">{prefix}</InputGroupText>
        </InputGroupAddon>
        <InputGroupInput
          id={inputId}
          ref={inputRef}
          type="text"
          value={raw}
          autoFocus
          placeholder={kind === "compound" ? "geo.parquet" : kind === "wildcard" ? "json" : "csv"}
          onChange={(e) => setRaw(e.target.value)}
          aria-invalid={raw.trim() !== "" && error ? true : undefined}
        />
        {suffix && (
          <InputGroupAddon align="inline-end">
            <InputGroupText className="font-mono">{suffix}</InputGroupText>
          </InputGroupAddon>
        )}
      </InputGroup>
      <Tiny>
        Key: <Identifier className="text-foreground">{key}</Identifier>
      </Tiny>
      {raw.trim() !== "" && error && <div className="text-xs text-destructive">{error}</div>}
    </div>
  );
}
