// One setting, one row: label and a line of help on the left, the control on
// the right, anything bigger (a health check, a pairing code, an editor) full
// width underneath. Every control in settings/ renders through this, so a row
// looks the same in its group and in search results — where it also carries
// the path back to its group and, for a synonym match, the word that matched.
import { Children, createContext, useContext, useState, type ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import { Badge } from "@platform/shadcn/ui/badge";
import { Switch } from "@platform/shadcn/ui/switch";
import { cn } from "@platform/lib/utils";
import type { Prefs } from "@platform/lib/api";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { groupOf, settingById, type SettingId } from "./registry";
import { matchedVia, splitLabel } from "./search";

export interface SettingsView {
  /** The search's words, or null while a group is shown. */
  tokens: string[] | null;
  /** The row to highlight after a jump or a `?s=` link. */
  flashId: string | null;
  /** Leave the search and show this row in its group. */
  jump: (id: SettingId) => void;
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
  control,
  status,
  children,
  off,
}: {
  id: SettingId;
  control?: ReactNode;
  /** One muted line under the description: live state ("Ready · signed in"). */
  status?: ReactNode;
  children?: ReactNode;
  /** Dim the label: the row only means something while its parent is on. */
  off?: boolean;
}) {
  const view = useContext(SettingsViewContext);
  const entry = settingById(id)!;
  const tokens = view.tokens;
  const label = tokens ? splitLabel(entry.label, tokens) : { pre: entry.label, hit: "", post: "" };
  const via = tokens ? matchedVia(entry, tokens) : [];
  return (
    <div
      id={rowId(id)}
      data-setting={id}
      className={cn(
        "settings-row",
        "child" in entry && entry.child && "settings-row-child",
        view.flashId === id && "settings-row-flash",
      )}
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
          {"badge" in entry && entry.badge && (
            <Badge variant="outline" className="settings-badge-beta">
              {entry.badge}
            </Badge>
          )}
          {"macOnly" in entry && entry.macOnly && <Badge variant="secondary">macOS</Badge>}
        </div>
        <p className="settings-row-desc">{entry.description}</p>
        {status && <div className="settings-row-status">{status}</div>}
        {via.length > 0 && <div className="settings-row-via">Matched: {via.join(", ")}</div>}
      </div>
      {control && <div className="settings-row-control">{control}</div>}
      {Children.toArray(children).length > 0 && <div className="settings-row-extra">{children}</div>}
    </div>
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
  id: SettingId;
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
