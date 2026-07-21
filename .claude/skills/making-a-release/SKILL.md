---
name: making-a-release
description: Use when cutting a new fused-render release, bumping the version, or creating a release tag — bumps __version__, commits, tags vX.Y.Z, and pushes to trigger the DMG build/release workflow.
---

# Making a Release

## Overview

A release is: bump `__version__`, commit it, create a matching `vX.Y.Z` tag, and push the tag. Pushing the tag is what triggers `.github/workflows/release.yml` (build → sign → notarize → publish DMG + GitHub Release).

**Single source of truth:** the version lives ONLY in `fused_render/__init__.py`. `pyproject.toml` derives it dynamically (`[tool.hatch.version]`) — never edit a version into `pyproject.toml`.

**The invariant that matters:** the git tag name must equal `__version__`. Tag `v0.3.5` ⟺ `__version__ = "0.3.5"`. A mismatch ships the wrong version silently.

## Steps

1. **Start clean and on the latest `main`.** Always pull first — tagging a stale
   commit ships an old build. Use `--ff-only` so a diverged local branch errors
   out loudly instead of creating a merge commit:
   ```bash
   git switch main
   git pull --ff-only origin main   # REQUIRED: get the latest before releasing
   git status --porcelain
   ```
   If `git pull --ff-only` fails, your local `main` has diverged — reconcile it
   before continuing. If `git status --porcelain` prints anything, stop and
   resolve it first.

2. **Pick the new version.** Read the current value and the latest tag; choose the next semver:
   ```bash
   grep __version__ fused_render/__init__.py
   git tag --sort=-creatordate | head -1
   ```

3. **Bump `__version__`** in `fused_render/__init__.py` (Edit tool). This is the only file to change.

4. **Commit** (message matches the repo's history convention exactly):
   ```bash
   git commit -am "Bump version to X.Y.Z"
   ```

5. **Tag** — annotated, name is `v` + the exact version:
   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z"
   ```

6. **Push commit and tag.** The tag push is the release trigger:
   ```bash
   git push origin main
   git push origin vX.Y.Z
   ```

7. **Verify** the release workflow started:
   ```bash
   gh run list --workflow=release.yml --limit 3
   ```

## Quick Reference

| Thing | Value |
|-------|-------|
| Version source | `fused_render/__init__.py` → `__version__` |
| Tag format | `vX.Y.Z` (must match `__version__`) |
| Release trigger | pushing a `v*` tag → `.github/workflows/release.yml` |
| Commit message | `Bump version to X.Y.Z` |
| Do NOT edit | `pyproject.toml` version (dynamic via hatchling) |

## Common Mistakes

- **Editing the version in `pyproject.toml`.** It's dynamic; the value there is ignored/derived. Edit `__init__.py` only.
- **Tag name ≠ `__version__`.** e.g. tagging `v0.3.5` while `__version__` is still `0.3.4`. Bump and commit *before* tagging.
- **Pushing the commit but forgetting the tag.** No tag push = no release. Both pushes are required.
- **Reusing an existing tag.** Tags are immutable releases; pick an unused version. Check `git tag` first.
- **Releasing off a dirty tree or a non-`main` branch.** Start from clean `main`.
- **Skipping `git pull`.** Tagging a stale local `main` builds and ships an old
  commit. Always pull (step 1) so the tag points at the true latest.

## Team Workflow Note

Historically version bumps often went through a PR (`Bump version to X.Y.Z (#NNN)`) before tagging `main`. If your project requires PRs into `main`, open the bump PR first, let it merge, then tag the merged commit on `main` (steps 5–6). The tag must point at a commit already on `main`.
