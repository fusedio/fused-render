// Voice chat (Moshi, `voice-to-voice`, SPEC AI-33): the Playground's client
// for one live conversation.
//
// Two halves. `startVoice` is the ordinary POST that opens a session on the
// resident model (a cold model answers the same `model_loading` 409 the chat
// route does — thrown as `ModelLoading`). `VoiceCall` is the live part: the
// microphone into an AudioWorklet that re-chunks it into the worker's 80 ms
// frames, a WebSocket carrying those frames to the server's proxy and
// Moshi's frames back, and a second worklet that plays what comes back from
// a ring buffer. The wire is the worker's own (`moshi_voice/worker.py`):
// `kind:u8 · len:u32 · payload`, Int16 PCM at 24 kHz, 1920 samples a frame.
//
// Raw PCM rather than Opus on purpose: the link is loopback, 48 kB/s each
// way, and a codec in the page would be a dependency for nothing. Echo
// cancellation is asked of the browser (`echoCancellation: true`) because
// Moshi listens while it speaks — on speakers, without it, it hears itself.
import { sourceHeader } from "@platform/lib/api";
import { ModelLoading } from "./client";

export const VOICE_SAMPLE_RATE = 24000;
export const VOICE_FRAME = 1920;

const KIND_MIC = 0;
const KIND_OUT = 1;
const KIND_TEXT = 2;
const KIND_END = 3;

export interface VoiceStarted {
  sessionId: string;
  token: string;
  model: string;
  maxSeconds: number;
  sampleRate: number;
  frame: number;
  provider: "local";
  warnings: { type: string; setting?: string; message: string }[];
}

export interface VoiceResult {
  text: string;
  steps: number;
  seconds: number;
  finishReason: "stop" | "length" | "cancelled";
  droppedFrames: number;
}

export async function startVoice(request: { model: string; maxSeconds?: number }): Promise<VoiceStarted> {
  const res = await fetch("/api/ai/voice", {
    method: "POST",
    headers: { ...sourceHeader(), "Content-Type": "application/json", "X-Fused": "1" },
    body: JSON.stringify(request),
  });
  const data = (await res.json().catch(() => null)) as
    | ({ ok?: boolean; error?: { type?: string; message?: string; jobId?: string } } & Partial<VoiceStarted>)
    | null;
  if (!res.ok || !data?.ok) {
    const error = data?.error;
    if (res.status === 409 && error?.type === "model_loading") {
      throw new ModelLoading(error.message || "model is loading", error.jobId ?? null);
    }
    throw new Error(error?.message || `the voice session could not start (${res.status})`);
  }
  return data as VoiceStarted;
}

// One module for both processors. `mic` turns the 128-sample render quanta
// into whole frames and reports each frame's RMS; `speaker` plays whatever
// the main thread has appended, silence when the buffer runs dry.
const WORKLET_SOURCE = `
class MicFrames extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.size = options.processorOptions.frame;
    this.buf = new Float32Array(this.size);
    this.fill = 0;
  }
  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;
    for (let i = 0; i < input.length; i++) {
      this.buf[this.fill++] = input[i];
      if (this.fill === this.size) {
        const pcm = new Int16Array(this.size);
        let sum = 0;
        for (let j = 0; j < this.size; j++) {
          const v = Math.max(-1, Math.min(1, this.buf[j]));
          pcm[j] = v < 0 ? v * 32768 : v * 32767;
          sum += v * v;
        }
        this.port.postMessage({ pcm, rms: Math.sqrt(sum / this.size) }, [pcm.buffer]);
        this.fill = 0;
      }
    }
    return true;
  }
}
class SpeakerFrames extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.offset = 0;
    this.port.onmessage = (e) => { this.queue.push(e.data); };
  }
  process(_inputs, outputs) {
    const out = outputs[0] && outputs[0][0];
    if (!out) return true;
    let i = 0;
    while (i < out.length && this.queue.length) {
      const head = this.queue[0];
      const n = Math.min(out.length - i, head.length - this.offset);
      out.set(head.subarray(this.offset, this.offset + n), i);
      i += n;
      this.offset += n;
      if (this.offset >= head.length) { this.queue.shift(); this.offset = 0; }
    }
    for (; i < out.length; i++) out[i] = 0;
    return true;
  }
}
registerProcessor("fused-voice-mic", MicFrames);
registerProcessor("fused-voice-speaker", SpeakerFrames);
`;

let workletUrl: string | null = null;
function workletModule(): string {
  if (!workletUrl) {
    workletUrl = URL.createObjectURL(new Blob([WORKLET_SOURCE], { type: "application/javascript" }));
  }
  return workletUrl;
}

export function voiceSocketUrl(started: { sessionId: string; token: string }): string {
  const proto = location.protocol === "https:" ? "wss://" : "ws://";
  return `${proto}${location.host}/api/ai/voice/${encodeURIComponent(started.sessionId)}/stream?token=${encodeURIComponent(started.token)}`;
}

export function packFrame(kind: number, payload: Uint8Array): ArrayBuffer {
  const out = new Uint8Array(5 + payload.length);
  out[0] = kind;
  new DataView(out.buffer).setUint32(1, payload.length, false);
  out.set(payload, 5);
  return out.buffer;
}

export function unpackFrame(data: ArrayBuffer): { kind: number; payload: Uint8Array } | null {
  if (data.byteLength < 5) return null;
  const view = new DataView(data);
  const length = view.getUint32(1, false);
  if (data.byteLength < 5 + length) return null;
  return { kind: view.getUint8(0), payload: new Uint8Array(data, 5, length) };
}

export interface VoiceHandlers {
  onText: (piece: string) => void;
  onLevel?: (mic: number, speaker: number) => void;
  /** The conversation is over: the worker's result, or null when the link
   *  broke before one arrived (then `error` says why when known). */
  onEnd: (result: VoiceResult | null, error?: string) => void;
}

/** The microphone, the audio graph and the worklets — everything that needs
 *  a user gesture — opened BEFORE the model is asked for. A cold model takes
 *  minutes to load, and WebKit ties an AudioContext's right to run to a
 *  recent click: open the mic after that wait and the context stays
 *  suspended, silent both ways. So the stage calls `openAudio` inside the
 *  click, waits for the model with the mic already live, then `connect`s. */
export class VoiceAudio {
  private constructor(
    readonly ctx: AudioContext,
    readonly stream: MediaStream,
  ) {}

  static async open(sampleRate = VOICE_SAMPLE_RATE): Promise<VoiceAudio> {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    const ctx = new AudioContext({ sampleRate });
    try {
      await ctx.audioWorklet.addModule(workletModule());
      if (ctx.state === "suspended") await ctx.resume();
    } catch (e) {
      stream.getTracks().forEach((t) => t.stop());
      void ctx.close();
      throw e;
    }
    return new VoiceAudio(ctx, stream);
  }

  close(): void {
    this.stream.getTracks().forEach((t) => t.stop());
    void this.ctx.close().catch(() => {});
  }
}

/** A live conversation. `connect` resolves once the socket is up; `hangUp`
 *  sends the end frame and tears everything down, the audio included.
 *  `onEnd` fires exactly once, whichever side ends it. */
export class VoiceCall {
  private ended = false;
  private constructor(
    private readonly ws: WebSocket,
    private readonly audio: VoiceAudio,
    private readonly handlers: VoiceHandlers,
  ) {}

  static async connect(audio: VoiceAudio, started: VoiceStarted, handlers: VoiceHandlers): Promise<VoiceCall> {
    const frame = started.frame || VOICE_FRAME;
    const ctx = audio.ctx;
    const ws = new WebSocket(voiceSocketUrl(started));
    ws.binaryType = "arraybuffer";
    const call = new VoiceCall(ws, audio, handlers);

    const source = ctx.createMediaStreamSource(audio.stream);
    const mic = new AudioWorkletNode(ctx, "fused-voice-mic", { processorOptions: { frame } });
    const speaker = new AudioWorkletNode(ctx, "fused-voice-speaker");
    source.connect(mic);
    speaker.connect(ctx.destination);
    let speakerLevel = 0;
    mic.port.onmessage = (e: MessageEvent<{ pcm: Int16Array; rms: number }>) => {
      if (ws.readyState !== WebSocket.OPEN) return;
      ws.send(packFrame(KIND_MIC, new Uint8Array(e.data.pcm.buffer)));
      handlers.onLevel?.(Math.min(1, e.data.rms * 3), speakerLevel);
      speakerLevel *= 0.6;
    };

    const decoder = new TextDecoder();
    ws.onmessage = (event: MessageEvent<ArrayBuffer>) => {
      const frameIn = unpackFrame(event.data);
      if (!frameIn) return;
      if (frameIn.kind === KIND_OUT) {
        // A COPY: the payload sits at byte offset 5 of the message, and an
        // Int16 view must start on an even byte.
        const pcm = new Int16Array(frameIn.payload.slice().buffer);
        const floats = new Float32Array(pcm.length);
        let sum = 0;
        for (let i = 0; i < pcm.length; i++) {
          const v = pcm[i] / 32768;
          floats[i] = v;
          sum += v * v;
        }
        speakerLevel = Math.min(1, Math.sqrt(sum / pcm.length) * 3);
        speaker.port.postMessage(floats, [floats.buffer]);
      } else if (frameIn.kind === KIND_TEXT) {
        handlers.onText(decoder.decode(frameIn.payload));
      } else if (frameIn.kind === KIND_END) {
        let result: VoiceResult | null = null;
        try {
          result = JSON.parse(decoder.decode(frameIn.payload)) as VoiceResult;
        } catch {
          result = null;
        }
        call.finish(result);
      }
    };
    ws.onclose = (event) => call.finish(null, event.reason || undefined);
    ws.onerror = () => call.finish(null, "the voice connection failed");

    await new Promise<void>((resolve, reject) => {
      ws.addEventListener("open", () => resolve(), { once: true });
      ws.addEventListener("close", (event) => reject(new Error(event.reason || "the voice session refused the connection")), { once: true });
    });
    if (ctx.state === "suspended") await ctx.resume().catch(() => {});
    return call;
  }

  hangUp(): void {
    if (this.ws.readyState === WebSocket.OPEN) {
      try {
        this.ws.send(packFrame(KIND_END, new Uint8Array(0)));
      } catch {
        // closing anyway
      }
    }
    this.finish(null);
  }

  private finish(result: VoiceResult | null, error?: string): void {
    if (this.ended) return;
    this.ended = true;
    try {
      this.ws.close();
    } catch {
      // already closed
    }
    this.audio.close();
    this.handlers.onEnd(result, error);
  }
}
