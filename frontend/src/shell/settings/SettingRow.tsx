// One setting, one row: the label (its help behind an ⓘ beside it) on the
// left, the control on the right, anything bigger (a health check, a pairing
// code, an editor) full width underneath. Every control in settings/ renders through this, so a row
// looks the same in its group and in search results — where it also carries
// the path back to its group and, for a synonym match, the word that matched.
import { Children, createContext, useContext, useState, type ReactNode } from "react";
import { Ban, ChevronRight, Info } from "lucide-react";
import { Badge } from "@platform/shadcn/ui/badge";
import { Popover, PopoverContent, PopoverTrigger } from "@platform/shadcn/ui/popover";
import { Switch } from "@platform/shadcn/ui/switch";
import { Dropdown } from "@shell/Dropdown";
import { cn } from "@platform/lib/utils";
import type { Prefs } from "@platform/lib/api";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { groupOf, settingById, unavailableReason, type SettingEntry, type SettingId } from "./registry";
import { matchedVia, splitLabel } from "./search";

export interface SettingsView {
  /** The search's words, or null while a group is shown. */
  tokens: string[] | null;
  /** The row to highlight after a jump or a `?s=` link. */
  flashId: string | null;
  /** Leave the search and show this row in its group. Any row id — the
   *  registry's or one built at runtime (settings/claude.tsx). */
  jump: (id: string) => void;
  /** The page's prefs, so a row can read `prefs.unavailable` (device
   *  capabilities) without a fetch of its own. Absent outside the page. */
  prefs?: Prefs;
}

export const SettingsViewContext = createContext<SettingsView>({
  tokens: null,
  flashId: null,
  jump: () => undefined,
});

export const labelId = (id: string) => `setting-label-${id}`;
export const rowId = (id: string) => `setting-${id}`;

export function SettingRow({
  id,
  entry: given,
  control,
  status,
  children,
  off,
}: {
  id: SettingId | string;
  /** A row the registry does not list — built at runtime from a catalog
   *  (settings/claude.tsx). A registry id is looked up instead. */
  entry?: SettingEntry;
  control?: ReactNode;
  /** One muted line under the description: live state ("Ready · signed in"). */
  status?: ReactNode;
  children?: ReactNode;
  /** Dim the label: the row only means something while its parent is on. */
  off?: boolean;
}) {
  const view = useContext(SettingsViewContext);
  const entry = given ?? settingById(id)!;
  const tokens = view.tokens;
  const label = tokens ? splitLabel(entry.label, tokens) : { pre: entry.label, hit: "", post: "" };
  const via = tokens ? matchedVia(entry, tokens) : [];
  const unavailable = unavailableReason(entry, view.prefs ? { prefs: view.prefs } : null);
  // `inert` takes every control inside out of focus and clicks at once, so no
  // control needs a `disabled` of its own. React 18 only passes it through as
  // a string attribute.
  const inertProps = unavailable ? { inert: "" } : {};
  return (
    <div
      id={rowId(id)}
      data-setting={id}
      className={cn(
        "settings-row",
        "child" in entry && entry.child && "settings-row-child",
        view.flashId === id && "settings-row-flash",
      )}
      data-unavailable={unavailable ? "true" : undefined}
    >
      <div className={cn("settings-row-main", off && "opacity-55")}>
        {tokens && (
          <button type="button" className="settings-crumb" onClick={() => view.jump(id)} title="Show in its group">
            {groupOf(entry.group).label} › {entry.section}
            <ChevronRight aria-hidden="true" className="size-3" />
          </button>
        )}
        <div className="settings-row-label" id={labelId(id)}>
          <span>
            {label.pre}
            {label.hit && <mark>{label.hit}</mark>}
            {label.post}
          </span>
          {entry.description && !entry.inlineDescription && <SettingHelp label={entry.label} text={entry.description} />}
          {"badge" in entry && entry.badge && (
            <Badge variant="outline" className="settings-badge-beta">
              {entry.badge}
            </Badge>
          )}
          {"macOnly" in entry && entry.macOnly && <Badge variant="secondary">macOS</Badge>}
        </div>
        {entry.description && entry.inlineDescription && <p className="settings-row-desc">{entry.description}</p>}
        {unavailable ? (
          <div className="settings-row-status" data-unavailable-reason>
            <Ban aria-hidden="true" className="mr-1 inline size-3.5 align-[-2px]" />
            {unavailable}
          </div>
        ) : (
          status && <div className="settings-row-status">{status}</div>
        )}
        {via.length > 0 && <div className="settings-row-via">Matched: {via.join(", ")}</div>}
      </div>
      {control && (
        <div className={cn("settings-row-control", unavailable && "opacity-55")} {...inertProps}>
          {control}
        </div>
      )}
      {Children.toArray(children).length > 0 && (
        <div className={cn("settings-row-extra", unavailable && "opacity-55")} {...inertProps}>
          {children}
        </div>
      )}
    </div>
  );
}

/** A row's description, behind an ⓘ beside its label: the page shows what
 *  each setting is called and where it stands, and says what it does only to
 *  the reader who asks (2026-11-20: a line of help under every row was too
 *  much at once). Opens on hover AND on click, tap or Enter — a hover-only
 *  ⓘ "looked disabled and said nothing" (Akshil, 2026-10-20) — and the icon
 *  brightens under the pointer so it reads as something to press. */
export function SettingHelp({ label, text }: { label: string; text: string }) {
  return (
    <Popover>
      <PopoverTrigger
        openOnHover
        delay={250}
        closeDelay={100}
        className="settings-help"
        aria-label={`About ${label}`}
      >
        <Info aria-hidden="true" />
      </PopoverTrigger>
      <PopoverContent side="top" align="start" className="settings-help-pop">
        {text}
      </PopoverContent>
    </Popover>
  );
}

/** A Switch labelled by its row, in the app's accent rather than shadcn's
 *  near-white primary — the one "on" colour the shell uses everywhere else. */
export function RowSwitch({
  id,
  checked,
  disabled,
  onChange,
  title,
}: {
  id: SettingId | string;
  checked: boolean;
  disabled?: boolean;
  onChange: (next: boolean) => void;
  title?: string;
}) {
  return (
    <span title={title} className="inline-flex">
      <Switch
        aria-labelledby={labelId(id)}
        checked={checked}
        disabled={disabled}
        onCheckedChange={(c) => onChange(!!c)}
        className="settings-switch"
      />
    </span>
  );
}

/** A row's list control: the app's own dropdown (shell/Dropdown, the New task
 *  form's), not the OS select — one control for one job across the shell
 *  (Akshil, 2026-10-20). Labelled by its row unless `ariaLabel` says
 *  otherwise (a second control in a row with a name of its own). */
export function SettingSelect({
  id,
  value,
  options,
  onChange,
  disabled,
  ariaLabel,
}: {
  id: SettingId | string;
  value: string;
  options: { key: string; label: string; disabled?: boolean; desc?: string }[];
  onChange: (key: string) => void;
  disabled?: boolean;
  ariaLabel?: string;
}) {
  const label = options.find((o) => o.key === value)?.label ?? value;
  return (
    <Dropdown
      className="settings-select"
      labelledBy={ariaLabel ? undefined : labelId(id)}
      ariaLabel={ariaLabel}
      value={label}
      options={options}
      onPick={onChange}
      disabled={disabled}
    />
  );
}

/** busy/error around one PUT /api/prefs. The PUT answers with the whole
 *  Prefs, which goes to the page; `after` gets it too, for the publish a
 *  surface already on screen needs (a sidebar row, a status-bar chip). */
export function usePrefWrite(onChange: (p: Prefs) => void) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async (put: () => Promise<Prefs>, after?: (p: Prefs) => void) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const next = await put();
      onChange(next);
      after?.(next);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, run, setError };
}

export interface PrefControlProps {
  prefs: Prefs;
  onChange: (p: Prefs) => void;
}

/** The common case: one boolean pref, one switch. */
export function PrefToggleRow({
  id,
  checked,
  put,
  after,
  disabled,
  off,
  status,
  children,
  onChange,
}: {
  id: SettingId;
  checked: boolean;
  put: (next: boolean) => Promise<Prefs>;
  after?: (p: Prefs) => void;
  disabled?: boolean;
  off?: boolean;
  status?: ReactNode;
  children?: ReactNode;
  onChange: (p: Prefs) => void;
}) {
  const { busy, error, run } = usePrefWrite(onChange);
  return (
    <SettingRow
      id={id}
      off={off}
      status={status}
      control={
        <RowSwitch id={id} checked={checked} disabled={busy || disabled} onChange={(v) => void run(() => put(v), after)} />
      }
    >
      {children}
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </SettingRow>
  );
}
