// The Apps page's "What do you want to build?" composer as a Home widget.
// HeroComposer owns the whole create path (name → scaffold → navigate to the
// new app's chat), so the widget only mounts it; onCreated has nothing to do.
import { ChevronDown } from "lucide-react";
import { HeroComposer, PICK_GLYPHS } from "@apps/builder/HomeHero";

export function BuildWidget({ edit }: { edit: boolean }) {
  if (edit) {
    // Static stand-in while editing: a live textarea inside a draggable card
    // would swallow the drag and the edit-mode typing.
    return (
      <div className="hw-body hw-ph hw-build-ph" aria-hidden="true">
        <div className="hw-build-hero">
          <p className="hw-build-headline">What do you want to <em>build</em>?</p>
          <div className="home-composer-wrap">
            <div className="home-composer">
              <div className="home-composer-input hw-ph-text">What do you want to build?</div>
              <div className="home-composer-bar">
                <div className="home-composer-picks">
                  <span className="home-composer-pick">
                    <span className="home-composer-pick-glyph">
                      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">{PICK_GLYPHS.model}</svg>
                    </span>
                    <span className="home-composer-pick-sel hw-ph-sel">Fable<ChevronDown size={12} /></span>
                  </span>
                  <span className="home-composer-pick">
                    <span className="home-composer-pick-glyph">
                      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">{PICK_GLYPHS.effort}</svg>
                    </span>
                    <span className="home-composer-pick-sel hw-ph-sel">medium<ChevronDown size={12} /></span>
                  </span>
                </div>
                <span className="home-composer-send">
                  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 19V5M5 12l7-7 7 7" /></svg>
                </span>
              </div>
            </div>
          </div>
          <div className="home-composer-samples">
            <div className="home-composer-sample-strip">
              {["Language drill", "Inbox triage", "How-to short", "Mini game"].map((s) => (
                <span key={s} className="home-composer-sample">{s}</span>
              ))}
            </div>
          </div>
        </div>
      </div>
    );
  }
  return (
    <div className="hw-build-hero">
      <p className="hw-build-headline">What do you want to <em>build</em>?</p>
      <HeroComposer onCreated={() => {}} />
    </div>
  );
}
