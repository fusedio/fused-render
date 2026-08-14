"""Unit tests for joblib_model/reader.py.

Needs joblib/scikit-learn/xgboost/lightgbm at test time (unlike every other
template's tests, since this reader genuinely has to unpickle real estimators
to introspect them — confirmed there's no existing wiring in this repo that
installs a template's own pyproject.toml deps into the pytest env, so this is
self-documented rather than added to CI):

    uv run --with joblib --with scikit-learn --with xgboost --with lightgbm --with pandas \
        pytest fused_render/templates/joblib_model/tests/test_reader.py -v
"""
import io
import json
import os
import pickle
import random
import sys
import time

import joblib
import joblib.numpy_pickle
import numpy as np
import pytest
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # import the sibling reader.py

import reader  # noqa: E402


def _dump(obj, tmp_path, name="model.joblib"):
    path = os.path.join(str(tmp_path), name)
    joblib.dump(obj, path)
    return path


def _make_bundle():
    rng = np.random.RandomState(0)
    x = rng.rand(200, 6)
    y = rng.randint(0, 3, size=200)
    scaler = StandardScaler().fit(x)
    clf = XGBClassifier(n_estimators=5, max_depth=2, eval_metric="mlogloss")
    clf.fit(scaler.transform(x), y)
    return {
        "scaler": scaler,
        "classifier": clf,
        "dataset": "synthetic_test_v1",
        "dropped_polygons": ["abc-123"],
    }


# --------------------------------------------------------------- safety scan


def test_dangerous_reduce_is_blocked_and_never_executed(tmp_path, monkeypatch):
    class _Reducer:
        def __reduce__(self):
            return (os.system, ("echo pwned",))

    path = os.path.join(str(tmp_path), "evil.pkl")
    with open(path, "wb") as handle:
        pickle.dump(_Reducer(), handle)  # dumped BEFORE patching os.system below

    calls = []
    monkeypatch.setattr(os, "system", lambda cmd: calls.append(cmd))

    out = reader.main(file=path)
    assert out["scan"]["verdict"] == "blocked"
    offending = [r for r in out["scan"]["refs"] if not r["allowed"]]
    assert any(r["name"] == "system" for r in offending)
    assert calls == []  # the real os.system was never reached
    assert "structure" not in out


def test_object_dtype_array_bypass_is_blocked_and_never_executed(tmp_path, monkeypatch):
    # Same os.system gadget via joblib's bare pickle.load() escape for object-dtype
    # arrays; patched on os.system's real module since pickle serializes by __module__.
    class _Reducer:
        def __reduce__(self):
            return (os.system, ("echo pwned",))

    path = os.path.join(str(tmp_path), "evil_array.joblib")
    joblib.dump({"arr": np.array([_Reducer()], dtype=object)}, path)

    calls = []
    monkeypatch.setattr(sys.modules[os.system.__module__], "system", lambda cmd: calls.append(cmd))

    out = reader.main(file=path)
    assert out["scan"]["verdict"] != "safe"
    assert calls == []  # the real os.system (nt.system/posix.system) was never reached
    assert "structure" not in out


def test_object_dtype_array_gates_the_call_not_just_the_reference(tmp_path, monkeypatch):
    # _DelegatingUnpickler (used for object-dtype array content) used to be built on
    # pickle.Unpickler, the C-accelerated one with no overridable dispatch table --
    # so load_reduce's unsafe-to-call gate silently never applied inside it, and a
    # prefix-trusted function reached only through an object-dtype array (unlike a
    # top-level REDUCE) was called for real.
    class _Reducer:
        def __reduce__(self):
            return (joblib.load, ("/etc/passwd",))

    path = os.path.join(str(tmp_path), "evil_array2.joblib")
    joblib.dump({"arr": np.array([_Reducer()], dtype=object)}, path)

    calls = []
    monkeypatch.setattr(joblib.numpy_pickle, "load", lambda *a, **k: calls.append(a))

    out = reader.main(file=path)
    assert out["scan"]["verdict"] == "blocked"
    offending = [r for r in out["scan"]["refs"] if not r["allowed"]]
    assert any(r["name"] == "load" for r in offending)
    assert calls == []


def test_legacy_inst_opcode_gates_the_call_too(tmp_path, monkeypatch):
    # The legacy INST/OBJ opcodes (pickle._Unpickler._instantiate) call a resolved
    # reference directly, the same as REDUCE, but through a completely different
    # code path load_reduce alone does not cover.
    path = os.path.join(str(tmp_path), "inst_gadget.pkl")
    with open(path, "wb") as handle:
        handle.write(pickle.MARK)
        handle.write(pickle.SHORT_BINSTRING + bytes([1]) + b"x")
        handle.write(pickle.INST + b"joblib.numpy_pickle\nload\n")
        handle.write(pickle.STOP)

    calls = []
    monkeypatch.setattr(joblib.numpy_pickle, "load", lambda *a, **k: calls.append(a))

    out = reader.main(file=path)
    assert out["scan"]["verdict"] == "blocked"
    offending = [r for r in out["scan"]["refs"] if not r["allowed"]]
    assert any(r["name"] == "load" for r in offending)
    assert calls == []


def test_prefix_trusted_function_gadget_is_blocked_and_never_executed(tmp_path, monkeypatch):
    # joblib.load is a real function under the trusted "joblib." prefix.
    class _Reducer:
        def __reduce__(self):
            return (joblib.load, ("/etc/passwd",))

    path = os.path.join(str(tmp_path), "gadget.pkl")
    with open(path, "wb") as handle:
        pickle.dump(_Reducer(), handle)

    calls = []
    monkeypatch.setattr(joblib.numpy_pickle, "load", lambda *a, **k: calls.append(a))

    out = reader.main(file=path)
    assert out["scan"]["verdict"] == "blocked"
    offending = [r for r in out["scan"]["refs"] if not r["allowed"]]
    assert any(r["name"] == "load" for r in offending)
    assert calls == []


def test_pipeline_surfaces_the_inner_estimators_it_wraps(tmp_path):
    # Pipeline's inner scaler/classifier are attributes, not get_params() values.
    from sklearn.pipeline import Pipeline

    rng = np.random.RandomState(0)
    x = rng.rand(100, 4)
    y = rng.randint(0, 2, size=100)
    pipe = Pipeline([
        ("scale", StandardScaler()),
        ("clf", RandomForestClassifier(n_estimators=3, max_depth=2, random_state=0)),
    ]).fit(x, y)
    path = _dump(pipe, tmp_path)

    out = reader.main(file=path)
    assert out["scan"]["verdict"] == "safe"
    types = {e["type"] for e in out["estimators"]}
    assert types == {"Pipeline", "StandardScaler", "RandomForestClassifier"}
    assert out["trees"]  # the wrapped RandomForestClassifier's trees surfaced


def test_pipeline_nested_in_a_dict_is_still_discovered(tmp_path):
    # joblib.dump({"model": pipe}) is the realistic layout (matches _make_bundle's
    # own shape) -- one extra depth level must not exhaust the traversal budget.
    from sklearn.pipeline import Pipeline

    rng = np.random.RandomState(0)
    x = rng.rand(100, 4)
    y = rng.randint(0, 2, size=100)
    pipe = Pipeline([
        ("scale", StandardScaler()),
        ("clf", RandomForestClassifier(n_estimators=3, max_depth=2, random_state=0)),
    ]).fit(x, y)
    path = _dump({"model": pipe}, tmp_path)

    out = reader.main(file=path)
    assert out["scan"]["verdict"] == "safe"
    types = {e["type"] for e in out["estimators"]}
    assert types == {"Pipeline", "StandardScaler", "RandomForestClassifier"}
    assert out["trees"]


def test_random_forest_members_are_not_treated_as_separate_models(tmp_path):
    # Each DecisionTreeClassifier inside estimators_ also has get_params(); walking
    # into it as if it were a nested composite estimator would duplicate the forest
    # once per tree instead of showing the forest as a single ensemble.
    rng = np.random.RandomState(1)
    x = rng.rand(100, 4)
    y = rng.randint(0, 2, size=100)
    clf = RandomForestClassifier(n_estimators=5, max_depth=2, random_state=0).fit(x, y)
    path = _dump(clf, tmp_path)

    out = reader.main(file=path)
    assert out["scan"]["verdict"] == "safe"
    assert [e["type"] for e in out["estimators"]] == ["RandomForestClassifier"]
    assert len(out["trees"]) == 1


def test_pandas_dataframe_bundle_is_safe(tmp_path):
    # pandas' own Block/Index reconstruction is via module-level functions
    # (_unpickle_block, _new_Index), not classes -- any DataFrame/Index in a bundle
    # used to be misreported as "blocked".
    pd = pytest.importorskip("pandas")
    rng = np.random.RandomState(2)
    df = pd.DataFrame({"a": rng.rand(20), "b": pd.Categorical(["x", "y"] * 10)})
    path = _dump({"data": df, "cols": df.columns}, tmp_path)

    out = reader.main(file=path)
    assert out["scan"]["verdict"] == "safe"


def test_function_transformer_wrapping_a_trusted_ufunc_is_safe(tmp_path):
    # A numpy ufunc reference is only ever STORED as an attribute here, never
    # called -- treating every non-class reference as unsafe over-blocked this.
    from sklearn.preprocessing import FunctionTransformer

    rng = np.random.RandomState(3)
    ft = FunctionTransformer(np.log1p).fit(rng.rand(20, 2))
    path = _dump(ft, tmp_path)

    out = reader.main(file=path)
    assert out["scan"]["verdict"] == "safe"


def test_array_with_huge_declared_shape_is_blocked_before_allocating(tmp_path):
    # shape is attacker-controlled and sizes np.empty(...) before any array byte is read.
    wrapper = joblib.numpy_pickle.NumpyArrayWrapper(
        subclass=np.ndarray, shape=(10**15,), order="C",
        dtype=np.dtype("float64"), allow_mmap=False,
    )
    buf = io.BytesIO()
    joblib.numpy_pickle.NumpyPickler(buf).dump(wrapper)  # no array bytes follow, on purpose
    path = os.path.join(str(tmp_path), "huge_shape.joblib")
    with open(path, "wb") as handle:
        handle.write(buf.getvalue())

    out = reader.main(file=path)
    assert out["scan"]["verdict"] != "safe"


def test_shared_references_do_not_blow_up_the_walk(tmp_path):
    # Pickle's memo lets a tiny file declare the SAME (large) list many times over.
    # Without identity-based dedup, a walk that recurses per-occurrence rather than
    # per-object does branch**depth work from a file of a few hundred bytes.
    level = 0
    for _ in range(24):
        level = [level] * 20
    path = _dump(level, tmp_path, name="shared_refs.joblib")
    assert os.path.getsize(path) < 2000

    start = time.time()
    out = reader.main(file=path)
    assert time.time() - start < 10
    assert out["scan"]["verdict"] == "safe"


def test_unrecognised_but_allowlist_prefixed_library_is_unavailable(tmp_path):
    # catboost isn't a bundled dependency; a real reference to it must resolve
    # to "unavailable" (trusted, just absent), not "blocked".
    payload = (
        pickle.PROTO + bytes([2])
        + pickle.GLOBAL + b"catboost.core\nCatBoostClassifier\n"
        + pickle.EMPTY_TUPLE
        + pickle.REDUCE
        + pickle.STOP
    )
    path = os.path.join(str(tmp_path), "catboost_ref.pkl")
    with open(path, "wb") as handle:
        handle.write(payload)

    out = reader.main(file=path)
    assert out["scan"]["verdict"] == "unavailable"
    assert "catboost" in out["scan"]["message"]


def test_truncated_compressed_file_reports_blocked_without_raising(tmp_path):
    # Malformed gzip magic + truncated body must not raise zlib.error out of main().
    path = os.path.join(str(tmp_path), "truncated.joblib")
    with open(path, "wb") as handle:
        handle.write(b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x00" + random.Random(0).randbytes(64))

    out = reader.main(file=path)  # must not raise
    assert out["scan"]["verdict"] == "blocked"
    assert "decompress" in out["scan"]["message"]


def test_json_serializable_on_every_verdict(tmp_path):
    # allow_nan=False mirrors the production response path's JSON encoder.
    bundle_path = _dump(_make_bundle(), tmp_path)
    json.dumps(reader.main(file=bundle_path), allow_nan=False)

    class _Reducer:
        def __reduce__(self):
            return (eval, ("1+1",))

    evil_path = os.path.join(str(tmp_path), "evil2.pkl")
    with open(evil_path, "wb") as handle:
        pickle.dump(_Reducer(), handle)
    json.dumps(reader.main(file=evil_path), allow_nan=False)


def test_xgboost_default_missing_nan_param_is_json_safe(tmp_path):
    # XGBClassifier's default `missing` param is float('nan'), which the
    # production response path's allow_nan=False would otherwise reject.
    rng = np.random.RandomState(3)
    x = rng.rand(50, 4)
    y = rng.randint(0, 2, size=50)
    clf = XGBClassifier(n_estimators=3, max_depth=2).fit(x, y)
    path = _dump(clf, tmp_path)

    out = reader.main(file=path)
    assert out["scan"]["verdict"] == "safe"
    summary = next(e for e in out["estimators"] if e["path"] == "")
    assert summary["params"]["missing"] == "NaN"
    json.dumps(out, allow_nan=False)


# ------------------------------------------------------------- introspection


def test_multiclass_linear_model_feature_importance_uses_all_classes(tmp_path):
    # coef_ is 2-D for multi-class; importance must reflect all classes, not just class 0.
    rng = np.random.RandomState(4)
    x = rng.rand(150, 5)
    y = rng.randint(0, 3, size=150)  # 3 classes
    clf = LogisticRegression(max_iter=500).fit(x, y)
    assert clf.coef_.shape[0] == 3  # sanity: genuinely multi-class

    path = _dump(clf, tmp_path)
    out = reader.main(file=path)
    fi = next(f for f in out["feature_importance"] if f["path"] == "")
    got = {row["feature"]: row["importance"] for row in fi["features"]}

    expected = np.abs(clf.coef_).mean(axis=0)
    class0_only = np.abs(clf.coef_[0])
    assert got != {i: class0_only[i] for i in range(len(class0_only))}
    for i, v in enumerate(expected):
        assert got[i] == pytest.approx(float(v))


def test_feature_importance_sorts_nan_last_not_arbitrarily(tmp_path):
    # NaN compares False against everything, so sorting by a raw NaN key can leave
    # it anywhere -- including rank 0, displayed as the MOST important feature.
    class _FakeEstimator:
        feature_importances_ = [0.2, float("nan"), 0.5, 0.1]

        def get_params(self, deep=True):
            return {}

    result = reader._feature_importance("", _FakeEstimator())
    order = [row["feature"] for row in result["features"]]
    assert order == [2, 0, 3, 1]  # descending by value, NaN (feature 1) last
    assert result["features"][-1]["importance"] == "NaN"


def test_classes_containing_nan_stay_json_safe(tmp_path):
    # classes_ used to bypass _json_float entirely -- a NaN there broke the whole
    # response's allow_nan=False serialization instead of degrading gracefully.
    class _FakeEstimator:
        classes_ = np.array([0.0, float("nan"), 2.0])

        def get_params(self, deep=False):
            return {}

    summary = reader._estimator_summary("", _FakeEstimator())
    assert summary["classes"] == [0.0, "NaN", 2.0]
    json.dumps(summary, allow_nan=False)


def test_one_bad_estimator_does_not_crash_the_others(tmp_path, monkeypatch):
    # One estimator raising from a getter must not take down the others in the bundle.
    rng = np.random.RandomState(7)
    x, y = rng.rand(20, 3), rng.randint(0, 2, size=20)
    good = StandardScaler().fit(x)
    bad = LogisticRegression().fit(x, y)
    path = _dump({"good": good, "bad": bad}, tmp_path)

    def rigged_get_params(self, deep=True):
        raise RuntimeError("rigged failure")

    monkeypatch.setattr(LogisticRegression, "get_params", rigged_get_params)

    out = reader.main(file=path)
    assert out["scan"]["verdict"] == "safe"
    paths = {e["path"] for e in out["estimators"]}
    assert "good" in paths
    assert "bad" not in paths


def test_dotted_dict_key_does_not_collide_with_a_nested_path(tmp_path):
    # A literal dotted key must not collide with the path a nested dict would produce.
    rng = np.random.RandomState(9)
    x, y = rng.rand(20, 2), rng.randint(0, 2, size=20)
    est_a = LogisticRegression().fit(x, y)
    est_b = StandardScaler().fit(x)
    bundle = {"sensor.temp": est_a, "sensor": {"temp": est_b}}
    path = _dump(bundle, tmp_path)

    out = reader.main(file=path)
    assert out["scan"]["verdict"] == "safe"
    paths = [e["path"] for e in out["estimators"]]
    assert len(paths) == 2
    assert len(set(paths)) == 2  # distinct paths -- no collision


def test_safe_dict_bundle_like_real_file(tmp_path):
    path = _dump(_make_bundle(), tmp_path)
    out = reader.main(file=path)

    assert out["scan"]["verdict"] == "safe"
    assert out["file"]["size"] == os.path.getsize(path)

    paths = {e["path"] for e in out["estimators"]}
    assert "scaler" in paths and "classifier" in paths

    clf_summary = next(e for e in out["estimators"] if e["path"] == "classifier")
    assert clf_summary["type"] == "XGBClassifier"
    assert clf_summary["params"]["max_depth"] == 2
    assert clf_summary["n_features_in"] == 6

    fi = next(f for f in out["feature_importance"] if f["path"] == "classifier")
    assert len(fi["features"]) == 6
    assert fi["features"] == sorted(fi["features"], key=lambda r: -r["importance"])

    trees = next(t for t in out["trees"] if t["path"] == "classifier")
    assert trees["library"] == "xgboost"
    assert trees["count"] == 15  # 5 rounds x one tree per class (3 classes) for multiclass softmax

    top_keys = {item["key"] for item in out["structure"]["items"]}
    assert "dataset" in top_keys and "dropped_polygons" in top_keys


def test_tree_detail_lazy_fetch_matches_count(tmp_path):
    path = _dump(_make_bundle(), tmp_path)
    overview = reader.main(file=path)
    trees = next(t for t in overview["trees"] if t["path"] == "classifier")

    detail = reader.main(file=path, tree_index=trees["count"] - 1)
    detail_trees = next(t for t in detail["trees"] if t["path"] == "classifier")
    assert "tree" in detail_trees
    node = detail_trees["tree"]
    assert "leaf" in node or ("feature" in node and "left" in node and "right" in node)


def test_bare_random_forest(tmp_path):
    rng = np.random.RandomState(1)
    x = rng.rand(100, 4)
    y = rng.randint(0, 2, size=100)
    clf = RandomForestClassifier(n_estimators=3, max_depth=2, random_state=0).fit(x, y)
    path = _dump(clf, tmp_path)

    out = reader.main(file=path, tree_index=0)
    assert out["scan"]["verdict"] == "safe"
    trees = next(t for t in out["trees"] if t["path"] == "")
    assert trees["library"] == "sklearn_forest"
    assert trees["count"] == 3
    assert "tree" in trees


def test_gradient_boosting_binary(tmp_path):
    # estimators_ is 2-D (rounds x classes), unlike RandomForest's flat estimators_.
    rng = np.random.RandomState(5)
    x = rng.rand(100, 4)
    y = rng.randint(0, 2, size=100)
    clf = GradientBoostingClassifier(n_estimators=4, max_depth=2, random_state=0).fit(x, y)
    path = _dump(clf, tmp_path)

    out = reader.main(file=path, tree_index=0)
    assert out["scan"]["verdict"] == "safe"
    trees = next(t for t in out["trees"] if t["path"] == "")
    assert trees["library"] == "sklearn_gradient_boosting"
    assert trees["count"] == 4  # 4 rounds x 1 tree (binary => single column)
    assert "tree" in trees
    assert "feature" in trees["tree"] or "leaf" in trees["tree"]


def test_gradient_boosting_multiclass(tmp_path):
    rng = np.random.RandomState(6)
    x = rng.rand(120, 4)
    y = rng.randint(0, 3, size=120)  # 3 classes
    clf = GradientBoostingClassifier(n_estimators=3, max_depth=2, random_state=0).fit(x, y)
    assert clf.estimators_.shape == (3, 3)
    path = _dump(clf, tmp_path)

    out = reader.main(file=path, tree_index=8)  # round 2, class 2
    trees = next(t for t in out["trees"] if t["path"] == "")
    assert trees["library"] == "sklearn_gradient_boosting"
    assert trees["count"] == 9  # 3 rounds x 3 classes
    assert trees["trees"][8] == {"index": 8, "round": 2, "class": 2}
    assert "tree" in trees
    assert "feature" in trees["tree"] or "leaf" in trees["tree"]


def test_xgboost_unicode_digit_feature_name_does_not_crash():
    # str.isdigit() accepts Unicode digits like "²" that int() can't parse.
    node = {
        "nodeid": 0, "split": "f²", "split_condition": 0.5,
        "yes": 1, "no": 2,
        "children": [{"nodeid": 1, "leaf": 0.1}, {"nodeid": 2, "leaf": -0.1}],
    }
    result = reader._xgboost_tree_node(node)
    assert result["feature"] == "f²"  # preserved as-is, not silently coerced


def test_xgboost_leading_zero_feature_name_is_preserved_literally():
    # "f007" doesn't round-trip back from int(), so it must stay a literal name.
    node = {
        "nodeid": 0, "split": "f007", "split_condition": 0.5,
        "yes": 1, "no": 2,
        "children": [{"nodeid": 1, "leaf": 0.1}, {"nodeid": 2, "leaf": -0.1}],
    }
    result = reader._xgboost_tree_node(node)
    assert result["feature"] == "f007"  # not silently turned into 7 or "f7"


def test_tree_node_recursion_has_a_depth_guard():
    # An excessively deep tree must terminate gracefully, not hit RecursionError.
    node = {"leaf_value": 0.0}
    for _ in range(300):
        node = {"split_feature": 0, "threshold": 0.5, "left_child": node, "right_child": {"leaf_value": 0.0}}

    result = reader._lightgbm_tree_node(node)  # must not raise RecursionError

    walked, depth_walked = result, 0
    while "left" in walked and depth_walked < 250:
        walked = walked["left"]
        depth_walked += 1
    assert "leaf" in walked  # terminated with a (possibly truncated) leaf, not a RecursionError
    assert depth_walked <= reader._MAX_TREE_DEPTH + 1  # guard fired well before the full 300-deep chain


def test_lightgbm_model(tmp_path):
    lgb = pytest.importorskip("lightgbm")
    rng = np.random.RandomState(2)
    x = rng.rand(100, 4)
    y = rng.randint(0, 2, size=100)
    clf = lgb.LGBMClassifier(n_estimators=3, max_depth=2, min_child_samples=5).fit(x, y)
    path = _dump(clf, tmp_path)

    out = reader.main(file=path, tree_index=0)
    assert out["scan"]["verdict"] == "safe"
    trees = next(t for t in out["trees"] if t["path"] == "")
    assert trees["library"] == "lightgbm"
    assert trees["count"] == 3
    assert "tree" in trees


def test_lightgbm_multiclass_tree_index_is_flat_not_a_round(tmp_path):
    # num_trees() counts trees flat, but dump_model's start_iteration selects a
    # boosting ROUND. Fetching by the flat index without splitting it by class
    # returns the wrong tree (idx=1) or nothing at all (idx >= n_rounds).
    lgb = pytest.importorskip("lightgbm")
    rng = np.random.RandomState(11)
    x = rng.rand(300, 5)
    y = rng.randint(0, 3, size=300)  # 3 classes -> 3 trees per round
    clf = lgb.LGBMClassifier(n_estimators=4, max_depth=2, min_child_samples=5, verbose=-1).fit(x, y)
    path = _dump(clf, tmp_path)

    expected = clf.booster_.dump_model()["tree_info"]
    overview = next(t for t in reader.main(file=path)["trees"] if t["path"] == "")
    assert overview["count"] == 12
    assert overview["trees"][5] == {"index": 5, "round": 1, "class": 2}

    for idx in (1, 5, 11):  # every one of these was wrong or missing before
        trees = next(t for t in reader.main(file=path, tree_index=idx)["trees"] if t["path"] == "")
        assert "tree_error" not in trees
        assert trees["tree"] == reader._lightgbm_tree_node(expected[idx]["tree_structure"])


def test_voting_classifier_is_not_mistaken_for_a_tree_ensemble(tmp_path):
    # _tree_kind used to call anything with `estimators_` a forest, so a composite
    # was skipped by the walk that exists to descend into it -- hiding its models.
    from sklearn.ensemble import VotingClassifier

    rng = np.random.RandomState(12)
    x = rng.rand(120, 4)
    y = rng.randint(0, 2, size=120)
    vc = VotingClassifier([
        ("lr", LogisticRegression()),
        ("rf", RandomForestClassifier(n_estimators=3, max_depth=2, random_state=0)),
    ]).fit(x, y)
    assert reader._tree_kind(vc) is None
    path = _dump(vc, tmp_path)

    out = reader.main(file=path)
    types = [e["type"] for e in out["estimators"]]
    assert "VotingClassifier" in types
    assert "LogisticRegression" in types
    assert "RandomForestClassifier" in types
    # The pre-fit copies in the `estimators` param must not double every sub-model.
    assert types.count("RandomForestClassifier") == 1
    assert types.count("LogisticRegression") == 1
    assert [t["library"] for t in out["trees"]] == ["sklearn_forest"]


def test_file_stats_and_sibling_npy_chunks(tmp_path):
    path = _dump(_make_bundle(), tmp_path, name="chunked.joblib")
    # Simulate joblib's own memmap-split sibling files.
    for suffix in ("_01.npy", "_02.npy"):
        open(path + suffix, "wb").close()

    out = reader.main(file=path)
    assert out["file"]["size"] == os.path.getsize(path)
    assert sorted(out["file"]["sibling_files"]) == ["chunked.joblib_01.npy", "chunked.joblib_02.npy"]


def test_dict_traversal_is_capped_like_list_traversal(tmp_path):
    # Dict iteration must be capped at 50 like list/tuple iteration already is.
    big_dict = {f"key_{i}": i for i in range(200)}
    path = _dump(big_dict, tmp_path)

    out = reader.main(file=path)
    assert out["scan"]["verdict"] == "safe"
    structure = out["structure"]
    assert structure["kind"] == "dict"
    assert structure["length"] == 200
    assert len(structure["items"]) == 50


def test_missing_file_reports_blocked_without_raising(tmp_path):
    out = reader.main(file=os.path.join(str(tmp_path), "does_not_exist.joblib"))
    assert out["scan"]["verdict"] == "blocked"
    # Same file_info shape as every other return path -- template.html always
    # reads file.size/file.mtime/file.sibling_files unconditionally.
    assert set(out["file"]) == {"size", "mtime", "sibling_files"}
