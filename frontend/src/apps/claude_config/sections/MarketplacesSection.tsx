// Marketplaces section: add/remove the user's own entries in settings.json →
// extraKnownMarketplaces. Anything resolved from plugins/known_marketplaces.json
// is Claude's, not ours — those render with a read-only pill and no Remove.
import { useCallback, useState } from "react";
import { copyToClipboard } from "@platform/lib/clipboard";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import * as cc from "../api";
import type { MarketplaceKind } from "../api";
import {
  Card,
  CardActions,
  CardTitle,
  Icon,
  ListRow,
  Pill,
  SKELETON_ROWS,
  SectionToolbar,
  toastErr,
  toastOk,
  useModuleData,
} from "../bits";
import type { SectionProps } from "../bits";

export default function MarketplacesSection({ onChanged }: SectionProps) {
  const load = useCallback(() => cc.marketplaces.list(), []);
  const { data, error, reload } = useModuleData(load);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<MarketplaceKind>("github");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);

  const add = async () => {
    const n = name.trim();
    const v = value.trim();
    if (!n || !v) {
      toastErr("name and value required");
      return;
    }
    setBusy(true);
    try {
      const res = await cc.marketplaces.add(n, kind, v);
      if (!res.ok) {
        toastErr(res.error || "Add failed");
        return;
      }
      toastOk("Added");
      setName("");
      setValue("");
      onChanged();
      reload();
    } catch (e) {
      toastErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (mktName: string) => {
    try {
      const res = await cc.marketplaces.remove(mktName);
      if (!res.ok) {
        toastErr(res.error || "Remove failed");
        return;
      }
      toastOk("Removed");
      onChanged();
      reload();
    } catch (e) {
      toastErr((e as Error).message);
    }
  };

  const share = async (command: string) => {
    // "Copy install command" everywhere it appears (Plugins, Skills, here):
    // one name for one act, even though what this one copies is the
    // `marketplace add` line.
    if (await copyToClipboard(command)) toastOk("Copied install command");
    else toastErr("Copy failed");
  };

  return (
    <>
      <SectionToolbar
        summary={data ? `${data.marketplaces.length} marketplace(s)` : "…"}
        onRefresh={reload}
      />
      <Card>
        <CardTitle>Add a marketplace</CardTitle>
        <CardActions>
          <input
            className="field-control"
            aria-label="Marketplace name"
            placeholder="name"
            value={name}
            disabled={busy}
            onChange={(e) => setName(e.target.value)}
          />
          <select
            className="field-control"
            aria-label="Marketplace kind"
            value={kind}
            disabled={busy}
            onChange={(e) => setKind(e.target.value as MarketplaceKind)}
          >
            <option value="github">github (owner/repo)</option>
            <option value="git">git url</option>
          </select>
          <input
            className="field-control cc-grow"
            aria-label="Marketplace source"
            placeholder="owner/repo or url"
            value={value}
            disabled={busy}
            onChange={(e) => setValue(e.target.value)}
          />
          <button type="button" className="btn btn-primary" disabled={busy} onClick={add}>
            Add
          </button>
        </CardActions>
      </Card>
      {error && <ErrorBanner>{error}</ErrorBanner>}
      {!data && !error && <SkeletonLines rows={SKELETON_ROWS} label="Loading marketplaces" />}
      {/* No chevron: a marketplace IS its name and its source, and both are on
          the line. There is nothing an expanded panel could add. */}
      {data?.marketplaces.map((m) => {
        const source = m.source.repo || m.source.url || "";
        return (
          <ListRow
            key={m.name}
            name={m.name}
            pills={!m.editable ? <Pill tone="ro">read-only</Pill> : null}
            secondary={source}
            secondaryTitle={source}
            secondaryMono
            actions={
              <>
                {m.shareCommand && (
                  <button
                    type="button"
                    className="cc-iconbtn"
                    title={`Copy install command — ${m.shareCommand}`}
                    aria-label={`Copy the install command for ${m.name}`}
                    onClick={() => share(m.shareCommand as string)}
                  >
                    <Icon name="copy" />
                  </button>
                )}
                {m.editable && (
                  <button type="button" className="btn btn-danger" onClick={() => remove(m.name)}>
                    Remove
                  </button>
                )}
              </>
            }
          />
        );
      })}
    </>
  );
}
