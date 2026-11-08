from __future__ import annotations

from dataclasses import dataclass
from typing import Any

INSTRUCTIONS = (
    "sparkwatch keeps you current on the NVIDIA DGX Spark / GB10 developer forums and on the "
    "GitHub repos you watch. It publishes four workflows: catch_up, serve_model, "
    "should_i_update and frontier. Call get_workflow(name) for the steps, or list_workflows() "
    "to see them with their arguments. A catch-up starts with sync_now, then digest."
)


@dataclass(frozen=True)
class Workflow:
    name: str
    purpose: str
    arguments: tuple[tuple[str, str], ...]
    template: str

    def render(self, args: dict[str, Any] | None = None) -> str:
        values = {key: default for key, default in self.arguments}
        for key, value in (args or {}).items():
            if key not in values:
                known = ", ".join(values) or "none"
                raise ValueError(f"unknown argument for {self.name}: {key} (known: {known})")
            values[key] = str(value)
        values["repo_label"] = values.get("repo") or "all watched repos"
        return self.template.format(**values)

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "arguments": [{"name": key, "default": default} for key, default in self.arguments],
            "usage": f"get_workflow('{self.name}')",
        }


WORKFLOWS: dict[str, Workflow] = {
    "catch_up": Workflow(
        name="catch_up",
        purpose="Catch up on the DGX Spark forums: sync, digest, read, flag, record.",
        arguments=(),
        template=(
            "Catch up on the NVIDIA DGX Spark / GB10 forums.\n"
            "1. Call sync_now, and call it again while it returns partial=true.\n"
            "2. Call digest to see what changed since your last look.\n"
            "3. For promising items, read them with get_posts(last_n=...) or get_post.\n"
            "4. Flag what matters with flag: new models or quants, performance claims, breaking"
            " engine changes, new recipes, watch sightings. For anticipated models, classify each"
            " hit as rumour or release, and flag only releases.\n"
            "5. Record clear recipes and benchmarks with record_recipe / record_benchmark, quoting"
            " the posts verbatim.\n"
            "6. Reply with a short briefing and a source link for every claim."
        ),
    ),
    "serve_model": Workflow(
        name="serve_model",
        purpose="Work out the best way to serve a model on DGX Spark hardware.",
        arguments=(("model", "GLM 5.3 Flash"), ("topology", "dual-spark")),
        template=(
            "What is the best way to serve {model} on {topology} today?\n"
            "1. Call model_summary and get_notes first, so you build on earlier conclusions.\n"
            "2. Call whats_new_for for the model, since the latest note's as_of.\n"
            "3. Read the newest relevant posts first with get_posts / get_post. Prefer recent"
            " reports, multiple users and accepted answers. Watch for contradictions and note"
            " which engine versions were used.\n"
            "4. Answer with the recommended quant, engine and version, container, launch command"
            " and flags, expected tok/s, gotchas, alternatives, and a source link and date per"
            " claim.\n"
            "5. Then call record_recipe / record_benchmark with verbatim quotes, and save_note"
            " (superseding the previous note)."
        ),
    ),
    "should_i_update": Workflow(
        name="should_i_update",
        purpose="Decide whether to update a watched repo.",
        arguments=(("repo", ""), ("label", "default")),
        template=(
            "Should I update {repo_label} (install label {label})?\n"
            "1. Call repo_status.\n"
            "2. For each repo that is behind, read the commit subjects and release notes. Pick out"
            " fixes and features relevant to GB10/Spark, your models and your engine; note breaking"
            " changes, dependency or CUDA bumps, and reverts.\n"
            "3. Read the forum posts mentioning the repo since the installed date, looking for"
            " reports that newer commits fix or break things.\n"
            "4. Recommend one of: update now to <sha or tag>, wait for <reason>, or no relevant"
            " changes. Give the reasoning and links.\n"
            "5. flag anything urgent, and save_note(subject='update check <repo>')."
        ),
    ),
    "frontier": Workflow(
        name="frontier",
        purpose="Ask what the strongest models on two Sparks are right now.",
        arguments=(("topology", "dual-spark"),),
        template=(
            "What are the strongest models people run on {topology} right now?\n"
            "1. Call list_watchlist, list_recipes(status='works'), list_benchmarks and get_notes.\n"
            "2. Fill gaps with find_model_mentions for heavily mentioned models with no recipes"
            " yet.\n"
            "3. Answer with a ranked list and its reasoning, showing quant and benchmark evidence.\n"
            "4. Save the conclusion with save_note(subject='frontier {topology}')."
        ),
    ),
}


def list_workflows() -> list[dict[str, Any]]:
    return [workflow.summary() for workflow in WORKFLOWS.values()]


def get_workflow(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    workflow = WORKFLOWS.get((name or "").strip())
    if workflow is None:
        raise ValueError(f"unknown workflow: {name}; known workflows: {', '.join(WORKFLOWS)}")
    return {
        "name": workflow.name,
        "purpose": workflow.purpose,
        "steps": workflow.render(args),
    }
