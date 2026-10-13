// The chat's composer: a quote strip when replying, the attached images, and a
// row of attach · text · send. Images go out as the original bytes (`data`,
// saved to the bot's files/) plus a canvas-scaled `preview` (long edge <= 1568
// px, the model's own ceiling) that rides the model turn — the same pair the
// desktop runtime's fused.bots builds (runtime.js botsReadFile).
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ArrowUpIcon, ImagePlusIcon, LoaderIcon, XIcon } from "lucide-react";
import { cn } from "@platform/lib/utils";
import { FILE_MAX, IMAGE_TYPES, oneLine, previewType, scaledSize } from "./lib";
import { toast } from "./Toast";
import type { SendFile } from "./types";

export interface Attachment {
  id: number;
  file: File;
  url: string;
}

function readBase64(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => {
      const s = String(r.result || "");
      resolve(s.slice(s.indexOf(",") + 1));
    };
    r.onerror = () => reject(r.error || new Error("could not read the file"));
    r.readAsDataURL(file);
  });
}

function preview(file: File): Promise<SendFile["preview"] | undefined> {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    const done = (v: SendFile["preview"] | undefined) => {
      URL.revokeObjectURL(url);
      resolve(v);
    };
    img.onload = () => {
      try {
        const { w, h } = scaledSize(img.naturalWidth, img.naturalHeight);
        const c = document.createElement("canvas");
        c.width = w;
        c.height = h;
        c.getContext("2d")!.drawImage(img, 0, 0, w, h);
        const mt = previewType(file.type || "");
        const d = c.toDataURL(mt, 0.85);
        // A canvas the browser could not allocate encodes as "data:," — send the original only.
        const data = d.slice(d.indexOf(",") + 1);
        done(data ? { media_type: mt, data } : undefined);
      } catch {
        done(undefined);
      }
    };
    img.onerror = () => done(undefined);
    img.src = url;
  });
}

export async function toSendFile(file: File): Promise<SendFile> {
  const out: SendFile = { name: file.name || "photo.jpg", data: await readBase64(file) };
  // Any image the browser can decode gets a preview (a HEIC from the photo
  // library included: Safari decodes it, the preview goes out as JPEG).
  if (IMAGE_TYPES.has(file.type) || file.type.startsWith("image/")) {
    const pv = await preview(file);
    if (pv) out.preview = pv;
  }
  return out;
}

const FINE_POINTER = typeof matchMedia !== "undefined" && matchMedia("(pointer: fine)").matches;
let nextId = 1;

export function Composer({
  replyTo,
  onClearReply,
  onSend,
  placeholder,
  disabled,
}: {
  replyTo: { seq: number; text: string } | null;
  onClearReply: () => void;
  /** Resolves true when the server took the message (the draft clears). */
  onSend: (text: string, files: File[]) => Promise<boolean>;
  placeholder: string;
  disabled?: boolean;
}) {
  const [text, setText] = useState("");
  const [files, setFiles] = useState<Attachment[]>([]);
  const [sending, setSending] = useState(false);
  const ta = useRef<HTMLTextAreaElement>(null);
  const picker = useRef<HTMLInputElement>(null);

  // Grow with the draft up to ~6 lines, then scroll inside.
  useLayoutEffect(() => {
    const el = ta.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 148)}px`;
  }, [text]);

  useEffect(() => {
    if (replyTo) ta.current?.focus();
  }, [replyTo]);

  // Object URLs die with their thumbnails.
  const filesRef = useRef(files);
  filesRef.current = files;
  useEffect(() => () => filesRef.current.forEach((f) => URL.revokeObjectURL(f.url)), []);

  const add = (list: FileList | null) => {
    const picked: Attachment[] = [];
    for (const f of Array.from(list || [])) {
      if (f.size > FILE_MAX) {
        toast(`${f.name || "That image"} is ${Math.round(f.size / 1048576)} MB; attachments are limited to 8 MB`, "error");
        continue;
      }
      picked.push({ id: nextId++, file: f, url: URL.createObjectURL(f) });
    }
    if (picked.length) setFiles((prev) => [...prev, ...picked].slice(0, 8));
  };
  const remove = (id: number) =>
    setFiles((prev) => {
      const gone = prev.find((f) => f.id === id);
      if (gone) URL.revokeObjectURL(gone.url);
      return prev.filter((f) => f.id !== id);
    });

  const canSend = !sending && !disabled && (text.trim().length > 0 || files.length > 0);
  const send = async () => {
    if (!canSend) return;
    setSending(true);
    try {
      const draft = text;
      const sent = files;
      const ok = await onSend(draft.trim(), sent.map((f) => f.file));
      if (ok) {
        // Only what went out: anything typed or attached while it sent stays.
        setText((t) => (t === draft ? "" : t));
        sent.forEach((f) => URL.revokeObjectURL(f.url));
        setFiles((prev) => prev.filter((f) => !sent.includes(f)));
      }
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="shrink-0 border-t border-[var(--sh-border)] bg-[var(--bg)]/95 px-3 pt-2 pb-[max(8px,env(safe-area-inset-bottom))] backdrop-blur [html[data-kb]_&]:pb-2">
      {replyTo && (
        <div className="mb-2 flex items-center gap-2 rounded-xl bg-[var(--bg-alt)] py-1 pr-1 pl-3">
          <div className="min-w-0 flex-1 border-l-2 border-[var(--accent)] pl-2 text-[13px] leading-snug">
            <div className="font-medium text-[var(--fg-muted)]">Replying to</div>
            <div className="truncate">{oneLine(replyTo.text, 120)}</div>
          </div>
          <button
            type="button"
            onClick={onClearReply}
            aria-label="Cancel reply"
            className="flex size-10 shrink-0 items-center justify-center rounded-full text-[var(--fg-muted)] active:bg-[var(--row-bg-hover)]"
          >
            <XIcon className="size-4" />
          </button>
        </div>
      )}
      {files.length > 0 && (
        <div className="lan-strip mb-2 flex gap-2 overflow-x-auto pt-1.5">
          {files.map((f) => (
            <div key={f.id} className="relative shrink-0">
              <img src={f.url} alt={f.file.name} className="size-16 rounded-xl border border-[var(--sh-border)] object-cover" />
              <button
                type="button"
                onClick={() => remove(f.id)}
                aria-label={`Remove ${f.file.name}`}
                className="absolute -top-1.5 -right-1.5 flex size-6 items-center justify-center rounded-full bg-[var(--fg)] text-[var(--bg)] shadow"
              >
                <XIcon className="size-3.5" />
              </button>
            </div>
          ))}
        </div>
      )}
      <div className="flex items-end gap-2">
        <button
          type="button"
          onClick={() => picker.current?.click()}
          disabled={disabled}
          aria-label="Attach images"
          className="flex size-11 shrink-0 items-center justify-center rounded-full text-[var(--fg-muted)] active:bg-[var(--row-bg-hover)] disabled:opacity-40"
        >
          <ImagePlusIcon className="size-[22px]" />
        </button>
        <input
          ref={picker}
          type="file"
          accept="image/*"
          multiple
          hidden
          onChange={(e) => {
            add(e.target.files);
            e.target.value = "";
          }}
        />
        <textarea
          ref={ta}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            // A hardware keyboard (iPad, desktop browser): Enter sends. On a
            // phone the return key is a newline; the button sends.
            if (FINE_POINTER && e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void send();
            }
          }}
          rows={1}
          placeholder={placeholder}
          aria-label="Message"
          disabled={disabled}
          className="min-h-11 flex-1 resize-none rounded-[22px] border border-[var(--sh-border)] bg-[var(--card)] px-4 py-[10px] text-[16px] leading-[1.4] outline-none placeholder:text-[var(--fg-muted)] focus:border-[color-mix(in_srgb,var(--accent)_60%,var(--sh-border))]"
        />
        <button
          type="button"
          onClick={send}
          disabled={!canSend}
          aria-label="Send"
          className={cn(
            "flex size-11 shrink-0 items-center justify-center rounded-full transition-colors",
            canSend ? "bg-[var(--accent)] text-[var(--on-accent)]" : "bg-[var(--bg-alt)] text-[var(--fg-muted)]",
          )}
        >
          {sending ? <LoaderIcon className="size-5 animate-spin" /> : <ArrowUpIcon className="size-5" />}
        </button>
      </div>
    </div>
  );
}
