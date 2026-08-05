// Fused mark + name, one centered row above a homepage hero's title. Both
// theme renders of the mark are in the DOM; CSS shows the one matching
// data-theme (dark is the :root default, light takes over under
// [data-theme="light"] — see .home-hero-logo-* in shell.css). Lives in
// platform because two apps wear it: the builder ("Fused App") and the
// explorer ("Fused Explorer").
import logoMarkDark from "@assets/logo-black-bg-transparent.png";
import logoMarkLight from "@assets/logo-white-bg-transparent.png";

export function HeroBrand({ name }: { name: string }) {
  return (
    <div className="home-hero-brand">
      <img className="home-hero-logo home-hero-logo-dark" src={logoMarkDark} alt="" aria-hidden="true" />
      <img className="home-hero-logo home-hero-logo-light" src={logoMarkLight} alt="" aria-hidden="true" />
      <span className="home-hero-brand-name">{name}</span>
    </div>
  );
}
