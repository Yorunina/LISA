"""Export the private LISA skill without configuration, credentials, or the full chat corpus."""

import argparse
import json
import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lisa.dialogue_skill import (  # noqa: E402
    PRIVATE_SKILL_ROOT,
    SKILL_ROOT,
    compile_dialogue_skill,
    dialogue_skill_fingerprint,
)


def export(
    evaluation: Path, *, allow_partial: bool = False, compatibility: Path | None = None
) -> Path:
    evaluation = evaluation.resolve()
    evaluation.relative_to(ROOT / "runtime/reports")
    report = json.loads(evaluation.read_text(encoding="utf-8"))
    if report["status"] != "completed" and not (
        allow_partial and report["status"] == "partial" and report.get("finished_at_utc")
    ):
        raise ValueError("Export requires a finished evaluation; partial coverage must be explicit")
    prompt = "你是LISA，正在和熟人用中文交流。\n" + compile_dialogue_skill("review")
    if sha256(prompt.encode()).hexdigest() != report["prompt_hashes"]["review"]:
        raise ValueError("Current skill differs from evaluated instructions")
    review = (SKILL_ROOT / "references/review.md").read_bytes()
    if sha256(review).hexdigest() != report["review_guide_sha256"]:
        raise ValueError("Current review differs from evaluated instructions")
    source = (
        (SKILL_ROOT / "SKILL.md")
        .read_text(encoding="utf-8")
        .replace(
            "需要对照回应选择时，读取 [对照示例](references/examples.md)。"
            "例子是原创示范，不套台词，不移植其中事实。",
            "需要对照接话选择时，读取 [真实交流样本](references/corpus.md)。"
            "这些是经授权的私人资源，不移植其中事实。",
        )
    )
    files = {
        "SKILL.md": source.encode(),
        "references/corpus.md": (PRIVATE_SKILL_ROOT / "references/corpus.md").read_bytes(),
        "references/review.md": review,
        "corpus_sources.json": (PRIVATE_SKILL_ROOT / "corpus_sources.json").read_bytes(),
    }
    provenance = json.loads(files["corpus_sources.json"])
    corpus = PRIVATE_SKILL_ROOT / "references/corpus.md"
    assert (
        provenance["corpus_sha256"]
        == sha256(corpus.read_text(encoding="utf-8").encode()).hexdigest()
    )
    provenance.update(
        corpus_sha256_basis="UTF-8 after universal newline normalization",
        corpus_raw_sha256=sha256(files["references/corpus.md"]).hexdigest(),
    )
    files["corpus_sources.json"] = json.dumps(provenance, ensure_ascii=False, indent=2).encode()
    manifest = {
        "name": "natural-dialogue",
        "private": True,
        "acceptance": "criteria_met"
        if report["summary"]["criteria_met"]
        else "experimental_candidate",
        "entrypoint": "SKILL.md",
        "mode": "review",
        "provider_calls_per_turn": 2,
        "compiled_core_and_corpus_bytes": len(compile_dialogue_skill("review").encode()),
        "instructions_and_review_sha256": dialogue_skill_fingerprint("review"),
        "files_sha256": {k: sha256(v).hexdigest() for k, v in files.items()},
        "lisa_host": {
            "compiler": "lisa/dialogue_skill.py",
            "runner": "lisa/dialogue_skill_review.py",
            "authoritative_core": "lisa/skills/natural-dialogue/SKILL.md",
            "authoritative_review": "lisa/skills/natural-dialogue/references/review.md",
            "private_resource": "runtime/skills/natural-dialogue/references/corpus.md",
            "enable_setting": "LISA_DIALOGUE_SKILL_MODE=review",
            "output_setting": "LISA_DIALOGUE_SKILL_OUTPUT_TOKENS="
            + str(report["generation_output_tokens"]),
            "currently_enabled_by_export": False,
            "private_SKILL_is_export_snapshot": True,
        },
        "evaluation": {
            "experiment": report.get("experiment"),
            "status": report["status"],
            "run_id": report["run_id"],
            "scope": report.get("validation_scope"),
            "model": report["model"],
            "reasoning": report["reasoning"],
            "protocol": report.get("protocol"),
            "summary": report["summary"],
            "source": str(evaluation.relative_to(ROOT)),
            "source_sha256": sha256(evaluation.read_bytes()).hexdigest(),
            "independent_human_acceptance": False,
        },
    }
    if compatibility is not None:
        compatibility = compatibility.resolve()
        compatibility.relative_to(ROOT / "runtime/reports")
        check = json.loads(compatibility.read_text(encoding="utf-8"))
        assert check.get("finished_at_utc") and check["model"] == report["model"]
        manifest["latest_compatibility"] = {
            "source": str(compatibility.relative_to(ROOT)),
            "source_sha256": sha256(compatibility.read_bytes()).hexdigest(),
            "status": check["status"],
            "tasks": len(check["probes"]),
            "parsed_tasks": sum(p["parsed"] for p in check["probes"]),
            "calls": len(check["calls"]),
            "failures": [c["error"] for c in check["calls"] if c["state"] != "succeeded"],
            "parser_sha256": check["source_sha256"].get("lisa\\structured_output.py"),
        }
    files["deployment.json"] = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    output = (
        ROOT
        / "runtime/exports"
        / ("natural-dialogue-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + ".zip")
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "x", compression=ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr("natural-dialogue/" + name, content)
    with ZipFile(output) as archive:
        assert archive.testzip() is None
        for name, content in files.items():
            assert archive.read("natural-dialogue/" + name) == content
    output.with_suffix(".sha256").write_text(sha256(output.read_bytes()).hexdigest() + "\n")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evaluation", type=Path)
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--compatibility", type=Path)
    args = parser.parse_args()
    print(
        export(args.evaluation, allow_partial=args.allow_partial, compatibility=args.compatibility)
    )
