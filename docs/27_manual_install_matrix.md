# Manual Install Matrix

The Windows installer is the recommended setup path. Manual setup requires:

| Component | Required version or source |
| --- | --- |
| Python | 3.11, managed by uv |
| Node.js | 22 |
| FFmpeg and ffprobe | A build with `libx264`, `aac`, `silencedetect`, and `overlay` |
| Whisper | Local `small` model or another locally supplied model |
| Remotion | Dependencies from `remotion/package-lock.json` |

Keep source media, model files, and project outputs in the local workspace.
Run `videoedit doctor` before the first project. The public workflow does not
install optional GPU or cloud workers.
