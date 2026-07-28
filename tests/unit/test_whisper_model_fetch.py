from __future__ import annotations

from pathlib import Path

SMALL_SHA256 = "9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794"


def test_whisper_fetch_helpers_pin_the_official_small_model() -> None:
    root = Path(__file__).resolve().parents[2]
    powershell = (root / "scripts" / "fetch-whisper-model.ps1").read_text(encoding="utf-8")
    shell = (root / "scripts" / "fetch-whisper-model.sh").read_text(encoding="utf-8")

    for script in (powershell, shell):
        assert SMALL_SHA256 in script
        assert "openaipublic.azureedge.net/main/whisper/models" in script
        assert "small.pt" in script
        assert "sha256" in script.lower()

    assert (
        "Move-Item -LiteralPath $staged -Destination $resolvedDestination -Force" not in powershell
    )
    assert "curl.exe" in powershell
    assert '--proto "=https"' in powershell
    assert "--max-time 1800" in powershell
    assert "Invoke-WebRequest" not in powershell
    assert "mv --" in shell
