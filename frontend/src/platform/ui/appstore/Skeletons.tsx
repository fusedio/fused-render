// Loading placeholders that mirror the real layouts (card, list row, detail
// page), so content lands where the shimmer already was and nothing jumps.
//
// `Delayed` holds them back ~150ms: a cached or fast answer paints straight
// over a blank frame instead of flashing a skeleton for one frame.
import { useEffect, useState, type CSSProperties, type ReactNode } from "react";

export function Delayed({ ms = 150, children }: { ms?: number; children: ReactNode }) {
  const [show, setShow] = useState(ms <= 0);
  useEffect(() => {
    if (ms <= 0) return;
    const id = setTimeout(() => setShow(true), ms);
    return () => clearTimeout(id);
  }, [ms]);
  return show ? <>{children}</> : null;
}

/** One shimmering block. */
export function Skel({ w, h, r, className, style }: { w?: number | string; h?: number | string; r?: number; className?: string; style?: CSSProperties }) {
  return (
    <span
      className={"as-skel" + (className ? " " + className : "")}
      style={{ width: w, height: h, borderRadius: r, ...style }}
      aria-hidden="true"
    />
  );
}

export function CardSkeleton({ variant = "default" }: { variant?: "default" | "compact" }) {
  return (
    <div className={"as-card is-" + variant + " is-skeleton"} aria-hidden="true">
      {variant === "default" && <span className="as-card-thumb as-skel" />}
      <span className="as-card-body">
        <Skel w={44} h={44} r={10} />
        <span className="as-card-text">
          <Skel w="56%" h={13} r={4} />
          <Skel w="92%" h={10} r={4} style={{ marginTop: 8 }} />
          <Skel w="70%" h={10} r={4} style={{ marginTop: 6 }} />
        </span>
      </span>
      {variant === "default" && (
        <span className="as-card-foot">
          <Skel w="40%" h={10} r={4} />
          <Skel w={68} h={28} r={14} />
        </span>
      )}
    </div>
  );
}

export function RowSkeleton() {
  return (
    <div className="as-row is-skeleton" aria-hidden="true">
      <Skel w={40} h={40} r={9} />
      <span className="as-row-text">
        <Skel w="30%" h={12} r={4} />
        <Skel w="55%" h={10} r={4} style={{ marginTop: 7 }} />
      </span>
      <Skel w={76} h={28} r={14} />
    </div>
  );
}

export function DetailSkeleton() {
  return (
    <div className="as-detail-skel" aria-busy="true" aria-label="Loading">
      <div className="as-hero">
        <Skel w={112} h={112} r={25} />
        <div className="as-hero-text">
          <Skel w={260} h={26} r={6} />
          <Skel w="70%" h={13} r={4} style={{ marginTop: 14 }} />
          <Skel w={120} h={12} r={4} style={{ marginTop: 10 }} />
        </div>
        <div className="as-hero-actions">
          <Skel w={112} h={36} r={18} />
        </div>
      </div>
      <Skel w="100%" h={78} r={14} style={{ marginTop: 28 }} />
      <Skel className="as-stage-skel" w="100%" r={12} style={{ marginTop: 28, aspectRatio: "16 / 10", height: "auto" }} />
    </div>
  );
}
