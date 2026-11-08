from __future__ import annotations

import sqlite3

from .config import Settings, get_settings

BITS_PER_WEIGHT: dict[str, float] = {
    "bf16": 16.0,
    "fp16": 16.0,
    "fp8": 8.5,
    "int8": 8.5,
    "nvfp4": 4.5,
    "mxfp4": 4.5,
    "fp4": 4.5,
    "int4-awq": 4.5,
    "int4-gptq": 4.5,
    "gguf-q8": 8.5,
    "gguf-q6": 6.6,
    "gguf-q5": 5.7,
    "gguf-q4": 4.8,
    "gguf-q3": 3.9,
    "gguf-q2": 3.0,
    "exl": 4.5,
}

USABLE_GB = {"single-spark": "single_spark_usable_gb", "dual-spark": "dual_spark_usable_gb"}


def bits_per_weight(quant_format: str | None) -> float | None:
    if not quant_format:
        return None
    return BITS_PER_WEIGHT.get(quant_format)


def estimate_gb(
    params_total_b: float | None,
    quant_format: str | None,
    settings: Settings | None = None,
) -> float | None:
    settings = settings or get_settings()
    bits = bits_per_weight(quant_format)
    if params_total_b is None or bits is None:
        return None
    return round(params_total_b * bits / 8.0 * (1.0 + settings.hardware.kv_overhead_frac), 1)


def usable_gb(topology: str, settings: Settings | None = None) -> float | None:
    settings = settings or get_settings()
    attribute = USABLE_GB.get(topology)
    return getattr(settings.hardware, attribute) if attribute else None


def fit_check(
    conn: sqlite3.Connection,
    params_total_b: float | None,
    quant_format: str | None,
    topology: str,
    memory_gb: float | None = None,
    status: str | None = None,
    settings: Settings | None = None,
) -> tuple[float | None, int | None, str | None]:
    settings = settings or get_settings()
    estimate = estimate_gb(params_total_b, quant_format, settings)
    limit = usable_gb(topology, settings)
    if memory_gb is not None and status == "works":
        return estimate, 1, None
    if estimate is None or limit is None:
        return estimate, None, None
    ok = 1 if estimate <= limit else 0
    if ok:
        return estimate, ok, None
    warning = (
        f"fit warning: {estimate} GB estimated for {topology} "
        f"({params_total_b}B at {quant_format}) exceeds the {limit} GB usable"
    )
    return estimate, ok, warning
