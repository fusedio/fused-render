// Taking a picture with the webcam, for the stages that accept one.
//
// Two stages want this: the image stage attaches a frame to edit, the text
// stage attaches one to ask about. The plumbing is identical and none of it is
// small — a MediaStream that must be stopped on every exit, a <video> that
// cannot be wired until the overlay is mounted, Escape, and a canvas draw — so
// it lives here rather than twice. The stages differ only in what they do with
// the blob, which is the argument `capture` takes.
import { useCallback, useEffect, useRef, useState, type MutableRefObject } from "react";
import { getConfig, mkdir } from "@platform/lib/api";
import { Button } from "@platform/shadcn/ui/button";
import { Lightbox, LightboxBox, LightboxClose, webcamVideoClass } from "@platform/ui/playground";
import { uploadFile } from "./client";
import type { AttachedImage } from "./imageInput";

/** The live camera, as one hook: whether it is open, the ref for the overlay's
 *  <video>, and the three things a caller does with it.
 *
 *  `onError` rather than a thrown promise, because every one of these failures
 *  is a sentence for the stage's own error line — the stages already have one
 *  and a second error surface would be a second thing to dismiss. */
export function useWebcam({ onError }: { onError: (message: string) => void }) {
  const [open, setOpen] = useState(false);
  const streamRef = useRef<MediaStream | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);

  const stop = useCallback(() => {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    setOpen(false);
  }, []);

  // The hook is the one owner of the stream's lifetime — a stage unmounting
  // mid-capture must not leave a camera light on. Its own alive flag, so a
  // getUserMedia that resolves after the unmount is stopped rather than shown.
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      streamRef.current?.getTracks().forEach((track) => track.stop());
      streamRef.current = null;
    };
  }, []);

  // The live view is attached HERE, not in the click that opened the camera:
  // the <video> does not exist until this render, so `srcObject` has nothing
  // to be set on until the overlay is mounted.
  useEffect(() => {
    if (!open) return;
    const video = videoRef.current;
    const stream = streamRef.current;
    if (!video || !stream) return;
    video.srcObject = stream;
    void video.play().catch(() => {});
  }, [open]);

  // Escape cancels the webcam, the same way it closes every overlay here.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && stop();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, stop]);

  /** Is a `getUserMedia` in flight? The button that calls this is enabled the
   *  whole time one is — `open` does not flip until the stream arrives, and on
   *  a first use that wait is however long the browser's permission prompt
   *  stands there. Two clicks across it used to mean two streams, the second
   *  overwriting the first in `streamRef`: the orphan had no owner left to
   *  stop it, so the camera light stayed on after the overlay closed. */
  const startingRef = useRef(false);

  const start = useCallback(async () => {
    // Already filming, or already asking. Either way this click is a repeat of
    // one still being answered.
    if (startingRef.current || streamRef.current) return;
    startingRef.current = true;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 1280 }, height: { ideal: 720 } },
      });
      // Stopped through the local handle, not through `stop()`: this stream is
      // not in the ref yet, and on a dead component it never should be.
      if (!aliveRef.current) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      streamRef.current = stream;
      setOpen(true);
    } catch (e) {
      onError(
        (e as Error).name === "NotAllowedError"
          ? "Camera access was refused — allow it in the browser and try again."
          : (e as Error).message,
      );
    } finally {
      startingRef.current = false;
    }
  }, [onError]);

  /** One frame off the live view, at the camera's own pixels. PNG because
   *  `toBlob` is guaranteed to produce one and the server reads it. */
  const capture = useCallback(
    (onBlob: (blob: Blob) => void) => {
      const video = videoRef.current;
      if (!video || !video.videoWidth) return;
      const canvas = document.createElement("canvas");
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      canvas.getContext("2d")?.drawImage(video, 0, 0);
      // Stopped before the encode, not after: the frame is already drawn, and
      // leaving the light on through an async toBlob reads as still filming.
      stop();
      canvas.toBlob((blob) => {
        if (blob) onBlob(blob);
      }, "image/png");
    },
    [stop],
  );

  return { open, start, stop, capture, videoRef };
}

/** The camera, over everything — the lightbox's own shape: a scrim, the live
 *  view where the picture goes, one ✕. Capture is the only action. Click the
 *  backdrop or press Escape to cancel, the two things anybody tries; both land
 *  in the hook's `stop`, the one place the stream ends. */
export function WebcamOverlay({
  videoRef,
  onCapture,
  onClose,
}: {
  videoRef: MutableRefObject<HTMLVideoElement | null>;
  onCapture: () => void;
  onClose: () => void;
}) {
  return (
    <Lightbox open onClose={onClose} label="Webcam">
      <LightboxBox onClick={(e) => e.stopPropagation()}>
        <video ref={videoRef} className={webcamVideoClass} playsInline muted />
        <Button variant="outline" onClick={onCapture}>
          Capture
        </Button>
      </LightboxBox>
      <LightboxClose
        type="button"
        title="Close without a picture"
        aria-label="Close without a picture"
        onClick={onClose}
      >
        ✕
      </LightboxClose>
    </Lightbox>
  );
}

/** Bytes this app invented, written where the app's own state lives.
 *
 *  A webcam frame has no path — it does not exist anywhere yet — so this one
 *  case genuinely has to be written before it can be pointed at. It lands in
 *  the app's scratch dir, `<cache>/image-playground` (`~/.fused-render/cache/…`),
 *  and NOT anywhere in the user's home: a capture dropped in `ai/images` — a
 *  folder the user browses, holding renders — is a picture nobody can tell
 *  from a generated one. Both stages' captures share the dir; they are the
 *  same kind of bytes and a second folder would only be a second thing to
 *  clear.
 *
 *  Both levels are mkdir'd, because `/api/fs/mkdir` creates ONE directory by
 *  design (a typo must not spawn a tree) and on a fresh machine neither exists.
 *
 *  IO only: it writes and hands back the path. Whether that becomes the
 *  stage's attachment, and what else moves when it does, is the caller's — the
 *  two stages genuinely differ there. Throws, so a caller keeps its own error
 *  and busy handling in one place. */
export async function saveToCache(
  data: Blob,
  name: string,
  /** A capture is stamped — every one is a different picture and losing the
   *  last would be a surprise. A shipped SAMPLE is not: the same bytes land at
   *  the same path however many times the pill is clicked, so the examples
   *  cannot slowly fill the cache with copies of themselves. */
  stamped = true,
): Promise<AttachedImage> {
  const config = await getConfig();
  await mkdir(config.cache_dir).catch(() => {});
  const dir = `${config.cache_dir}/image-playground`;
  await mkdir(dir).catch(() => {});
  const stamp = stamped
    ? new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19) + "-"
    : "sample-";
  const path = `${dir}/${stamp}${name}`;
  await uploadFile(path, data, name);
  return { path, name };
}
