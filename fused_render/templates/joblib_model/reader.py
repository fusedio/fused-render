"""Reader backing joblib_model/template.html: a restricted, allowlisted load
of a `.joblib`/`.pkl`/`.pickle` file, and — only when every referenced class
turned out to be trusted — real introspection of what's inside (estimator
hyperparameters, feature importances, and tree-ensemble structure).

`pickle.load()`/`joblib.load()` is arbitrary code execution: a crafted
`__reduce__` calls any importable callable with attacker-chosen arguments the
moment it's unpickled, and this template is offered on any `.joblib`/`.pkl`
a user opens — including downloaded files. So the object is never
reconstructed except through a single restricted pass that resolves every
referenced class/callable against an explicit allowlist *before* it is ever
invoked.

The restriction uses the "Restricting Globals" pattern from the `pickle`
module's own docs — subclassing `Unpickler` and overriding `find_class` —
rather than hand-parsing opcodes: CPython's Unpickler already gets the
stack/memo bookkeeping right (GET/BINGET can make a class reference arrive
via a memo slot rather than an adjacent literal, which a naive "last two
strings seen" opcode-stream heuristic would get wrong), and, just as
importantly, joblib's own on-disk format for numpy arrays isn't plain
pickle: `joblib.numpy_pickle.NumpyUnpickler.load_build` reads raw array
bytes directly off the file handle immediately after a genuine
`NumpyArrayWrapper` is built, bypassing normal opcode parsing entirely. A
naive scanner that replaces every class — including that wrapper — with an
inert stand-in desyncs from the byte stream the moment it hits the first
array (confirmed by hand: it throws "invalid load key" a few opcodes later,
reading array bytes as if they were opcodes). So `_RestrictedUnpickler`
subclasses `NumpyUnpickler` itself to inherit its framing, and
`find_class(module, name)` resolves the REAL class via the normal
`super().find_class()` for anything on the allowlist (needed both for
correct framing and because the object has to be genuinely usable
afterwards) and substitutes `_Stub` — an inert class that swallows any
constructor/state/append/etc. call as a no-op — for anything that is not.
`EXT1`/`EXT2`/`EXT4` and `INST`/`OBJ` all route through the same
`find_class` call as `GLOBAL`/`STACK_GLOBAL`, so nothing bypasses this.

The one guarantee this preserves absolutely: a disallowed class or callable
is never instantiated or invoked — `find_class` simply never returns it.
What it does NOT try to prevent is an allowlisted class (real numpy/sklearn/
xgboost/etc.) being constructed with adversarial arguments before a later,
disallowed reference is reached and blocks the overall verdict — that
residual resource-exhaustion risk is the same one a from-scratch pure-opcode
scan would still face for the eventually-safe case, and it is bounded the
same way: the byte cap in `_bounded_bytes` (a pickle stream can only encode
as many opcodes as it has bytes, so the cap bounds the restricted load's own
work too), and this reader's process — like every template reader not
listed in `fused_render/executor.py`'s `INPROCESS_HELPERS` — already runs in
a fresh, timeout-killed subprocess per call.

One allowlisted class needs a carve-out from "find_class resolves the real
class": `NumpyArrayWrapper.read_array` (the same joblib method this reader
depends on for framing, see above) calls a bare `pickle.load()` directly on
the file handle when the array's dtype is `object` — entirely outside
`find_class`, so an object-dtype array can smuggle in any callable regardless
of the allowlist. Its declared `shape` is also used to size a `np.empty(...)`
allocation before any array bytes are even read, so a tiny pickle can still
declare a huge shape. `find_class` handles this by substituting
`_SafeNumpyArrayWrapper` (and, for the joblib<=0.9 on-disk format,
`_SafeNDArrayWrapper`) for the real wrapper class: same fields, same framing,
but the oversized-shape case is refused outright, and the object-dtype case
is re-read with a fresh `pickle.Unpickler` wired to the *same* restricted
`find_class` rather than a plain one — real ensembles (GradientBoosting,
AdaBoost, bagging, ...) genuinely store their per-round estimators in an
object-dtype array, so it can't simply be refused without breaking ordinary
models.

A module that resolves but is not actually importable (e.g. a pickle
referencing `catboost.*`, which is allowlisted-by-policy but not bundled
with this template) is reported as "unavailable" rather than "blocked" —
trusted, just absent.

Self-contained on purpose: a template is a set of scripts run by the engine,
not part of the package (SPEC PY-15/D166), so it never imports
`fused_render`.
"""
import io
import json
import math
import os
import pickle
import zlib

import joblib.numpy_pickle

_RAW_SIZE_CAP = 2 * 1024 * 1024 * 1024  # refuse to even open a pickle bigger than this
_DECOMPRESSED_CAP = 512 * 1024 * 1024  # cap on the decompressed byte stream handed to the loader
_MAX_ARRAY_BYTES = _DECOMPRESSED_CAP  # cap on a single array's declared size; see _SafeNumpyArrayWrapper
_MAX_TREE_DEPTH = 200  # real trees rarely exceed depth 30-40; this only bounds pathological/adversarial state

# Exact-symbol allow: the handful of builtin/stdlib names a pickled ML object
# routinely needs to reconstruct plain containers and numpy scalars. Never
# widened to a prefix — "builtins" as a prefix would allow eval/exec/open.
_EXACT_ALLOW = {
    "builtins": {"dict", "list", "tuple", "set", "frozenset", "complex",
                 "bytes", "bytearray", "str", "int", "float", "bool", "slice"},
    "collections": {"OrderedDict", "defaultdict"},
    "copyreg": {"_reconstructor", "__newobj__", "__newobj_ex__"},
    "_codecs": {"encode"},  # numpy scalars round-trip through latin1 via this
}

# Prefix allow: trust-the-package, not its internal module layout (a name
# like "sklearn.tree._tree" breaks silently on every version bump; the real
# security boundary is which packages are trusted, not where their classes
# happen to live this release).
_PREFIX_ALLOW = (
    "numpy.", "numpy._core.", "numpy.core.",
    "scipy.", "sklearn.", "xgboost.", "lightgbm.", "catboost.",
    "pandas.", "joblib.",
)


class _Stub:
    """Stands in for every disallowed class a scanned pickle references.
    Swallows any constructor call, state, or container mutation as a no-op,
    so a mixed object graph (some allowed, some not) can still finish
    loading — the disallowed branches just come out inert."""

    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return _Stub()

    def __setstate__(self, state):
        pass

    def __reduce__(self):
        return (_Stub, ())

    def __setitem__(self, key, value):
        pass

    def __getitem__(self, key):
        return _Stub()

    def append(self, value):
        pass

    def extend(self, values):
        pass

    def update(self, *args, **kwargs):
        pass

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0


def _classify(module: str, name: str) -> bool:
    """True if (module, name) is on the allowlist. Fail closed: anything not
    explicitly recognised is unsafe by default."""
    allowed_names = _EXACT_ALLOW.get(module)
    if allowed_names is not None and name in allowed_names:
        return True
    return any(module == prefix.rstrip(".") or module.startswith(prefix) for prefix in _PREFIX_ALLOW)


class _DelegatingUnpickler(pickle.Unpickler):
    """A plain `pickle.Unpickler` whose `find_class`/`persistent_load`
    delegate to externally supplied callables. Needed because the
    C-accelerated `pickle.Unpickler` refuses to have `find_class` set as an
    instance attribute ("object attribute 'find_class' is read-only") — it
    has to be overridden by subclassing, even though the callable itself is
    just forwarded straight through unchanged."""

    def __init__(self, file_handle, find_class, persistent_load):
        super().__init__(file_handle)
        self._find_class = find_class
        self._persistent_load = persistent_load

    def find_class(self, module, name):
        return self._find_class(module, name)

    def persistent_load(self, pid):
        return self._persistent_load(pid)


class _SafeNumpyArrayWrapper(joblib.numpy_pickle.NumpyArrayWrapper):
    """Same fields and on-disk framing as the real `NumpyArrayWrapper`, but
    `read_array` closes the two ways it escapes the restricted-unpickle
    guarantee entirely (module docstring): an object-dtype array is read via
    a bare `pickle.load()` on the file handle, and the element count comes
    from this wrapper's own declared `shape`/`dtype`, otherwise never
    checked against anything before `np.empty(count, dtype)` allocates it.

    Real ensembles (GradientBoostingClassifier, AdaBoost, bagging, ...)
    genuinely store their per-round estimators in an object-dtype array, so
    the object-dtype case can't simply be refused outright without breaking
    ordinary models — instead it's re-read with a fresh `pickle.Unpickler`
    whose `find_class` is `unpickler.find_class` itself (the enclosing
    `_RestrictedUnpickler`), so every reference inside still goes through the
    same allowlist and is recorded in the same refs/blocked/missing
    bookkeeping."""

    def read_array(self, unpickler, ensure_native_byte_order):
        if self.dtype.hasobject:
            # (no native-byte-order fixup needed here: that only ever matters
            # for numeric dtypes, never for "|O" object arrays)
            embedded = _DelegatingUnpickler(unpickler.file_handle, unpickler.find_class, unpickler.persistent_load)
            return embedded.load()
        count = 1
        for dim in self.shape:
            count *= int(dim)
        declared_bytes = count * self.dtype.itemsize
        if declared_bytes > _MAX_ARRAY_BYTES:
            raise pickle.UnpicklingError(
                f"refusing to allocate a {declared_bytes:,}-byte array "
                f"(declared shape {tuple(self.shape)!r}), above the "
                f"{_MAX_ARRAY_BYTES:,}-byte safety cap"
            )
        return super().read_array(unpickler, ensure_native_byte_order)


class _SafeNDArrayWrapper(joblib.numpy_pickle.NDArrayWrapper):
    """The joblib<=0.9 compat wrapper: `read` loads an attacker-named sibling
    file via `np.load(..., allow_pickle=True)`, the same bare-pickle escape
    as `_SafeNumpyArrayWrapper` above, plus an attacker-controlled filename.
    Nothing produced by any joblib in the last decade uses this format, so
    refuse it outright rather than re-deriving numpy's own dtype sniffing."""

    def read(self, unpickler):
        raise pickle.UnpicklingError(
            "refusing to load a legacy (joblib<=0.9) NDArrayWrapper array"
        )


class _RestrictedUnpickler(joblib.numpy_pickle.NumpyUnpickler):
    """`NumpyUnpickler` (not plain `pickle.Unpickler`) so joblib's own
    array-wrapper framing (see module docstring) still works. `find_class`
    is the sole gate: allowed references resolve for real, everything else
    becomes an inert `_Stub` and is recorded as blocked."""

    def __init__(self, filename, file_obj):
        super().__init__(filename, file_obj, ensure_native_byte_order=True)
        self.refs = []  # [{"module", "name", "allowed"}], first-seen order, deduped
        self._seen = set()
        self.blocked = []
        self.missing = set()

    def find_class(self, module, name):
        allowed = _classify(module, name)
        key = (module, name)
        if key not in self._seen:
            self._seen.add(key)
            self.refs.append({"module": module, "name": name, "allowed": allowed})
        if not allowed:
            self.blocked.append({"module": module, "name": name})
            return _Stub
        try:
            resolved = super().find_class(module, name)
        except (ImportError, AttributeError):
            self.missing.add(module.split(".", 1)[0])
            return _Stub
        if resolved is joblib.numpy_pickle.NumpyArrayWrapper:
            return _SafeNumpyArrayWrapper
        if resolved is joblib.numpy_pickle.NDArrayWrapper:
            return _SafeNDArrayWrapper
        return resolved

    def persistent_load(self, pid):
        # Out-of-band persistent references have no callable to allowlist —
        # refuse rather than guess at what they'd resolve to.
        raise pickle.UnpicklingError(f"unsupported persistent reference: {pid!r}")


def _decompress_capped(raw: bytes, cap: int):
    """None on cap exceeded. Recognises joblib's own zlib/gzip wrapping;
    anything else (lz4, xz, or genuinely raw pickle bytes) passes through
    unchanged — an unrecognised compressed format simply fails to parse as
    pickle opcodes later, which is already treated as blocked."""
    if raw[:2] == b"\x1f\x8b":
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif raw[:1] == b"\x78":
        decompressor = zlib.decompressobj()
    else:
        return raw

    out = bytearray()
    pending = raw
    while pending or decompressor.unconsumed_tail:
        chunk = decompressor.unconsumed_tail or pending
        pending = b""
        piece = decompressor.decompress(chunk, max(1, cap - len(out)))
        out.extend(piece)
        if len(out) > cap:
            return None
        if not piece and not decompressor.unconsumed_tail:
            break
    out.extend(decompressor.flush())
    return bytes(out) if len(out) <= cap else None


def _bounded_bytes(path: str):
    """(bytes, error) — error is a human-readable string on failure, bytes is
    None in that case. Never raises: a bound that can itself throw defeats
    the point of having one."""
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return None, f"could not stat file: {exc}"
    if size > _RAW_SIZE_CAP:
        return None, f"file is {size:,} bytes, above the {_RAW_SIZE_CAP:,}-byte safety cap"
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        return None, f"could not read file: {exc}"
    try:
        data = _decompress_capped(raw, _DECOMPRESSED_CAP)
    except zlib.error as exc:
        return None, f"could not decompress: {exc}"
    if data is None:
        return None, f"decompressed content exceeds the {_DECOMPRESSED_CAP:,}-byte safety cap"
    return data, None


def _restricted_load(path: str, data: bytes):
    """(verdict, refs, message, obj). verdict is "safe" | "unavailable" |
    "blocked"; obj is the real reconstructed object only when verdict is
    "safe" — a partially-stubbed object from a blocked/unavailable file is
    never handed to the introspection helpers below."""
    unpickler = _RestrictedUnpickler(path, io.BytesIO(data))
    try:
        obj = unpickler.load()
    except Exception as exc:  # noqa: BLE001 - any failure here means "not
        # proven safe", never "safe by default".
        return "blocked", [], f"could not parse as a pickle stream: {exc}", None

    if unpickler.blocked:
        return "blocked", unpickler.refs, None, None
    if unpickler.missing:
        return "unavailable", unpickler.refs, ", ".join(sorted(unpickler.missing)), None
    return "safe", unpickler.refs, None, obj


def _sibling_chunks(path: str):
    """joblib's memmap-backing sibling files, when `joblib.dump` split large
    arrays out of the main pickle (`name.pkl_01.npy`, `_02.npy`, ...)."""
    base = os.path.basename(path)
    directory = os.path.dirname(path) or "."
    try:
        entries = os.listdir(directory)
    except OSError:
        return []
    prefix = base + "_"
    return sorted(
        name for name in entries
        if name.startswith(prefix) and name.endswith(".npy")
    )


def _short_repr(value, limit=120) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _json_float(v):
    """float, or a JSON-safe string sentinel for nan/inf. The production
    response path (fused_render/executor.py's dumps_result, `allow_nan:
    False`) rejects non-finite floats outright with an uncaught ValueError —
    and nan is not an exotic edge case here: XGBClassifier's default
    `missing` param IS nan, so almost every real fitted XGBoost model has one
    in its params."""
    f = float(v)
    if math.isnan(f):
        return "NaN"
    if math.isinf(f):
        return "Infinity" if f > 0 else "-Infinity"
    return f


def _escape_path_key(key) -> str:
    """A dict key, escaped for splicing into a synthesized dotted/bracketed
    path. Without this, a dict key containing a literal "." (or "[]") could
    collide with a path built from actually-nested dicts — e.g.
    {"sensor.temp": a, "sensor": {"temp": b}} would otherwise both produce
    the path "sensor.temp" — and template.html's
    `data.trees.find(x => x.path === t.path)` would then silently pick the
    wrong one."""
    return str(key).replace("\\", "\\\\").replace(".", "\\.").replace("[", "\\[").replace("]", "\\]")


def _describe(obj, path="", depth=0, max_depth=4):
    """Bounded recursive structure walk: what _describe returns is also how
    plain training-provenance metadata (dataset name, dropped-sample ids, ...)
    surfaces for a bundle like {"classifier": ..., "dataset": "...", ...} —
    it's just dict values the walk already shows, no separate "model card"
    merge step needed for that part."""
    if depth >= max_depth:
        return {"path": path, "kind": "truncated"}
    if isinstance(obj, dict):
        return {
            "path": path, "kind": "dict",
            "length": len(obj),
            "items": [
                {"key": str(k), **_describe(v, f"{path}.{_escape_path_key(k)}" if path else _escape_path_key(k), depth + 1, max_depth)}
                for k, v in list(obj.items())[:50]
            ],
        }
    if isinstance(obj, (list, tuple)):
        return {
            "path": path, "kind": "list",
            "length": len(obj),
            "items": [_describe(v, f"{path}[{i}]", depth + 1, max_depth) for i, v in enumerate(obj[:50])],
        }
    if _is_ndarray(obj):
        return {"path": path, "kind": "ndarray", "shape": list(obj.shape), "dtype": str(obj.dtype)}
    if hasattr(obj, "get_params") and callable(obj.get_params):
        return {"path": path, "kind": "estimator", "type": type(obj).__name__, "module": type(obj).__module__}
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        value = _json_float(obj) if isinstance(obj, float) else obj
        return {"path": path, "kind": "scalar", "value": value if not isinstance(value, str) or len(value) <= 200 else value[:200] + "…"}
    return {"path": path, "kind": "object", "type": type(obj).__name__, "repr": _short_repr(obj)}


def _is_ndarray(obj) -> bool:
    return type(obj).__module__.startswith("numpy") and type(obj).__name__ == "ndarray"


def _find_estimators(obj, path="", depth=0, max_depth=4, seen=None):
    """All objects with a scikit-learn-style get_params(), anywhere in the
    (bounded-depth) structure, tagged with their path."""
    if seen is None:
        seen = set()
    if depth >= max_depth:
        return []
    obj_id = id(obj)
    if obj_id in seen:
        return []
    found = []
    if hasattr(obj, "get_params") and callable(obj.get_params):
        seen.add(obj_id)
        found.append((path, obj))
    if isinstance(obj, dict):
        for key, value in list(obj.items())[:50]:
            child_path = f"{path}.{_escape_path_key(key)}" if path else _escape_path_key(key)
            found.extend(_find_estimators(value, child_path, depth + 1, max_depth, seen))
    elif isinstance(obj, (list, tuple)):
        for index, value in enumerate(obj[:50]):
            found.extend(_find_estimators(value, f"{path}[{index}]", depth + 1, max_depth, seen))
    return found


def _jsonable_params(params: dict) -> dict:
    out = {}
    for key, value in params.items():
        if isinstance(value, float):
            out[key] = _json_float(value)
        elif isinstance(value, (str, int, bool)) or value is None:
            out[key] = value
        else:
            out[key] = _short_repr(value, 80)
    return out


def _estimator_summary(path: str, est) -> dict:
    summary = {
        "path": path,
        "type": type(est).__name__,
        "module": type(est).__module__,
        "params": _jsonable_params(est.get_params(deep=False)),
    }
    if hasattr(est, "n_features_in_"):
        summary["n_features_in"] = int(est.n_features_in_)
    if hasattr(est, "classes_"):
        try:
            summary["classes"] = [c.item() if hasattr(c, "item") else c for c in list(est.classes_)]
        except Exception:  # noqa: BLE001 - best-effort, never fatal to the view
            pass
    return summary


def _feature_importance(path: str, est):
    values = None
    if hasattr(est, "feature_importances_"):
        values = list(est.feature_importances_)
    elif hasattr(est, "coef_"):
        coef = est.coef_
        if getattr(coef, "ndim", 1) > 1:
            # multi-class: one coefficient row per class. Mean of absolute
            # values across classes is a standard, defensible way to
            # summarize importance in one ranking, rather than arbitrarily
            # picking class 0 and silently discarding the rest.
            values = list(abs(coef).mean(axis=0))
        else:
            values = [abs(float(v)) for v in coef]
    if values is None:
        return None
    ranked = sorted(
        ({"feature": i, "importance": float(v)} for i, v in enumerate(values)),
        key=lambda row: -row["importance"],
    )
    for row in ranked:
        row["importance"] = _json_float(row["importance"])
    return {"path": path, "features": ranked}


def _tree_kind(est):
    if hasattr(est, "get_booster"):
        return "xgboost"
    if hasattr(est, "booster_") and hasattr(est.booster_, "dump_model"):
        return "lightgbm"
    if hasattr(est, "estimators_"):
        # GradientBoostingClassifier/Regressor's estimators_ is 2-D — one row
        # of trees per boosting round, one column per class (a single column
        # for binary/regression) — not a flat list of one tree each like a
        # RandomForest/AdaBoost's estimators_.
        if getattr(est.estimators_, "ndim", 1) > 1:
            return "sklearn_gradient_boosting"
        return "sklearn_forest"
    if hasattr(est, "tree_") and hasattr(est.tree_, "children_left"):
        return "sklearn_tree"
    return None


def _sklearn_tree_node(tree, node_id, depth=0):
    if depth >= _MAX_TREE_DEPTH:
        return {"leaf": [], "truncated": True}
    if tree.children_left[node_id] == -1:
        value = tree.value[node_id]
        return {"leaf": [_json_float(v) for v in value.reshape(-1)]}
    return {
        "feature": int(tree.feature[node_id]),
        "threshold": _json_float(tree.threshold[node_id]),
        "left": _sklearn_tree_node(tree, tree.children_left[node_id], depth + 1),
        "right": _sklearn_tree_node(tree, tree.children_right[node_id], depth + 1),
    }


def _xgboost_feature_index(split: str):
    # xgboost's JSON dump names an unnamed feature "f<index>" (a string) where
    # sklearn/lightgbm give a bare int — normalized back to an int here so the
    # three libraries agree on what "feature" means and the view can format it
    # uniformly. A booster trained with real feature_names keeps its own name
    # — including one that happens to look like "f<something>" (e.g. "f007",
    # or "f²" whose "²" is a Unicode digit str.isdigit() accepts but int()
    # can't parse) — so this only treats it as an auto-generated index if it
    # round-trips exactly back to the original string.
    if split.startswith("f"):
        try:
            index = int(split[1:])
        except ValueError:
            return split
        if f"f{index}" == split:
            return index
    return split


def _xgboost_tree_node(node, depth=0):
    if depth >= _MAX_TREE_DEPTH:
        return {"leaf": [], "truncated": True}
    if "leaf" in node:
        return {"leaf": [_json_float(node["leaf"])]}
    children = {child["nodeid"]: child for child in node.get("children", [])}
    return {
        "feature": _xgboost_feature_index(node["split"]),
        "threshold": _json_float(node["split_condition"]),
        "left": _xgboost_tree_node(children[node["yes"]], depth + 1),
        "right": _xgboost_tree_node(children[node["no"]], depth + 1),
    }


def _lightgbm_tree_node(node, depth=0):
    if depth >= _MAX_TREE_DEPTH:
        return {"leaf": [], "truncated": True}
    if "leaf_value" in node:
        return {"leaf": [_json_float(node["leaf_value"])]}
    return {
        "feature": int(node["split_feature"]),
        "threshold": _json_float(node["threshold"]),
        "left": _lightgbm_tree_node(node["left_child"], depth + 1),
        "right": _lightgbm_tree_node(node["right_child"], depth + 1),
    }


def _tree_ensemble(path: str, est, tree_index):
    kind = _tree_kind(est)
    if kind is None:
        return None

    if kind == "sklearn_tree":
        count = 1
        meta = [{"index": 0}]
        tree = est.tree_
        node_builder = lambda: _sklearn_tree_node(tree, 0)  # noqa: E731
    elif kind == "sklearn_forest":
        count = len(est.estimators_)
        meta = [{"index": i} for i in range(count)]
        idx = tree_index if tree_index is not None else 0
        node_builder = lambda: _sklearn_tree_node(est.estimators_[idx].tree_, 0)  # noqa: E731
    elif kind == "sklearn_gradient_boosting":
        n_rounds, n_classes = est.estimators_.shape
        count = n_rounds * n_classes
        meta = [{"index": i, "round": i // n_classes, "class": i % n_classes} for i in range(count)]
        idx = tree_index if tree_index is not None else 0

        def node_builder():
            round_idx, class_idx = divmod(idx, n_classes)
            return _sklearn_tree_node(est.estimators_[round_idx, class_idx].tree_, 0)
    elif kind == "xgboost":
        # Tree count comes from num_boosted_rounds() (cheap: no dump), not
        # len(get_dump()) — dumping every tree as JSON text just to count them
        # cost 6+ seconds on a 992-tree model; get_dump() only ever runs, lazily,
        # for the one tree actually requested.
        booster = est.get_booster()
        n_classes = len(est.classes_) if hasattr(est, "classes_") and len(est.classes_) > 2 else 1
        count = booster.num_boosted_rounds() * n_classes
        meta = [{"index": i, "round": i // n_classes, "class": i % n_classes} for i in range(count)]
        idx = tree_index if tree_index is not None else 0

        def node_builder():
            return _xgboost_tree_node(json.loads(booster.get_dump(dump_format="json")[idx]))
    elif kind == "lightgbm":
        count = est.booster_.num_trees()  # cheap: dump_model() only runs lazily below
        meta = [{"index": i} for i in range(count)]
        idx = tree_index if tree_index is not None else 0

        def node_builder():
            return _lightgbm_tree_node(est.booster_.dump_model()["tree_info"][idx]["tree_structure"])
    else:
        return None

    result = {"path": path, "library": kind, "count": count, "trees": meta}
    if tree_index is not None or count == 1:
        try:
            result["tree"] = node_builder()
        except Exception as exc:  # noqa: BLE001 - a bad index still returns a usable summary
            result["tree_error"] = str(exc)
    return result


def _safe(fn, *args):
    """None on any failure. The estimator being introspected here came
    through __reduce__/__setstate__ rather than a normal constructor, so a
    real allowlisted class with adversarial internal state can raise from
    these getters in ways hasattr()'s AttributeError-only suppression won't
    catch — and one bad estimator anywhere in a multi-model bundle must not
    kill every other estimator's data too."""
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 - best-effort, isolated per estimator
        return None


def main(file: str, tree_index=None) -> dict:
    # tree_index is intentionally unannotated (matches structure/reader.py's
    # row_group convention): a param round-tripped through the URL arrives
    # as a string or JS null, and an `int` annotation would blow up on null.
    try:
        idx = int(tree_index) if tree_index not in (None, "") else None
    except (TypeError, ValueError):
        idx = None

    try:
        size = os.path.getsize(file)
        mtime = os.path.getmtime(file)
    except OSError as exc:
        return {"file": {"error": str(exc)}, "scan": {"verdict": "blocked", "refs": [], "message": str(exc)}}

    data, error = _bounded_bytes(file)
    file_info = {"size": size, "mtime": mtime, "sibling_files": _sibling_chunks(file)}
    if error is not None:
        return {"file": file_info, "scan": {"verdict": "blocked", "refs": [], "message": error}}

    verdict, refs, message, obj = _restricted_load(file, data)
    scan_info = {"verdict": verdict, "refs": refs, "message": message}
    if verdict != "safe":
        return {"file": file_info, "scan": scan_info}

    found = _find_estimators(obj)

    return {
        "file": file_info,
        "scan": scan_info,
        "structure": _describe(obj),
        "estimators": [s for s in (_safe(_estimator_summary, p, e) for p, e in found) if s],
        "feature_importance": [fi for fi in (_safe(_feature_importance, p, e) for p, e in found) if fi],
        "trees": [t for t in (_safe(_tree_ensemble, p, e, idx) for p, e in found) if t],
    }
