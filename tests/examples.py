"""Synthetic protocol fixtures shared by codec and contract tests."""

from dataclasses import dataclass
import json
from pathlib import Path

from lancollab.common.contracts import Envelope


@dataclass(frozen=True)
class ExampleCase:
    direction: str
    message: Envelope


def example_cases() -> dict[str, ExampleCase]:
    path = Path(__file__).parent / "fixtures" / "sample-messages.json"
    return {
        name: ExampleCase(case["direction"], Envelope(**case["message"]))
        for name, case in json.loads(path.read_text(encoding="utf-8")).items()
    }
