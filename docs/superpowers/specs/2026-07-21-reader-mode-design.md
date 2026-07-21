# Reader Mode — Listen Instead of Read (design)

Date: 2026-07-21
Status: approved (Akshil, 2026-07-21) — engine choice, image deferral, form factor, and design sections all confirmed in brainstorming session.

## Problem

Akshil doesn't want to read long text — paragraphs, docs, PDFs. He wants to
select any text or open any file and *listen* to it. fused-render already opens
everything (universal file opener), so it should also be able to dictate
everything.

## Decisions made

| Question | Decision | Why |
|---|---|---|
| TTS engine | Browser `speechSynthesis`, wrapped in a swappable `speak()` engine interface | Free, offline, instant, word-level `onboundary` events for karaoke highlight. OpenAI TTS drops in later as a premium-voice engine without redesign |
| Images / OCR | Deferred to v1 | OCR (engine-side) or vision LLM both add deps/keys; not needed to start listening |
| Form factor | **Option A: ordinary view template** (`templates/reader/`), the annotate pattern | Zero shell changes, containment invariant respected, swappable/shadowable, `_mode=reader` deep-linkable. Global select→speak hotkey deferred to v1 |

## Architecture

- One self-contained `fused_render/templates/reader/` (template.html + icon.svg),
  registered in `registry.json` as a trailing mode on text-bearing extensions:
  md, txt/text, code, csv, xlsx, pdf, docs, latex, log, json/structure.
- Containment invariant (SPEC §17): every line of reader logic lives inside
  `templates/reader/template.html`. No shell code, no server injection, no
  hooks in other view templates.
- `_mode=reader` in the shell URL opens a file straight into listen mode.

## Text extraction

Two paths, both inside the template:

1. **Default (DOM walk):** hidden same-origin iframe loads the file's normal
   view (`/embed/…` with the mode picked by a `view` template param, like
   annotate). After load, walk visible DOM text nodes and group them into
   ordered blocks: headings, paragraphs, list items; table rows verbalized as
   comma-joined cells ("Alpha, 42, active").
2. **PDF special case:** the pdf view renders canvases (no DOM text), so the
   reader detects pdf and instead fetches the raw file and extracts text with
   the already-vendored `pdfjs` bundle (`getTextContent()` per page, grouped
   into page/paragraph blocks). Stays self-contained — no new deps.

Empty extraction → explicit "nothing to read here" state.

## Reading surface — clean column

The reader renders its **own clean text column** (Safari-Reader style: large
type, generous spacing) built from the extracted blocks. It does NOT highlight
inside the original view.

- Karaoke follow-along: current block highlighted, current word underlined
  (via utterance `onboundary`), auto-scroll keeps the current block centered.
- Click any block → playback jumps there.
- Select any text in the column → floating ▶ pill reads just the selection.

Rationale: highlight/jump/scroll are trivial in DOM the template owns; original
layouts (tables, code) are awkward to karaoke-highlight.

## Playback engine

- `speak()` engine interface wrapping `speechSynthesis`; the interface is the
  seam where OpenAI TTS (v1) plugs in.
- Sentence chunking via `Intl.Segmenter` — one utterance per sentence. This
  also defeats Chrome's known ~15s utterance-death bug.
- Controls bar: play/pause · speed 0.75–2× · skip ± sentence / ± block ·
  voice picker · progress indicator.
- Template params synced to the shell URL: `view` (which mode was extracted),
  `rate`, `voice`, `pos` (current block index). Session restore (SPEC §21)
  then resumes where you left off for free; URLs are shareable
  "listen from here" links.

## Error handling

- No voices available → clear message + hint to install system voices.
- Utterance error → skip that sentence, keep playing.
- Iframe/view load failure → error state with retry.
- Huge files → lazy extraction: cap the initial pass, "keep reading" extends.

## Testing

- Python (existing `test_templates_api` pattern): registry includes `reader`
  on the expected extensions; template file serves.
- Playwright (headless): speechSynthesis is stubbed via an init script (fake
  voices + timed `onboundary`/`onend` events) so the full pipeline — extract →
  chunk → play → highlight advance → pause/resume → select→speak → `pos`
  survives reload — is asserted deterministically in CI-like conditions.
- Live manual verify in real Chrome before handoff (real voices).

## v1 backlog (designed-in, not built)

- Images: OCR (engine-side rapidocr/tesseract) or vision-LLM describe.
- OpenAI TTS premium voice behind the same engine interface.
- Global select→speak hotkey (small shell hook, deliberate containment
  exception).
- Smarter per-view narration (CSV column summaries, code skipping punctuation).
