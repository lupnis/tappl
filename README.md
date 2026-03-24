# TAP: Terminal Audio Player

> <font color="red">This is a pure vibe-coding project, thus might be hard to maintain XOX</font>

> <font color="red">This project has only been tested on Windows yet.</font>

TAP is a Rich-based terminal audio player built for fast keyboard control, synchronized multi-track playback, and terminal-first workflows.

It can scan folders and files from the command line, render a tree-style library view, switch between solo and together playback strategies, and optionally use `ffmpeg` as the decode backend.

## Highlights

- terminal UI rendered with Rich
- grouped library tree for mixed `--files` inputs
- synchronized `together` playback with shared transport
- solo and together strategy families with runtime switching
- optional `ffmpeg` backend with automatic fallback to `miniaudio`
- keyboard-first controls for Windows, Linux, and macOS
- publishable Python package with a `tappl` command

## Installation

Install from PyPI:

```bash
pip install tappl
```

Then run:

```bash
tappl --help
python -m tappl --help
```

If you prefer isolated CLI installs:

```bash
pipx install tappl
tappl --help
```

Install from source:

```bash
git clone <your-repo-url>
cd tap
uv sync --extra dev
uv run tappl --help
```

For local development, the compatibility launcher still works:

```bash
uv run python main.py --help
```

TAP pins `uv` to free-threaded CPython `3.14t` via [`.python-version`](.python-version). CI, PyPI builds, and Windows EXE releases are all expected to run on that interpreter variant.

## Windows EXE

TAP also ships with a GitHub Actions workflow that builds a standalone Windows executable.

- Run the `Build Windows EXE` workflow manually to get a downloadable Actions artifact.
- Push a version tag like `v0.1.0` to build `tappl.exe` and attach a `.zip` archive to the GitHub release.
- The archive contains `tappl.exe`, `README.md`, and `LICENSE`.
- The workflow builds with uv-managed free-threaded CPython `3.14t`.

To build the executable locally on Windows:

```bash
uv run --with pyinstaller pyinstaller --noconfirm --clean tap.spec
dist/tappl.exe --help
```

## Quick Start

```bash
tappl --files "~/Music/Drums" "~/Music/bass.wav" --strategy together --time auto-max
```

Common examples:

```bash
tappl --files ./stems ./vox.wav
tappl --files ./album --generic-mode solo --strategy playlist-once
tappl --files ./stems --decode-backend auto
tappl --files ./stems --arrow-mode hjkl
```

## Features

- `--files` accepts a mixed list of audio files and folders
- folders are scanned recursively and grouped under source nodes
- direct single-file inputs are collected under one `Single Files` node
- global play, pause, stop, reverse, seek, and volume controls stay global
- `--generic-mode` limits the available strategy family to `solo`, `together`, or `both`
- `--strategy` supports `playlist-once`, `track-loop`, `track-once`, `playlist-loop`, `random`, `together`, and `together-loop`
- `--time` controls the shared transport length for `together` and `together-loop`
- `--decode-backend` supports `auto`, `miniaudio`, and `ffmpeg`
- `--disable-*` flags can hide risky controls and disable the matching key paths

## Platform Notes

- keyboard input is supported on Windows, Linux, and macOS
- the current UI is intentionally keyboard-only for better terminal compatibility
- Linux and macOS use `termios` / `tty` / `select`
- Windows uses guarded Win32 keyboard APIs
- `ffmpeg` decoding is optional and only requires the `ffmpeg` executable on `PATH`
- if your terminal does not send `F1` reliably, use `U` for help

## Supported Formats

`wav`, `mp3`, `flac`, `ogg`, `vorbis`

Actual decode support depends on the active backend. `ffmpeg` generally handles more real-world edge cases.

## CLI Overview

- `--files PATH [PATH ...]`
  Mix audio files and folders in one command.
- `--generic-mode solo|together|both`
  Limits the available strategy family.
- `--strategy MODE`
  Sets the initial strategy inside the selected generic mode.
- `--time auto-max|auto-min`
  Shared transport policy for `together` and `together-loop`.
- `--decode-backend auto|miniaudio|ffmpeg`
  Chooses the decode backend. `auto` prefers `ffmpeg` when available.
- `--arrow-mode hjkl|arrows|both`
  Chooses directional input style.
- `--forward-seconds` / `--backward-seconds`
  Seek step sizes.
- `--rate-fast-forward` / `--rate-slow-forward`
  Hold playback rates.
- `--disable-forward`
- `--disable-fast-forward`
- `--disable-backward`
- `--disable-fast-backward`
- `--disable-reverse`
- `--disable-strategy-change`
- `--disable-list-modify`

Run `tappl --help` for the full argument list.

## Runtime Controls

- `P`: global play or pause
- `S`: global stop
- `D`: tap to cycle strategy within the active generic mode, hold to open the mode picker
- `Enter`: context action for the focused row
- `Space`: context action, or hold for fast playback
- `Left/Right` or `H/L`: seek, depending on `--arrow-mode`
- `B`: toggle reverse playback
- `Backspace` / `Delete`: open a confirmation dialog and remove the focused track from the current playlist
- `M`: toggle track mute in `together` modes
- `G`: toggle group mute in `together` modes
- `T`: tap to cycle `together` time mode
- `+` / `-`: adjust volume
- `U` / `F1`: show help
- `Esc` / `Ctrl+C` / `Q`: quit

If the focused solo track is currently playing, opening the delete confirmation pauses playback until you confirm or cancel. Empty source folders disappear automatically when their last track is removed from the active playlist.

## Development

Install the project in editable mode:

```bash
uv sync --extra dev
```

Useful commands:

```bash
uv run python -m py_compile main.py src/tap_player/app.py
uv run --extra dev python -m build
uv run tappl --help
uv run python -m tappl --help
```

Please keep changes cross-platform when possible. Avoid introducing Windows-only dependencies unless they are fully guarded and optional.
For CI, releases, and local `uv` workflows, prefer the pinned free-threaded CPython `3.14t`.

## Project Layout

```text
src/tap_player/     Python package
main.py             local compatibility launcher
.github/            issue templates and GitHub Actions
README.md           project overview and usage
CONTRIBUTING.md     contributor workflow
CODE_OF_CONDUCT.md  community expectations
LICENSE             project license
```

## Release Workflow

TAP ships with GitHub Actions for CI, PyPI publishing, and Windows executable packaging.

For a release:

1. Update `src/tap_player/_version.py`.
2. Commit the change.
3. Create and push a tag like `v0.1.0`.
4. The publish workflow builds the Python distribution and uploads it to PyPI.
5. The Windows EXE workflow builds `tappl.exe`, uploads a workflow artifact, and attaches a release archive on tag builds.
6. All release automation runs on uv-managed free-threaded CPython `3.14t`.

The PyPI workflow is configured for Trusted Publishing. Before the first release, configure your PyPI project to trust this GitHub repository and workflow.

## Community

- Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.
- Please follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) in all project spaces.
- Use the GitHub issue templates for bugs and feature requests.

## License

TAP is released under the [MIT License](LICENSE).
