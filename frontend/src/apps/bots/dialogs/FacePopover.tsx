// The avatar picker (OpenBot core.js pickFace): shapes, then brand marks, then colours, the preview on top (#pface)
// playing the mood cycle after each pick. A Popover on the avatar button in Settings › General and the New bot form,
// not a modal over the dialog (owner, 2026-10-09): every pick applies at once through onPick (the opener keeps the
// draft), outside press or Escape closes. A brand brings its own colour; picking a shape clears the brand and falls
// back to a palette colour. Shapes are drawn in the draft colour only while it is a palette colour; a brand colour
// would paint every blob the brand's blue.
//
// The popup is portaled, but it is a React descendant of the dialog, and Base UI's dismiss marks any press inside the
// React tree as inside (useDismiss: onPointerDownCapture → insideReactTree), so a swatch press never dismisses the
// dialog. The press OUTSIDE the popover that closes it is outside the dialog too, though: `onOpenChange` lets the
// opener hold its `busy` ref while the popover is up and release it a tick after it closes, so that press closes the
// popover alone.
import { useEffect, useState, type ReactNode } from "react";
import { cn } from "@platform/lib/utils";
import { Popover, PopoverContent, PopoverTrigger } from "@platform/shadcn/ui/popover";
import { endPickCycle, playPickCycle } from "../components/faceAnim";
import { Face } from "../components/Face";
import { BRANDS, FACE_COLORS, FACE_SHAPES, faceOf, pickableBrands, type FaceDraft, type FaceSubject } from "../lib/face";

const SWATCH = "flex cursor-pointer appearance-none items-center justify-center rounded-full border-0 bg-transparent p-1 outline-2 outline-offset-3 outline-transparent transition-[outline-color,transform] duration-150 hover:-translate-y-0.5 focus-visible:outline-ring aria-pressed:outline-foreground [&_svg]:block [&_svg]:size-full [&_svg]:overflow-visible";

export interface FacePopoverProps {
  /** Whose face: the draft is read from it (faceOf) on every render, so the opener's state is the one source. */
  subject: FaceSubject;
  onPick: (f: FaceDraft) => void;
  /** Open / closed, for the opener's `busy` guard (see the header comment). */
  onOpenChange?: (open: boolean) => void;
  disabled?: boolean;
  className?: string;
  title?: string;
  /** The trigger's content (the avatar and its caption). */
  children: ReactNode;
}

export function FacePopover({ subject, onPick, onOpenChange, disabled, className, title, children }: FacePopoverProps) {
  const [open, setOpenState] = useState(false);
  const setOpen = (o: boolean) => { setOpenState(o); onOpenChange?.(o); };
  const [picks, setPicks] = useState(0);  // bumped per pick; the cycle starts once the new preview is committed
  useEffect(() => { if (picks) void playPickCycle(); }, [picks]);
  useEffect(() => { if (!open) endPickCycle(); }, [open]);
  const draft = faceOf(subject);
  const palette = (c: string) => (FACE_COLORS.includes(c) ? c : FACE_COLORS[1]);
  const set = (patch: Partial<FaceDraft>) => { onPick({ ...draft, ...patch }); setPicks((n) => n + 1); };
  const blobColor = palette(draft.color);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger disabled={disabled} title={title}
        className={cn("group flex cursor-pointer appearance-none items-center gap-2.5 rounded-lg border-0 bg-transparent p-0 outline-none focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-default", className)}>
        {children}
      </PopoverTrigger>
      <PopoverContent align="end" className="w-[340px] items-center gap-4 p-4" aria-label="Edit avatar">
        <span className="size-20 [&>svg]:block [&>svg]:size-full" id="pfacew"><Face b={{ face: draft }} svgId="pface" /></span>
        <div className="flex flex-wrap justify-center gap-3" id="fshapes">
          {Object.keys(FACE_SHAPES).map((k) => (
            <button key={k} type="button" data-shape={k} aria-pressed={!draft.icon && k === draft.shape} title={k} onClick={() => set({ shape: k, icon: "", color: blobColor })} className={cn(SWATCH, "size-14")}>
              <Face b={{ face: { shape: k, color: blobColor } }} />
            </button>
          ))}
        </div>
        <div className="flex flex-wrap justify-center gap-2" id="fbrands">
          {pickableBrands().map((k) => (
            <button key={k} type="button" data-icon={k} aria-pressed={k === draft.icon} title={BRANDS[k].name} onClick={() => set({ icon: k, color: BRANDS[k].color })} className={cn(SWATCH, "size-10 p-0.5")}>
              <Face b={{ face: { icon: k, color: k === draft.icon ? draft.color : BRANDS[k].color } }} />
            </button>
          ))}
        </div>
        <div className="flex flex-wrap justify-center gap-3" id="fcolors">
          {FACE_COLORS.map((c) => (
            <button key={c} type="button" data-color={c} aria-pressed={c === draft.color} aria-label={c} style={{ background: c }} onClick={() => set({ color: c })} className={cn(SWATCH, "size-8 p-0")} />
          ))}
        </div>
      </PopoverContent>
    </Popover>
  );
}
