// The mesh stage: a picture in, an untextured 3D shape out (SPEC §48).
//
// Shaped like the video stage (D431): heading with a cog, one required
// input, Generate — and every parameter behind the Config card.
//
// `VideoStage`'s sibling in structure — job-backed, a settled caption that
// stays after the render, a strip of past results — but the input is a
// REQUIRED image rather than a prompt (this pipeline only ever reads a
// picture, there is no prompt-only mode), and the output renders in a
// rotating three.js viewer rather than an `<img>`/`<video>`. No live
// preview mid-render either (this pipeline exposes no progress hook at
// all, not even the video stage's per-step ticks — see `MeshStarted`'s own
// worker for why).
//
// The viewer is a dynamic import of `/template-assets/three.bundle.mjs` —
// the SAME bundle `fused_render/templates/glb/template.html` already loads
// for the file-browser's own `.glb` preview. No new frontend dependency,
// and no second copy of Three.js in this build: one `<script>`-free ES
// module, fetched once, shared by both surfaces.
import { useEffect, useRef, useState } from "react";
import { cancelJob, type Job } from "@platform/lib/jobs";
import { pickFile, rawUrl, type AiCatalogCapability, type AiCatalogModel } from "@platform/lib/api";
import { startMesh, watchJob, type MeshStarted } from "./client";
import { Card } from "@platform/shadcn/ui/card";
import { Input } from "@platform/shadcn/ui/input";
import {
  ConfigPanel,
  useConfigOpen,
  RailField,
  RailSlider,
  ResultSlot,
  StageHeader,
} from "./controls";
import { StarterIcons } from "./starterIcons";
import { numParam, readParam, writeParams } from "@apps/ai_models/lib/params";

// Upstream's own `gradio_app.py` sliders, at the pinned commit (registry.py's
// own `MeshTraits`/`MIN_MESH_*` comment has the evidence) — mirrored here so
// this stage's rails cannot offer a value the server would silently clamp.
const STEPS_RANGE = [1, 100] as const;
const GUIDANCE_RANGE = [0, 20] as const;
const OCTREE_RANGE = [16, 512] as const;

// **The fallback when the server sends no traits at all** — the identical
// argument `VideoStage.tsx`'s own `FALLBACK_TRAITS` makes, restated for this
// capability: a build old enough to predate `meshTraits`, or a row built by
// hand for a test. Must stay byte-for-byte the row `registry.mesh_traits_for`
// falls back to (`hunyuan3d-mlx`'s own — the pipeline's `__call__` defaults).
const FALLBACK_TRAITS: NonNullable<AiCatalogCapability["meshTraits"]> = {
  defaultSteps: 50,
  defaultGuidance: 5.0,
  defaultOctreeResolution: 256,
};

const ATTACH_EXTENSIONS = [".png", ".jpg", ".jpeg", ".webp"] as const;
const ATTACH_TYPES = ATTACH_EXTENSIONS.map((e) => e.slice(1));

interface Attached {
  path: string;
  name: string;
}

interface Run {
  started: MeshStarted;
  job: Job | null;
  done: boolean;
}

/** A rotating three.js viewer for one `.glb` path — a minimal, read-only
 *  cousin of `templates/glb/template.html`'s viewer: the same loader, the
 *  same framing math, orbit controls, a floor grid; no `LoadingManager` URL
 *  rewriting because a `.glb` this route writes is self-contained (embedded
 *  buffers/textures) and never carries the sibling-resource case that
 *  rewriting exists for. */
function MeshViewer({ path, jobId }: { path: string; jobId: string }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    let cleanup = () => {};
    const canvas = canvasRef.current;
    if (!canvas) return;

    (async () => {
      try {
        // A non-literal specifier, deliberately: this is a runtime asset
        // path served by the app's own server (the same bundle `templates/
        // glb/template.html` loads), not a module this build's own graph
        // knows about — a literal import here would ask Vite/tsc to
        // resolve a module they can never see, and `@vite-ignore` alone
        // does not stop tsc from trying.
        const bundlePath = "/template-assets/three.bundle.mjs";
        const { THREE, OrbitControls, GLTFLoader } = await import(/* @vite-ignore */ bundlePath);
        if (!alive) return;

        const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
        renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
        renderer.outputColorSpace = THREE.SRGBColorSpace;

        const scene = new THREE.Scene();
        scene.background = new THREE.Color(0x111418);
        const camera = new THREE.PerspectiveCamera(45, 1, 0.01, 5000);
        camera.position.set(5, 4, 6);
        const controls = new OrbitControls(camera, canvas);
        controls.enableDamping = true;
        scene.add(new THREE.HemisphereLight(0xffffff, 0x223344, 1.0));
        const keyLight = new THREE.DirectionalLight(0xffffff, 2.0);
        keyLight.position.set(5, 8, 5);
        scene.add(keyLight);
        const grid = new THREE.GridHelper(20, 20, 0x333a42, 0x22272d);
        scene.add(grid);

        function resize() {
          const w = canvas!.clientWidth, h = canvas!.clientHeight;
          if (!w || !h) return;
          renderer.setSize(w, h, false);
          camera.aspect = w / h;
          camera.updateProjectionMatrix();
        }
        window.addEventListener("resize", resize);

        let raf = 0;
        const loop = () => {
          raf = requestAnimationFrame(loop);
          resize();
          controls.update();
          renderer.render(scene, camera);
        };
        loop();

        // Assigned HERE, immediately after the rAF loop starts — not after
        // `loader.parse(...)` below, which is preceded by two `await`s
        // (`fetch`, `arrayBuffer()`). An unmount, or a failed/non-OK
        // `/api/fs/raw` fetch, landing in that window used to run this
        // effect's cleanup against the still-default no-op `cleanup = ()
        // => {}` — leaving the loop rendering forever and one WebGL
        // context leaked per navigation (code review, 2026-08-28,
        // finding 3). Everything the loop actually touches (`raf`,
        // `resize`, `renderer`) already exists by this point.
        cleanup = () => {
          cancelAnimationFrame(raf);
          window.removeEventListener("resize", resize);
          renderer.dispose();
        };

        function frame(root: unknown) {
          const box = new THREE.Box3().setFromObject(root);
          if (box.isEmpty()) return;
          const size = box.getSize(new THREE.Vector3());
          const center = box.getCenter(new THREE.Vector3());
          const radius = Math.max(size.x, size.y, size.z, 1e-3) * 0.5;
          const dist = (radius / Math.sin((camera.fov * Math.PI) / 180 / 2)) * 1.4;
          controls.target.copy(center);
          const dir = new THREE.Vector3(1, 0.7, 1).normalize();
          camera.position.copy(center).addScaledVector(dir, dist);
          camera.near = Math.max(dist / 1000, 1e-3);
          camera.far = dist * 1000;
          camera.updateProjectionMatrix();
          grid.position.y = box.min.y;
        }

        const resp = await fetch(rawUrl(path) + "&t=" + jobId);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const buf = await resp.arrayBuffer();
        if (!alive) return;
        const loader = new GLTFLoader();
        loader.parse(
          buf,
          "",
          (gltf: { scene: unknown }) => {
            if (!alive) return;
            scene.add(gltf.scene);
            frame(gltf.scene);
          },
          (err: unknown) => {
            if (alive) setError(`Failed to load mesh: ${String(err)}`);
          },
        );

      } catch (e) {
        if (alive) setError((e as Error).message);
      }
    })();

    return () => {
      alive = false;
      cleanup();
    };
  }, [path, jobId]);

  return (
    <div className="pg-image-frame" style={{ aspectRatio: "1 / 1", width: "100%" }}>
      <canvas ref={canvasRef} style={{ width: "100%", height: "100%", display: "block" }} />
      {error && <p className="pg-error">{error}</p>}
    </div>
  );
}

export function MeshStage({
  model,
  entry,
  traits,
}: {
  model: string;
  entry: AiCatalogModel;
  /** `selected.row.meshTraits` — the RESOLVED engine's own request shape,
   *  never a fact about `entry`. `null` on a machine where nothing serves
   *  image-to-3D, or from a build old enough to predate this field — see
   *  `FALLBACK_TRAITS`. */
  traits: AiCatalogCapability["meshTraits"];
}) {
  const engineTraits = traits ?? FALLBACK_TRAITS;
  const modelSteps = entry.defaults?.steps ?? engineTraits.defaultSteps;

  const [attachment, setAttachment] = useState<Attached | null>(null);
  const [attaching, setAttaching] = useState(false);
  //: Whether the attached picture is showing full-size — `ImageStage.tsx`'s
  //: own `showBase` state, under this stage's own name. The thumbnail
  //: button that opens it had no `onClick` at all until code review caught
  //: it (2026-08-28, finding 5): a 28px thumbnail that claims "See this
  //: picture" and does nothing when clicked.
  const [showAttachment, setShowAttachment] = useState(false);
  const [steps, setSteps] = useState(() => numParam("steps", modelSteps, ...STEPS_RANGE));
  const [guidance, setGuidance] = useState(() =>
    numParam("guidance", engineTraits.defaultGuidance, ...GUIDANCE_RANGE),
  );
  const [octree, setOctree] = useState(() =>
    numParam("octree", engineTraits.defaultOctreeResolution, ...OCTREE_RANGE),
  );
  const [seed, setSeed] = useState<string>(() => readParam("seed") ?? "");
  const { open: configOpen, toggle: toggleConfig, touched: configTouched } = useConfigOpen();
  const [run, setRun] = useState<Run | null>(null);
  const [gallery, setGallery] = useState<MeshStarted[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      writeParams({
        steps: steps !== modelSteps ? String(steps) : null,
        guidance: guidance !== engineTraits.defaultGuidance ? String(guidance) : null,
        octree: octree !== engineTraits.defaultOctreeResolution ? String(octree) : null,
        seed: seed ? seed : null,
      });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [steps, guidance, octree, seed, modelSteps, engineTraits.defaultGuidance,
      engineTraits.defaultOctreeResolution]);

  const abortRef = useRef<AbortController | null>(null);
  useEffect(() => () => abortRef.current?.abort(), []);
  const aliveRef = useRef(true);
  useEffect(() => () => {
    aliveRef.current = false;
  }, []);

  // Escape closes the preview — `ImageStage.tsx`'s own effect, verbatim:
  // the one keystroke anybody reaches for before the ✕.
  useEffect(() => {
    if (!showAttachment) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setShowAttachment(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [showAttachment]);

  /** Point the render at a file already on this disk — no copy, no upload —
   *  the identical trade `ImageStage.tsx`'s own `choose` makes and for the
   *  same reason: `<input type=file>` strips the path, and the server route
   *  needs one. */
  const choose = async () => {
    setError(null);
    setAttaching(true);
    try {
      const path = await pickFile({ title: "Choose a picture to convert", types: ATTACH_TYPES });
      if (path === null || !aliveRef.current) return;
      const name = path.split("/").pop() || path;
      if (!ATTACH_EXTENSIONS.some((ext) => name.toLowerCase().endsWith(ext))) {
        setError(`${name} is not a PNG, JPEG or WebP.`);
        return;
      }
      setAttachment({ path, name });
    } catch (e) {
      if (aliveRef.current) setError((e as Error).message);
    } finally {
      if (aliveRef.current) setAttaching(false);
    }
  };

  const generate = async () => {
    if (!attachment || (run && !run.done)) return;
    setError(null);
    try {
      const controller = new AbortController();
      abortRef.current = controller;
      const started = await startMesh({
        image: attachment.path,
        model,
        steps,
        guidance,
        octreeResolution: octree,
        ...(seed.trim() !== "" ? { seed: Number(seed) } : {}),
      });
      setRun({ started, job: null, done: false });
      try {
        const outcome = await watchJob(started.jobId, controller.signal, (job) =>
          setRun((r) => (r && r.started.jobId === started.jobId ? { ...r, job } : r)),
        );
        if (outcome.state === "cancelled") {
          setRun(null);
          return;
        }
        setRun((r) => (r && r.started.jobId === started.jobId ? { ...r, done: true } : r));
        setGallery((g) => [started, ...g.filter((i) => i.jobId !== started.jobId)].slice(0, 12));
      } catch (e) {
        if ((e as Error).name === "AbortError") return;
        setError((e as Error).message);
        setRun(null);
      }
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const clear = () => {
    setAttachment(null);
    setRun(null);
    setError(null);
  };

  const busy = !!run && !run.done;
  const job = busy ? run.job : null;
  const pct = job && job.total ? Math.min(100, ((job.done ?? 0) / job.total) * 100) : null;
  const settled = run?.started;

  return (
    <div className={"pg-work" + (configOpen ? " has-config" : "")}>
      <Card className="pg-work-card flex-none gap-3 px-(--card-spacing) [--card-spacing:--spacing(6)]">
        <StageHeader title="Turn a picture into a mesh" configOpen={configOpen} onToggleConfig={toggleConfig} />

        <div className="pg-composer pg-composer-image">
          {/* No text box — this pipeline only ever reads a picture, and the
              picture IS the composer's body, not a chip hanging off its
              foot. A dropzone-sized button stands in for it until one is
              attached. */}
          <div className="pg-mesh-preview">
            {attachment ? (
              <>
                <button
                  type="button"
                  className="pg-mesh-preview-open"
                  title="See this picture"
                  aria-label="See this picture"
                  onClick={() => setShowAttachment(true)}
                >
                  <img src={rawUrl(attachment.path)} alt="" />
                </button>
                <button
                  type="button"
                  className="pg-clear-corner"
                  title="Remove this image"
                  aria-label="Remove this image"
                  onClick={() => setAttachment(null)}
                >
                  ✕
                </button>
              </>
            ) : (
              <button
                type="button"
                className="pg-mesh-dropzone"
                title="Point at a picture already on this disk — nothing is copied"
                disabled={attaching}
                onClick={() => void choose()}
              >
                {StarterIcons.landscape}
                <span>{attaching ? "Working…" : "Choose a picture to convert"}</span>
              </button>
            )}
          </div>
          <div className="pg-composer-foot">
            <div className="pg-attach-row">
              {attachment && (
                <button
                  type="button"
                  className="pg-attach-btn"
                  title="Point at a different picture — nothing is copied"
                  disabled={attaching}
                  onClick={() => void choose()}
                >
                  {StarterIcons.landscape}
                  <span>Replace</span>
                </button>
              )}
            </div>
            <div className="pg-composer-side">
            {!busy && run && (
              <button
                type="button"
                className="pg-ghost-btn pg-clear"
                title="Clear the picture and the mesh"
                onClick={clear}
              >
                Clear
              </button>
            )}
            {busy ? (
              <button
                type="button"
                className="btn btn-secondary pg-send"
                onClick={() => void cancelJob(run.started.jobId).catch(() => {})}
              >
                Stop
              </button>
            ) : (
              <button
                type="button"
                className="btn btn-primary pg-send"
                disabled={!attachment}
                title={attachment ? "Generate the mesh" : "Choose a picture first"}
                onClick={() => void generate()}
              >
                Generate
              </button>
            )}
            </div>
          </div>
        </div>

        <ConfigPanel open={configOpen} animated={configTouched.current}>
          <RailSlider
            label="Steps"
            hint="Denoising passes — more is slower and usually cleaner."
            min={STEPS_RANGE[0]}
            max={STEPS_RANGE[1]}
            step={1}
            value={steps}
            fallback={modelSteps}
            onChange={setSteps}
          />
          <RailSlider
            label="Guidance"
            hint="How closely the shape follows the picture — higher can distort it."
            min={GUIDANCE_RANGE[0]}
            max={GUIDANCE_RANGE[1]}
            step={0.5}
            value={guidance}
            fallback={engineTraits.defaultGuidance}
            onChange={setGuidance}
          />
          <RailSlider
            label="Octree resolution"
            hint="Grid resolution for the mesh's surface — also this engine's face-count ceiling: higher means more detail and more triangles."
            min={OCTREE_RANGE[0]}
            max={OCTREE_RANGE[1]}
            step={16}
            value={octree}
            fallback={engineTraits.defaultOctreeResolution}
            onChange={setOctree}
          />
          <RailField label="Seed" hint="Same seed + same picture + same settings = the same mesh.">
            <Input
              type="text"
              inputMode="numeric"
              value={seed}
              placeholder="Random each time"
              onChange={(e) => setSeed(e.target.value.replace(/[^0-9]/g, ""))}
            />
          </RailField>
        </ConfigPanel>

        {/* The attached picture at full size — `ImageStage.tsx`'s own
            lightbox, verbatim: the whole modal is an image and a way out,
            because a 28px thumbnail cannot be looked at. Click the
            backdrop or press Escape to close. */}
        {attachment && showAttachment && (
          <div
            className="pg-lightbox"
            role="dialog"
            aria-label="The attached picture"
            onClick={() => setShowAttachment(false)}
          >
            <img src={rawUrl(attachment.path)} alt="" onClick={(e) => e.stopPropagation()} />
            <button
              type="button"
              className="pg-lightbox-close"
              title="Close"
              aria-label="Close"
              onClick={() => setShowAttachment(false)}
            >
              ✕
            </button>
          </div>
        )}

        {error && <p className="pg-error">{error}</p>}

        {!run ? (
          <ResultSlot
            label="Result"
            capability="image-to-3d"
            note={
              attachment
                ? "Your mesh appears here. Press Generate to build it."
                : "Your mesh appears here. Choose a picture above, then Generate."
            }
          />
        ) : (
          <div className="pg-answer-block">
            <p className="pg-answer-label">Result</p>
            <figure className="pg-image-result">
              {run.done ? (
                // `key={path}`: without it, clicking a gallery thumbnail
                // (a different render, a different `path`) re-runs this
                // effect on the SAME <canvas> and calls `new THREE.
                // WebGLRenderer({ canvas })` again on one whose renderer
                // was already `dispose()`d — reusing a canvas across two
                // WebGLRenderer instances is unsupported (code review,
                // 2026-08-28, finding 8). The key forces React to unmount
                // and remount a fresh <canvas> instead.
                <MeshViewer key={run.started.path} path={run.started.path} jobId={run.started.jobId} />
              ) : (
                <div className="pg-image-frame" style={{ aspectRatio: "1 / 1", width: "100%" }}>
                  <div className="pg-image-wait" aria-hidden="true" />
                </div>
              )}
              <figcaption className="pg-image-caption">
                {busy ? (
                  <>
                    <span>{job?.detail || "Starting — a cold model loads first…"}</span>
                    {pct !== null && (
                      <span className="pg-bar">
                        <span className="pg-bar-fill" style={{ width: `${pct}%` }} />
                      </span>
                    )}
                  </>
                ) : settled ? (
                  <>
                    {settled.steps} steps · guidance {settled.guidance} · octree{" "}
                    {settled.octreeResolution} ·{" "}
                    <button
                      type="button"
                      className="pg-seed"
                      title="Reuse this seed — the same picture and settings render the same mesh"
                      onClick={() => setSeed(String(settled.seed))}
                    >
                      seed {settled.seed}
                    </button>
                  </>
                ) : null}
              </figcaption>
            </figure>
          </div>
        )}

        {gallery.length > 0 && (
          <div className="pg-image-strip">
            {gallery.map((item) => (
              <button
                key={item.jobId}
                type="button"
                className={
                  "pg-thumb" +
                  (run?.started.jobId === item.jobId ? " active" : "") +
                  (busy ? " disabled" : "")
                }
                title={
                  busy
                    ? "Finish or stop the current render to view another mesh"
                    : `${item.image.split("/").pop()} — seed ${item.seed}`
                }
                disabled={busy}
                onClick={() => setRun({ started: item, job: null, done: true })}
              >
                {StarterIcons.sparkle}
              </button>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
