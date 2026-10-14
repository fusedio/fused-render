// The "Before you install" rows for a catalog entry — the detail sidebar's
// trust card and the install confirmation sheet say the same thing.
import { BadgeCheck, Cpu, Globe2, Terminal } from "lucide-react";
import type { TrustItem } from "@platform/ui/appstore";
import type { AppEntry } from "@platform/lib/marketplace";

export function trustItemsFor(app: AppEntry): TrustItem[] {
  const items: TrustItem[] = [];
  if (app.requires_python) {
    items.push({
      key: "python",
      icon: <Terminal size={15} strokeWidth={2} />,
      title: "Runs Python on your machine",
      detail: "Its backend runs locally with the same access you have. Dependencies install into the app's own folder.",
      tone: "caution",
    });
  } else {
    items.push({
      key: "static",
      icon: <Globe2 size={15} strokeWidth={2} />,
      title: "Runs in the page only",
      detail: "No Python backend — just HTML and JavaScript in a sandboxed view.",
      tone: "ok",
    });
  }
  if (app.ai_capabilities.length > 0 || app.ai_models.length > 0 || app.ai_model_param) {
    const what = app.ai_capabilities.length ? app.ai_capabilities.map((c) => c.replace(/-/g, " ")).join(", ") : null;
    items.push({
      key: "ai",
      icon: <Cpu size={15} strokeWidth={2} />,
      title: "Uses local AI models",
      detail: what
        ? `For ${what}. Models download to this machine on first use and run offline.`
        : "Models download to this machine on first use and run offline.",
    });
  }
  items.push({
    key: "community",
    icon: <BadgeCheck size={15} strokeWidth={2} />,
    title: "Community app",
    detail: "Published to the Fused community repo and reviewed via pull request.",
  });
  return items;
}

