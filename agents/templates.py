"""The AI agents' prompt templates (protocol §8, v1.15), loaded from agents/prompts/.

    from agents.templates import load, template_hashes
    template = load("8")            # equity_common.md + "\\n\\n" + goal_autonomous.md
    python -m agents.templates      # agent, files, SHA-256: the table listed in the protocol at the freeze

Assembled exactly as agents/prompts/README.md says: each file read as UTF-8 and
stripped of trailing whitespace, parts joined with a blank line. The harness
adds the output spec and the input (agents/harness.build_prompt). Agent 13
(the blender) is not wired in yet. Rule 3: frozen from Mon 11 Jan 2027.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

_COMMON = "equity_common.md"
TEMPLATE_FILES: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "7": (_COMMON, "goal_constrained.md"),
    **{a: (_COMMON, "goal_autonomous.md") for a in ("8", "9", "10", "11", "12")},
    "14": (_COMMON, "goal_cautious.md"),
    "15": (_COMMON, "goal_aggressive.md"),
    "C1": ("crypto_c1.md",),
})


def _files(agent_id: int | str) -> tuple[str, ...]:
    try:
        return TEMPLATE_FILES[str(agent_id)]
    except KeyError:
        raise KeyError(f"no prompt template for agent {agent_id!r}; known: {sorted(TEMPLATE_FILES)}") from None


def load(agent_id: int | str) -> str:
    """The agent's template: its files, UTF-8, each rstripped, joined with "\\n\\n"."""
    return "\n\n".join((PROMPTS_DIR / f).read_text(encoding="utf-8").rstrip() for f in _files(agent_id))


def template_sha256(agent_id: int | str) -> str:
    return hashlib.sha256(load(agent_id).encode("utf-8")).hexdigest()


def template_hashes() -> dict[str, str]:
    return {a: template_sha256(a) for a in TEMPLATE_FILES}


def main() -> int:
    for a, h in template_hashes().items():
        print(f"{a:<4} {' + '.join(TEMPLATE_FILES[a]):<42} {h}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
