---
name: making-a-release
description: Use when cutting a new fused-render release, bumping the version, or creating a release tag — bumps __version__, lands it on protected main via a bump PR, then tags vX.Y.Z and pushes the tag to trigger the DMG build/release workflow.
---

# Making a Release

## Overview

A release is: bump `__version__`, land that bump on `main` **through a PR**, then tag the merged commit `vX.Y.Z` and push the tag. Pushing the tag is what triggers `.github/workflows/release.yml` (build → sign → notarize → publish DMG + GitHub Release).

**`main` is a protected branch.** Direct pushes are rejected (`GH006: Changes must be made through a pull request`), so the bump PR is not optional — it is the only path. Do not try `git push origin main` first.

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

3. **Branch, then bump.** Work on a branch from the start — `main` is protected:
   ```bash
   git switch -c bump-X.Y.Z
   ```
   Bump `__version__` in `fused_render/__init__.py` (Edit tool). This is the only
   file to change.

4. **Commit** (message matches the repo's history convention exactly):
   ```bash
   git commit -am "Bump version to X.Y.Z"
   ```

5. **Open the bump PR** and let it merge. Push the branch and open the PR with the
   `creating-pull-requests` skill (title `Bump version to X.Y.Z`), then enable
   auto-merge so it lands as soon as checks go green:
   ```bash
   git push -u origin HEAD
   gh pr create --title "Bump version to X.Y.Z" --body-file <body.md>
   gh pr merge <N> --squash --auto
   ```
   Poll until it actually reports `MERGED` — do NOT tag before then:
   ```bash
   gh pr view <N> --json state,mergeStateStatus,mergeCommit
   ```

6. **Return to `main` and pull the merged bump.** The squash merge creates a NEW
   commit, so the tag must point at that commit, not your local branch commit:
   ```bash
   git switch main
   git pull --ff-only origin main
   grep __version__ fused_render/__init__.py   # confirm it reads X.Y.Z
   ```

7. **Tag** — annotated, name is `v` + the exact version:
   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z"
   ```

8. **Push the tag.** This is the release trigger:
   ```bash
   git push origin vX.Y.Z
   ```

9. **Verify** the release workflow started, then clean up the branch:
   ```bash
   gh run list --workflow=release.yml --limit 3
   git branch -d bump-X.Y.Z && git push origin --delete bump-X.Y.Z
   ```

## Quick Reference

| Thing | Value |
|-------|-------|
| Version source | `fused_render/__init__.py` → `__version__` |
| Tag format | `vX.Y.Z` (must match `__version__`) |
| Release trigger | pushing a `v*` tag → `.github/workflows/release.yml` |
| Commit message | `Bump version to X.Y.Z` |
| How the bump lands | bump PR into protected `main`, squash-merged (never a direct push) |
| What gets tagged | the squash-merge commit on `main`, after `git pull` |
| Do NOT edit | `pyproject.toml` version (dynamic via hatchling) |

## Common Mistakes

- **Editing the version in `pyproject.toml`.** It's dynamic; the value there is ignored/derived. Edit `__init__.py` only.
- **Tag name ≠ `__version__`.** e.g. tagging `v0.3.5` while `__version__` is still `0.3.4`. Bump and commit *before* tagging.
- **Committing the bump straight onto local `main`.** The push will be rejected by
  branch protection and you'll have to move the commit to a branch anyway
  (`git branch bump-X.Y.Z && git reset --hard origin/main`). Branch first (step 3).
- **Tagging your local bump commit instead of the merged one.** The squash merge
  produces a different SHA; tagging the pre-merge commit points the release at a
  commit that is not on `main`. Pull `main` first (step 6), then tag.
- **Tagging before the PR is actually merged.** "Auto-merge enabled" is not merged —
  wait for `state: MERGED`.
- **Forgetting the tag push.** No tag push = no release, even once the bump is on `main`.
- **Reusing an existing tag.** Tags are immutable releases; pick an unused version. Check `git tag` first.
- **Skipping `git pull`.** Tagging a stale local `main` builds and ships an old
  commit. Always pull (steps 1 and 6) so the tag points at the true latest.
