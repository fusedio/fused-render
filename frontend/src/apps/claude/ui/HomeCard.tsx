// The landing page's headline and its big composer (T:4216-4265,
// T:12394-12446 for the title's own fit).
//
// The title keeps its full 26px until the NAME it is showing collides with the
// column, and then it takes exactly the size that fits on one line — a folder
// name set as a two-line 26px headline is the loudest thing on a page whose
// subject is the composer under it. MEASURED, off-DOM, and deliberately not a
// container query: the thing worth measuring is whether THIS name fits, not how
// wide the column is.
import { useLayoutEffect, useRef } from "react";
import { ComposerCard, type ComposerCardProps } from "./Composer";
import { HOME_TITLE_STEPS, measureTextIn, pickHomeTitleStep } from "./fit";

/** The steps' class names, so a caller can reason about them. */
export { HOME_TITLE_STEPS };

function useHomeTitleFit(
  ref: React.RefObject<HTMLElement | null>,
  name: string,
): void {
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      if (!el.offsetWidth) return; // landing page not on screen
      const box = el.clientWidth;
      if (!box) return;
      // Measured at the BASE size, with any step class off, so the reading is
      // the same one whatever the last verdict was.
      el.classList.remove("c-t-mid", "c-t-min");
      const spark = el.querySelector(".c-spark");
      const need =
        measureTextIn(el, el.textContent || "") +
        (spark ? parseFloat(getComputedStyle(spark).marginRight) || 0 : 0);
      const base =
        parseFloat(getComputedStyle(el).fontSize) || HOME_TITLE_STEPS[0][0];
      const step = pickHomeTitleStep(need, base, box);
      if (step) el.classList.add(step);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(el.parentElement ?? el);
    return () => observer.disconnect();
  }, [ref, name]);
}

export type HomeCardProps = Omit<ComposerCardProps, "variant"> & {
  /** The target's own name (`#home-file`) and the path under it
   *  (`#home-path`). */
  name: string;
  path?: string;
};

export function HomeCard({ name, path, ...card }: HomeCardProps) {
  const titleRef = useRef<HTMLDivElement | null>(null);
  useHomeTitleFit(titleRef, name);
  return (
    <>
      <div className="c-home-title" ref={titleRef}>
        <span className="c-spark">✻</span>
        <span>{name}</span>
      </div>
      {path ? <div className="c-home-sub">{path}</div> : null}
      <ComposerCard {...card} variant="home" />
    </>
  );
}
