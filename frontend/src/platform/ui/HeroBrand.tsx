// Fused mark + name, one centered row above a homepage hero's title. Both
// theme renders of the mark are in the DOM; CSS shows the one matching
// data-theme (dark is the :root default, light takes over under
// [data-theme="light"] — see .home-hero-logo-* in shell.css). Lives in
// platform because two apps wear it: the builder ("App") and the
// explorer ("Explorer"). An optional tagline sits on the same line, smaller
// and dimmer than the name so the name stays the visual anchor.
import type { ReactNode } from "react";
import logoMarkDark from "@assets/logo-black-bg-transparent.png";
import logoMarkLight from "@assets/logo-white-bg-transparent.png";

export function HeroBrand({ name, tagline }: { name: string; tagline?: ReactNode }) {
  return (
    <h1 className="home-hero-brand">
      <span className="home-hero-brand-mark">
        <img className="home-hero-logo home-hero-logo-dark" src={logoMarkDark} alt="" aria-hidden="true" />
        <img className="home-hero-logo home-hero-logo-light" src={logoMarkLight} alt="" aria-hidden="true" />
        <span className="home-hero-brand-name">{name}</span>
      </span>
      {tagline && <span className="home-hero-tagline">{tagline}</span>}
    </h1>
  );
}
