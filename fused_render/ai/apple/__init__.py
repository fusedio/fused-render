"""The `provider: "apple"` tier: Apple's on-device models, on macOS 26+.

Three pinned ids, one per capability — `afm-text`, `afm-speech`,
`afm-embedding` — and no catalog behind them: the OS owns the weights,
downloads them, and picks the variant (AFM 3 Core vs Core Advanced on
macOS 27) by hardware. A page never chooses a version because the API
exposes none to choose.

`host.py` spawns and talks to the Swift helper (`helper/main.swift`) that
owns FoundationModels and SpeechAnalyzer, both Swift-only frameworks pyobjc
cannot reach. `speech.py` runs one transcription as a job. Everything
server-facing (tier resolution, the frame, the refusals) lives beside the
other tiers in `server/ai.py` and `routers/ai_runtime.py`.
"""
