# Contributing to fused-render

For installing and running the released app, see the [README](README.md). This
page covers building from source and the local development loop. The full
design lives in `SPEC.md` / `ARCHITECTURE.md` / `DECISIONS.md`.

## Building from source

A source checkout builds the React shell once before the server starts:

```
cd frontend && npm install && npm run build   # Node 22
```

Or run `scripts/dev.sh` for a watch + server dev loop. Wheels and the DMG build
the shell automatically at package time, so installed users never need Node.

## Building the macOS app

Build the macOS app with:

```
bash scripts/build_dmg.sh   # py2app → dist/FusedRender-<version>.dmg
```

Signing and notarization are credential-driven — see
[docs/signing.md](docs/signing.md).
