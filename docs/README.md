# Documentation

Start here based on what you want to do:

| Goal | Read |
| --- | --- |
| Install and run VideoEdit | [`31_user_quickstart.md`](31_user_quickstart.md) |
| Use the Windows installer | [`../INSTALL.md`](../INSTALL.md) |
| Copy a realistic prompt | [`../examples/input-prompts/README.md`](../examples/input-prompts/README.md) |
| Run or recover a project | [`11_runbook.md`](11_runbook.md) |
| Understand commands | [`16_cli_contract.md`](16_cli_contract.md) |
| Understand the implementation | [`02_architecture.md`](02_architecture.md) |
| Review accepted project decisions | [`adr/`](adr/) |

The supported public workflow is for screen recordings. It runs locally with
Python 3.11, FFmpeg/ffprobe, local Whisper, and Node.js 22 with Remotion.
Source footage stays outside Git, and project data is kept in the ignored
workspace.
