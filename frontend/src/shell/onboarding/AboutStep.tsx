// Step 1 — what this is. Copy and video come from the download page
// (scripts/download_page/index.html), which is what the user just came from;
// the video streams from the same origin that page ships from rather than
// riding in the wheel (3.9 MB). A failed load hides the frame — an offline
// first run gets the words alone.
import { useState } from "react";
import { Bot, Code2, Cpu, Mic } from "lucide-react";

import { StepHeader } from "./StepHeader";

const HERO_VIDEO = "https://render.fused.io/assets/showcase/hero.mp4";

const FEATURES = [
  {
    icon: <Bot className="size-4" />,
    title: "Frontier AI models",
  },
  {
    icon: <Cpu className="size-4" />,
    title: "AI models on device",
  },
  {
    icon: <Code2 className="size-4" />,
    title: "Local Python",
  },
  {
    icon: <Mic className="size-4" />,
    title: "Native APIs",
  },
];

export function AboutStep({ eyebrow }: { eyebrow: string }) {
  const [videoOk, setVideoOk] = useState(true);
  return (
    <div className="flex flex-col gap-6">
      <StepHeader
        eyebrow={eyebrow}
        title="Your files, your AI, your apps."
        lead="FusedRender turns any folder on this machine into an app: a web page you see, a Python file that does the work, and Claude Code to write both. Everything runs here — no account, no cloud."
      />

      {videoOk && (
        <div className="overflow-hidden rounded-xl border border-border bg-muted">
          <video
            src={HERO_VIDEO}
            autoPlay
            muted
            loop
            playsInline
            preload="metadata"
            aria-label="FusedRender in use"
            className="block aspect-video w-full object-cover"
            onError={() => setVideoOk(false)}
          />
        </div>
      )}

      <ul className="m-0 grid list-none gap-3 p-0 sm:grid-cols-4">
        {FEATURES.map((f) => (
          <li key={f.title} className="flex items-center gap-3 rounded-xl border border-border bg-card p-4">
            <span className="grid size-8 shrink-0 place-items-center rounded-md bg-muted">{f.icon}</span>
            <div className="min-w-0">
              <div className="text-sm font-medium">{f.title}</div>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
