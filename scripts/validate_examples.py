from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


def find_package_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "examples" / "index.json").is_file():
            return candidate
    raise RuntimeError("Could not find examples/index.json")


PACKAGE_ROOT = find_package_root(Path(__file__).resolve().parent)
EXAMPLE_DIR = PACKAGE_ROOT / "examples"


def main() -> None:
    index = json.loads((EXAMPLE_DIR / "index.json").read_text(encoding="utf-8"))
    failures: list[str] = []
    for item in index["examples"]:
        example_path = EXAMPLE_DIR / item["example"]
        schema_path = (EXAMPLE_DIR / item["schema"]).resolve()
        example = json.loads(example_path.read_text(encoding="utf-8"))
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        errors = sorted(validator.iter_errors(example), key=lambda error: list(error.path))
        for error in errors:
            location = ".".join(str(part) for part in error.path) or "root"
            failures.append(f"{example_path.name}:{location}: {error.message}")
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"Validated {len(index['examples'])} examples")


if __name__ == "__main__":
    main()
