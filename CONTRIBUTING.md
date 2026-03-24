# Contributing to TAP

Thanks for helping improve TAP.

## Development Setup

```bash
uv sync --extra dev
uv run tappl --help
```

If you want to use the local compatibility launcher:

```bash
uv run python main.py --help
```

## Before Opening a Pull Request

Please make sure the following commands succeed:

```bash
uv run python -m py_compile main.py src/tap_player/app.py
uv run --extra dev python -m build
uv run tappl --help
```

## Guidelines

- keep changes cross-platform unless a platform-specific behavior is intentionally guarded
- avoid introducing Windows-only dependencies unless there is a clear fallback
- keep the terminal UX keyboard-friendly and robust in narrow terminals
- update documentation when behavior or flags change
- prefer small, focused pull requests over large unrelated bundles

## Issues

Use the issue templates for:

- bug reports
- feature requests

Please include reproduction steps, platform details, and any decode backend information that may be relevant.

## Release Process

Releases are published from Git tags through GitHub Actions.

To cut a release:

1. Update `src/tap_player/_version.py`.
2. Commit the version bump.
3. Push a tag like `v0.1.0`.
4. Wait for the publish workflow to upload the package to PyPI.

The repository should be configured as a Trusted Publisher in PyPI before the first release.
