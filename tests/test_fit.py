from __future__ import annotations

from sparkwatch import fit


def test_estimate_for_qwen3_235b_nvfp4(settings):
    estimate = fit.estimate_gb(235.0, "nvfp4", settings)
    assert estimate == 145.4


def test_single_spark_warns_and_dual_spark_does_not(settings):
    estimate, ok, warning = fit.fit_check(None, 235.0, "nvfp4", "single-spark", settings=settings)
    assert ok == 0
    assert warning and "exceeds" in warning

    estimate, ok, warning = fit.fit_check(None, 235.0, "nvfp4", "dual-spark", settings=settings)
    assert ok == 1
    assert warning is None


def test_unknown_params_or_quant_gives_null(settings):
    assert fit.fit_check(None, None, "nvfp4", "dual-spark", settings=settings)[1] is None
    assert fit.fit_check(None, 235.0, None, "dual-spark", settings=settings)[1] is None
    assert fit.fit_check(None, 235.0, "nvfp4", "other", settings=settings)[1] is None


def test_stated_memory_with_works_status_is_ok(settings):
    _estimate, ok, warning = fit.fit_check(
        None, 235.0, "nvfp4", "single-spark", memory_gb=110.0, status="works", settings=settings
    )
    assert ok == 1
    assert warning is None


def test_bits_table(settings):
    assert fit.bits_per_weight("bf16") == 16.0
    assert fit.bits_per_weight("fp8") == 8.5
    assert fit.bits_per_weight("gguf-q4") == 4.8
    assert fit.bits_per_weight("unknown") is None
