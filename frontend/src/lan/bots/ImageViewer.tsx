// Full-screen image: a step thumbnail or the live browser shot. Tap anywhere
// (or ×, or Escape) to close; pinch-zoom is the browser's own.
import { useEffect } from "react";
import { XIcon } from "lucide-react";

export function ImageViewer({ src, alt, onClose }: { src: string | null; alt?: string; onClose: () => void }) {
  useEffect(() => {
    if (!src) return;
    const key = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", key);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", key);
      document.body.style.overflow = prev;
    };
  }, [src, onClose]);
  if (!src) return null;
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={alt || "Image"}
      className="fixed inset-0 z-[80] flex items-center justify-center bg-black/95 duration-150 animate-in fade-in-0"
      onClick={onClose}
    >
      <img src={src} alt={alt || ""} className="max-h-full max-w-full object-contain" />
      <button
        type="button"
        aria-label="Close"
        onClick={onClose}
        className="absolute right-3 flex size-11 items-center justify-center rounded-full bg-white/15 text-white backdrop-blur"
        style={{ top: "max(12px, env(safe-area-inset-top))" }}
      >
        <XIcon className="size-5" />
      </button>
    </div>
  );
}
