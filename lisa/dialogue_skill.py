"""Compile the bundled conversation skill for an external model; no discovery or I/O tools."""

from hashlib import sha256
from pathlib import Path
from typing import Literal

SKILL_ROOT = Path(__file__).parent / "skills" / "natural-dialogue"
PRIVATE_SKILL_ROOT = Path(__file__).resolve().parents[1] / "runtime/skills/natural-dialogue"
SkillMode = Literal["core", "examples", "corpus"]
MAX_SKILL_BYTES = 12000


def compile_dialogue_skill(mode: SkillMode = "core") -> str:
    """Return model instructions, excluding discovery metadata and unresolved file links."""
    if mode not in {"core", "examples", "corpus"}:
        raise ValueError("Unknown dialogue skill mode")
    source = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    if not source.startswith("---\n") or "\n---\n" not in source[4:]:
        raise ValueError("Dialogue skill frontmatter missing")
    body = source.split("\n---\n", 1)[1]
    body = body.split("\n## 参考资源\n", 1)[0].strip()
    # External calls cannot follow local paths. References are explicitly attached by the host.
    if mode == "examples":
        body += "\n\n" + (SKILL_ROOT / "references" / "examples.md").read_text(encoding="utf-8")
    if mode == "corpus":
        body += "\n\n" + (PRIVATE_SKILL_ROOT / "references" / "corpus.md").read_text(
            encoding="utf-8"
        )
    if len(body.encode("utf-8")) > MAX_SKILL_BYTES:
        raise ValueError("Dialogue skill exceeds its input budget")
    return body


def dialogue_skill_fingerprint(mode: SkillMode = "core") -> str:
    text = compile_dialogue_skill(mode)
    return sha256(text.encode("utf-8")).hexdigest()


def review_guide() -> str:
    # Historical benchmark imports remain readable, but cannot load a removed resource.
    raise ValueError("Dialogue review was removed; historical experiments require frozen inputs")


def dialogue_guide_snapshot(mode: str, default: str) -> dict[str, str]:
    """Freeze the exact instructions so a retried task cannot switch skill versions."""
    text = default if mode == "off" else compile_dialogue_skill(mode)
    snapshot = {"mode": mode, "text": text, "sha256": sha256(text.encode("utf-8")).hexdigest()}
    return snapshot
