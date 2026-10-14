// The large screenshot on a detail page: a 16:10 stage (12px radius,
// hairline) that shimmers until the image decodes, fades it in, and opens a
// LIGHTBOX on click — the full image over a scrim, Esc / click / ✕ to close.
//
// Generic: a src and an alt. A missing or broken image renders the caller's
// `fallback` (or a quiet placeholder) rather than a broken-image glyph.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ImageOff, X } from "lucide-react";

export interface LightboxProps {
  src: string;
  alt: string;
  onClose: () => void;
}

export function Lightbox({ src, alt, onClose }: LightboxProps) {
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        e.preventDefault();
        onClose();
      }
    };
    // Capture: a page-level Esc handler (search clear, a sheet) must not see
    // the key that was meant for the lightbox.
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      prev?.focus?.();
    };
  }, [onClose]);
  return createPortal(
    <div className="as-lightbox" role="dialog" aria-modal="true" aria-label={alt} onClick={onClose}>
      <img className="as-lightbox-img" src={src} alt={alt} onClick={(e) => e.stopPropagation()} />
      <button ref={closeRef} type="button" className="as-lightbox-close" aria-label="Close" onClick={onClose}>
        <X size={18} strokeWidth={2.2} aria-hidden="true" />
      </button>
    </div>,
    document.body,
  );
}

export interface PreviewStageProps {
  src: string | null | undefined;
  alt: string;
  /** CSS aspect-ratio (default "16 / 10"). */
  aspect?: string;
  /** What to show when there is no image (or it fails to load). */
  fallback?: ReactNode;
  className?: string;
}

export function PreviewStage({ src, alt, aspect = "16 / 10", fallback, className }: PreviewStageProps) {
  // Keyed on the src rather than reset by an effect: a cached image can fire
  // `load` before a post-mount reset runs, and the reset would then hide a
  // loaded image for good.
  const [loadedSrc, setLoadedSrc] = useState<string | null>(null);
  const [failedSrc, setFailedSrc] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const loaded = !!src && loadedSrc === src;
  const usable = !!src && failedSrc !== src;
  return (
    <figure className={"as-stage" + (className ? " " + className : "")} style={{ aspectRatio: aspect }}>
      {usable ? (
        <button
          type="button"
          className={"as-stage-btn" + (loaded ? "" : " is-loading")}
          onClick={() => setOpen(true)}
          aria-label={`Enlarge: ${alt}`}
        >
          <img
            src={src}
            alt={alt}
            className={"as-stage-img" + (loaded ? " is-loaded" : "")}
            ref={(el) => {
              // Already decoded (from cache) before React attached onLoad.
              if (el && el.complete && el.naturalWidth > 0 && loadedSrc !== src) setLoadedSrc(src);
            }}
            onLoad={() => setLoadedSrc(src)}
            onError={() => setFailedSrc(src)}
            draggable={false}
          />
        </button>
      ) : (
        <div className="as-stage-empty">
          {fallback ?? (
            <>
              <ImageOff size={22} strokeWidth={1.6} aria-hidden="true" />
              <span>No preview</span>
            </>
          )}
        </div>
      )}
      {open && usable && <Lightbox src={src} alt={alt} onClose={() => setOpen(false)} />}
    </figure>
  );
}

export default PreviewStage;
