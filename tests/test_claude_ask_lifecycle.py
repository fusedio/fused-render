"""The shell's half of "Fix with AI" (review #804 round 2): the redesign from
push (a `_fused_ask` query param on the claude iframe's src, kept "one-shot"
by a cache) to pull (plain host-side state, taken and cleared by the claude
template's own boot).

Round 1's shape had a structural hole findings 1/2/3 named concretely: ANY
remount of the claude iframe for a reason that has nothing to do with a new
ask (toggling the sidebar away and back, closing and reopening the folder
pane, a panel/tab reattaching) rebuilt the identical cached src and replayed
the stale ask into a brand-new conversation. No cache design closes that,
because a `src` is an address, and "follow this part of the address only the
first time" is not something a URL can express.

Round 2's fix removes the ask from the src entirely. `frontend/src/apps/
explorer/lib/claude-ask.ts`'s `takeClaudeAsk` (and its own bun tests) pin the
pure "read clears it" primitive; this file pins that both hosts
(Preview.tsx/Listing.tsx) actually wire it in, that neither leaves the old
push mechanism (`_fused_ask`) anywhere, and that the one remaining lifecycle
gap a pure take cannot close by itself — a SECOND ask arriving while the
sidebar/pane is ALREADY on claude, where nothing about the mode or the
folder/file changes to force a remount — is closed by an explicit instance
bump folded into the remount key.
"""
import os

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXPLORER = os.path.join(_ROOT, "frontend", "src", "apps", "explorer")


def _read(*parts: str) -> str:
    with open(os.path.join(_EXPLORER, *parts), encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def preview() -> str:
    return _read("Preview.tsx")


@pytest.fixture(scope="module")
def preview_sidebar() -> str:
    return _read("PreviewSidebar.tsx")


@pytest.fixture(scope="module")
def listing() -> str:
    return _read("Listing.tsx")


@pytest.fixture(scope="module")
def listing_pane() -> str:
    return _read("ListingPreviewPane.tsx")


# ------------------------------------------------------------- no more push

def test_the_push_mechanism_is_gone_from_every_surface(preview, preview_sidebar,
                                                         listing, listing_pane):
    """No `_fused_ask` query param, and no caching module resurrected in its
    place, on either surface."""
    for label, src in [("Preview.tsx", preview), ("PreviewSidebar.tsx", preview_sidebar),
                        ("Listing.tsx", listing), ("ListingPreviewPane.tsx", listing_pane)]:
        # The literal QUOTED param form, not the bare word: these files' own
        # comments record the round-1 shape in prose (the repo's convention),
        # and a bare-substring check would fail on that history.
        assert '"_fused_ask"' not in src, label
        assert "resolveClaudeAskSeed" not in src, label
        assert "claude-ask-seed" not in src, label


def test_the_seed_module_and_its_cache_type_are_deleted():
    seed_module = os.path.join(_EXPLORER, "lib", "claude-ask-seed.ts")
    seed_test = os.path.join(_EXPLORER, "lib", "claude-ask-seed.test.ts")
    assert not os.path.exists(seed_module)
    assert not os.path.exists(seed_test)


# ------------------------------------------------------------------ the pull

def test_both_hosts_install_and_use_the_shared_take_primitive(preview, listing):
    """Both call into `takeClaudeAsk` (lib/claude-ask.ts) rather than each
    hand-rolling its own read-and-clear — one implementation, not two that can
    drift."""
    for label, src in [("Preview.tsx", preview), ("Listing.tsx", listing)]:
        assert 'import { takeClaudeAsk } from "@apps/explorer/lib/claude-ask";' in src, label
        assert "window._fusedClaudeAskTake = () => takeClaudeAsk(claudeSeedRef);" in src, label


def test_the_shared_take_primitive_reads_and_clears_in_one_step():
    src = _read("lib", "claude-ask.ts")
    fn = src[src.index("export function takeClaudeAsk("):]
    fn = fn[:fn.index("\n}")]
    assert "pending.current = null;" in fn
    assert "return value;" in fn
    # The clear happens unconditionally, not gated on the value being
    # non-null — a take of an already-empty pending must still leave it empty
    # (trivially true here, but the shape — no `if` around the clear — is
    # what makes it trivially true rather than incidentally true).
    assert "if" not in fn


# ------------------------------------------------ forcing a remount to pull

def test_preview_keys_the_claude_iframe_separately_from_the_mode(preview, preview_sidebar):
    """A second ask while the sidebar is ALREADY on claude changes neither
    `activeSide` nor `fsPath` — the ordinary `key={active}` a mode switch uses
    would not remount, and the claude template would never reboot to pull the
    new text. `claudeFrameKey` closes that gap; `PreviewSidebar` must actually
    use it for the iframe's key."""
    assert "const claudeFrameKey = (m: string) => (m === \"claude\" ? `claude:${claudeAskInstance}` : m);" in preview
    assert "frameKey={claudeFrameKey(activeSide)}" in preview
    assert "key={frameKey}" in preview_sidebar
    # And PreviewSidebar's prop must actually be read from a variable frameKey
    # defaults to `active`, not hardcoded — ordinary mode switches still key
    # off the mode alone when the caller passes nothing special.
    assert "frameKey = active" in preview_sidebar


def test_preview_bumps_the_claude_instance_on_every_incoming_ask(preview):
    hook = preview[preview.index("window._fusedClaudeAsk = (text: unknown) => {"):]
    hook = hook[:hook.index("\n    };")]
    assert "setClaudeAskInstance((n) => n + 1);" in hook


def test_preview_abandons_a_still_pending_ask_on_file_navigation(preview):
    """review #804 round 2 finding 3's root cause: an ask that arrives while
    claude's own gate is still pending (or denies claude outright) never gets
    pulled by anything — nothing ever mounts to pull it — and `fsPath` carries
    no key of its own into the claude iframe (unlike the folder pane's
    `paneKey`), so without an explicit clear the ref would sit there until an
    UNRELATED later file's claude sidebar opened and pulled someone else's
    error."""
    assert (
        "useEffect(() => {\n    claudeSeedRef.current = null;\n  }, [fsPath]);"
    ) in preview


def test_listing_folds_the_claude_instance_into_the_pane_key(listing):
    """The folder pane's key already includes `fsPath` (`paneKey`), so a
    folder change already forces a remount. The gap `Preview.tsx` closes with
    `claudeFrameKey` exists here too, for the SAME folder: a second ask while
    already open on claude, same folder, changes neither `paneSide` nor
    `fsPath`."""
    assert "const [claudeAskInstance, setClaudeAskInstance] = useState(0);" in listing
    hook = listing[listing.index("window._fusedClaudeAsk = (text: unknown) => {"):]
    hook = hook[:hook.index("\n    };")]
    assert "setClaudeAskInstance((n) => n + 1);" in hook
    key_site = listing[listing.index("<ListingPreviewPane"):]
    key_site = key_site[:key_site.index("/>")]
    assert 'paneSide === "claude"' in key_site
    assert "claudeAskInstance" in key_site


def test_listing_abandons_a_still_pending_ask_on_folder_navigation(listing):
    assert (
        "useEffect(() => {\n    claudeSeedRef.current = null;\n  }, [fsPath]);"
    ) in listing


def test_listing_pane_no_longer_takes_a_seed_prop(listing_pane):
    """The claude template pulls its own seed now; `ListingPreviewPane` has no
    reason left to accept one as a prop, and if it did, that would mean the
    push mechanism crept back in."""
    assert "claudeSeed" not in listing_pane
