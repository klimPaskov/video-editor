#!/usr/bin/env python3
"""Check or safely apply the version 2.2.1 video-editing planning overlay."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import sys
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", help="Path to the active repository root")
    parser.add_argument("--apply", action="store_true", help="Copy files after preflight")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Back up and replace non-root locally modified payload files",
    )
    args = parser.parse_args()

    overlay_root = Path(__file__).resolve().parents[1]
    target = Path(args.target).expanduser().resolve()
    meta = json.loads((Path(__file__).with_name("OVERLAY_BASE_HASHES.json")).read_text())

    if not target.is_dir():
        print(f"Target is not a directory: {target}", file=sys.stderr)
        return 2

    conflicts: list[tuple[str, str]] = []
    states: list[tuple[str, str]] = []
    for item in meta["files"]:
        rel = item["path"]
        dest = target / rel
        required = bool(item.get("replace_even_if_modified"))
        if not dest.exists():
            states.append((rel, "add" if item["operation"] == "add" else "restore-missing"))
            continue
        if not dest.is_file():
            conflicts.append((rel, "target path is not a regular file"))
            continue
        current = digest(dest)
        if current == item["overlay_sha256"]:
            states.append((rel, "already-current"))
        elif required:
            states.append((rel, "required-root-replace"))
        elif current in item.get("accepted_base_sha256", []):
            states.append((rel, "safe-replace"))
        else:
            conflicts.append((rel, "locally modified or from an unsupported package revision"))

    print(f"Overlay {meta['overlay_version']} against {target}")
    for rel, state in states:
        print(f"  {state:22} {rel}")
    if conflicts:
        print("\nConflicts:")
        for rel, reason in conflicts:
            print(f"  CONFLICT               {rel}: {reason}")
        if not args.force:
            print(
                "\nNo files were changed. Reconcile conflicts or rerun with --force "
                "after creating a Git snapshot."
            )
            return 2

    if not args.apply:
        print("\nPreflight only. Add --apply to copy the overlay.")
        return 0

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_root = target / ".videoedit-update-backups" / f"v2.2.1-{stamp}"
    copied = 0
    backed_up = 0
    for item in meta["files"]:
        rel = item["path"]
        src = overlay_root / rel
        dest = target / rel
        if dest.is_file() and digest(dest) != item["overlay_sha256"]:
            backup = backup_root / rel
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, backup)
            backed_up += 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        if digest(dest) != item["overlay_sha256"]:
            raise RuntimeError(f"Hash verification failed after copying {rel}")
        copied += 1

    print(
        f"\nApplied {copied} payload files. Backed up {backed_up} existing files "
        f"under {backup_root}."
    )
    print(
        "Give FOLLOW_UP_PROMPT_ACTIVE_AGENT_2_2_1.md to the active Codex agent "
        "before resuming implementation."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
