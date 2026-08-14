// Plugins section: enable/disable toggles over settings.json → enabledPlugins,
// grouped by marketplace, with the read-only enrichment (version, installed?)
// the module reads from plugins/installed_plugins.json.
//
// The toggle is optimistic: the flip shows immediately and is rolled back if the
// write fails, because the write is a git commit in the config repo and waiting
// for it made a switch feel like a form submit. There is deliberately no reload
// after a successful toggle — the only thing that changed is the flag we already
// painted, and a refetch here would fight the optimistic value.
import { useCallback, useState } from "react";
import { copyToClipboard } from "@platform/lib/clipboard";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import * as cc from "../api";
import type { Plugin } from "../api";
import {
  Card,
  CardActions,
  CardSub,
  CardTitle,
  Empty,
  Group,
  Pill,
  Toggle3,
  toastErr,
  toastOk,
  useModuleData,
} from "../bits";
import type { SectionProps } from "../bits";

export default function PluginsSection({ onChanged }: SectionProps) {
  const load = useCallback(() => cc.plugins.list(), []);
  const { data, error } = useModuleData(load);
  // id -> optimistically-shown enabled flag, overriding the fetched value.
  const [flipped, setFlipped] = useState<Record<string, boolean>>({});
  const [updating, setUpdating] = useState<string | null>(null);

  const toggle = async (p: Plugin, next: boolean) => {
    setFlipped((f) => ({ ...f, [p.id]: next }));
    try {
      const res = await cc.plugins.toggle(p.id, next);
      if (!res.ok) throw new Error(res.error || "Toggle failed");
      toastOk(next ? "Enabled" : "Disabled");
      onChanged();
    } catch (e) {
      toastErr((e as Error).message);
      setFlipped((f) => {
        const rest = { ...f };
        delete rest[p.id];
        return rest;
      });
    }
  };

  const update = async (p: Plugin) => {
    setUpdating(p.id);
    try {
      const res = await cc.plugins.update(p.id);
      if (res.ok) toastOk("Updated");
      else toastErr(res.error || "Update failed");
    } catch (e) {
      toastErr((e as Error).message);
    } finally {
      setUpdating(null);
    }
  };

  const share = async (command: string) => {
    if (await copyToClipboard(command)) toastOk("Copied install command");
    else toastErr("Copy failed");
  };

  if (error) return <ErrorBanner>{error}</ErrorBanner>;
  if (!data) return <SkeletonLines rows={4} label="Loading plugins" />;
  if (!data.plugins.length) return <Empty>No plugins enabled or installed.</Empty>;

  const byMarketplace: { name: string; items: Plugin[] }[] = [];
  for (const p of data.plugins) {
    let g = byMarketplace.find((x) => x.name === p.marketplace);
    if (!g) {
      g = { name: p.marketplace, items: [] };
      byMarketplace.push(g);
    }
    g.items.push(p);
  }

  return (
    <>
      {byMarketplace.map((g) => (
        <Group key={g.name} title={g.name}>
          {g.items.map((p) => {
            const enabled = flipped[p.id] ?? p.enabled;
            return (
              <Card key={p.id}>
                <div className="cc-card-head">
                  <Toggle3
                    label={`Enable ${p.name}`}
                    value={enabled}
                    onChange={(next) => toggle(p, next)}
                  />
                  <div className="cc-card-headtext">
                    <CardTitle>
                      {p.name} {enabled && <Pill tone="on">enabled</Pill>}{" "}
                      {!p.installed && <Pill>not installed</Pill>}
                    </CardTitle>
                    <CardSub mono>
                      {p.id}
                      {p.version ? ` · v${p.version}` : ""}
                    </CardSub>
                  </div>
                </div>
                <CardActions>
                  {p.installed && (
                    <button
                      type="button"
                      className="btn"
                      disabled={updating === p.id}
                      onClick={() => update(p)}
                    >
                      {updating === p.id ? "Updating…" : "Update"}
                    </button>
                  )}
                  <button
                    type="button"
                    className="btn"
                    title={p.shareCommand}
                    onClick={() => share(p.shareCommand)}
                  >
                    Copy install command
                  </button>
                </CardActions>
              </Card>
            );
          })}
        </Group>
      ))}
    </>
  );
}
