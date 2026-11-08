from __future__ import annotations

import pytest

from sparkwatch import normalize


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("GLM-5.3-Flash", "glm-5.3-flash"),
        ("glm 5.3 flash", "glm-5.3-flash"),
        ("GLM_5.3_Flash", "glm-5.3-flash"),
        ("GLM-5.3-Flash-FP8", "glm-5.3-flash"),
        ("GLM-5.3-Flash-Instruct-NVFP4", "glm-5.3-flash"),
        ("DeepSeek V4.1 Flash", "deepseek-v4.1-flash"),
        ("Qwen3.8 Flash Next", "qwen3.8-flash-next"),
        ("Qwen4", "qwen4"),
    ],
)
def test_canonical_model_known(conn, raw, expected):
    canonical, is_new = normalize.canonical_model(conn, raw)
    assert canonical == expected
    assert is_new is False


def test_canonical_model_unknown_is_reported_as_new(conn):
    canonical, is_new = normalize.canonical_model(conn, "SomeNewModel-7B-NVFP4")
    assert canonical == "somenewmodel-7b"
    assert is_new is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("NVFP4", "nvfp4"),
        ("nv fp4", "nvfp4"),
        ("MXFP4", "mxfp4"),
        ("fp4", "fp4"),
        ("FP4", "fp4"),
        ("fp4 mixed-precision", "fp4"),
        ("fp8", "fp8"),
        ("FP8_E4M3", "fp8"),
        ("bf16", "bf16"),
        ("fp16", "fp16"),
        ("int8", "int8"),
        ("AWQ", "int4-awq"),
        ("gptq", "int4-gptq"),
        ("Q8_0", "gguf-q8"),
        ("q4_k_m", "gguf-q4"),
        ("q6_k", "gguf-q6"),
        ("exl3", "exl"),
        ("4bit", "other"),
        ("mystery", "other"),
        (None, None),
    ],
)
def test_canonical_quant(raw, expected):
    assert normalize.canonical_quant(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("vLLM 0.11.0", "vllm"),
        ("SGLang", "sglang"),
        ("TRT-LLM", "trtllm"),
        ("tensorrt-llm", "trtllm"),
        ("llama.cpp", "llamacpp"),
        ("llama-server", "llamacpp"),
        ("ollama", "ollama"),
        ("LM Studio", "lmstudio"),
        ("NIM", "nim"),
        ("ExLlamaV2", "exllama"),
        ("transformers", "transformers"),
        ("somethingelse", "other"),
        (None, None),
    ],
)
def test_canonical_engine(raw, expected):
    assert normalize.canonical_engine(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("SWE-bench", "swe-bench-verified"),
        ("swebench", "swe-bench-verified"),
        ("GPQA", "gpqa-diamond"),
        ("MMLU", "mmlu-pro"),
        ("tau-bench", "tau2-bench"),
        ("Humaneval", "humaneval"),
        ("Aider", "aider-polyglot"),
        ("my-custom-eval", "my-custom-eval"),
    ],
)
def test_canonical_benchmark(raw, expected):
    assert normalize.canonical_benchmark(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("dual-spark", "dual-spark"),
        ("Dual Spark", "dual-spark"),
        ("single_spark", "single-spark"),
        ("cluster", "other"),
        (None, "unknown"),
    ],
)
def test_canonical_topology(raw, expected):
    assert normalize.canonical_topology(raw) == expected
