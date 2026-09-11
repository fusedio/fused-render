"""`_job_page` (SPEC-actionable-notifications.md): the one place
`ai/supervisor.py` decides what a job row's `Job.page` should be, now that
the field means "where clicking this row goes" rather than mere attribution.

The spec's producer table names only `sys:ai-model:*` — `/ai-models/local`.
Every other job-id-prefix family this module reports through (`ai-image:`,
`ai-transcribe:`, `ai-video:`, `ai-text:`) is deliberately left with no
destination: the spec is binding even where its own coverage looks
inconsistent with the rest of this module's job surface.
"""
from fused_render.ai import supervisor


def test_an_ai_model_job_page_is_the_local_models_page():
    job = supervisor.JOB_PREFIX + "some/repo"
    assert supervisor._job_page(job) == "/ai-models/local"


def test_every_other_job_prefix_family_gets_no_page():
    for prefix in (
        supervisor.IMAGE_JOB_PREFIX,
        supervisor.TRANSCRIBE_JOB_PREFIX,
        supervisor.VIDEO_JOB_PREFIX,
        supervisor.TEXT_JOB_PREFIX,
    ):
        assert supervisor._job_page(prefix + "x") == ""


def test_a_benchmark_job_page_is_the_benchmark_page():
    # `ai/benchmark.py` reports through this same `_report`/`_job_page` — its
    # own prefix constant lives here (`BENCHMARK_JOB_PREFIX`) rather than as
    # an inline literal in benchmark.py, so this table stays the one place
    # that owns the mapping.
    job = supervisor.BENCHMARK_JOB_PREFIX + "abc123"
    assert supervisor._job_page(job) == "/ai-models/benchmark"


def test_report_writes_the_page_onto_an_ai_model_row(monkeypatch):
    from fused_render import jobs

    captured = {}
    real_upsert = jobs.upsert

    def spy_upsert(body, **kw):
        captured.update(kw)
        return real_upsert(body, **kw)

    monkeypatch.setattr(jobs, "upsert", spy_upsert)
    job = supervisor.JOB_PREFIX + "owner/model"
    supervisor._report(job, title="owner/model", state="running", kind="download")
    assert captured.get("page") == "/ai-models/local"


def test_report_writes_no_page_for_a_non_model_job(monkeypatch):
    from fused_render import jobs

    captured = {}
    real_upsert = jobs.upsert

    def spy_upsert(body, **kw):
        captured.update(kw)
        return real_upsert(body, **kw)

    monkeypatch.setattr(jobs, "upsert", spy_upsert)
    job = supervisor.IMAGE_JOB_PREFIX + "x"
    supervisor._report(job, title="an image", state="running", kind="task")
    assert captured.get("page") == ""
