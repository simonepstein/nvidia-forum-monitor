from __future__ import annotations

import re
import sqlite3

QUANT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bnv[\s_-]?fp4\b|\bnvfp4\b", re.I), "nvfp4"),
    (re.compile(r"\bmx[\s_-]?fp4\b|\bmxfp4\b", re.I), "mxfp4"),
    (re.compile(r"\bfp4\b", re.I), "fp4"),
    (re.compile(r"\bfp8\b|fp8[_\-]?e4m3|fp8[_\-]?dynamic|w8a8", re.I), "fp8"),
    (re.compile(r"\bbf16\b|bfloat16", re.I), "bf16"),
    (re.compile(r"\bfp16\b|float16|half\b", re.I), "fp16"),
    (re.compile(r"\bint8\b|w8a16", re.I), "int8"),
    (re.compile(r"\bawq\b", re.I), "int4-awq"),
    (re.compile(r"\bgptq\b", re.I), "int4-gptq"),
    (re.compile(r"gguf.*q8_0|q8_0.*gguf|\bq8_0\b", re.I), "gguf-q8"),
    (re.compile(r"gguf.*q6|\bq6_k\b", re.I), "gguf-q6"),
    (re.compile(r"gguf.*q5|\bq5_k", re.I), "gguf-q5"),
    (re.compile(r"gguf.*q4|\bq4_k|\bq4_0\b", re.I), "gguf-q4"),
    (re.compile(r"gguf.*q3|\bq3_k", re.I), "gguf-q3"),
    (re.compile(r"gguf.*q2|\bq2_k", re.I), "gguf-q2"),
    (re.compile(r"\bexl2\b|\bexl3\b|\bexl\b", re.I), "exl"),
    (re.compile(r"\bint4\b|\b4bit\b|4-bit", re.I), "other"),
    (re.compile(r"\bgguf\b", re.I), "gguf-q4"),
)

ENGINE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"vllm", re.I), "vllm"),
    (re.compile(r"sglang", re.I), "sglang"),
    (re.compile(r"trt[\s_-]?llm|tensorrt[\s_-]?llm", re.I), "trtllm"),
    (re.compile(r"llama[\s._-]?(cpp|server)|llama\.cpp", re.I), "llamacpp"),
    (re.compile(r"ollama", re.I), "ollama"),
    (re.compile(r"lm[\s_-]?studio", re.I), "lmstudio"),
    (re.compile(r"\bnim\b", re.I), "nim"),
    (re.compile(r"exllama", re.I), "exllama"),
    (re.compile(r"transformers", re.I), "transformers"),
)

BENCHMARK_ALIASES: dict[str, str] = {
    "humaneval": "humaneval",
    "human eval": "humaneval",
    "humaneval+": "humaneval+",
    "humaneval plus": "humaneval+",
    "mbpp": "mbpp",
    "mbpp+": "mbpp+",
    "livecodebench": "livecodebench",
    "live code bench": "livecodebench",
    "swe-bench": "swe-bench-verified",
    "swebench": "swe-bench-verified",
    "swe-bench-verified": "swe-bench-verified",
    "aider": "aider-polyglot",
    "aider-polyglot": "aider-polyglot",
    "bfcl": "bfcl",
    "tau2": "tau2-bench",
    "tau2-bench": "tau2-bench",
    "tau-bench": "tau2-bench",
    "gpqa": "gpqa-diamond",
    "gpqa-diamond": "gpqa-diamond",
    "mmlu": "mmlu-pro",
    "mmlu-pro": "mmlu-pro",
    "aime": "aime",
    "ifeval": "ifeval",
    "ruler": "ruler",
}

TOPOLOGIES = ("single-spark", "dual-spark", "other", "unknown")

_QUANT_SUFFIX_RE = re.compile(
    r"[\s._-]*(bf16|fp16|fp8|nvfp4|mxfp4|fp4|int8|int4|awq|gptq|gguf|exl2|exl3|instruct|chat|it|"
    r"base|q[2-8]_[k0-9]|gguf-q[2-8])\s*$",
    re.I,
)


def clean_model_key(value: str) -> str:
    return re.sub(r"[\s_-]+", "-", (value or "").strip().lower()).strip("-")


def strip_quant_suffixes(value: str) -> str:
    current = value
    while True:
        stripped = _QUANT_SUFFIX_RE.sub("", current)
        if stripped == current or not stripped:
            return current
        current = stripped


def loose_key(value: str) -> str:
    return re.sub(r"[\s._-]+", "-", (value or "").strip().lower()).strip("-")


def _alias_lookup(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT canonical FROM model_aliases WHERE alias=?", (key,)).fetchone()
    if row is not None:
        return row["canonical"]
    loose = loose_key(key)
    for alias, canonical in conn.execute("SELECT alias, canonical FROM model_aliases"):
        if loose_key(alias) == loose:
            return canonical
    return None


def _model_lookup(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT canonical FROM models WHERE canonical=?", (key,)).fetchone()
    if row is not None:
        return row["canonical"]
    loose = loose_key(key)
    for (canonical,) in conn.execute("SELECT canonical FROM models"):
        if loose_key(canonical) == loose:
            return canonical
    return None


def canonical_model(conn: sqlite3.Connection, raw: str) -> tuple[str, bool]:
    key = clean_model_key(raw)

    canonical = _alias_lookup(conn, key)
    if canonical is not None:
        return canonical, False

    stripped = strip_quant_suffixes(key)
    if stripped != key:
        canonical = _alias_lookup(conn, stripped)
        if canonical is not None:
            return canonical, False
        key = stripped

    canonical = _model_lookup(conn, key)
    if canonical is not None:
        return canonical, False
    return key, True


def canonical_quant(value: str | None) -> str | None:
    if not value:
        return None
    for pattern, canonical in QUANT_PATTERNS:
        if pattern.search(value):
            return canonical
    return "other"


def canonical_engine(value: str | None) -> str | None:
    if not value:
        return None
    for pattern, canonical in ENGINE_PATTERNS:
        if pattern.search(value):
            return canonical
    return "other"


def canonical_benchmark(value: str) -> str:
    key = re.sub(r"\s+", " ", (value or "").strip().lower())
    return BENCHMARK_ALIASES.get(key, key)


def canonical_topology(value: str | None) -> str:
    if not value:
        return "unknown"
    key = value.strip().lower().replace("_", "-").replace(" ", "-")
    return key if key in TOPOLOGIES else "other"
