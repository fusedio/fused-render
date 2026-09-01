import { useEffect, useState } from "react";
import type { KeyKind } from "@platform/lib/api";
import { buildKey, KEY_KINDS } from "@shell/templates/helpers";
import { ToggleGroup, ToggleGroupItem } from "@platform/shadcn/ui/toggle-group";
import { Input } from "@platform/shadcn/ui/input";

export function KeyBuilder({
  onChange,
  inputId,
}: {
  onChange: (key: string, valid: boolean) => void;
  inputId?: string;
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
    <div className="templates-keybuilder">
      <ToggleGroup
        variant="outline"
        value={[kind]}
        onValueChange={(groupValue) => {
          const next = (groupValue as KeyKind[])[0];
          if (next) setKind(next);
        }}
      >
        {KEY_KINDS.map((k) => (
          <ToggleGroupItem key={k.kind} value={k.kind} title={k.hint}>
            {k.label}
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
      <div className="templates-key-input">
        <span className="templates-key-fix">{prefix}</span>
        <Input
          id={inputId}
          type="text"
          value={raw}
          autoFocus
          placeholder={kind === "compound" ? "geo.parquet" : kind === "wildcard" ? "json" : "csv"}
          onChange={(e) => setRaw(e.target.value)}
        />
        {suffix && <span className="templates-key-fix">{suffix}</span>}
      </div>
      <div className="templates-key-preview">
        Key: <code>{key}</code>
      </div>
      {raw.trim() !== "" && error && <div className="templates-key-error">{error}</div>}
    </div>
  );
}
