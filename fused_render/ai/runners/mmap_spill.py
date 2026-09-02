"""File-backed mmap residency for CPU-parked torch weights.

**The problem this exists to solve.** `enable_model_cpu_offload()` parks a
pipeline's offloaded components in ordinary Python-allocated CPU tensors —
ANONYMOUS pages, in kernel terms. The kernel can only reclaim anonymous
memory by swapping it, which on a machine with little or no swap it simply
does not do, so a model sitting idle between renders holds its full
CPU-resident footprint in RAM for as long as the worker process lives. A
FILE-BACKED mapping is different: the kernel treats those pages as page
cache and drops the clean ones under memory pressure, the same way it
already treats any other memory-mapped file — no code anywhere has to ask
for that, it falls out of how the page reclaimer already works.

**Why `enable_group_offload`'s own `offload_to_disk_path` and accelerate's
`disk_offload`/`dispatch_model` cannot be used for this instead** — both
move weights by walking a module's `named_parameters()`/`named_buffers()`
only. A quantized model's dequantization state commonly does NOT live in
either bucket: bitsandbytes' `Params4bit.quant_state` holds `absmax`, a
quant map and a nested `state2` as plain Python attributes, invisible to
that walk, so a mechanism keyed off it alone cannot spill or restore them.
Reaching those tensors needs the STRUCTURAL walk this module does
(`iter_untracked_tensors`) — every plain attribute of a parameter or buffer,
recursed one level, looking for a `torch.Tensor` that walk would otherwise
miss. That is the whole reason this module exists instead of reusing
diffusers' or accelerate's own disk paths — see `fused_render/ai/runners/
torch_image.py`'s module docstring and `DECISIONS.md` for the fuller
history of what was tried first and why it does not generalize.

**Generic on purpose, and generic in fact — not just in name.** Nothing
here imports diffusers, bitsandbytes, sdnq, gguf or any pipeline-shaped
concept. The input to every function below is `{name: torch.nn.Module}` —
`pipe.components` for a diffusers pipeline today, but equally the state
dict of any other torch-based runner this app ever adds. `torch_image.py`
is the only caller wired up in this pass (see its own docstring for the
`load()`/`release()` integration); a second torch-based engine can import
this module directly.

**Spilling is destructive to no history.** `spill()` re-walks the given
components' current tensors from scratch on every call — there is no
incremental state carried between calls, because the only thing that
changes which tensors are CPU-resident between two calls is a render moving
a component to the accelerator and back (for `enable_model_cpu_offload`),
never this module itself.
"""

import atexit
import os
import time
import uuid

# `torch` and `safetensors` are imported inside each function that needs
# them, never here at module scope — the same lazy-import convention
# `torch_image.py` uses throughout (see its own `import torch` calls): the
# CPU-only build this app ships by default is not on every machine that
# imports this module (a test venv, in particular), and a function-local
# import lets a caller substitute a fake `torch` via `sys.modules` before
# ever calling in, exactly like `torch_image.py`'s own tests already do.


def iter_untracked_tensors(obj, registered_ids, _depth=0):
    """Yield `(owner, attr_name, tensor)` for every plain attribute of `obj`
    — or, one level down, of a plain-object attribute of `obj` — that is a
    `torch.Tensor` not present in `registered_ids`.

    `registered_ids` is the SAME set at every level of the recursion: it
    always names the owning component's actual `named_parameters()`/
    `named_buffers()`, never the attributes of whatever `obj` happens to be
    at this level — so a tensor found two levels down (bitsandbytes'
    `weight.quant_state.absmax`) is judged against the same registration a
    caller like `enumerate_spillable` already computed once, not against
    `quant_state`'s own attributes as if reaching `absmax` by recursion
    somehow registered it.

    `owner` is whichever object DIRECTLY carries the yielded attribute —
    `obj` itself at depth 0, or the nested plain object one level down — and
    `attr_name` is always a single dict key on it, never a dotted path, so a
    caller can rebind with `owner.__dict__[attr_name] = new_tensor` (or
    `getattr`/`setattr`) without re-parsing anything.

    `_depth` bounds the walk to `obj` -> `obj`'s own attribute -> that
    attribute's own attribute, and no further — a fixed, small cost
    regardless of what an object's attributes reference. This does not reach
    bitsandbytes' double-quantization `quant_state.state2`'s OWN `absmax`/
    `code` (three levels down); nothing in this codebase's supported recipes
    needs that third level today, and a walk with no depth bound at all
    risks following a reference cycle a quantization library's internals
    happen to hold.
    """
    import torch

    if not hasattr(obj, "__dict__"):
        return
    for attr_name, value in vars(obj).items():
        if isinstance(value, torch.Tensor):
            if id(value) not in registered_ids:
                yield obj, attr_name, value
        elif _depth < 1 and hasattr(value, "__dict__"):
            yield from iter_untracked_tensors(
                value, registered_ids, _depth=_depth + 1)


def has_untracked_tensor(obj, registered_ids):
    """True when `iter_untracked_tensors` finds at least one match — a cheap
    boolean predicate for a caller that only needs presence, not the tensors
    themselves. Short-circuits on the first hit via the generator above,
    rather than building the full list just to check it is non-empty.
    """
    return next(iter_untracked_tensors(obj, registered_ids), None) is not None


def _is_plain_tensor(tensor):
    """True when `tensor` is exactly `torch.Tensor` — never a subclass,
    flatten-capable or not. The other half of the three-way split every
    call site below needs: a plain tensor is appended directly (unchanged
    from before this fix), a `__tensor_flatten__` subclass is recursed
    through, and anything else — a subclass this module has no protocol for
    reaching into at all — is neither: not spillable, skipped silently.
    """
    import torch

    return type(tensor) is torch.Tensor


def _is_flatten_subclass(tensor):
    """True when `tensor` is a torch tensor SUBCLASS (not a plain `torch.
    Tensor`) that implements `__tensor_flatten__` — the protocol every
    subclass participating in `torch.compile`/serialization implements to
    name its own inner tensors (`torchao`'s `AffineQuantizedTensor` ->
    `tensor_impl` -> `int_data`/`scale`/`zero_point`, each layer its own
    subclass or a plain leaf). `type(tensor) is not torch.Tensor` rather
    than `isinstance` — a subclass instance passes `isinstance(t, torch.
    Tensor)` too (that is what makes it a *tensor* subclass), so `type`
    identity is the only check that tells "this object IS the plain class"
    from "this object is something wrapping it."
    """
    return not _is_plain_tensor(tensor) and hasattr(tensor, "__tensor_flatten__")


def iter_subclass_leaves(tensor, key_prefix):
    """Yield `(key, leaf_tensor, setter)` for every plain leaf `torch.Tensor`
    reachable from `tensor` — which may itself already be a plain tensor, or
    a (possibly nested) tensor SUBCLASS reached through `__tensor_flatten__`.

    **Generic on purpose — this is the whole fix for quantizers whose
    weights are not plain tensors at all.** `AffineQuantizedTensor` (torchao)
    has no single contiguous storage of its own; calling `.untyped_storage
    ().data_ptr()` on it directly raises `RuntimeError: Attempted to access
    the data pointer on an invalid python storage`, because that call is
    only meaningful on a LEAF. `__tensor_flatten__()` returns the attribute
    names an object's own inner tensors are held under (`(names, ctx)` per
    the documented protocol, though this only ever reads `names` — `ctx` is
    metadata for reconstructing the subclass, irrelevant to spilling its
    bytes to disk and back) — recursing through those names, at each level
    checking again whether the value found is itself a plain tensor or a
    further subclass, reaches every real leaf regardless of how many layers
    of wrapping a given quantizer's layout stacks (`AffineQuantizedTensor`
    -> `tensor_impl` -> `int_data`, two layers for the plain layout torchao
    logged in the observed failure; a future layout nesting one layer
    deeper needs no change here, since the recursion has no depth bound the
    way `iter_untracked_tensors`'s plain-attribute walk deliberately does).

    A subclass with no `__tensor_flatten__` anywhere in its chain is not
    spillable by this mechanism — it yields nothing for that branch, never
    raises. Nothing here is specific to torchao, or to any other quantizer:
    the only thing read off `tensor` is the standard subclass protocol.

    **CPU-resident only, at every level**, same rule `enumerate_spillable`
    already applies to plain tensors: a subclass instance already moved to
    an accelerator (or wrapping inner tensors that have been) is skipped
    without descending further, so this recursion stays correct after any
    placement `torch_image._place()` might choose, exactly as the plain
    tensor path already is.

    Each yielded leaf's `setter` is a `_TensorSetter`, never an `_AttrSetter`
    — see that class's own docstring for why owner-level `setattr` is not
    the mechanism used to rebind a nested subclass's inner tensor.
    """
    device = getattr(tensor, "device", None)
    if device is not None and getattr(device, "type", None) != "cpu":
        return
    if not _is_flatten_subclass(tensor):
        yield key_prefix, tensor, _TensorSetter(tensor)
        return
    names, _ctx = tensor.__tensor_flatten__()
    for attr_name in names:
        inner = getattr(tensor, attr_name, None)
        if inner is None:
            continue
        yield from iter_subclass_leaves(inner, f"{key_prefix}.{attr_name}")


class _TensorSetter:
    """Rebinds one spilled LEAF tensor's storage IN PLACE, via `Tensor.set_
    (...)`, rather than reassigning any attribute on whatever object holds
    it.

    This exists because `_AttrSetter`'s move — reassign `.data` on the
    tensor reached by walking a DOTTED PATH off a stable owner — assumes
    every step of that path is an ordinary attribute a caller can safely
    `getattr` through to a settable `.data` slot. A tensor subclass breaks
    that assumption at the attribute-holding end: `AffineQuantizedTensor`'s
    `tensor_impl` (itself a further subclass) is not guaranteed to permit
    `setattr` for whatever future layout torchao ships, and this module
    cannot special-case every layout's storage rules — it does not import
    torchao at all, by design (see this module's own docstring).

    `Tensor.set_(source)` sidesteps the question entirely: it mutates the
    LEAF tensor's own storage/shape/stride in place, so the exact same
    Python object the subclass already holds a reference to (`tensor_impl.
    int_data`, found by `iter_subclass_leaves`'s recursion) keeps that
    identity — nothing upstream of the leaf (`tensor_impl`, the outer
    `AffineQuantizedTensor`, the `nn.Parameter` wrapping it) is ever
    written to. A NESTED subclass (`AffineQuantizedTensor` -> `tensor_impl`
    -> `int_data`) rebinds correctly for exactly this reason: only the
    bottom-most object changes, and every object above it goes on holding
    the reference it always held.
    """

    __slots__ = ("_target",)

    def __init__(self, target):
        self._target = target

    def apply(self, tensor):
        self._target.set_(tensor)


class _AttrSetter:
    """Rebinds the `.data` of the tensor reached by walking `owner`'s
    (possibly dotted) `path` — a `named_parameters()`/`named_buffers()` name
    on a `torch.nn.Module` (`"transformer_blocks.0.attn.to_q.weight"`), or a
    single, undotted attribute name on a plain object an untracked tensor
    hangs off of (`"absmax"` on a `quant_state`).

    `.data` reassignment, never `setattr`/`register_buffer` — the same move
    diffusers' own `_transfer_tensor_to_device` performs on a `Parameter`
    (`diffusers/hooks/group_offloading.py`), which is why it is a supported
    move on `Params4bit` and any ordinary buffer alike: both are tensors
    with a `.data` slot, and reassigning it swaps the storage without
    disturbing the Python object identity anything else in the pipeline
    (a hook, a reference held elsewhere) may be holding onto.
    """

    __slots__ = ("_owner", "_path")

    def __init__(self, owner, path):
        self._owner = owner
        self._path = path

    def apply(self, tensor):
        parts = self._path.split(".")
        obj = self._owner
        for part in parts[:-1]:
            obj = getattr(obj, part)
        old = getattr(obj, parts[-1])
        old.data = tensor.view(old.shape).to(old.dtype)


class TensorSlot:
    """One CPU-resident tensor `enumerate_spillable` found, the safetensors
    key it will be written under, and the setter that rebinds a replacement
    tensor back onto the exact attribute this one was read from.
    """

    __slots__ = ("key", "tensor", "setter")

    def __init__(self, key, tensor, setter):
        self.key = key
        self.tensor = tensor
        self.setter = setter


def enumerate_spillable(components):
    """Every CPU-RESIDENT tensor across `components` (`{name: torch.nn.
    Module}`) that can be spilled to disk and rebound: each module's own
    parameters, buffers, and untracked tensors (one level into their own
    plain attributes — see `iter_untracked_tensors`). Returns a list of
    `TensorSlot`.

    **Only tensors currently on `cpu` are candidates.** This is what makes
    the same call correct after EVERY placement `torch_image._place()` can
    choose, with no placement-specific branching needed at the call site:
    all-gpu leaves nothing here (every tensor already moved to `cuda`), MPS
    leaves nothing here (moved to `mps`), CPU-only placement returns
    everything (there is nowhere else for it to be), and `enable_model_cpu_
    offload()`'s parked components return everything they currently hold —
    which is also everything a render's `.to()` round trip has NOT already
    reclaimed back onto the accelerator for the render in progress.

    A component with no `named_parameters` (`None`, a non-`nn.Module` the
    pipeline happens to carry — a tokenizer, a scheduler) is skipped, same
    as `torch_image._group_offload_exclusions` used to skip it for the same
    reason before this replaced it.

    **A parameter or buffer that is itself a tensor SUBCLASS** (a torchao
    `AffineQuantizedTensor`, in place of an ordinary `torch.Tensor`) is
    routed through `iter_subclass_leaves` instead of appended directly —
    the plain-leaf tensors that recursion finds become the `TensorSlot`s,
    named by the attribute path that reaches them (`"<comp>.param.<name>.
    tensor_impl.int_data"`), each rebound by its own `_TensorSetter`. An
    ordinary parameter/buffer (`_is_flatten_subclass` false) is untouched by
    any of this — same single `TensorSlot`, same `_AttrSetter`, as before
    this fix.

    The untracked-attribute walk below (`iter_untracked_tensors`, bnb's
    `quant_state.absmax` and neighbours) is skipped for a parameter/buffer
    that is itself a `__tensor_flatten__` subclass — `iter_subclass_leaves`
    already reaches everything that recursion could, and running both would
    double-count the same underlying storage under two different keys. A
    subclass with NO `__tensor_flatten__` (bnb's `Params4bit`, which predates
    and does not implement this protocol) still goes through the untracked
    walk exactly as before — this rung of the fix is additive, not a
    replacement for the mechanism the untracked walk exists for.
    """
    slots = []
    for comp_name, component in components.items():
        if component is None or not hasattr(component, "named_parameters"):
            continue
        named_params = list(component.named_parameters())
        named_bufs = list(component.named_buffers())
        registered_ids = {id(t) for _, t in named_params}
        registered_ids |= {id(t) for _, t in named_bufs}

        for pname, t in named_params:
            if t.device.type != "cpu":
                continue
            if _is_plain_tensor(t):
                slots.append(TensorSlot(
                    f"{comp_name}.param.{pname}", t,
                    _AttrSetter(component, pname)))
            elif _is_flatten_subclass(t):
                for key, tensor, setter in iter_subclass_leaves(
                        t, f"{comp_name}.param.{pname}"):
                    slots.append(TensorSlot(key, tensor, setter))
            # else: a subclass with no `__tensor_flatten__` — not spillable
            # by any mechanism this module has, skipped silently.
        for bname, t in named_bufs:
            if t.device.type != "cpu":
                continue
            if _is_plain_tensor(t):
                slots.append(TensorSlot(
                    f"{comp_name}.buffer.{bname}", t,
                    _AttrSetter(component, bname)))
            elif _is_flatten_subclass(t):
                for key, tensor, setter in iter_subclass_leaves(
                        t, f"{comp_name}.buffer.{bname}"):
                    slots.append(TensorSlot(key, tensor, setter))

        for pname, t in named_params + named_bufs:
            if t.device.type != "cpu" or _is_flatten_subclass(t):
                continue
            for owner, attr_name, tensor in iter_untracked_tensors(
                    t, registered_ids):
                if tensor.device.type != "cpu":
                    continue
                if _is_plain_tensor(tensor):
                    slots.append(TensorSlot(
                        f"{comp_name}.untracked.{pname}.{attr_name}", tensor,
                        _AttrSetter(owner, attr_name)))
                elif _is_flatten_subclass(tensor):
                    for key, leaf, setter in iter_subclass_leaves(
                            tensor,
                            f"{comp_name}.untracked.{pname}.{attr_name}"):
                        slots.append(TensorSlot(key, leaf, setter))
    return slots


#: `spill()`'s empty-input answer — nothing found, nothing written. A plain
#: dict literal rather than a dataclass: every caller either logs this or
#: ignores it, nothing indexes into it structurally enough to want a type.
#: `skipped`/`skipped_reason` extend this the same way every other key here
#: does — present and zero/`None` even when nothing ran, so a caller never
#: has to check for the key's existence before reading it.
_EMPTY_STATS = {
    "tensors": 0, "bytes": 0, "contiguous_copies": 0,
    "dedup_count": 0, "write_seconds": 0.0,
    "skipped": 0, "skipped_reason": None,
}


def spill(components, path):
    """Write every CPU-resident tensor in `components` to one safetensors
    file at `path`, then rebind each tensor's `.data` to the mmap'd load —
    the values are byte-identical to what was there before, but the pages
    backing them are now file-backed and the kernel can drop them under
    memory pressure instead of only being able to swap them out.

    Returns a stats dict (see `_EMPTY_STATS`'s keys) — `tensors`/`bytes`
    spilled, `contiguous_copies` (tensors that needed a `.contiguous()` copy
    before `save_file` would accept them — it rejects a non-contiguous
    view), `dedup_count` (tensors that shared another tensor's storage and
    were written once, then rebound from the SAME loaded tensor rather than
    a duplicate — `save_file` also rejects two keys pointing at identical
    storage), and `write_seconds`. All zero, via `_EMPTY_STATS`, when
    `enumerate_spillable` finds nothing to do — every component already on
    an accelerator, the ordinary all-gpu/MPS case.

    **The write goes to a temp path first, then `os.replace`s it into
    place.** `path` is reused across every call for a given worker (see
    `torch_image._spill_idle_weights`) — a render's `.to()` round trip can
    put SOME of it back into anonymous CPU tensors between one spill and the
    next, and a crash or a kill signal mid-`save_file` must not leave a
    half-written file at the path the NEXT call (or a future load of this
    same identity) would otherwise mmap and read garbage from.

    Deliberately no incremental diffing against a PRIOR spill at the same
    path: `enumerate_spillable` is cheap (walking already-resident Python
    attributes, not weight-sized work), and every tensor here needs a fresh
    write regardless — `save_file` cannot append or patch a subset of keys
    in an existing file, it always writes the whole set it is given.

    **Per-slot fault tolerant: one bad tensor skips itself, not the whole
    pass.** The observed failure this guards against is a single slot whose
    tensor cannot actually be touched the way a plain tensor can — the
    `RuntimeError` a torchao tensor subclass's `.untyped_storage().data_ptr
    ()` raises when `enumerate_spillable`'s subclass recursion still lands
    on something that is not a genuine leaf (a future layout this module's
    generic `__tensor_flatten__` walk cannot see all the way through) is the
    one that was actually observed, but this loop treats ANY exception from
    the contiguity check or the storage-key computation the same way: count
    it, remember one representative message, move on to the next slot. A
    slot that fails here contributes nothing to `to_save` and is excluded
    from the rebind pass below — its owner is left pointing at whatever
    tensor it already held, exactly as if `enumerate_spillable` had never
    found it, rather than the ENTIRE pass losing every OTHER tensor's
    reclaimable bytes over one that could not be reached. Only the first
    failure's message is kept in `skipped_reason` — logging one line per
    bad tensor is exactly what this is trying not to do; the caller's own
    breadcrumb (`torch_image._spill_idle_weights`) is the summary line.
    """
    from safetensors.torch import load_file, save_file

    slots = enumerate_spillable(components)
    if not slots:
        return dict(_EMPTY_STATS)

    to_save = {}
    seen_storage = {}
    dedup_map = {}
    contiguous_copies = 0
    total_bytes = 0
    skipped = 0
    skipped_reason = None
    prepared = []
    for slot in slots:
        tensor = slot.tensor
        try:
            if not tensor.is_contiguous():
                tensor = tensor.contiguous()
                contiguous_copies += 1
            storage_key = (
                tensor.untyped_storage().data_ptr(), tensor.storage_offset(),
                tuple(tensor.shape), str(tensor.dtype),
            )
        except Exception as error:
            skipped += 1
            if skipped_reason is None:
                skipped_reason = (
                    f"{slot.key}: {error.__class__.__name__}: {error}")
            continue
        if storage_key in seen_storage:
            dedup_map[slot.key] = seen_storage[storage_key]
            prepared.append(slot)
            continue
        seen_storage[storage_key] = slot.key
        to_save[slot.key] = tensor
        total_bytes += tensor.numel() * tensor.element_size()
        prepared.append(slot)

    if not to_save:
        stats = dict(_EMPTY_STATS)
        stats["skipped"] = skipped
        stats["skipped_reason"] = skipped_reason
        return stats

    tmp_path = f"{path}.{os.getpid()}.tmp"
    started = time.time()
    save_file(to_save, tmp_path)
    os.replace(tmp_path, path)
    write_seconds = time.time() - started

    loaded = load_file(path, device="cpu")
    for index, slot in enumerate(prepared):
        source_key = dedup_map.get(slot.key, slot.key)
        try:
            slot.setter.apply(loaded[source_key])
        except Exception as error:
            # Everything above this loop succeeding is what makes the write
            # itself safe to fail (the pipeline is untouched, still pointing
            # at its original tensors) — but a raise HERE, mid-rebind
            # (`loaded[source_key]` missing a key, or `_AttrSetter.apply`'s
            # `tensor.view(old.shape)` hitting a shape mismatch), leaves
            # `index` slots already rebound and the rest untouched. Values
            # are correct either way — rebinding only swaps `.data` for a
            # byte-identical mmap'd copy — so this names the slot and the
            # progress rather than rolling anything back. This is a
            # DIFFERENT failure mode from the per-slot skip above — a slot
            # that made it this far already proved itself spillable (its
            # bytes are safely on disk), so a rebind failure here is a real
            # bug, not an unreachable tensor, and stays fatal to the whole
            # call rather than being swallowed the same way.
            raise RuntimeError(
                f"mmap_spill rebind failed on slot {index + 1}/{len(prepared)} "
                f"(key={slot.key!r}); {index} of {len(prepared)} slots were "
                f"already rebound before this one"
            ) from error

    return {
        "tensors": len(prepared), "bytes": total_bytes,
        "contiguous_copies": contiguous_copies,
        "dedup_count": len(dedup_map), "write_seconds": write_seconds,
        "skipped": skipped, "skipped_reason": skipped_reason,
    }


# --------------------------------------------------------- per-worker identity


def spill_base_dir():
    """The directory per-worker spill files live under — `~/.fused-render/
    cache/mmap-spill` (or `$FUSED_RENDER_HOME`'s equivalent), the app's own
    real-disk home, reusing the same convention `torch_image.py`'s
    now-deleted group-offload cache used and the same reasoning: never
    `$TMPDIR`/`/tmp`, never `$XDG_RUNTIME_DIR` — both are commonly
    tmpfs-backed on Linux (`fused_render/supervisor/paths.py` documents the
    second), and spilling onto tmpfs would just move these same anonymous,
    unreclaimable pages from the process's RSS to a filesystem backed by
    that same RAM — clean-looking in `/proc/<pid>/smaps_rollup` but not
    actually freeing anything on a memory-constrained host.
    """
    home = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")
    return os.path.join(home, "cache", "mmap-spill")


def _sweep_stale_spill_files(base):
    """Remove every file in `base` left behind by a PROCESS THAT NO LONGER
    EXISTS, best-effort, before this worker claims its own spill path.

    Ported from the group-offload cache this module replaces
    (`torch_image._sweep_stale_group_offload_dirs`, deleted) because the
    shape of the problem is identical, only the unit swept is a FILE rather
    than a whole directory: a worker's two shutdown paths (`/quit`'s thread,
    which ends in `os._exit(0)`; and the supervisor's SIGTERM-then-SIGKILL)
    both skip `atexit` handlers entirely, so a spill file survives every
    ordinary quit unless something else sweeps it.

    Only a file whose pid prefix names a process that is no longer alive is
    removed, and a filename this function cannot parse as `<pid>-<random>.
    safetensors` is left alone rather than guessed at.

    **Liveness is probed with `psutil.pid_exists(pid)`, never `os.kill`.**
    `os.kill(pid, 0)` is the ordinary POSIX liveness idiom — send signal 0,
    no-op if the process exists, `ProcessLookupError` if it does not — but
    on Windows CPython's `os.kill` has no such no-op signal at all: any
    value other than `CTRL_C_EVENT`/`CTRL_BREAK_EVENT` is routed through
    `OpenProcess` + `TerminateProcess(handle, sig)`, so `os.kill(pid, 0)`
    unconditionally KILLS whatever process `pid` names. A sweep meant to
    clean up after dead workers would instead terminate live ones on every
    Windows worker whose pid happened to prefix a file in this directory.
    `psutil.pid_exists` is a real liveness probe on every platform it
    supports, with no such side effect, so it replaces `os.kill` entirely
    rather than only being consulted on Windows — one code path, correct
    everywhere, instead of a platform branch that could silently regress
    back to the lethal one.

    `psutil` is imported function-locally here, guarded, matching the
    convention `worker_base.py`'s `os_footprint_bytes`/memory probes already
    use for the same package (see that module's docstring: "psutil comes
    with every runner's environment; if it is somehow absent..."). If the
    import fails, or `pid_exists` itself raises, this sweeps NOTHING rather
    than falling back to `os.kill` — a POSIX-only fallback would be safe on
    POSIX, but the whole point of this function is to run unmodified on
    every platform a runner ships on, and a conditional fallback is exactly
    the kind of platform branch that is easy to get right in the common
    case and wrong on the one platform nobody is testing against. Sweeping
    nothing just means a stale file survives one more worker's cleanup
    pass — the disk usage it costs is bounded and swept next time; killing
    a live render is not recoverable at all. "When in doubt, keep it."
    """
    try:
        entries = os.listdir(base)
    except OSError:
        return
    try:
        import psutil
    except ImportError:
        return
    for entry in entries:
        pid_str = entry.split("-", 1)[0]
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        try:
            alive = psutil.pid_exists(pid)
        except Exception:
            # Indeterminate — this process cannot tell either way, so leave
            # the file alone rather than guess.
            continue
        if alive:
            continue
        try:
            os.remove(os.path.join(base, entry))
        except OSError:
            pass


#: Guards `spill_path()`'s `atexit.register` against registering the same
#: path's cleanup twice — mirrors `torch_image._GROUP_OFFLOAD_DISK_PATHS_
#: REGISTERED`'s reasoning, ported here for the same call-more-than-once-in-
#: tests case.
_SPILL_PATHS_REGISTERED = set()


def spill_path():
    """A fresh, unique path for this process's spill file under
    `spill_base_dir()`: `<pid>-<random>.safetensors`.

    **Deliberately per-process identity, not a stable path keyed by model
    id shared across loads or across concurrently-running workers.** Two
    reasons, both load-bearing:

    1. **Collision safety.** A pid-only identity is reused whenever Linux
       recycles a pid — the same hazard D661 fixed for the group-offload
       cache this replaces: a worker whose pid collided with a dead one's
       populated file would inherit it and rebind from THE WRONG MODEL'S
       WEIGHTS with no error. The random suffix closes that; stale files
       are additionally swept by `_sweep_stale_spill_files` before a new
       identity is claimed.
    2. **A stable cross-load cache would need reload-without-requantizing to
       be worth the risk, and this pass does not build that.** Reusing a
       PRIOR load's quantized weights (skipping the compute pass a fresh
       `load()` pays for `bnb`/`sdnq`/GGUF quantization) needs the pipeline
       reconstructed on a meta device and its tensors loaded straight from a
       cache file instead of materialized-then-spilled — real engineering
       this pass does not attempt (see `DECISIONS.md`). Without that, a
       shared path buys nothing (this process still pays the full
       quantization cost to have anything to spill) and only adds risk: two
       workers loading the same model concurrently would read and write the
       identical path, and a spill mid-write racing another process's mmap
       read is exactly the corruption class D661 already had to fix once.

    `atexit.register` runs as a best-effort EXTRA for the ordinary Python-
    level exit path — cheap insurance, not the mechanism cleanup actually
    depends on; see `_sweep_stale_spill_files` for that.
    """
    base = spill_base_dir()
    _sweep_stale_spill_files(base)
    os.makedirs(base, exist_ok=True)
    identity = f"{os.getpid()}-{uuid.uuid4().hex[:12]}.safetensors"
    path = os.path.join(base, identity)
    if os.path.exists(path):
        os.remove(path)
    if path not in _SPILL_PATHS_REGISTERED:
        atexit.register(_remove_if_exists, path)
        _SPILL_PATHS_REGISTERED.add(path)
    return path


def _remove_if_exists(path):
    """`atexit`-registered cleanup for one `spill_path()` identity. A plain
    module-level function, not a lambda closing over `path` inline at the
    call site — `atexit.register` keeps a reference forever, and a bound
    lambda per call site is indistinguishable from this in behaviour but
    harder to spot in a stack trace if `os.remove` ever raises something
    other than the `OSError` this already guards against.
    """
    try:
        os.remove(path)
    except OSError:
        pass
