// The "Before you install" rows for a catalog entry — the detail sidebar's
// trust card and the install confirmation sheet say the same thing.
import { BadgeCheck, Cpu, FileArchive, Globe2, ShieldCheck, Terminal } from "lucide-react";
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
  // Stage B (CONTRIB_LOOP.md §18): every catalog row is a reviewed release
  // pinned in its author's GitHub repo — never the author's latest.
  items.push({
    key: "reviewed",
    icon: <BadgeCheck size={15} strokeWidth={2} />,
    title: "Reviewed by Fused",
    detail: app.repo
      ? `Every release is checked by Fused before it reaches you, and updates arrive only after that check. The code comes from ${app.repo} on GitHub.`
      : "Every release is checked by Fused before it reaches you, and updates arrive only after that check.",
    tone: "ok",
  });
  return items;
}


/** The same card for a `.fused` app file (FusedFilePage): Python or page-only
 *  read off what the file carries, that nothing runs before Install, and that
 *  a file — unlike a Marketplace app — was reviewed by nobody. */
export function trustItemsForFile(file: {
  files: { path: string }[];
  metadata?: { requires_python: boolean | null } | null;
  git?: { url: string } | null;
}): TrustItem[] {
  const python =
    file.metadata?.requires_python ??
    file.files.some((f) => /\.py$/i.test(f.path) || /(^|\/)(pyproject\.toml|requirements\.txt)$/i.test(f.path));
  const items: TrustItem[] = [
    {
      key: "inert",
      icon: <ShieldCheck size={15} strokeWidth={2} />,
      title: "Nothing runs until you install",
      detail: "This page only reads the file's index and readme. Its pages and code run once it is installed.",
      tone: "ok",
    },
  ];
  if (python) {
    items.push({
      key: "python",
      icon: <Terminal size={15} strokeWidth={2} />,
      title: "Runs Python on your machine",
      detail: "Once installed, its backend runs locally with the same access you have.",
      tone: "caution",
    });
  } else {
    items.push({
      key: "static",
      icon: <Globe2 size={15} strokeWidth={2} />,
      title: "Runs in the page only",
      detail: "No Python in this file — just HTML and JavaScript in a sandboxed view.",
    });
  }
  items.push(
    file.git
      ? {
          key: "source",
          icon: <FileArchive size={15} strokeWidth={2} />,
          title: "From a GitHub repo, unverified",
          detail: "Fused has not reviewed this repo. It can contain malicious code. Install only from people you trust.",
          tone: "caution",
        }
      : {
          key: "source",
          icon: <FileArchive size={15} strokeWidth={2} />,
          title: "App file, not from the Marketplace",
          detail: "Nobody reviewed it. Install app files only from people you trust.",
        },
  );
  return items;
}
