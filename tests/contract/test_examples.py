from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


def find_package_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "examples" / "index.json").is_file():
            return candidate
    raise RuntimeError("Could not find examples/index.json")


def test_all_examples_validate() -> None:
    package_root = find_package_root(Path(__file__).resolve().parent)
    example_dir = package_root / "examples"
    index = json.loads((example_dir / "index.json").read_text(encoding="utf-8"))
    for item in index["examples"]:
        example = json.loads((example_dir / item["example"]).read_text(encoding="utf-8"))
        schema = json.loads((example_dir / item["schema"]).resolve().read_text(encoding="utf-8"))
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(example)
