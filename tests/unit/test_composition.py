from __future__ import annotations

from pathlib import Path

from videoedit.services.composition import _duration_frames_from_manifest


class _ProbeAdapter:
    def probe(self, _path: Path) -> dict[str, object]:
        return {"streams": [{"codec_type": "video", "duration": "1.000000"}]}


class _Remotion:
    ffmpeg = _ProbeAdapter()


def test_composition_derives_integer_frames_from_decoded_video_duration(tmp_path: Path) -> None:
    frames = _duration_frames_from_manifest(
        {"actual_duration_us": 1_037_333},
        tmp_path / "candidate.mp4",
        numerator=30,
        denominator=1,
        remotion=_Remotion(),
    )

    assert frames == 30
