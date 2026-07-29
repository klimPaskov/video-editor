# Windows installer

`VideoEditInstaller.exe` is a no-admin bootstrapper for 64-bit Windows. It
downloads the pinned local runtime and media tools, installs managed Python
3.11, installs the locked Python and Remotion dependencies, downloads the
small local Whisper model, and creates `VideoEdit.cmd` in the install folder.

The installer defaults to `%LOCALAPPDATA%\VideoEdit`. It can be run again to
reuse verified downloads.

```text
VideoEditInstaller.exe
VideoEditInstaller.exe --install-dir D:\Tools\VideoEdit
VideoEditInstaller.exe --dry-run
```

The installer downloads external software from its upstream publishers. See
[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) for the relevant terms.
