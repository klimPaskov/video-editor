from __future__ import annotations

from typer.testing import CliRunner

import videoedit.cli as cli


def test_public_cli_excludes_retired_media_feature_commands() -> None:
    result = CliRunner().invoke(cli.app, ["--help"])

    assert result.exit_code == 0, result.stdout
    for retired_command in (
        "encode-mask",
        "chroma-key",
        "validate-mask",
        "run-worker",
        "approve-worker-runtime",
        "prepare-matte",
        "review-matte-quality",
    ):
        assert retired_command not in result.stdout


def test_new_projects_default_to_screen_recording(tmp_path) -> None:
    layout = cli.initialize_project(tmp_path, "screen_scope")

    assert "recording_mode: screen_recording" in (layout.config / "project.yaml").read_text(
        encoding="utf-8"
    )
