import { useEffect, useState } from "react";
import type { KeyKind } from "../../lib/api";
import { buildKey, KEY_KINDS } from "./helpers";

export function KeyBuilder({ onChange }: { onChange: (key: string, valid: boolean) => void }) {
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
      <div className="templates-seg">
        {KEY_KINDS.map((k) => (
          <button
            key={k.kind}
            type="button"
            className={"templates-seg-btn" + (kind === k.kind ? " active" : "")}
            onClick={() => setKind(k.kind)}
            title={k.hint}
          >
            {k.label}
          </button>
        ))}
      </div>
      <div className="templates-key-input">
        <span className="templates-key-fix">{prefix}</span>
        <input
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
