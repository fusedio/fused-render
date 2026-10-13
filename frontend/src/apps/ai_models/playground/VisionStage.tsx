// The vision stage (SPEC AI-33): one picture, one task, the answer drawn on it.
//
// A vision model answers a SUBSET of `fused.ai.vision`'s tasks (the catalog
// row's `tasks`), so the task switch offers only those. Boxes and points come
// back in 0–1 image coordinates and are drawn as an SVG over the picture.
// Picture, task and prompt are session state, never URL state.
import { useEffect, useMemo, useRef, useState } from "react";
import { Card } from "@platform/shadcn/ui/card";
import { pickFile, rawUrl, type AiCatalogModel } from "@platform/lib/api";
import { vision, withModelReady, type VisionResult, type VisionTask } from "./client";
import { saveToCache } from "./webcam";
import { useConfigOpen, ConfigPanel, CopyButton, RailField, StageHeader } from "./controls";

const IMAGE_EXTENSIONS = [".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff"] as const;
const IMAGE_TYPES = IMAGE_EXTENSIONS.map((e) => e.slice(1));

const TASKS: { value: VisionTask; label: string; placeholder: string }[] = [
  { value: "caption", label: "Caption", placeholder: "" },
  { value: "query", label: "Ask", placeholder: "Ask anything about the picture…" },
  { value: "detect", label: "Detect", placeholder: "What to find — e.g. cat, remote control" },
  { value: "point", label: "Point", placeholder: "What to point at — e.g. window" },
];

interface Picture {
  path: string;
  name: string;
}

function formatSeconds(s: number): string {
  return s < 1 ? `${Math.round(s * 1000)} ms` : `${s.toFixed(1)} s`;
}

export function VisionStage({
  model,
  downloaded,
  entry,
}: {
  model: string;
  downloaded: boolean;
  entry?: AiCatalogModel;
}) {
  const taskKey = (entry?.tasks ?? ["caption", "query", "detect", "point"]).join(",");
  const offered = useMemo(() => TASKS.filter((t) => taskKey.split(",").includes(t.value)), [taskKey]);
  const [picture, setPicture] = useState<Picture | null>(null);
  const [task, setTask] = useState<VisionTask>(offered[0]?.value ?? "caption");
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<VisionResult | null>(null);
  const { open: configOpen, toggle: toggleConfig, touched: configTouched } = useConfigOpen();
  const abortRef = useRef<AbortController | null>(null);
  useEffect(() => () => abortRef.current?.abort(), []);

  // A reply answers the picture, task and prompt it was asked with; a change drops the run in flight.
  useEffect(() => {
    abortRef.current?.abort();
    abortRef.current = null;
  }, [picture, task, prompt]);

  // A model's task list can drop the current task (Grounding DINO only detects).
  useEffect(() => {
    if (!offered.some((t) => t.value === task)) setTask(offered[0]?.value ?? "caption");
  }, [offered, task]);

  const needsPrompt = task !== "caption";
  const request = useMemo(() => {
    const body: Record<string, unknown> = { image: picture?.path ?? "photo.jpg", task };
    if (needsPrompt) body.prompt = prompt.trim() || "…";
    body.model = model;
    return JSON.stringify(body, null, 2);
  }, [picture, task, prompt, needsPrompt, model]);

  const choose = async () => {
    setError(null);
    const path = await pickFile({ title: "Choose a picture", types: IMAGE_TYPES }).catch((e) => {
      setError((e as Error).message);
      return null;
    });
    if (!path) return;
    const name = path.split("/").pop() || path;
    if (!IMAGE_EXTENSIONS.some((ext) => name.toLowerCase().endsWith(ext))) {
      setError(`${name} is not an image this stage reads.`);
      return;
    }
    setPicture({ path, name });
    setResult(null);
  };

  // A dropped or pasted picture has no path of its own; it lands in the cache dir first.
  const take = async (file: File | null | undefined) => {
    if (!file || !file.type.startsWith("image/")) return;
    setError(null);
    try {
      setPicture(await saveToCache(file, file.name || "pasted.png"));
      setResult(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      const item = [...(e.clipboardData?.items ?? [])].find((i) => i.type.startsWith("image/"));
      if (item) void take(item.getAsFile());
    };
    window.addEventListener("paste", onPaste);
    return () => window.removeEventListener("paste", onPaste);
  }, []);

  const run = async () => {
    if (!picture || busy || (needsPrompt && !prompt.trim())) return;
    setError(null);
    setBusy(true);
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const reply = await withModelReady(
        () =>
          vision(
            model,
            { image: picture.path, task, ...(needsPrompt ? { prompt: prompt.trim() } : {}) },
            controller.signal,
          ),
        { signal: controller.signal, downloaded, onStatus: setStatus },
      );
      if (abortRef.current !== controller) return;
      setResult(reply);
    } catch (e) {
      if ((e as Error).name !== "AbortError") setError((e as Error).message);
    } finally {
      setStatus(null);
      setBusy(false);
      if (abortRef.current === controller) abortRef.current = null;
    }
  };

  const seconds = result?.providerMetadata?.local?.seconds ?? null;
  const count = result?.objects?.length ?? result?.points?.length;

  return (
    <div
      className={"pg-work pg-embed" + (configOpen ? " has-config" : "")}
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        e.preventDefault();
        void take([...e.dataTransfer.files].find((f) => f.type.startsWith("image/")));
      }}
    >
      <Card className="pg-work-card flex-none gap-3 px-(--card-spacing) [--card-spacing:--spacing(6)]">
        <StageHeader title="Look at a picture" configOpen={configOpen} onToggleConfig={toggleConfig} />

        <div className="pg-vision-bar">
          <button type="button" className="btn" onClick={() => void choose()}>
            {picture ? "Change picture…" : "Choose a picture…"}
          </button>
          {picture && <span className="pg-vision-name" title={picture.path}>{picture.name}</span>}
          <div className="pg-decide-seg ml-auto" role="radiogroup" aria-label="Task">
            {offered.map((t) => (
              <button
                key={t.value}
                type="button"
                role="radio"
                aria-checked={task === t.value}
                className={"pg-decide-seg-item" + (task === t.value ? " active" : "")}
                onClick={() => {
                  setTask(t.value);
                  setResult(null);
                }}
              >
                {t.label}
              </button>
            ))}
          </div>
        </div>

        <div className="pg-vision-ask">
          {needsPrompt && (
            <input
              type="text"
              className="pg-vision-prompt"
              value={prompt}
              placeholder={offered.find((t) => t.value === task)?.placeholder}
              aria-label="Prompt"
              onChange={(e) => {
                setPrompt(e.target.value);
                setResult(null);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") void run();
              }}
            />
          )}
          <button
            type="button"
            className="btn btn-primary pg-send ml-auto"
            disabled={busy || !picture || (needsPrompt && !prompt.trim())}
            onClick={() => void run()}
          >
            {busy ? "Looking…" : "Run"} <kbd className="pg-kbd">⏎</kbd>
          </button>
        </div>

        {picture ? (
          <figure className="pg-vision-figure">
            <div className="pg-vision-frame">
              <img src={rawUrl(picture.path)} alt={picture.name} />
              {result && (result.objects || result.points) && (
                <svg className="pg-vision-overlay" viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden="true">
                  {result.objects?.map((o, i) => (
                    <rect
                      key={i}
                      className="pg-vision-box"
                      x={o.xMin}
                      y={o.yMin}
                      width={Math.max(o.xMax - o.xMin, 0.002)}
                      height={Math.max(o.yMax - o.yMin, 0.002)}
                    />
                  ))}
                  {result.points?.map((p, i) => (
                    <circle key={i} className="pg-vision-point" cx={p.x} cy={p.y} r={0.012} />
                  ))}
                </svg>
              )}
            </div>
          </figure>
        ) : (
          <p className="pg-vision-empty">Choose, drop or paste a picture to caption, ask about, or search.</p>
        )}

        {status && <p className="pg-status">{status}</p>}
        {error && <p className="pg-error">{error}</p>}
        {result && !busy && (
          <div className="pg-vision-result">
            <p className="pg-answer-label">
              {result.text !== undefined ? "Answer" : `${count ?? 0} found`}
              <span className="pg-answer-provenance">
                {result.response?.modelId ?? model}
                {seconds != null && ` · ${formatSeconds(seconds)}`}
              </span>
            </p>
            {result.text !== undefined && <p className="pg-vision-text">{result.text}</p>}
            {result.reasoning && <p className="pg-vision-reasoning">{result.reasoning}</p>}
            {result.objects && result.objects.length > 0 && (
              <ol className="pg-vision-list">
                {result.objects.map((o, i) => (
                  <li key={i}>
                    {o.label}
                    {o.score != null && <span className="pg-vision-score"> {Math.round(o.score * 100)}%</span>}
                  </li>
                ))}
              </ol>
            )}
          </div>
        )}

        <ConfigPanel open={configOpen} animated={configTouched.current}>
          <RailField
            label="Request"
            hint="What fused.ai.vision sends. A relative image path resolves beside the page."
            action={<CopyButton text={request} label="Copy the request JSON" />}
          >
            <pre className="pg-decide-json">{request}</pre>
          </RailField>
          {result && (
            <RailField label="Result" hint="The payload half of the reply, coordinates 0–1 of the image.">
              <pre className="pg-decide-json">
                {JSON.stringify(
                  { task: result.task, imageSize: result.imageSize, text: result.text,
                    objects: result.objects, points: result.points },
                  null, 2)}
              </pre>
            </RailField>
          )}
        </ConfigPanel>
      </Card>
    </div>
  );
}
