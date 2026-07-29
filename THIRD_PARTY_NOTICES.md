# Third-party notices

The MIT licence in [`LICENSE`](LICENSE) applies to original code,
documentation, and examples in this repository. It does not re-license
anything installed or supplied from elsewhere.

- Python packages are selected in `pyproject.toml` and pinned by `uv.lock`.
- Remotion, React, Node packages, FFmpeg, and ffprobe retain their upstream
  licences and terms.
- Local Whisper software and checkpoints retain their upstream terms. The
  repository's helper downloads a pinned public checkpoint only after an
  operator invokes it.
- The Windows installer is open-source repository code. It downloads uv, Node.js,
  FFmpeg, and the Whisper checkpoint from the upstream URLs pinned in
  `installer/Program.cs`; those components retain their own licences and
  notices.
- Fonts, music, B-roll, backgrounds, logos, replacement objects, and other
  media are project inputs. They are not included here and must be supplied
  with permission for the intended use.
- Optional model experiments are not installed, invoked, or required by the
  public workflow. Any future use must pass its own upstream licence, checkpoint,
  hardware, and approval checks.

Before redistributing a rendered video, review the terms of every external
asset, model, font, and software component used by that project.
