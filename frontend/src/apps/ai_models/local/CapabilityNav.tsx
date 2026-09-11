// The Local tab's only global navigation: one row per capability, plus the
// one bucket that is not a capability ("Engine files"). Everything else on
// the tab is scoped to whichever of these is selected.
//
// Transparent on purpose (`.tp-nav` in ai-models.css) — the app already has a
// filled sidebar to the left, and a second filled panel beside it reads as
// two chromes rather than one page with an index inside it. Ported from the
// approved mockup's `nav()`/`item()` functions verbatim: markup, class
// names and copy match, only the data source changed (the mockup's `mine()`
// fixture becomes a `count` prop the caller derives from `mergeSections`).
import { capabilityMeta, PARTS_ICON } from "@apps/ai_models/lib/capabilityMeta";
import type { SectionRunner } from "@apps/ai_models/lib/aiModelGroups";

export interface CapabilityNavEntry {
  /** The capability tag, e.g. "text-generation". */
  key: string;
  /** How many of this capability's models are on this Mac. */
  count: number;
  /** The catalog's runner verdict for this capability, or null when the
   *  catalog has nothing to say (treated as available — a capability with no
   *  catalog entry has nothing to be UNavailable about). */
  runner: SectionRunner | null;
}

export interface CapabilityNavProps {
  capabilities: CapabilityNavEntry[];
  /** How many files sit in the Engine files bucket — fetched by an engine,
   *  belonging to no capability of their own. */
  partsCount: number;
  /** The selected capability's key, or "parts", or null before anything has
   *  loaded. */
  selected: string | null;
  onSelect: (key: string) => void;
}

function NavRow({
  navKey,
  icon,
  title,
  count,
  off,
  offReason,
  selected,
  onSelect,
}: {
  navKey: string;
  icon: string;
  title: string;
  count: number;
  off: boolean;
  offReason: string | null;
  selected: boolean;
  onSelect: (key: string) => void;
}) {
  const hint = off
    ? `Not available on this device: ${offReason ?? ""}`
    : count
      ? `${count} on this Mac`
      : "Nothing downloaded yet";
  return (
    <button
      type="button"
      data-nav={navKey}
      className={`${selected ? "on" : ""}${off ? " off" : ""}`}
      aria-disabled={off || undefined}
      title={hint}
      onClick={() => onSelect(navKey)}
    >
      <span className="capicon" dangerouslySetInnerHTML={{ __html: icon }} />
      <span className="n">{title}</span>
      <span className={`c${count ? "" : " none"}`}>{off ? "n/a" : count || 0}</span>
    </button>
  );
}

export function CapabilityNav({ capabilities, partsCount, selected, onSelect }: CapabilityNavProps) {
  return (
    <div className="tp-nav" data-part="nav">
      <h5>What this Mac can do</h5>
      {capabilities.map((entry) => {
        const meta = capabilityMeta(entry.key);
        const off = entry.runner !== null && !entry.runner.available;
        return (
          <NavRow
            key={entry.key}
            navKey={entry.key}
            icon={meta.icon}
            title={meta.plain}
            count={entry.count}
            off={off}
            offReason={entry.runner?.reason ?? null}
            selected={selected === entry.key}
            onSelect={onSelect}
          />
        );
      })}
      <div className="rule" />
      <NavRow
        navKey="parts"
        icon={PARTS_ICON}
        title="Engine files"
        count={partsCount}
        off={false}
        offReason={null}
        selected={selected === "parts"}
        onSelect={onSelect}
      />
    </div>
  );
}
