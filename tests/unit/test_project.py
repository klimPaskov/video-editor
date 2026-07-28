from pathlib import Path

from videoedit.services.project import ingest_source, initialize_project, sha256_file


class FakeFFmpegAdapter:
    ffprobe_path = "ffprobe"

    def probe(self, _path: Path) -> dict[str, object]:
        return {
            "format": {
                "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                "duration": "1.000000",
                "bit_rate": "8000",
            },
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "time_base": "1/30",
                    "start_time": "0",
                    "duration": "1",
                    "width": 320,
                    "height": 180,
                    "avg_frame_rate": "30/1",
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "time_base": "1/48000",
                    "start_time": "0",
                    "duration": "1",
                    "sample_rate": "48000",
                    "channels": 2,
                },
            ],
        }

    def version(self, _executable: str | None = None) -> str:
        return "test"


def test_project_init_and_ingest_are_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"media")
    layout = initialize_project(tmp_path, "demo")
    adapter = FakeFFmpegAdapter()
    first = ingest_source(layout, source, adapter=adapter)
    second = ingest_source(layout, source, adapter=adapter)
    assert first["sha256"] == second["sha256"]
    stored = Path(str(first["managed_path"]))
    assert stored.is_file()
    assert sha256_file(stored) == first["sha256"]
