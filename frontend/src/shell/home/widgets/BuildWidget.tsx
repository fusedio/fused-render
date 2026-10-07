// The Apps page's "What do you want to build?" composer as a Home widget.
// HeroComposer owns the whole create path (name → scaffold → navigate to the
// new app's chat), so the widget only mounts it; onCreated has nothing to do.
import { Hammer } from "lucide-react";
import { HeroComposer } from "@apps/builder/HomeHero";

export function BuildWidget({ edit }: { edit: boolean }) {
  if (edit) {
    // Static stand-in while editing: a live textarea inside a draggable card
    // would swallow the drag and the edit-mode typing.
    return (
      <div className="hw-body hw-build-ph" aria-hidden="true">
        <Hammer size={16} />
        <span>What do you want to build?</span>
      </div>
    );
  }
  return <HeroComposer onCreated={() => {}} />;
}
