from __future__ import annotations

import argparse
import functools
import math
import os
import random
import select
import shlex
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import miniaudio
import numpy as np
from rich import box
from rich.align import Align
from rich.columns import Columns
from rich.console import Console, Group, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.progress_bar import ProgressBar
from rich.segment import Segment
from rich.table import Table
from rich.text import Text

if os.name == "nt":
    import ctypes
    from ctypes import wintypes


SUPPORTED_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".vorbis"}
HEADER_HEIGHT = 0
CONTROL_HEIGHT_COMPACT = 5
CONTROL_HEIGHT_WRAPPED = 8
STATUS_HEIGHT = 8
TABLE_CHROME_HEIGHT = 4
HOLD_TAP_DELAY = 0.80
HOLD_RELEASE_DELAY = 0.16
STATUS_NOTE_TTL = 1.25
CONTROL_FLASH_TTL = 1.1
VOLUME_STEP = 0.05

RUNTIME_HELP_TEXT = """Runtime controls:
  Arrow mode        --arrow-mode hjkl|arrows|both (default: both)
  Decode backend    --decode-backend auto|miniaudio|ffmpeg (default: auto)
  Generic mode      --generic-mode solo|together|both (default: both)
  Up/Down or J/K    Move focus (depends on arrow mode)
  P                 Global play/pause
  S                 Global stop
  D                 Tap: cycle strategy | Hold: mode menu
  T                 Tap: cycle together time mode
  Enter             Context action for the focused track
  Space             Context action or hold for fast playback
  Left/Right/H/L    Seek on the forward timeline (depends on arrow mode)
  B                 Toggle reverse playback
  Backspace/Delete  Remove the focused track from the current playlist
  M                 Toggle track mute in TOGETHER modes
  G                 Toggle source mute in TOGETHER modes
  + / =             Volume up
  - / _             Volume down
  U / F1            Toggle the runtime help panel
  Esc / Ctrl+C / Q  Close popup or quit
"""


class OverlayRenderable:
    def __init__(self, base: RenderableType, overlay: RenderableType, *, x: int, y: int) -> None:
        self.base = base
        self.overlay = overlay
        self.x = max(0, x)
        self.y = max(0, y)

    def __rich_console__(self, console: Console, options):
        width = options.max_width
        base_lines = console.render_lines(self.base, options, pad=True)
        base_lines = Segment.set_shape(base_lines, width)

        if self.x < width:
            overlay_options = options.update_width(max(1, width - self.x)).reset_height()
            overlay_lines = console.render_lines(self.overlay, overlay_options, pad=False)
            overlay_width, _overlay_height = Segment.get_shape(overlay_lines)
            overlay_width = min(overlay_width, max(0, width - self.x))

            if overlay_width > 0:
                for line_index, overlay_line in enumerate(overlay_lines):
                    target_y = self.y + line_index
                    if target_y >= len(base_lines):
                        break
                    left, right = self._split_line(base_lines[target_y], self.x, overlay_width)
                    popup_line = Segment.adjust_line_length(overlay_line, overlay_width)
                    base_lines[target_y] = left + popup_line + right

        for index, line in enumerate(base_lines):
            yield from line
            if index != len(base_lines) - 1:
                yield Segment.line()

    @staticmethod
    def _split_line(line: list[Segment], start: int, width: int) -> tuple[list[Segment], list[Segment]]:
        line_length = Segment.get_line_length(line)
        pieces = list(Segment.divide(line, [start, start + width, line_length]))
        if len(pieces) == 1:
            return pieces[0], []
        if len(pieces) == 2:
            return pieces[0], []
        return pieces[0], pieces[2]

if os.name == "nt":
    STD_INPUT_HANDLE = -10
    KEY_EVENT = 0x0001
    WAIT_OBJECT_0 = 0x00000000
    VK_UP = 0x26
    VK_DOWN = 0x28
    VK_LEFT = 0x25
    VK_RIGHT = 0x27
    VK_RETURN = 0x0D
    VK_BACK = 0x08
    VK_DELETE = 0x2E
    VK_ESCAPE = 0x1B
    VK_F1 = 0x70
    VK_ADD = 0x6B
    VK_SUBTRACT = 0x6D

    class _COORD(ctypes.Structure):
        _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

    class _KEY_EVENT_RECORD(ctypes.Structure):
        _fields_ = [
            ("bKeyDown", wintypes.BOOL),
            ("wRepeatCount", wintypes.WORD),
            ("wVirtualKeyCode", wintypes.WORD),
            ("wVirtualScanCode", wintypes.WORD),
            ("uChar", wintypes.WCHAR),
            ("dwControlKeyState", wintypes.DWORD),
        ]

    class _MOUSE_EVENT_RECORD(ctypes.Structure):
        _fields_ = [
            ("dwMousePosition", _COORD),
            ("dwButtonState", wintypes.DWORD),
            ("dwControlKeyState", wintypes.DWORD),
            ("dwEventFlags", wintypes.DWORD),
        ]

    class _INPUT_RECORD_EVENT(ctypes.Union):
        _fields_ = [("KeyEvent", _KEY_EVENT_RECORD), ("MouseEvent", _MOUSE_EVENT_RECORD)]

    class _INPUT_RECORD(ctypes.Structure):
        _fields_ = [("EventType", wintypes.WORD), ("Event", _INPUT_RECORD_EVENT)]


class TimeMode(Enum):
    AUTO_MAX = "auto-max"
    AUTO_MIN = "auto-min"

    @property
    def label(self) -> str:
        return "AUTO-MAX" if self is TimeMode.AUTO_MAX else "AUTO-MIN"

    def next_mode(self) -> "TimeMode":
        return TimeMode.AUTO_MIN if self is TimeMode.AUTO_MAX else TimeMode.AUTO_MAX


class ArrowMode(Enum):
    HJKL = "hjkl"
    ARROWS = "arrows"
    BOTH = "both"

    @property
    def label(self) -> str:
        return {
            ArrowMode.HJKL: "HJKL",
            ArrowMode.ARROWS: "ARROWS",
            ArrowMode.BOTH: "BOTH",
        }[self]


class DecodeBackend(Enum):
    AUTO = "auto"
    MINIAUDIO = "miniaudio"
    FFMPEG = "ffmpeg"

    @property
    def label(self) -> str:
        return {
            DecodeBackend.AUTO: "AUTO",
            DecodeBackend.MINIAUDIO: "MINIAUDIO",
            DecodeBackend.FFMPEG: "FFMPEG",
        }[self]


class GenericMode(Enum):
    SOLO = "solo"
    TOGETHER = "together"
    BOTH = "both"

    @property
    def label(self) -> str:
        return {
            GenericMode.SOLO: "SOLO",
            GenericMode.TOGETHER: "TOGETHER",
            GenericMode.BOTH: "BOTH",
        }[self]


class PlaybackStrategy(Enum):
    PLAYLIST_ONCE = "playlist-once"
    TRACK_LOOP = "track-loop"
    TRACK_ONCE = "track-once"
    PLAYLIST_LOOP = "playlist-loop"
    RANDOM = "random"
    TOGETHER = "together"
    TOGETHER_LOOP = "together-loop"

    @property
    def label(self) -> str:
        return {
            PlaybackStrategy.PLAYLIST_ONCE: "SEQ ONCE",
            PlaybackStrategy.TRACK_LOOP: "TRACK LOOP",
            PlaybackStrategy.TRACK_ONCE: "TRACK ONCE",
            PlaybackStrategy.PLAYLIST_LOOP: "LIST LOOP",
            PlaybackStrategy.RANDOM: "RANDOM",
            PlaybackStrategy.TOGETHER: "TOGETHER",
            PlaybackStrategy.TOGETHER_LOOP: "TOGETHER LOOP",
        }[self]

    @property
    def description(self) -> str:
        return {
            PlaybackStrategy.PLAYLIST_ONCE: "Play unmuted tracks one-by-one from the focused track to the end.",
            PlaybackStrategy.TRACK_LOOP: "Loop the focused track.",
            PlaybackStrategy.TRACK_ONCE: "Play the focused track once and stop.",
            PlaybackStrategy.PLAYLIST_LOOP: "Loop the whole unmuted list.",
            PlaybackStrategy.RANDOM: "Keep picking random unmuted tracks.",
            PlaybackStrategy.TOGETHER: "Play all unmuted tracks at the same time.",
            PlaybackStrategy.TOGETHER_LOOP: "Loop the full together mix on the shared timeline.",
        }[self]


STRATEGY_ALIASES = {
    "playlist-once": PlaybackStrategy.PLAYLIST_ONCE,
    "sequence": PlaybackStrategy.PLAYLIST_ONCE,
    "seq": PlaybackStrategy.PLAYLIST_ONCE,
    "track-loop": PlaybackStrategy.TRACK_LOOP,
    "loop-one": PlaybackStrategy.TRACK_LOOP,
    "track-once": PlaybackStrategy.TRACK_ONCE,
    "playlist-loop": PlaybackStrategy.PLAYLIST_LOOP,
    "random": PlaybackStrategy.RANDOM,
    "shuffle": PlaybackStrategy.RANDOM,
    "together": PlaybackStrategy.TOGETHER,
    "together-loop": PlaybackStrategy.TOGETHER_LOOP,
    "all-loop": PlaybackStrategy.TOGETHER_LOOP,
    "all": PlaybackStrategy.TOGETHER,
}

SOLO_PLAYBACK_STRATEGIES = (
    PlaybackStrategy.PLAYLIST_ONCE,
    PlaybackStrategy.TRACK_LOOP,
    PlaybackStrategy.TRACK_ONCE,
    PlaybackStrategy.PLAYLIST_LOOP,
    PlaybackStrategy.RANDOM,
)

TOGETHER_PLAYBACK_STRATEGIES = (
    PlaybackStrategy.TOGETHER,
    PlaybackStrategy.TOGETHER_LOOP,
)


def is_together_strategy(strategy: PlaybackStrategy) -> bool:
    return strategy in TOGETHER_PLAYBACK_STRATEGIES


def allowed_strategies_for_generic_mode(generic_mode: GenericMode) -> tuple[PlaybackStrategy, ...]:
    if generic_mode is GenericMode.SOLO:
        return SOLO_PLAYBACK_STRATEGIES
    if generic_mode is GenericMode.TOGETHER:
        return TOGETHER_PLAYBACK_STRATEGIES
    return SOLO_PLAYBACK_STRATEGIES + TOGETHER_PLAYBACK_STRATEGIES

TIME_MODE_ALIASES = {
    "auto-max": TimeMode.AUTO_MAX,
    "max": TimeMode.AUTO_MAX,
    "longest": TimeMode.AUTO_MAX,
    "auto-min": TimeMode.AUTO_MIN,
    "min": TimeMode.AUTO_MIN,
    "shortest": TimeMode.AUTO_MIN,
}


@dataclass(slots=True)
class SourceSpec:
    source_index: int
    source: Path
    label: str
    kind: str
    files: list[Path]


@dataclass(slots=True)
class TrackSpec:
    spec_index: int
    source_index: int
    source_label: str
    path: Path
    label: str


@dataclass(slots=True)
class AudioTrack:
    index: int
    source_index: int
    source_label: str
    path: Path
    label: str
    samples: np.ndarray
    frame_count: int
    duration_seconds: float
    muted: bool = False


@dataclass(slots=True)
class SourceGroup:
    index: int
    source: Path
    label: str
    kind: str
    track_indices: list[int]


@dataclass(slots=True)
class TreeRow:
    kind: str
    group_index: int
    track_index: int | None
    tree_label: str


@dataclass(slots=True)
class LoadFailure:
    path: Path
    reason: str


@dataclass(slots=True)
class MixerSnapshot:
    tracks: list[AudioTrack]
    groups: list[SourceGroup]
    strategy: PlaybackStrategy
    time_mode: TimeMode
    playing: bool
    paused: bool
    playhead_frame: float
    current_track_index: int | None
    transport_end_frame: int
    live_track_count: int
    muted_track_count: int
    muted_group_count: int
    playback_rate: float
    volume: float
    reverse_enabled: bool
    audio_error: str | None


@dataclass(slots=True)
class TrackRemovalResult:
    message: str
    focus_index: int


def format_seconds(value: float) -> str:
    total_millis = max(0, int(value * 1000))
    total_seconds, millis = divmod(total_millis, 1000)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"
    return f"{minutes:02d}:{seconds:02d}.{millis:03d}"


def format_compact_seconds(value: float) -> str:
    total_centis = max(0, int(round(value * 100)))
    total_seconds, centis = divmod(total_centis, 100)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}.{centis:02d}"
    return f"{minutes:02d}:{seconds:02d}.{centis:02d}"


def format_duration_label(value: float) -> str:
    total_seconds = max(0, int(round(value)))
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def button_text(label: str, style: str, *, padding: int = 1) -> Text:
    spacer = " " * max(0, padding)
    return Text(f"{spacer}{label}{spacer}", style=style, justify="center", no_wrap=True)


def parse_strategy(value: str) -> PlaybackStrategy:
    normalized = value.strip().lower()
    if normalized in STRATEGY_ALIASES:
        return STRATEGY_ALIASES[normalized]
    raise argparse.ArgumentTypeError(f"Unsupported strategy: {value}")


def parse_time_mode(value: str) -> TimeMode:
    normalized = value.strip().lower()
    if normalized in TIME_MODE_ALIASES:
        return TIME_MODE_ALIASES[normalized]
    raise argparse.ArgumentTypeError(f"Unsupported time mode: {value}")


def parse_positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Expected a number, got: {value}") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError(f"Expected a positive number, got: {value}")
    return number


def parse_fast_playback_rate(value: str) -> float:
    number = parse_positive_float(value)
    if number < 1.0:
        raise argparse.ArgumentTypeError(f"Fast playback rate must be >= 1.0, got: {value}")
    return number


def parse_slow_playback_rate(value: str) -> float:
    number = parse_positive_float(value)
    if number > 1.0:
        raise argparse.ArgumentTypeError(f"Slow playback rate must be <= 1.0, got: {value}")
    return number


def parse_arrow_mode(value: str) -> ArrowMode:
    normalized = value.strip().lower()
    if normalized == "hjkl":
        return ArrowMode.HJKL
    if normalized in {"arrows", "arrow"}:
        return ArrowMode.ARROWS
    if normalized == "both":
        return ArrowMode.BOTH
    raise argparse.ArgumentTypeError(f"Unsupported arrow mode: {value}")


def parse_decode_backend(value: str) -> DecodeBackend:
    normalized = value.strip().lower()
    if normalized == "auto":
        return DecodeBackend.AUTO
    if normalized in {"miniaudio", "mini"}:
        return DecodeBackend.MINIAUDIO
    if normalized == "ffmpeg":
        return DecodeBackend.FFMPEG
    raise argparse.ArgumentTypeError(f"Unsupported decode backend: {value}")


def parse_generic_mode(value: str) -> GenericMode:
    normalized = value.strip().lower()
    if normalized == "solo":
        return GenericMode.SOLO
    if normalized == "together":
        return GenericMode.TOGETHER
    if normalized == "both":
        return GenericMode.BOTH
    raise argparse.ArgumentTypeError(f"Unsupported generic mode: {value}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="TAP: Terminal Audio Player",
        epilog=RUNTIME_HELP_TEXT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--files",
        nargs="+",
        metavar="PATH",
        help="Audio files or directories. You can mix both and they will be grouped by each input item.",
    )
    parser.add_argument(
        "--time",
        dest="time_mode",
        type=parse_time_mode,
        default=TimeMode.AUTO_MAX,
        metavar="MODE",
        help="Timeline reference for TOGETHER and TOGETHER LOOP modes: auto-max or auto-min.",
    )
    parser.add_argument(
        "--strategy",
        type=parse_strategy,
        default=None,
        metavar="MODE",
        help="Initial strategy: playlist-once, track-loop, track-once, playlist-loop, random, together, or together-loop. Default depends on --generic-mode.",
    )
    parser.add_argument(
        "--forward-seconds",
        "--forward_seconds",
        dest="forward_seconds",
        type=parse_positive_float,
        default=5.0,
        metavar="SECONDS",
        help="Seek forward step for the forward direction key. Default: 5.0.",
    )
    parser.add_argument(
        "--backward-seconds",
        "--backward_seconds",
        dest="backward_seconds",
        type=parse_positive_float,
        default=5.0,
        metavar="SECONDS",
        help="Seek backward step for the backward direction key. Default: 5.0.",
    )
    parser.add_argument(
        "--rate-fast-forward",
        "--rate_fast_forward",
        dest="rate_fast_forward",
        type=parse_fast_playback_rate,
        default=2.0,
        metavar="RATE",
        help="Playback rate while holding Space or the fast direction key. Must be >= 1.0. Default: 2.0.",
    )
    parser.add_argument(
        "--rate-slow-forward",
        "--rate_slow_forward",
        dest="rate_slow_forward",
        type=parse_slow_playback_rate,
        default=0.5,
        metavar="RATE",
        help="Playback rate while holding the slow direction key. Must be > 0 and <= 1.0. Default: 0.5.",
    )
    parser.add_argument(
        "--arrow-mode",
        type=parse_arrow_mode,
        default=ArrowMode.BOTH,
        metavar="MODE",
        help="Directional input mode: hjkl, arrows, or both. Default: both.",
    )
    parser.add_argument(
        "--decode-backend",
        "--decode_backend",
        dest="decode_backend",
        type=parse_decode_backend,
        default=DecodeBackend.AUTO,
        metavar="MODE",
        help="Decode backend: auto, miniaudio, or ffmpeg. Auto prefers ffmpeg when it is available.",
    )
    parser.add_argument(
        "--generic-mode",
        "--generic_mode",
        dest="generic_mode",
        type=parse_generic_mode,
        default=GenericMode.BOTH,
        metavar="MODE",
        help="Allowed strategy family: solo, together, or both. Default: both.",
    )
    parser.add_argument(
        "--disable-forward",
        action="store_true",
        help="Disable the Right key seek action and hide its control button.",
    )
    parser.add_argument(
        "--disable-fast-forward",
        action="store_true",
        help="Disable fast hold playback actions (Space and the fast-direction arrow hold).",
    )
    parser.add_argument(
        "--disable-backward",
        action="store_true",
        help="Disable the Left key seek action and hide its control button.",
    )
    parser.add_argument(
        "--disable-fast-backward",
        action="store_true",
        help="Disable slow/backward hold playback actions that use the slow hold rate.",
    )
    parser.add_argument(
        "--disable-reverse",
        action="store_true",
        help="Disable reverse playback toggling and hide the B button.",
    )
    parser.add_argument(
        "--disable-strategy-change",
        action="store_true",
        help="Disable runtime strategy switching and hide the D button.",
    )
    parser.add_argument(
        "--disable-list-modify",
        action="store_true",
        help="Disable runtime playlist deletion from Backspace/Delete.",
    )
    return parser


def discover_audio_files(folder: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in folder.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
        ),
        key=lambda path: str(path).lower(),
    )


def source_label(path: Path) -> str:
    return path.name or str(path)


def single_file_label(path: Path) -> str:
    parent_name = path.parent.name
    if parent_name:
        return f"{parent_name}/{path.name}"
    return path.name


def collect_source_specs(raw_items: list[str]) -> tuple[list[SourceSpec], list[str]]:
    specs: list[SourceSpec] = []
    warnings: list[str] = []
    seen_files: set[Path] = set()
    single_files: list[Path] = []

    for raw_item in raw_items:
        candidate = Path(raw_item.strip('"')).expanduser()
        if not candidate.exists():
            warnings.append(f"Missing input skipped: {candidate}")
            continue

        resolved = candidate.resolve()
        if resolved.is_file():
            if resolved.suffix.lower() not in SUPPORTED_EXTENSIONS:
                warnings.append(f"Unsupported file skipped: {resolved}")
                continue
            files = [resolved]
            kind = "file"
        elif resolved.is_dir():
            files = discover_audio_files(resolved)
            kind = "dir"
            if not files:
                warnings.append(f"No supported audio in folder: {resolved}")
                continue
        else:
            warnings.append(f"Unsupported input skipped: {resolved}")
            continue

        unique_files: list[Path] = []
        duplicate_count = 0
        for file_path in files:
            if file_path in seen_files:
                duplicate_count += 1
                continue
            seen_files.add(file_path)
            unique_files.append(file_path)

        if not unique_files:
            warnings.append(f"All audio already loaded from: {resolved}")
            continue
        if duplicate_count:
            warnings.append(f"Skipped {duplicate_count} duplicate track(s) from: {resolved}")

        if kind == "file":
            single_files.extend(unique_files)
            continue

        specs.append(
            SourceSpec(
                source_index=len(specs),
                source=resolved,
                label=source_label(resolved),
                kind=kind,
                files=unique_files,
            )
        )

    if single_files:
        specs.append(
            SourceSpec(
                source_index=len(specs),
                source=Path("[single-files]"),
                label="Single Files",
                kind="single-files",
                files=single_files,
            )
        )

    return specs, warnings


def build_track_specs(source_specs: list[SourceSpec]) -> list[TrackSpec]:
    track_specs: list[TrackSpec] = []
    for source_spec in source_specs:
        for file_path in source_spec.files:
            if source_spec.kind == "dir":
                label = str(file_path.relative_to(source_spec.source))
            elif source_spec.kind == "single-files":
                label = single_file_label(file_path)
            else:
                label = file_path.name
            track_specs.append(
                TrackSpec(
                    spec_index=len(track_specs),
                    source_index=source_spec.source_index,
                    source_label=source_spec.label,
                    path=file_path,
                    label=label,
                )
            )
    return track_specs


@functools.lru_cache(maxsize=1)
def find_ffmpeg_executable() -> str | None:
    return shutil.which("ffmpeg")


def resolve_decode_backend(
    backend: DecodeBackend,
    *,
    ffmpeg_executable: str | None = None,
) -> DecodeBackend:
    if backend is DecodeBackend.AUTO:
        return DecodeBackend.FFMPEG if ffmpeg_executable else DecodeBackend.MINIAUDIO
    return backend


def decode_track_miniaudio(spec: TrackSpec, sample_rate: int, channels: int) -> AudioTrack:
    decoded = miniaudio.decode(
        spec.path.read_bytes(),
        output_format=miniaudio.SampleFormat.FLOAT32,
        nchannels=channels,
        sample_rate=sample_rate,
    )
    samples = np.asarray(decoded.samples, dtype=np.float32).reshape(-1, channels).copy()
    return AudioTrack(
        index=-1,
        source_index=spec.source_index,
        source_label=spec.source_label,
        path=spec.path,
        label=spec.label,
        samples=samples,
        frame_count=samples.shape[0],
        duration_seconds=samples.shape[0] / sample_rate,
    )


def decode_track_ffmpeg(
    spec: TrackSpec,
    sample_rate: int,
    channels: int,
    *,
    ffmpeg_executable: str,
) -> AudioTrack:
    command = [
        ffmpeg_executable,
        "-v",
        "error",
        "-nostdin",
        "-i",
        os.fspath(spec.path),
        "-vn",
        "-sn",
        "-dn",
        "-f",
        "f32le",
        "-acodec",
        "pcm_f32le",
        "-ac",
        str(channels),
        "-ar",
        str(sample_rate),
        "pipe:1",
    ]
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip() or "ffmpeg decode failed"
        raise RuntimeError(message)

    samples = np.frombuffer(completed.stdout, dtype=np.float32)
    if samples.size == 0:
        raise RuntimeError("ffmpeg decode produced no samples")
    remainder = samples.size % channels
    if remainder:
        samples = samples[: samples.size - remainder]
    if samples.size == 0:
        raise RuntimeError("ffmpeg decode produced incomplete audio frames")
    reshaped = samples.reshape(-1, channels).copy()
    return AudioTrack(
        index=-1,
        source_index=spec.source_index,
        source_label=spec.source_label,
        path=spec.path,
        label=spec.label,
        samples=reshaped,
        frame_count=reshaped.shape[0],
        duration_seconds=reshaped.shape[0] / sample_rate,
    )


def decode_track(
    spec: TrackSpec,
    sample_rate: int,
    channels: int,
    backend: DecodeBackend,
    ffmpeg_executable: str | None,
) -> AudioTrack:
    if backend is DecodeBackend.MINIAUDIO:
        return decode_track_miniaudio(spec, sample_rate, channels)

    if backend is DecodeBackend.FFMPEG:
        if ffmpeg_executable is None:
            raise RuntimeError("ffmpeg executable was not found on PATH")
        return decode_track_ffmpeg(
            spec,
            sample_rate,
            channels,
            ffmpeg_executable=ffmpeg_executable,
        )

    if ffmpeg_executable is not None:
        try:
            return decode_track_ffmpeg(
                spec,
                sample_rate,
                channels,
                ffmpeg_executable=ffmpeg_executable,
            )
        except Exception as ffmpeg_exc:
            try:
                return decode_track_miniaudio(spec, sample_rate, channels)
            except Exception as miniaudio_exc:
                raise RuntimeError(
                    f"ffmpeg decode failed: {ffmpeg_exc}; miniaudio fallback failed: {miniaudio_exc}"
                ) from miniaudio_exc

    return decode_track_miniaudio(spec, sample_rate, channels)


class SyncMixer:
    def __init__(
        self,
        *,
        strategy: PlaybackStrategy,
        time_mode: TimeMode,
        allowed_strategies: tuple[PlaybackStrategy, ...],
        sample_rate: int = 44_100,
        channels: int = 2,
        buffer_size_msec: int = 50,
        enable_audio: bool = True,
    ) -> None:
        self.strategy = strategy
        self.time_mode = time_mode
        self.allowed_strategies = allowed_strategies
        self.sample_rate = sample_rate
        self.channels = channels
        self.buffer_size_msec = buffer_size_msec
        self.enable_audio = enable_audio
        self.lock = threading.RLock()
        self.tracks: list[AudioTrack] = []
        self.groups: list[SourceGroup] = []
        self.playing = False
        self.paused = False
        self.playhead_frame = 0.0
        self.playback_rate = 1.0
        self.volume = 1.0
        self.reverse_enabled = False
        self.current_track_index: int | None = None
        self.audio_error: str | None = None
        self._closed = False
        self._device: miniaudio.PlaybackDevice | None = None
        self._generator = None
        self._rng = random.Random()

        if self.enable_audio:
            self._start_device()

    def _start_device(self) -> None:
        try:
            self._device = miniaudio.PlaybackDevice(
                output_format=miniaudio.SampleFormat.FLOAT32,
                nchannels=self.channels,
                sample_rate=self.sample_rate,
                buffersize_msec=self.buffer_size_msec,
                app_name="tapplayer",
            )
            self._generator = self._playback_stream()
            next(self._generator)
            self._device.start(self._generator)
        except Exception as exc:  # pragma: no cover - depends on host audio device
            self.audio_error = str(exc)
            if self._device is not None:
                try:
                    self._device.close()
                except Exception:
                    pass
            self._device = None
            self._generator = None

    def snapshot(self) -> MixerSnapshot:
        with self.lock:
            live_tracks = self._live_track_indices_locked()
            muted_track_count = sum(1 for track in self.tracks if is_together_strategy(self.strategy) and track.muted)
            muted_group_count = sum(1 for group in self.groups if self._group_muted_locked(group.index))
            return MixerSnapshot(
                tracks=list(self.tracks),
                groups=list(self.groups),
                strategy=self.strategy,
                time_mode=self.time_mode,
                playing=self.playing,
                paused=self.paused,
                playhead_frame=self.playhead_frame,
                current_track_index=self.current_track_index,
                transport_end_frame=self._transport_end_frame_locked(live_tracks),
                live_track_count=len(live_tracks),
                muted_track_count=muted_track_count,
                muted_group_count=muted_group_count,
                playback_rate=self.playback_rate,
                volume=self.volume,
                reverse_enabled=self.reverse_enabled,
                audio_error=self.audio_error,
            )

    def set_library(self, tracks: list[AudioTrack], groups: list[SourceGroup]) -> None:
        with self.lock:
            self.tracks = tracks
            self.groups = groups
            self.playing = False
            self.paused = False
            self.playhead_frame = 0.0
            self.playback_rate = 1.0
            self.current_track_index = None

    def cycle_strategy(self) -> str:
        with self.lock:
            if len(self.allowed_strategies) <= 1:
                return f"Strategy is locked to {self.strategy.label}."
            order = self.allowed_strategies
            index = order.index(self.strategy)
            next_strategy = order[(index + 1) % len(order)]
            return self._set_strategy_locked(next_strategy)

    def set_strategy(self, strategy: PlaybackStrategy) -> str:
        with self.lock:
            if strategy not in self.allowed_strategies:
                return f"Strategy {strategy.label} is disabled."
            return self._set_strategy_locked(strategy)

    def _set_strategy_locked(self, next_strategy: PlaybackStrategy) -> str:
        previous_strategy = self.strategy
        if previous_strategy is next_strategy:
            return f"Strategy kept at {self.strategy.label}."

        previous_together = is_together_strategy(previous_strategy)
        next_together = is_together_strategy(next_strategy)
        preserve_transport = previous_together == next_together
        had_transport_state = (
            self.current_track_index is not None
            or self.playhead_frame > 0.0
            or self.playing
            or self.paused
        )

        self.strategy = next_strategy

        if preserve_transport:
            if next_together:
                live_tracks = self._live_track_indices_locked()
                end_frame = float(self._transport_end_frame_locked(live_tracks))
                self.playhead_frame = min(max(self.playhead_frame, 0.0), end_frame)
                if not self.playing:
                    self.playback_rate = 1.0
            else:
                if self.current_track_index is not None and 0 <= self.current_track_index < len(self.tracks):
                    track_end_frame = float(self.tracks[self.current_track_index].frame_count)
                    self.playhead_frame = min(max(self.playhead_frame, 0.0), track_end_frame)
                if not self.playing:
                    self.playback_rate = 1.0
            if not had_transport_state:
                return f"Strategy set to {self.strategy.label}."
            if self.playing:
                return f"Strategy set to {self.strategy.label}. Playback continues."
            if self.paused:
                return f"Strategy set to {self.strategy.label}. Pause position preserved."
            return f"Strategy set to {self.strategy.label}. Track position preserved."

        self.playing = False
        self.paused = False
        self.playhead_frame = 0.0
        self.playback_rate = 1.0
        self.current_track_index = None
        return f"Strategy set to {self.strategy.label}. Transport reset for mode change."

    def cycle_time_mode(self) -> str:
        with self.lock:
            self.time_mode = self.time_mode.next_mode()
            if is_together_strategy(self.strategy):
                self.playing = False
                self.paused = False
                self.playhead_frame = 0.0
                self.playback_rate = 1.0
            return f"Time mode set to {self.time_mode.label}."

    def toggle_reverse(self) -> str:
        with self.lock:
            self.reverse_enabled = not self.reverse_enabled
            mode = "enabled" if self.reverse_enabled else "disabled"
            return f"Reverse playback {mode}."

    def toggle_track_muted(self, index: int) -> bool:
        with self.lock:
            track = self.tracks[index]
            track.muted = not track.muted
            self._after_mute_change_locked()
            return track.muted

    def toggle_group_muted(self, group_index: int) -> bool:
        with self.lock:
            group = self.groups[group_index]
            target_muted = not self._group_muted_locked(group_index)
            for track_index in group.track_indices:
                self.tracks[track_index].muted = target_muted
            self._after_mute_change_locked()
            return target_muted

    def remove_track(self, index: int) -> TrackRemovalResult:
        with self.lock:
            if not (0 <= index < len(self.tracks)):
                return TrackRemovalResult("Track is no longer available.", max(0, len(self.tracks) - 1))

            removed_track = self.tracks[index]
            old_tracks = list(self.tracks)
            old_groups = list(self.groups)
            old_current_index = self.current_track_index
            old_playhead_frame = self.playhead_frame
            old_playing = self.playing
            old_paused = self.paused
            removed_current_track = old_current_index == index

            new_tracks: list[AudioTrack] = []
            new_groups: list[SourceGroup] = []
            old_to_new_track: dict[int, int] = {}

            for old_group in old_groups:
                new_group_track_indices: list[int] = []
                new_group_index = len(new_groups)
                for old_track_index in old_group.track_indices:
                    if old_track_index == index:
                        continue
                    track = old_tracks[old_track_index]
                    new_track_index = len(new_tracks)
                    old_to_new_track[old_track_index] = new_track_index
                    track.index = new_track_index
                    track.source_index = new_group_index
                    new_tracks.append(track)
                    new_group_track_indices.append(new_track_index)
                if new_group_track_indices:
                    new_groups.append(
                        SourceGroup(
                            index=new_group_index,
                            source=old_group.source,
                            label=old_group.label,
                            kind=old_group.kind,
                            track_indices=new_group_track_indices,
                        )
                    )

            self.tracks = new_tracks
            self.groups = new_groups

            if not self.tracks:
                self.playing = False
                self.paused = False
                self.playhead_frame = 0.0
                self.playback_rate = 1.0
                self.current_track_index = None
                return TrackRemovalResult(
                    f"Removed [{index + 1}] {removed_track.label}. Playlist is now empty.",
                    0,
                )

            focus_index = min(index, len(self.tracks) - 1)

            if is_together_strategy(self.strategy):
                self.current_track_index = None
                live_tracks = self._live_track_indices_locked()
                if not live_tracks:
                    self.playing = False
                    self.paused = False
                    self.playhead_frame = 0.0
                    self.playback_rate = 1.0
                else:
                    end_frame = float(self._transport_end_frame_locked(live_tracks))
                    self.playhead_frame = min(max(self.playhead_frame, 0.0), end_frame)
                    if self.reverse_enabled:
                        if self.playhead_frame <= 0.0:
                            if self.strategy is PlaybackStrategy.TOGETHER_LOOP and end_frame > 0:
                                self.playhead_frame = float(end_frame)
                            else:
                                self.playing = False
                                self.paused = False
                                self.playhead_frame = 0.0
                                self.playback_rate = 1.0
                    elif self.playhead_frame >= end_frame:
                        if self.strategy is PlaybackStrategy.TOGETHER_LOOP and end_frame > 0:
                            self.playhead_frame = 0.0
                        else:
                            self.playing = False
                            self.paused = False
                            self.playback_rate = 1.0
                return TrackRemovalResult(
                    f"Removed [{index + 1}] {removed_track.label} from the current playlist.",
                    focus_index,
                )

            if old_current_index is None:
                self.current_track_index = None
                self.playing = False
                self.paused = False
                self.playhead_frame = 0.0
                self.playback_rate = 1.0
            elif removed_current_track:
                self.current_track_index = None
                self.playing = False
                self.paused = False
                self.playhead_frame = 0.0
                self.playback_rate = 1.0
            else:
                new_current_index = old_to_new_track.get(old_current_index)
                if new_current_index is None:
                    self.current_track_index = None
                    self.playing = False
                    self.paused = False
                    self.playhead_frame = 0.0
                    self.playback_rate = 1.0
                else:
                    self.current_track_index = new_current_index
                    current_track = self.tracks[new_current_index]
                    self.playing = old_playing
                    self.paused = old_paused
                    self.playhead_frame = min(max(old_playhead_frame, 0.0), float(current_track.frame_count))

            return TrackRemovalResult(
                f"Removed [{index + 1}] {removed_track.label} from the current playlist.",
                focus_index,
            )

    def play(self, preferred_index: int | None) -> tuple[bool, str]:
        with self.lock:
            if self.enable_audio and self._device is None:
                reason = self.audio_error or "No playback device available."
                return False, f"Audio output unavailable: {reason}"
            if not self.tracks:
                return False, "No tracks loaded."
            if self.playing:
                return True, "Playback is already running."

            live_tracks = self._live_track_indices_locked()
            if not live_tracks:
                return False, "All tracks are muted."

            if self.paused and self._can_resume_locked():
                self.playing = True
                self.paused = False
                return True, "Playback resumed."

            self.paused = False
            if is_together_strategy(self.strategy):
                end_frame = self._transport_end_frame_locked(live_tracks)
                if end_frame <= 0:
                    return False, "Loaded tracks are empty."
                self.current_track_index = None
                if not self._can_resume_locked():
                    self.playhead_frame = self._transport_start_frame_locked(float(end_frame))
                self.playing = True
                if self.strategy is PlaybackStrategy.TOGETHER_LOOP:
                    return True, f"Looping {len(live_tracks)} unmuted track(s) together."
                return True, f"Playing {len(live_tracks)} unmuted track(s) together."

            start_index = self._find_start_track_locked(preferred_index)
            if start_index is None:
                return False, "No playable track available for the current strategy."
            if self.current_track_index != start_index or not self._can_resume_locked():
                self.playhead_frame = self._track_start_frame_locked(start_index)
            self.current_track_index = start_index
            self.playing = True
            return True, f"Playing with strategy {self.strategy.label}."

    def pause(self) -> str:
        with self.lock:
            if not self.playing:
                return "Nothing is playing."
            self.playing = False
            self.paused = True
            self.playback_rate = 1.0
            return "Playback paused."

    def stop(self) -> str:
        with self.lock:
            self.playing = False
            self.paused = False
            self.playhead_frame = 0.0
            self.playback_rate = 1.0
            self.current_track_index = None
        return "Playback stopped and timeline reset."

    def set_playback_rate(self, rate: float) -> None:
        with self.lock:
            self.playback_rate = max(1e-6, float(rate))

    def set_volume(self, volume: float) -> float:
        with self.lock:
            self.volume = min(1.0, max(0.0, float(volume)))
            return self.volume

    def change_volume(self, delta: float) -> float:
        with self.lock:
            self.volume = min(1.0, max(0.0, self.volume + float(delta)))
            return self.volume

    def seek_relative(self, delta_seconds: float, preferred_index: int | None = None) -> str:
        with self.lock:
            if not self.tracks:
                return "No tracks loaded."
            delta_frames = delta_seconds * self.sample_rate
            if is_together_strategy(self.strategy):
                live_tracks = self._live_track_indices_locked()
                if not live_tracks:
                    return "All tracks are muted."
                end_frame = float(self._transport_end_frame_locked(live_tracks))
                target = min(max(self.playhead_frame + delta_frames, 0.0), end_frame)
                self.playhead_frame = target
                if target >= end_frame and not self.reverse_enabled:
                    self.playing = False
                    self.paused = False
                    self.playback_rate = 1.0
                elif target <= 0.0 and self.reverse_enabled:
                    self.playing = False
                    self.paused = False
                    self.playback_rate = 1.0
                elif not self.playing:
                    self.paused = target > 0
                return (
                    f"{'Forward' if delta_seconds >= 0 else 'Backward'} "
                    f"{abs(delta_seconds):.1f}s -> {format_seconds(target / self.sample_rate)}"
                )

            current_index = self.current_track_index
            if current_index is None or self._track_effectively_muted_locked(current_index):
                current_index = self._find_start_track_locked(preferred_index)
                if current_index is None:
                    return "No playable track available for seek."
                self.current_track_index = current_index
                self.playhead_frame = 0.0

            track = self.tracks[current_index]
            end_frame = float(track.frame_count)
            target = min(max(self.playhead_frame + delta_frames, 0.0), end_frame)
            self.playhead_frame = target
            if target >= end_frame:
                self.playing = False
                self.paused = False
                self.playback_rate = 1.0
            elif not self.playing:
                self.paused = target > 0
            return (
                f"{'Forward' if delta_seconds >= 0 else 'Backward'} "
                f"{abs(delta_seconds):.1f}s -> {track.label} @ {format_seconds(target / self.sample_rate)}"
            )

    def close(self) -> None:
        self._closed = True
        with self.lock:
            self.playing = False
            self.paused = False
            self.playback_rate = 1.0
        if self._device is not None:
            try:
                self._device.stop()
            except Exception:
                pass
            self._device.close()

    def _playback_stream(self):
        requested_frames = yield np.zeros((0, self.channels), dtype=np.float32)
        while not self._closed:
            frame_count = requested_frames or max(256, self.sample_rate // 20)
            requested_frames = yield self._mix_next_frames(frame_count)

    def _mix_next_frames(self, requested_frames: int) -> np.ndarray:
        requested_frames = max(1, int(requested_frames))
        with self.lock:
            strategy = self.strategy
        if is_together_strategy(strategy):
            return self._mix_together_frames(requested_frames)
        return self._mix_single_stream_frames(requested_frames)

    def _mix_together_frames(self, requested_frames: int) -> np.ndarray:
        output = np.zeros((requested_frames, self.channels), dtype=np.float32)
        written = 0

        while written < requested_frames:
            with self.lock:
                if not self.playing:
                    break
                strategy = self.strategy
                if not is_together_strategy(strategy):
                    break
                live_tracks = [track for track in self.tracks if not self._track_effectively_muted_locked(track.index)]
                start_frame = float(self.playhead_frame)
                end_frame = float(self._transport_end_frame_locked([track.index for track in live_tracks]))
                playback_rate = self.playback_rate
                volume = self.volume
                reverse_enabled = self.reverse_enabled

            if not live_tracks:
                with self.lock:
                    self.playing = False
                    self.paused = False
                    self.playback_rate = 1.0
                break

            if self._at_transport_boundary(start_frame, end_frame, reverse_enabled):
                with self.lock:
                    if self.strategy is PlaybackStrategy.TOGETHER_LOOP and end_frame > 0:
                        self.playhead_frame = self._transport_start_frame_locked(end_frame)
                    else:
                        self.playing = False
                        self.paused = False
                        self.playback_rate = 1.0
                continue

            chunk_frames = min(
                requested_frames - written,
                self._available_output_frames(start_frame, end_frame, playback_rate, reverse_enabled),
            )
            if chunk_frames <= 0:
                with self.lock:
                    if self.strategy is PlaybackStrategy.TOGETHER_LOOP and end_frame > 0:
                        self.playhead_frame = self._transport_start_frame_locked(end_frame)
                    else:
                        self.playing = False
                        self.paused = False
                        self.playback_rate = 1.0
                continue

            positions = self._transport_positions(start_frame, chunk_frames, playback_rate, reverse_enabled)
            mix = np.zeros((chunk_frames, self.channels), dtype=np.float32)
            for track in live_tracks:
                valid_positions = positions < track.frame_count
                if not np.any(valid_positions):
                    continue
                segment = self._sample_positions(track.samples, positions[valid_positions])
                if segment.shape[0] <= 0:
                    continue
                mix[valid_positions] += segment
            if len(live_tracks) > 1:
                mix /= float(len(live_tracks))
            np.clip(mix, -1.0, 1.0, out=mix)
            if volume != 1.0:
                mix *= volume
            output[written : written + chunk_frames] = mix
            written += chunk_frames

            with self.lock:
                if self.playing and self.playhead_frame == start_frame and is_together_strategy(self.strategy):
                    advanced = self._advance_playhead(start_frame, chunk_frames, playback_rate, end_frame, reverse_enabled)
                    if self._at_transport_boundary(advanced, end_frame, reverse_enabled):
                        if self.strategy is PlaybackStrategy.TOGETHER_LOOP and end_frame > 0:
                            self.playhead_frame = self._transport_start_frame_locked(end_frame)
                        else:
                            self.playhead_frame = advanced
                            self.playing = False
                            self.paused = False
                            self.playback_rate = 1.0
                    else:
                        self.playhead_frame = advanced
        return output

    def _mix_single_stream_frames(self, requested_frames: int) -> np.ndarray:
        output = np.zeros((requested_frames, self.channels), dtype=np.float32)
        written = 0

        while written < requested_frames:
            with self.lock:
                if not self.playing:
                    break
                strategy = self.strategy
                current_index = self.current_track_index
                if current_index is None:
                    self.playing = False
                    self.paused = False
                    self.playback_rate = 1.0
                    break
                track = self.tracks[current_index]
                start_frame = float(self.playhead_frame)
                playback_rate = self.playback_rate
                reverse_enabled = self.reverse_enabled

            if self._track_effectively_muted_locked(current_index):
                with self.lock:
                    self._advance_after_boundary_locked(reason="muted")
                continue

            if self._at_transport_boundary(start_frame, float(track.frame_count), reverse_enabled):
                with self.lock:
                    self._advance_after_boundary_locked(reason="ended")
                continue

            chunk_frames = min(
                requested_frames - written,
                self._available_output_frames(start_frame, float(track.frame_count), playback_rate, reverse_enabled),
            )
            if chunk_frames <= 0:
                with self.lock:
                    self._advance_after_boundary_locked(reason="ended")
                continue

            positions = self._transport_positions(start_frame, chunk_frames, playback_rate, reverse_enabled)
            segment = self._sample_positions(track.samples, positions)
            segment_frames = segment.shape[0]
            if segment_frames <= 0:
                with self.lock:
                    self._advance_after_boundary_locked(reason="ended")
                continue
            output[written : written + segment_frames] = segment
            written += segment_frames

            with self.lock:
                if (
                    self.playing
                    and self.strategy is strategy
                    and self.current_track_index == current_index
                    and self.playhead_frame == start_frame
                ):
                    self.playhead_frame = self._advance_playhead(
                        start_frame,
                        segment_frames,
                        playback_rate,
                        float(track.frame_count),
                        reverse_enabled,
                    )
                    if self._at_transport_boundary(self.playhead_frame, float(track.frame_count), reverse_enabled):
                        self._advance_after_boundary_locked(reason="ended")

        with self.lock:
            volume = self.volume
        if volume != 1.0 and written > 0:
            output[:written] *= volume
        return output

    def _available_output_frames(
        self,
        start_frame: float,
        stop_frame: float,
        playback_rate: float,
        reverse_enabled: bool,
    ) -> int:
        if playback_rate <= 0:
            return 0
        if reverse_enabled:
            if start_frame <= 0:
                return 0
            return max(0, int(math.floor((start_frame / playback_rate) + 1e-9)))
        if start_frame >= stop_frame:
            return 0
        return max(0, int(math.ceil((stop_frame - start_frame) / playback_rate)))

    def _transport_positions(
        self,
        start_frame: float,
        output_frames: int,
        playback_rate: float,
        reverse_enabled: bool,
    ) -> np.ndarray:
        if output_frames <= 0:
            return np.zeros((0,), dtype=np.float64)
        if reverse_enabled:
            return start_frame - ((np.arange(output_frames, dtype=np.float64) + 1.0) * playback_rate)
        return start_frame + (np.arange(output_frames, dtype=np.float64) * playback_rate)

    def _sample_positions(self, samples: np.ndarray, positions: np.ndarray) -> np.ndarray:
        if positions.size <= 0:
            return np.zeros((0, self.channels), dtype=np.float32)

        max_index = samples.shape[0] - 1
        lower = np.floor(positions).astype(np.int64)
        lower = np.clip(lower, 0, max_index)
        upper = np.minimum(lower + 1, max_index)
        weight = (positions - lower).astype(np.float32).reshape(-1, 1)
        segment = (samples[lower] * (1.0 - weight)) + (samples[upper] * weight)
        return np.asarray(segment, dtype=np.float32)

    def _transport_start_frame_locked(self, end_frame: float) -> float:
        return end_frame if self.reverse_enabled else 0.0

    def _track_start_frame_locked(self, track_index: int) -> float:
        return float(self.tracks[track_index].frame_count) if self.reverse_enabled else 0.0

    def _advance_playhead(
        self,
        start_frame: float,
        output_frames: int,
        playback_rate: float,
        end_frame: float,
        reverse_enabled: bool,
    ) -> float:
        moved = output_frames * playback_rate
        if reverse_enabled:
            return max(0.0, start_frame - moved)
        return min(end_frame, start_frame + moved)

    def _at_transport_boundary(self, playhead_frame: float, end_frame: float, reverse_enabled: bool) -> bool:
        if reverse_enabled:
            return playhead_frame <= 0.0
        return playhead_frame >= end_frame

    def _advance_after_boundary_locked(self, *, reason: str) -> None:
        current_index = self.current_track_index
        if current_index is None:
            self.playing = False
            self.paused = False
            self.playhead_frame = 0.0
            self.playback_rate = 1.0
            return

        if self.strategy is PlaybackStrategy.TRACK_LOOP:
            if self._track_effectively_muted_locked(current_index):
                self.playing = False
                self.paused = False
                self.playhead_frame = 0.0
                self.playback_rate = 1.0
                self.current_track_index = None
            else:
                self.playhead_frame = self._track_start_frame_locked(current_index)
            return

        if self.strategy is PlaybackStrategy.TRACK_ONCE:
            self.playing = False
            self.paused = False
            if reason == "ended":
                self.playhead_frame = 0.0 if self.reverse_enabled else float(self.tracks[current_index].frame_count)
            else:
                self.playhead_frame = self._track_start_frame_locked(current_index)
            self.playback_rate = 1.0
            if reason != "ended":
                self.current_track_index = None
            return

        if self.strategy is PlaybackStrategy.PLAYLIST_ONCE:
            next_index = self._next_live_track_locked(current_index, wrap=False)
        elif self.strategy is PlaybackStrategy.PLAYLIST_LOOP:
            next_index = self._next_live_track_locked(current_index, wrap=True)
        elif self.strategy is PlaybackStrategy.RANDOM:
            next_index = self._random_live_track_locked(current_index)
        else:
            next_index = None

        if next_index is None:
            self.playing = False
            self.paused = False
            if reason == "ended":
                self.playhead_frame = 0.0 if self.reverse_enabled else float(self.tracks[current_index].frame_count)
            else:
                self.playhead_frame = self._track_start_frame_locked(current_index)
            self.playback_rate = 1.0
            if reason != "ended":
                self.current_track_index = None
            return

        self.current_track_index = next_index
        self.playhead_frame = self._track_start_frame_locked(next_index)

    def _live_track_indices_locked(self) -> list[int]:
        if not is_together_strategy(self.strategy):
            return [track.index for track in self.tracks]
        return [track.index for track in self.tracks if not self._track_effectively_muted_locked(track.index)]

    def _group_muted_locked(self, group_index: int) -> bool:
        if not is_together_strategy(self.strategy):
            return False
        group = self.groups[group_index]
        return bool(group.track_indices) and all(self._track_effectively_muted_locked(index) for index in group.track_indices)

    def _transport_end_frame_locked(self, live_tracks: list[int] | None = None) -> int:
        if is_together_strategy(self.strategy):
            indices = live_tracks if live_tracks is not None else self._live_track_indices_locked()
            if not indices:
                return 0
            lengths = [self.tracks[index].frame_count for index in indices]
            return min(lengths) if self.time_mode is TimeMode.AUTO_MIN else max(lengths)

        if self.current_track_index is None:
            return 0
        return self.tracks[self.current_track_index].frame_count

    def _can_resume_locked(self) -> bool:
        if is_together_strategy(self.strategy):
            live_tracks = self._live_track_indices_locked()
            end_frame = self._transport_end_frame_locked(live_tracks)
            return bool(live_tracks) and 0 < self.playhead_frame < end_frame

        current_index = self.current_track_index
        if current_index is None or self._track_effectively_muted_locked(current_index):
            return False
        return 0 < self.playhead_frame < self.tracks[current_index].frame_count

    def _track_effectively_muted_locked(self, track_index: int) -> bool:
        return is_together_strategy(self.strategy) and self.tracks[track_index].muted

    def _find_start_track_locked(self, preferred_index: int | None) -> int | None:
        live_tracks = self._live_track_indices_locked()
        if not live_tracks:
            return None
        if preferred_index is None:
            return live_tracks[0]
        if preferred_index in live_tracks:
            return preferred_index
        for index in live_tracks:
            if index > preferred_index:
                return index
        return live_tracks[0]

    def _next_live_track_locked(self, current_index: int, *, wrap: bool) -> int | None:
        live_tracks = self._live_track_indices_locked()
        if not live_tracks:
            return None
        for index in live_tracks:
            if index > current_index:
                return index
        return live_tracks[0] if wrap else None

    def _random_live_track_locked(self, current_index: int | None) -> int | None:
        live_tracks = self._live_track_indices_locked()
        if not live_tracks:
            return None
        if len(live_tracks) == 1:
            return live_tracks[0]
        pool = [index for index in live_tracks if index != current_index]
        return self._rng.choice(pool or live_tracks)

    def _after_mute_change_locked(self) -> None:
        if not is_together_strategy(self.strategy):
            return
        live_tracks = self._live_track_indices_locked()
        if not live_tracks:
            self.playing = False
            self.paused = False
            self.playhead_frame = 0.0
            self.playback_rate = 1.0
            self.current_track_index = None
            return

        if is_together_strategy(self.strategy):
            end_frame = self._transport_end_frame_locked(live_tracks)
            self.playhead_frame = min(max(self.playhead_frame, 0.0), float(end_frame))
            if self.reverse_enabled:
                if self.playhead_frame <= 0.0:
                    if self.strategy is PlaybackStrategy.TOGETHER_LOOP and end_frame > 0:
                        self.playhead_frame = float(end_frame)
                    else:
                        self.playing = False
                        self.paused = False
                        self.playhead_frame = 0.0
                        self.playback_rate = 1.0
            elif self.playhead_frame >= end_frame:
                if self.strategy is PlaybackStrategy.TOGETHER_LOOP and end_frame > 0:
                    self.playhead_frame = 0.0
                else:
                    self.playing = False
                    self.paused = False
                    self.playhead_frame = 0.0
                    self.playback_rate = 1.0
            return

        current_index = self.current_track_index
        if current_index is None or not self._track_effectively_muted_locked(current_index):
            return

        if self.strategy in {PlaybackStrategy.TRACK_LOOP, PlaybackStrategy.TRACK_ONCE}:
            self.playing = False
            self.paused = False
            self.playhead_frame = 0.0
            self.playback_rate = 1.0
            self.current_track_index = None
            return

        if self.strategy is PlaybackStrategy.PLAYLIST_ONCE:
            next_index = self._next_live_track_locked(current_index, wrap=False)
        elif self.strategy is PlaybackStrategy.PLAYLIST_LOOP:
            next_index = self._next_live_track_locked(current_index, wrap=True)
        else:
            next_index = self._random_live_track_locked(current_index)

        if next_index is None:
            self.playing = False
            self.paused = False
            self.playhead_frame = 0.0
            self.playback_rate = 1.0
            self.current_track_index = None
            return

        self.current_track_index = next_index
        self.playhead_frame = self._track_start_frame_locked(next_index)


class KeyReader:
    def __enter__(self) -> "KeyReader":
        if os.name == "nt":
            self._stdin_handle = ctypes.windll.kernel32.GetStdHandle(STD_INPUT_HANDLE)
        else:
            import termios
            import tty

            self._stdin_fd = sys.stdin.fileno()
            self._old_settings = termios.tcgetattr(self._stdin_fd)
            tty.setcbreak(self._stdin_fd)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if os.name != "nt":
            import termios

            termios.tcsetattr(self._stdin_fd, termios.TCSADRAIN, self._old_settings)

    def read_key(self, timeout: float = 0.1) -> str | None:
        if os.name == "nt":
            return self._read_key_windows(timeout)
        return self._read_key_posix(timeout)

    def _read_key_windows(self, timeout: float) -> str | None:
        deadline = time.monotonic() + timeout
        kernel32 = ctypes.windll.kernel32
        while time.monotonic() < deadline:
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            wait_result = kernel32.WaitForSingleObject(self._stdin_handle, min(remaining_ms, 50))
            if wait_result != WAIT_OBJECT_0:
                continue
            record = _INPUT_RECORD()
            count = wintypes.DWORD()
            if not kernel32.ReadConsoleInputW(self._stdin_handle, ctypes.byref(record), 1, ctypes.byref(count)):
                continue
            if record.EventType == KEY_EVENT:
                key = self._translate_windows_key_event(record.Event.KeyEvent)
                if key is not None:
                    return key
        return None

    def _translate_windows_key_event(self, event: _KEY_EVENT_RECORD) -> str | None:
        if not event.bKeyDown:
            return None
        if event.wVirtualKeyCode == VK_UP:
            return "up"
        if event.wVirtualKeyCode == VK_DOWN:
            return "down"
        if event.wVirtualKeyCode == VK_LEFT:
            return "left"
        if event.wVirtualKeyCode == VK_RIGHT:
            return "right"
        if event.wVirtualKeyCode == VK_F1:
            return "f1"
        if event.wVirtualKeyCode == VK_BACK:
            return "backspace"
        if event.wVirtualKeyCode == VK_DELETE:
            return "delete"
        if event.wVirtualKeyCode == VK_RETURN or event.uChar in ("\r", "\n"):
            return "enter"
        if event.wVirtualKeyCode == VK_ESCAPE:
            return "escape"
        if event.uChar == " ":
            return "space"
        if event.uChar == "\x03":
            return "quit"
        if event.wVirtualKeyCode == VK_ADD:
            return "+"
        if event.wVirtualKeyCode == VK_SUBTRACT:
            return "-"
        return event.uChar.lower() if event.uChar else None

    def _read_key_posix(self, timeout: float) -> str | None:
        readable, _, _ = select.select([sys.stdin], [], [], timeout)
        if not readable:
            return None
        key = sys.stdin.read(1)
        if key == "\x1b":
            readable, _, _ = select.select([sys.stdin], [], [], 0.01)
            if readable:
                prefix = sys.stdin.read(1)
                if prefix == "[":
                    sequence = ""
                    while True:
                        readable, _, _ = select.select([sys.stdin], [], [], 0.001)
                        if not readable:
                            break
                        sequence += sys.stdin.read(1)
                        if sequence and sequence[-1].isalpha():
                            break
                        if sequence.endswith("~"):
                            break
                    if sequence in {"A", "B", "C", "D"}:
                        return {"A": "up", "B": "down", "C": "right", "D": "left"}[sequence]
                    if sequence == "3~":
                        return "delete"
                    if sequence in {"11~", "[A"}:
                        return "f1"
                if prefix == "O":
                    readable, _, _ = select.select([sys.stdin], [], [], 0.001)
                    if readable and sys.stdin.read(1) == "P":
                        return "f1"
            return "escape"
        if key == " ":
            return "space"
        if key in ("\r", "\n"):
            return "enter"
        if key == "\x03":
            return "quit"
        if key in {"\x7f", "\b"}:
            return "backspace"
        return key.lower()


class MixListenApp:
    def __init__(
        self,
        console: Console,
        *,
        strategy: PlaybackStrategy,
        time_mode: TimeMode,
        arrow_mode: ArrowMode,
        decode_backend: DecodeBackend,
        generic_mode: GenericMode,
        forward_seconds: float,
        backward_seconds: float,
        rate_fast_forward: float,
        rate_slow_forward: float,
        disable_forward: bool,
        disable_fast_forward: bool,
        disable_backward: bool,
        disable_fast_backward: bool,
        disable_reverse: bool,
        disable_strategy_change: bool,
        disable_list_modify: bool,
        enable_audio: bool = True,
    ) -> None:
        self.console = console
        allowed_strategies = allowed_strategies_for_generic_mode(generic_mode)
        self.mixer = SyncMixer(
            strategy=strategy,
            time_mode=time_mode,
            allowed_strategies=allowed_strategies,
            enable_audio=enable_audio,
        )
        self.arrow_mode = arrow_mode
        self.decode_backend = decode_backend
        self.generic_mode = generic_mode
        self.forward_seconds = forward_seconds
        self.backward_seconds = backward_seconds
        self.rate_fast_forward = rate_fast_forward
        self.rate_slow_forward = rate_slow_forward
        self.disable_forward = disable_forward
        self.disable_fast_forward = disable_fast_forward
        self.disable_backward = disable_backward
        self.disable_fast_backward = disable_fast_backward
        self.disable_reverse = disable_reverse
        self.disable_strategy_change = disable_strategy_change
        self.disable_list_modify = disable_list_modify
        self.focus_index = 0
        self._last_current_track_index: int | None = None
        self.current_control_height = CONTROL_HEIGHT_COMPACT
        self.table_scroll_top = 0
        self.status_message = "Ready."
        self.status_message_expires_at = 0.0
        self.status_message_sticky = False
        self.failures: list[LoadFailure] = []
        self.input_warnings: list[str] = []
        self.running = True
        self.state_lock = threading.RLock()
        self._pending_hold_key: str | None = None
        self._pending_hold_since = 0.0
        self._active_hold_key: str | None = None
        self._active_hold_last_repeat = 0.0
        self._pending_strategy_menu_key: str | None = None
        self._pending_strategy_menu_since = 0.0
        self._flashed_control: str | None = None
        self._flashed_control_expires_at = 0.0
        self.help_visible = False
        self.strategy_menu_visible = False
        self.strategy_menu_index = 0
        self.strategy_menu_trigger_key = "d"
        self.strategy_menu_popup_indent = 2
        self.delete_confirm_visible = False
        self.delete_confirm_track_index: int | None = None
        self.delete_confirm_resume_on_cancel = False

    def load_inputs(self, raw_items: list[str]) -> None:
        source_specs, warnings = collect_source_specs(raw_items)
        if not source_specs:
            supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
            raise RuntimeError(f"No supported inputs were found. Supported extensions: {supported}")

        track_specs = build_track_specs(source_specs)
        decoded_tracks: dict[int, AudioTrack] = {}
        failures: list[LoadFailure] = []
        workers = max(1, min(len(track_specs), os.cpu_count() or 4))
        ffmpeg_executable = find_ffmpeg_executable()
        if self.decode_backend is DecodeBackend.FFMPEG and ffmpeg_executable is None:
            raise RuntimeError("The ffmpeg backend was requested, but the ffmpeg executable was not found on PATH.")
        resolved_backend = resolve_decode_backend(
            self.decode_backend,
            ffmpeg_executable=ffmpeg_executable,
        )

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            transient=True,
            console=self.console,
        ) as progress:
            task_id = progress.add_task(f"Decoding audio tracks via {resolved_backend.label}", total=len(track_specs))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="decoder") as executor:
                future_map = {
                    executor.submit(
                        decode_track,
                        spec,
                        self.mixer.sample_rate,
                        self.mixer.channels,
                        self.decode_backend,
                        ffmpeg_executable,
                    ): spec
                    for spec in track_specs
                }
                for future in as_completed(future_map):
                    spec = future_map[future]
                    try:
                        decoded_tracks[spec.spec_index] = future.result()
                    except Exception as exc:
                        failures.append(LoadFailure(path=spec.path, reason=str(exc)))
                    finally:
                        progress.advance(task_id)

        tracks: list[AudioTrack] = []
        source_remap: dict[int, int] = {}
        group_tracks: dict[int, list[int]] = {}

        for spec in track_specs:
            decoded = decoded_tracks.get(spec.spec_index)
            if decoded is None:
                continue
            if spec.source_index not in source_remap:
                source_remap[spec.source_index] = len(source_remap)
                group_tracks[source_remap[spec.source_index]] = []
            new_source_index = source_remap[spec.source_index]
            track_index = len(tracks)
            tracks.append(
                AudioTrack(
                    index=track_index,
                    source_index=new_source_index,
                    source_label=decoded.source_label,
                    path=decoded.path,
                    label=decoded.label,
                    samples=decoded.samples,
                    frame_count=decoded.frame_count,
                    duration_seconds=decoded.duration_seconds,
                )
            )
            group_tracks[new_source_index].append(track_index)

        groups: list[SourceGroup] = []
        for source_spec in source_specs:
            if source_spec.source_index not in source_remap:
                warnings.append(f"All decode attempts failed for: {source_spec.source}")
                continue
            new_index = source_remap[source_spec.source_index]
            groups.append(
                SourceGroup(
                    index=new_index,
                    source=source_spec.source,
                    label=source_spec.label,
                    kind=source_spec.kind,
                    track_indices=group_tracks[new_index],
                )
            )

        groups.sort(key=lambda group: group.index)
        if not tracks:
            raise RuntimeError("Inputs were found, but all audio files failed to decode.")

        with self.state_lock:
            self.failures = sorted(failures, key=lambda failure: failure.path.name.lower())
            self.input_warnings = warnings
            self.focus_index = 0
            self._last_current_track_index = None
            backend_label = (
                f"AUTO->{resolved_backend.label}"
                if self.decode_backend is DecodeBackend.AUTO
                else resolved_backend.label
            )
            self._set_status(
                f"Loaded {len(tracks)} track(s) from {len(groups)} source group(s) via {backend_label}.",
                ttl=2.5,
            )
            if self.failures:
                self.status_message += f" {len(self.failures)} decode failure(s)."
        self.mixer.set_library(tracks, groups)

    def run(self) -> None:
        with KeyReader() as reader:
            input_thread = threading.Thread(
                target=self._input_loop,
                args=(reader,),
                daemon=True,
                name="input-loop",
            )
            input_thread.start()
            try:
                with Live(self.render(), console=self.console, screen=True, auto_refresh=False) as live:
                    while self.running:
                        live.update(self.render(), refresh=True)
                        time.sleep(0.05)
            finally:
                self.running = False
                input_thread.join(timeout=0.5)
                self.mixer.close()

    def _input_loop(self, reader: KeyReader) -> None:
        try:
            while self.running:
                now = time.monotonic()
                key = reader.read_key(timeout=0.05)
                if key is None:
                    self._flush_pending_hold(now)
                    self._flush_pending_strategy_menu(now)
                    self._release_hold_if_needed(now)
                    continue
                self._handle_input_key(key, now)
        except Exception as exc:
            self._set_status(f"Input loop stopped: {exc}", sticky=True)
            self.running = False
        finally:
            self._clear_pending_hold()
            self._clear_pending_strategy_menu()
            self._release_hold(force=True)

    def _flash_control(self, control_id: str) -> None:
        self._flashed_control = control_id
        self._flashed_control_expires_at = time.monotonic() + CONTROL_FLASH_TTL

    def _arrow_mode_uses_hjkl(self) -> bool:
        return self.arrow_mode in {ArrowMode.HJKL, ArrowMode.BOTH}

    def _arrow_mode_uses_arrows(self) -> bool:
        return self.arrow_mode in {ArrowMode.ARROWS, ArrowMode.BOTH}

    def _strategy_change_available(self) -> bool:
        return not self.disable_strategy_change and len(self.mixer.allowed_strategies) > 1

    def _strategy_menu_trigger_available(self, snapshot: MixerSnapshot, key: str) -> bool:
        if self.help_visible or not self._strategy_change_available():
            return False
        return key in {"d", "r"}

    def _normalize_focus_key(self, key: str) -> str | None:
        if self._arrow_mode_uses_arrows() and key in {"up", "down"}:
            return key
        if self._arrow_mode_uses_hjkl():
            if key == "k":
                return "up"
            if key == "j":
                return "down"
        return None

    def _normalize_seek_key(self, key: str) -> str | None:
        if self._arrow_mode_uses_arrows() and key in {"left", "right"}:
            return key
        if self._arrow_mode_uses_hjkl():
            if key == "h":
                return "left"
            if key == "l":
                return "right"
        return None

    def _control_is_flashed(self, control_id: str) -> bool:
        if self._flashed_control != control_id:
            return False
        if time.monotonic() >= self._flashed_control_expires_at:
            if self._flashed_control == control_id:
                self._flashed_control = None
                self._flashed_control_expires_at = 0.0
            return False
        return True

    def _seek_enabled_for_key(self, key: str) -> bool:
        if key == "left":
            return not self.disable_backward
        if key == "right":
            return not self.disable_forward
        return True

    def _hold_enabled_for_key(self, snapshot: MixerSnapshot, key: str) -> bool:
        if key in {"left", "right"} and not self._seek_enabled_for_key(key):
            return False
        if key == "space":
            return not self.disable_fast_forward
        hold_rate = self._hold_rate_for_key(snapshot, key)
        if hold_rate > 1.0:
            return not self.disable_fast_forward
        return not self.disable_fast_backward

    def _tap_enabled_for_key(self, key: str) -> bool:
        if key in {"left", "right"}:
            return self._seek_enabled_for_key(key)
        return True

    def _handle_input_key(self, key: str, now: float) -> None:
        snapshot = self.mixer.snapshot()
        if self.delete_confirm_visible:
            self._flush_pending_strategy_menu(now, force=True)
            self._flush_pending_hold(now, force=True)
            self._release_hold_if_needed(now, force=True)
            self.handle_key(key)
            return

        if self.strategy_menu_visible:
            self._flush_pending_strategy_menu(now, force=True)
            self._flush_pending_hold(now, force=True)
            self._release_hold_if_needed(now, force=True)
            self.handle_key(key)
            return

        if key in {"d", "r"} and self._strategy_menu_trigger_available(snapshot, key):
            self._flush_pending_hold(now, force=True)
            self._release_hold_if_needed(now, force=True)
            if self._pending_strategy_menu_key == key:
                self._clear_pending_strategy_menu()
                self._open_strategy_menu(snapshot, trigger_key=key)
                return
            if self._pending_strategy_menu_key is not None and self._pending_strategy_menu_key != key:
                self._flush_pending_strategy_menu(now, force=True)
            self._pending_strategy_menu_key = key
            self._pending_strategy_menu_since = now
            return

        self._flush_pending_strategy_menu(now, force=True)
        seek_key = self._normalize_seek_key(key)
        hold_key = "space" if key == "space" else seek_key
        hold_candidate = hold_key is not None
        if hold_candidate:
            assert hold_key is not None
            can_hold = self._hold_enabled_for_key(snapshot, hold_key)
            can_tap = self._tap_enabled_for_key(hold_key)
            if not can_hold and not can_tap:
                return
            if self._active_hold_key == hold_key:
                self._active_hold_last_repeat = now
                return
            if self._pending_hold_key == hold_key:
                if can_hold:
                    self._activate_hold(hold_key, now, snapshot)
                return
            self._flush_pending_hold(now, force=True)
            self._release_hold_if_needed(now, force=hold_key != self._active_hold_key)
            self._pending_hold_key = hold_key
            self._pending_hold_since = now
            return

        self._flush_pending_hold(now, force=True)
        self._release_hold_if_needed(now, force=True)
        self.handle_key(key)

    def _flush_pending_hold(self, now: float, force: bool = False) -> None:
        if self._pending_hold_key is None:
            return
        if not force and now - self._pending_hold_since < HOLD_TAP_DELAY:
            return
        key = self._pending_hold_key
        self._clear_pending_hold()
        self.handle_key(key)

    def _clear_pending_hold(self) -> None:
        self._pending_hold_key = None
        self._pending_hold_since = 0.0

    def _flush_pending_strategy_menu(self, now: float, force: bool = False) -> None:
        if self._pending_strategy_menu_key is None:
            return
        if not force and now - self._pending_strategy_menu_since < HOLD_TAP_DELAY:
            return
        key = self._pending_strategy_menu_key
        self._clear_pending_strategy_menu()
        self.handle_key(key)

    def _clear_pending_strategy_menu(self) -> None:
        self._pending_strategy_menu_key = None
        self._pending_strategy_menu_since = 0.0

    def _strategy_menu_options(self) -> list[PlaybackStrategy]:
        return list(self.mixer.allowed_strategies)

    def _open_strategy_menu(self, snapshot: MixerSnapshot, *, trigger_key: str) -> None:
        options = self._strategy_menu_options()
        if not options:
            return
        self.strategy_menu_visible = True
        self.strategy_menu_trigger_key = "d"
        current_strategy = snapshot.strategy
        self.strategy_menu_index = options.index(current_strategy) if current_strategy in options else 0
        self._flash_control(self.strategy_menu_trigger_key)
        self._set_status("Mode menu opened.")

    def _close_strategy_menu(self, *, message: str | None = None) -> None:
        self.strategy_menu_visible = False
        self.strategy_menu_trigger_key = "d"
        if message is not None:
            self._set_status(message)

    def _close_help(self, *, message: str | None = None) -> None:
        self.help_visible = False
        if message is not None:
            self._set_status(message)

    def _list_modify_available(self) -> bool:
        return not self.disable_list_modify

    def _open_delete_confirm(self, snapshot: MixerSnapshot, track_index: int) -> None:
        if not self._list_modify_available():
            self._set_status("Playlist deletion is disabled.")
            return
        if not (0 <= track_index < len(snapshot.tracks)):
            self._set_status("Track is no longer available.")
            return
        self.delete_confirm_visible = True
        self.delete_confirm_track_index = track_index
        self.delete_confirm_resume_on_cancel = False
        if (
            not is_together_strategy(snapshot.strategy)
            and snapshot.current_track_index == track_index
            and snapshot.playing
        ):
            self.mixer.pause()
            self.delete_confirm_resume_on_cancel = True
        track = snapshot.tracks[track_index]
        self._set_status(f"Remove [{track.index + 1}] {track.label} from the current playlist?")

    def _close_delete_confirm(self) -> None:
        self.delete_confirm_visible = False
        self.delete_confirm_track_index = None
        self.delete_confirm_resume_on_cancel = False

    def _cancel_delete_confirm(self) -> None:
        resume_on_cancel = self.delete_confirm_resume_on_cancel
        target_index = self.delete_confirm_track_index
        self._close_delete_confirm()
        if resume_on_cancel and target_index is not None:
            _ok, message = self.mixer.play(target_index)
            self._set_status(f"Removal cancelled. {message}")
            return
        self._set_status("Removal cancelled.")

    def _confirm_delete_confirm(self) -> None:
        target_index = self.delete_confirm_track_index
        paused_for_prompt = self.delete_confirm_resume_on_cancel
        if target_index is None:
            self._close_delete_confirm()
            self._set_status("Track is no longer available.")
            return

        snapshot_before = self.mixer.snapshot()
        if not (0 <= target_index < len(snapshot_before.tracks)):
            self._close_delete_confirm()
            self._set_status("Track is no longer available.")
            return

        should_autoplay = paused_for_prompt
        if (
            not is_together_strategy(snapshot_before.strategy)
            and snapshot_before.current_track_index == target_index
            and snapshot_before.playing
        ):
            self.mixer.pause()
            should_autoplay = True

        result = self.mixer.remove_track(target_index)
        self._close_delete_confirm()

        with self.state_lock:
            self.focus_index = result.focus_index
            self._last_current_track_index = None

        status_message = result.message
        if should_autoplay:
            snapshot_after = self.mixer.snapshot()
            if snapshot_after.tracks:
                autoplay_index = min(result.focus_index, len(snapshot_after.tracks) - 1)
                _ok, message = self.mixer.play(autoplay_index)
                status_message = f"{status_message} {message}"
        self._set_status(status_message)

    def _activate_hold(self, key: str, now: float, snapshot: MixerSnapshot | None = None) -> None:
        self._clear_pending_hold()
        snapshot = snapshot or self.mixer.snapshot()
        focus_index = self._focused_index(snapshot)
        can_hold = snapshot.playing
        if not snapshot.playing:
            can_hold, message = self.mixer.play(focus_index)
            self._set_status(message)
        if not can_hold:
            return
        self._active_hold_key = key
        self._active_hold_last_repeat = now
        hold_rate = self._hold_rate_for_key(snapshot, key)
        self.mixer.set_playback_rate(hold_rate)
        self._flash_control("space" if key == "space" else key)
        self._set_status(f"Hold playback at {hold_rate:.2f}x.")

    def _release_hold_if_needed(self, now: float, force: bool = False) -> None:
        if self._active_hold_key is None:
            return
        if not force and now - self._active_hold_last_repeat < HOLD_RELEASE_DELAY:
            return
        self._release_hold(force=force)

    def _release_hold(self, force: bool = False) -> None:
        if self._active_hold_key is None:
            return
        self.mixer.set_playback_rate(1.0)
        self._active_hold_key = None
        self._active_hold_last_repeat = 0.0
        self._expire_transient_status()

    def _focused_index(self, snapshot: MixerSnapshot) -> int:
        with self.state_lock:
            focus_index = min(self.focus_index, max(len(snapshot.tracks) - 1, 0))
            self.focus_index = focus_index
            return focus_index

    def _sync_focus_with_current_track(self, snapshot: MixerSnapshot) -> None:
        track_count = len(snapshot.tracks)
        current_index = snapshot.current_track_index
        normalized_current = current_index if current_index is not None and 0 <= current_index < track_count else None

        with self.state_lock:
            if (
                not is_together_strategy(snapshot.strategy)
                and normalized_current is not None
                and normalized_current != self._last_current_track_index
            ):
                self.focus_index = normalized_current
            self._last_current_track_index = normalized_current
            self.focus_index = min(self.focus_index, max(track_count - 1, 0))

    def _hold_rate_for_key(self, snapshot: MixerSnapshot, key: str) -> float:
        if snapshot.reverse_enabled:
            if key in {"space", "left"}:
                return self.rate_fast_forward
            return self.rate_slow_forward
        if key == "left":
            return self.rate_slow_forward
        return self.rate_fast_forward

    def handle_key(self, key: str) -> None:
        snapshot = self.mixer.snapshot()
        self._sync_focus_with_current_track(snapshot)
        if self.delete_confirm_visible:
            if key == "enter":
                self._confirm_delete_confirm()
                return
            if key.lower() == "y":
                self._confirm_delete_confirm()
                return
            if key in {"n", "q", "quit", "escape"}:
                self._cancel_delete_confirm()
                return
            return

        if self.strategy_menu_visible:
            focus_key = self._normalize_focus_key(key)
            options = self._strategy_menu_options()
            if key in {"q", "quit", "escape"}:
                self._close_strategy_menu(message="Mode menu closed.")
                return
            if key in {"t", "d", "r"}:
                return
            if not options:
                self._close_strategy_menu(message="No strategy options available.")
                return
            if focus_key == "up":
                self.strategy_menu_index = (self.strategy_menu_index - 1) % len(options)
                return
            if focus_key == "down":
                self.strategy_menu_index = (self.strategy_menu_index + 1) % len(options)
                return
            if key == "enter":
                self._flash_control(self.strategy_menu_trigger_key)
                message = self.mixer.set_strategy(options[self.strategy_menu_index])
                self._close_strategy_menu(message=message)
                return
            return

        if key in {"u", "f1"}:
            self.help_visible = not self.help_visible
            self._set_status("Help opened." if self.help_visible else "Help closed.")
            return
        if self.help_visible:
            if key in {"q", "quit", "escape"}:
                self._close_help(message="Help closed.")
                return
            return
        if key in {"q", "quit", "escape"}:
            self._set_status("Closing TAP.", sticky=True)
            self.running = False
            return
        if not snapshot.tracks:
            return

        focus_key = self._normalize_focus_key(key)
        seek_key = self._normalize_seek_key(key)
        with self.state_lock:
            if focus_key == "up":
                self.focus_index = (self.focus_index - 1) % len(snapshot.tracks)
                return
            if focus_key == "down":
                self.focus_index = (self.focus_index + 1) % len(snapshot.tracks)
                return
            focus_index = min(self.focus_index, max(len(snapshot.tracks) - 1, 0))
            self.focus_index = focus_index

        track = snapshot.tracks[focus_index]
        group = snapshot.groups[track.source_index]

        if key == "m":
            if not is_together_strategy(snapshot.strategy):
                self._set_status("Track mute is only available in TOGETHER modes.")
                return
            muted = self.mixer.toggle_track_muted(focus_index)
            self._flash_control("m")
            self._set_status(f"Track {focus_index + 1} {'muted' if muted else 'unmuted'}.")
            return
        if key == "enter":
            self._flash_control("enter")
            self._set_status(self._handle_enter(snapshot, focus_index))
            return
        if key == "g":
            if not is_together_strategy(snapshot.strategy):
                self._set_status("Source mute is only available in TOGETHER modes.")
                return
            muted = self.mixer.toggle_group_muted(group.index)
            self._flash_control("g")
            self._set_status(f"Source {group.label} {'muted' if muted else 'unmuted'}.")
            return
        if key == "p":
            self._flash_control("p")
            self._set_status(self._handle_global_play_pause(snapshot, focus_index))
            return
        if key == "space":
            self._flash_control("space")
            self._set_status(self._handle_space(snapshot, focus_index))
            return
        if key in {"backspace", "delete"}:
            if not self._list_modify_available():
                self._set_status("Playlist deletion is disabled.")
                return
            self._open_delete_confirm(snapshot, focus_index)
            return
        if seek_key == "left":
            if self.disable_backward:
                return
            self._flash_control("left")
            self._set_status(self.mixer.seek_relative(-self.backward_seconds, focus_index))
            return
        if seek_key == "right":
            if self.disable_forward:
                return
            self._flash_control("right")
            self._set_status(self.mixer.seek_relative(self.forward_seconds, focus_index))
            return
        if key == "s":
            self._flash_control("s")
            self._set_status(self.mixer.stop())
            return
        if key in {"+", "="}:
            self._flash_control("vol_up")
            volume = self.mixer.change_volume(VOLUME_STEP)
            self._set_status(f"Volume {int(round(volume * 100))}%.")
            return
        if key in {"-", "_"}:
            self._flash_control("vol_down")
            volume = self.mixer.change_volume(-VOLUME_STEP)
            self._set_status(f"Volume {int(round(volume * 100))}%.")
            return
        if key == "b":
            if self.disable_reverse:
                return
            self._flash_control("b")
            self._set_status(self.mixer.toggle_reverse())
            return
        if key in {"d", "r"}:
            if not self._strategy_change_available():
                return
            self._flash_control("d")
            self._set_status(self.mixer.cycle_strategy())
            return
        if key == "t":
            if not is_together_strategy(snapshot.strategy):
                self._set_status("Time mode is only available in TOGETHER modes.")
                return
            self._flash_control("t")
            self._set_status(self.mixer.cycle_time_mode())

    def _handle_global_play_pause(self, snapshot: MixerSnapshot, focus_index: int) -> str:
        if snapshot.playing:
            return self.mixer.pause()
        _, message = self.mixer.play(focus_index)
        return message

    def _handle_enter(self, snapshot: MixerSnapshot, focus_index: int) -> str:
        if is_together_strategy(snapshot.strategy):
            muted = self.mixer.toggle_track_muted(focus_index)
            return f"Track {focus_index + 1} {'muted' if muted else 'unmuted'}."

        is_current_track = snapshot.current_track_index == focus_index
        if is_current_track and snapshot.playing:
            return self.mixer.pause()
        if is_current_track and snapshot.paused:
            _, message = self.mixer.play(focus_index)
            return message

        self.mixer.stop()
        _, message = self.mixer.play(focus_index)
        return message

    def _handle_space(self, snapshot: MixerSnapshot, focus_index: int) -> str:
        if is_together_strategy(snapshot.strategy):
            if snapshot.playing:
                return self.mixer.pause()
            _, message = self.mixer.play(focus_index)
            return message
        return self._handle_enter(snapshot, focus_index)

    def render(self):
        snapshot = self.mixer.snapshot()
        self._sync_focus_with_current_track(snapshot)
        current_seconds = snapshot.playhead_frame / self.mixer.sample_rate
        controls_panel = self._build_controls(snapshot, focus_index := self._focused_index(snapshot))
        with self.state_lock:
            status_message = self._status_line(snapshot)
            failures = list(self.failures)
            warnings = list(self.input_warnings)

        if self.help_visible:
            return self._build_help_screen(snapshot, focus_index, failures, warnings)

        layout = Layout()
        layout.split_column(
            Layout(controls_panel, size=self.current_control_height),
            Layout(self._build_table(snapshot, focus_index, current_seconds), ratio=1),
            Layout(self._build_status_panel(snapshot, focus_index, status_message, failures, warnings), size=STATUS_HEIGHT),
        )
        renderable: RenderableType = layout
        if self.strategy_menu_visible:
            popup = self._build_strategy_menu_popup(snapshot)
            if popup is not None:
                renderable = OverlayRenderable(
                    renderable,
                    popup,
                    x=max(0, self.strategy_menu_popup_indent - 2),
                    y=max(0, self.current_control_height - 4),
                )
        if self.delete_confirm_visible:
            popup = self._build_delete_confirm_popup(snapshot)
            if popup is not None:
                popup_width = self._delete_confirm_popup_width()
                renderable = OverlayRenderable(
                    renderable,
                    popup,
                    x=max(0, (self.console.size.width - popup_width) // 2),
                    y=max(1, (self.console.size.height - self._delete_confirm_popup_height(snapshot)) // 2),
                )
        return renderable

    def _set_status(self, message: str, *, sticky: bool = False, ttl: float = STATUS_NOTE_TTL) -> None:
        with self.state_lock:
            self.status_message = message
            self.status_message_sticky = sticky
            self.status_message_expires_at = float("inf") if sticky else time.monotonic() + ttl

    def _expire_transient_status(self) -> None:
        with self.state_lock:
            if not self.status_message_sticky:
                self.status_message_expires_at = 0.0

    def _active_status_note(self) -> str | None:
        with self.state_lock:
            if self.status_message_sticky:
                return self.status_message
            if time.monotonic() < self.status_message_expires_at:
                return self.status_message
            return None

    def _status_line(self, snapshot: MixerSnapshot) -> str:
        current = self._current_transport_status(snapshot)
        note = self._active_status_note()
        if note and note != current:
            return f"{current} | {note}"
        return current

    def _current_transport_status(self, snapshot: MixerSnapshot) -> str:
        if not snapshot.tracks:
            return "READY"

        rate_suffix = f" x{snapshot.playback_rate:.2f}" if abs(snapshot.playback_rate - 1.0) > 1e-6 else ""
        direction_suffix = " REV" if snapshot.reverse_enabled else ""
        if is_together_strategy(snapshot.strategy):
            detail = f"{snapshot.live_track_count} live"
            if snapshot.playing:
                return f"PLAYING{direction_suffix}{rate_suffix} | {detail}"
            if snapshot.paused:
                return f"PAUSED{direction_suffix} | {detail}"
            if snapshot.reverse_enabled and snapshot.playhead_frame <= 0:
                return f"DONE{direction_suffix} | {detail}"
            if snapshot.transport_end_frame > 0 and snapshot.playhead_frame >= snapshot.transport_end_frame:
                return f"DONE{direction_suffix} | {detail}"
            return f"STOPPED{direction_suffix} | {detail}"

        current_label = None
        if snapshot.current_track_index is not None and 0 <= snapshot.current_track_index < len(snapshot.tracks):
            current_label = snapshot.tracks[snapshot.current_track_index].label

        if snapshot.playing:
            return f"PLAYING{direction_suffix}{rate_suffix}" + (f" | {current_label}" if current_label else "")
        if snapshot.paused:
            return f"PAUSED{direction_suffix}" + (f" | {current_label}" if current_label else "")
        if current_label:
            if snapshot.reverse_enabled and snapshot.playhead_frame <= 0:
                return f"DONE{direction_suffix} | {current_label}"
            if snapshot.transport_end_frame > 0 and snapshot.playhead_frame >= snapshot.transport_end_frame:
                return f"DONE{direction_suffix} | {current_label}"
            return f"STOPPED{direction_suffix} | {current_label}"
        return f"STOPPED{direction_suffix}"

    def _build_header(self, snapshot: MixerSnapshot, current_seconds: float) -> Panel:
        grid = Table.grid(expand=True)
        grid.add_column(ratio=2, no_wrap=True, overflow="ellipsis")
        grid.add_column(justify="right", no_wrap=True)
        grid.add_row(
            "[bold cyan]TAP[/bold cyan] [dim]terminal audio player[/dim]",
            self._transport_badge(snapshot, current_seconds),
        )
        grid.add_row(
            (
                f"[green]{snapshot.live_track_count} live[/green]  "
                f"[yellow]{snapshot.muted_track_count} muted[/yellow]  "
                f"[cyan]{len(snapshot.groups)} groups[/cyan]  "
                f"[white]{len(snapshot.tracks)} tracks[/white]"
            ),
            (
                f"[bold]{snapshot.strategy.label}[/bold]  [dim]{snapshot.time_mode.label}[/dim]"
                f"{'  [bold bright_magenta]REV[/bold bright_magenta]' if snapshot.reverse_enabled else ''}"
            ),
        )
        return Panel(grid, border_style="cyan", box=box.ROUNDED)

    def _transport_badge(self, snapshot: MixerSnapshot, current_seconds: float) -> str:
        if snapshot.playing:
            state = "PLAYING"
        elif snapshot.paused:
            state = "PAUSED"
        else:
            state = "STOPPED"
        if snapshot.reverse_enabled:
            state = f"{state} REV"
        rate_text = f"  [dim]x{snapshot.playback_rate:.2f}[/dim]" if abs(snapshot.playback_rate - 1.0) > 1e-6 else ""
        return f"[bold]{state}[/bold]  {format_seconds(current_seconds)}{rate_text}"

    def _build_controls(self, snapshot: MixerSnapshot, focus_index: int):
        current_track = snapshot.tracks[focus_index] if snapshot.tracks else None
        current_group = snapshot.groups[current_track.source_index] if current_track is not None else None
        group_muted = bool(current_group and self._group_is_muted(snapshot, current_group.index))
        left_specs: list[tuple[str, str, str, bool, bool, str]] = [
            (
                "P",
                self._play_pause_button_text(snapshot),
                self._play_pause_button_style(snapshot),
                self._control_is_flashed("p"),
                True,
                "p",
            ),
            (
                "S",
                "STOP",
                self._stop_button_style(snapshot),
                self._control_is_flashed("s"),
                False,
                "s",
            ),
        ]
        if self._strategy_change_available():
            left_specs.append(
                (
                    "D",
                    "MENU" if self.strategy_menu_visible and self.strategy_menu_trigger_key == "d" else snapshot.strategy.label,
                    "bold black on cyan",
                    self._control_is_flashed("d") or (self.strategy_menu_visible and self.strategy_menu_trigger_key == "d"),
                    False,
                    "d",
                )
            )
        if is_together_strategy(snapshot.strategy):
            left_specs.extend(
                [
                    (
                        "T",
                        "MENU" if self.strategy_menu_visible and self.strategy_menu_trigger_key == "t" else snapshot.time_mode.label,
                        "bold black on magenta",
                        self._control_is_flashed("t") or (self.strategy_menu_visible and self.strategy_menu_trigger_key == "t"),
                        False,
                        "t",
                    ),
                    (
                        "M",
                        "UNMUTE" if current_track and current_track.muted else "MUTE",
                        "bold black on yellow" if current_track and current_track.muted else "bold white on rgb(62,62,72)",
                        self._control_is_flashed("m") or bool(current_track and current_track.muted),
                        bool(current_track and current_track.muted),
                        "m",
                    ),
                    (
                        "G",
                        "UNMUTE" if group_muted else "MUTE",
                        "bold black on yellow" if group_muted else "bold white on rgb(45,58,88)",
                        self._control_is_flashed("g") or group_muted,
                        group_muted,
                        "g",
                    ),
                ]
            )

        left_buttons = [
            self._control_button(key_label, detail, style, expanded=expanded, filled=filled)
            for key_label, detail, style, expanded, filled, _trigger_key in left_specs
        ]
        right_buttons = [self._space_control_button(snapshot, focus_index), self._enter_control_button(snapshot, focus_index)]
        right_specs: list[tuple[str, str, str, bool, str]] = [
            ("SPACE", self._space_button_detail(snapshot, focus_index), "", self._active_hold_key == "space" or self._control_is_flashed("space"), "space"),
            ("ENTER", self._enter_button_detail(snapshot, focus_index), "", self._control_is_flashed("enter"), "enter"),
        ]
        if not self.disable_backward:
            right_buttons.append(self._seek_control_button("left", snapshot))
            right_specs.append(
                (
                    self._seek_key_label("left"),
                    self._seek_button_detail("left", snapshot),
                    "",
                    self._active_hold_key in {"left", "h"} or self._control_is_flashed("left"),
                    "left",
                )
            )
        if not self.disable_forward:
            right_buttons.append(self._seek_control_button("right", snapshot))
            right_specs.append(
                (
                    self._seek_key_label("right"),
                    self._seek_button_detail("right", snapshot),
                    "",
                    self._active_hold_key in {"right", "l"} or self._control_is_flashed("right"),
                    "right",
                )
            )
        if not self.disable_reverse:
            right_buttons.append(self._reverse_control_button(snapshot))
            right_specs.append(("B", "REV", "", snapshot.reverse_enabled or self._control_is_flashed("b"), "b"))
        right_buttons.extend([self._volume_control_button("up", snapshot), self._volume_control_button("down", snapshot)])
        right_specs.extend(
            [
                ("+", self._volume_button_detail(snapshot), "", self._control_is_flashed("vol_up"), "+"),
                ("-", self._volume_button_detail(snapshot), "", self._control_is_flashed("vol_down"), "-"),
            ]
        )

        left_row = Columns(left_buttons, expand=False, equal=False, padding=(0, 1))
        right_row = Columns(right_buttons, expand=False, equal=False, padding=(0, 1))
        available_width = max(20, self.console.size.width - 6)
        left_width = self._measure_control_row(left_specs)
        right_width = self._measure_control_row(right_specs)
        wrapped = left_width + right_width + 6 > available_width
        base_height = CONTROL_HEIGHT_WRAPPED if wrapped else CONTROL_HEIGHT_COMPACT
        if not wrapped:
            controls_body = Table.grid(expand=True)
            controls_body.add_column(ratio=1)
            controls_body.add_column(ratio=1)
            controls_body.add_row(Align.left(left_row), Align.right(right_row))
        else:
            controls_body = Group(Align.left(left_row), Align.right(right_row))

        self.current_control_height = base_height
        self.strategy_menu_popup_indent = self._strategy_menu_anchor_indent(left_specs)

        return Panel(
            Align.center(controls_body, vertical="middle"),
            title="Controls",
            border_style="white",
            box=box.ROUNDED,
            padding=(0, 1),
        )

    def _strategy_menu_anchor_indent(self, left_specs: list[tuple[str, str, str, bool, bool, str]]) -> int:
        target_key = self.strategy_menu_trigger_key.upper()
        offset = 2
        for index, spec in enumerate(left_specs):
            key_label, detail, _style, expanded = spec[:4]
            if key_label == target_key:
                return offset
            offset += self._control_button_width(key_label, detail, expanded)
            if index < len(left_specs) - 1:
                offset += 2
        return 2

    def _build_strategy_menu_popup(self, snapshot: MixerSnapshot) -> RenderableType | None:
        if not self.strategy_menu_visible:
            return None

        menu_lines = self._strategy_menu_lines(snapshot)
        return self._strategy_menu_panel(snapshot, menu_lines)

    def _delete_confirm_popup_width(self) -> int:
        return min(64, max(44, self.console.size.width - 10))

    def _delete_confirm_popup_height(self, snapshot: MixerSnapshot) -> int:
        extra_lines = 1 if self.delete_confirm_resume_on_cancel else 0
        if self.delete_confirm_track_index is None or not snapshot.tracks:
            extra_lines += 1
        return 6 + extra_lines

    def _build_delete_confirm_popup(self, snapshot: MixerSnapshot) -> RenderableType | None:
        target_index = self.delete_confirm_track_index
        if target_index is None:
            return None

        lines: list[RenderableType] = []
        if 0 <= target_index < len(snapshot.tracks):
            track = snapshot.tracks[target_index]
            group = snapshot.groups[track.source_index]
            lines.append(Text(f"Remove [{track.index + 1}] {track.label}?", style="bold bright_white"))
            lines.append(Text(f"Source: {group.label}", style="dim"))
        else:
            lines.append(Text("The selected track is no longer available.", style="yellow"))
        lines.append(Text("This only removes it from the current playlist.", style="dim"))
        if self.delete_confirm_resume_on_cancel:
            lines.append(Text("Current playback is paused until you confirm or cancel.", style="yellow"))

        return Panel(
            Group(*lines),
            title="Remove Track",
            subtitle="Enter confirm | Esc cancel",
            border_style="yellow",
            box=box.SQUARE,
            padding=(0, 1),
            width=self._delete_confirm_popup_width(),
        )

    def _strategy_menu_lines(self, snapshot: MixerSnapshot) -> list[Text]:
        options = self._strategy_menu_options()
        if not options:
            return [Text("No modes available.", style="dim")]

        selected_strategy = options[min(self.strategy_menu_index, max(len(options) - 1, 0))]
        solo_options = [strategy for strategy in options if strategy in SOLO_PLAYBACK_STRATEGIES]
        together_options = [strategy for strategy in options if is_together_strategy(strategy)]

        lines: list[Text] = []
        for strategy in solo_options:
            lines.append(self._strategy_menu_option_line(strategy, selected_strategy, snapshot.strategy, "white"))
        if solo_options and together_options:
            lines.append(Text("  ---------------------------"))
        for strategy in together_options:
            lines.append(self._strategy_menu_option_line(strategy, selected_strategy, snapshot.strategy, "bold magenta"))
        return lines

    def _strategy_menu_option_line(
        self,
        strategy: PlaybackStrategy,
        selected_strategy: PlaybackStrategy,
        active_strategy: PlaybackStrategy,
        base_style: str,
    ) -> Text:
        selected = strategy is selected_strategy
        active = strategy is active_strategy
        style = "bold black on bright_cyan" if selected else base_style
        prefix = "> " if selected else "  "
        suffix = "  <" if active else ""
        return Text.assemble((prefix, style), (strategy.label, style), (suffix, "green"))

    def _strategy_menu_panel(self, snapshot: MixerSnapshot, lines: list[Text]) -> Panel:
        title = "Modes"
        move_hint = self._focus_key_hint()
        available_width = max(16, self.console.size.width - self.strategy_menu_popup_indent - 2)
        return Panel(
            Group(*lines),
            title=title,
            subtitle=f"{move_hint} choose | Enter apply | Esc close",
            border_style="magenta" if is_together_strategy(snapshot.strategy) else "white",
            box=box.SQUARE,
            padding=(0, 1),
            width=min(34, available_width),
        )

    def _control_button(
        self,
        key_label: str,
        detail: str,
        style: str,
        *,
        expanded: bool,
        filled: bool = False,
    ) -> RenderableType:
        label = self._control_display_label(key_label, detail, expanded)
        text_style, fill_style, border_style = self._split_control_style(style, filled=filled)
        content = Align.center(
            Text(f" {label} ", style=f"{text_style}{fill_style}", justify="center", no_wrap=True),
            vertical="middle",
        )
        return Panel(
            content,
            box=box.SQUARE,
            border_style=border_style,
            padding=(0, 1),
        )

    def _measure_control_row(self, specs: list[tuple]) -> int:
        widths = []
        for spec in specs:
            key_label, detail, _style, expanded = spec[:4]
            widths.append(self._control_button_width(key_label, detail, expanded))
        return sum(widths) + max(0, len(widths) - 1) * 2

    def _control_button_width(self, key_label: str, detail: str, expanded: bool) -> int:
        label = self._control_display_label(key_label, detail, expanded)
        return len(label) + 6

    def _control_display_label(self, key_label: str, detail: str, expanded: bool) -> str:
        return f"{key_label} {detail}" if expanded and detail else key_label

    def _focus_key_hint(self) -> str:
        if self.arrow_mode is ArrowMode.HJKL:
            return "J/K"
        if self.arrow_mode is ArrowMode.ARROWS:
            return "\u2191/\u2193"
        return "\u2191/\u2193/J/K"

    def _split_control_style(self, style: str, *, filled: bool) -> tuple[str, str, str]:
        if " on " not in style:
            return style, "", style
        text_style, background = style.rsplit(" on ", 1)
        foreground = text_style.replace("bold", "").strip() or "white"
        if filled:
            accent = foreground if foreground != "black" else background
            return text_style, f" on {background}", f"bold {accent}"
        accent = foreground if foreground != "black" else background
        return f"bold {accent}", "", f"bold {accent}"

    def _play_pause_button_text(self, snapshot: MixerSnapshot) -> str:
        return "PAUSE" if snapshot.playing else "PLAY"

    def _play_pause_button_style(self, snapshot: MixerSnapshot) -> str:
        if snapshot.playing:
            return "bold black on yellow"
        return "bold black on green"

    def _stop_button_style(self, snapshot: MixerSnapshot) -> str:
        if not snapshot.playing and not snapshot.paused:
            return "bold black on red"
        return "bold white on rgb(98,35,35)"

    def _space_button_detail(self, snapshot: MixerSnapshot, focus_index: int) -> str:
        if self._active_hold_key == "space":
            return f"x{snapshot.playback_rate:.2f}"
        action = self._space_button_label(snapshot, focus_index)
        return action.split(" ", 1)[1] if " " in action else action

    def _space_control_button(self, snapshot: MixerSnapshot, focus_index: int) -> RenderableType:
        active_hold = self._active_hold_key == "space"
        flashed = self._control_is_flashed("space")
        if active_hold:
            style, _ = self._transport_progress_styles(snapshot)
            button_style = f"bold black on {style}"
        else:
            button_style = "bold black on yellow"
        return self._control_button(
            "SPACE",
            self._space_button_detail(snapshot, focus_index),
            button_style,
            expanded=active_hold or flashed,
            filled=False,
        )

    def _enter_button_detail(self, snapshot: MixerSnapshot, focus_index: int) -> str:
        action = self._enter_button_label(snapshot, focus_index)
        return action.split(" ", 1)[1] if " " in action else action

    def _enter_control_button(self, snapshot: MixerSnapshot, focus_index: int) -> RenderableType:
        return self._control_button(
            "ENTER",
            self._enter_button_detail(snapshot, focus_index),
            "bold black on bright_cyan",
            expanded=self._control_is_flashed("enter"),
            filled=False,
        )

    def _seek_key_label(self, key: str) -> str:
        if key == "left":
            if self.arrow_mode is ArrowMode.HJKL:
                return "H"
            if self.arrow_mode is ArrowMode.ARROWS:
                return "\u2190"
            return "\u2190/H"
        if self.arrow_mode is ArrowMode.HJKL:
            return "L"
        if self.arrow_mode is ArrowMode.ARROWS:
            return "\u2192"
        return "\u2192/L"

    def _seek_button_detail(self, key: str, snapshot: MixerSnapshot) -> str:
        if self._active_hold_key == key:
            return f"x{snapshot.playback_rate:.2f}"
        seconds = self.backward_seconds if key in {"left", "h"} else self.forward_seconds
        sign = "-" if key in {"left", "h"} else "+"
        return f"{sign}{seconds:.1f}s"

    def _seek_control_button(self, key: str, snapshot: MixerSnapshot) -> RenderableType:
        active_hold = self._active_hold_key == key
        flashed = self._control_is_flashed(key)
        if active_hold:
            style, _ = self._transport_progress_styles(snapshot)
            button_style = f"bold black on {style}"
        else:
            button_style = "bold black on white"
        return self._control_button(
            self._seek_key_label(key),
            self._seek_button_detail(key, snapshot),
            button_style,
            expanded=active_hold or flashed,
            filled=False,
        )

    def _reverse_control_button(self, snapshot: MixerSnapshot) -> RenderableType:
        flashed = self._control_is_flashed("b")
        if snapshot.reverse_enabled or flashed:
            return self._control_button(
                "B",
                "REV",
                "bold black on bright_magenta",
                expanded=True,
                filled=snapshot.reverse_enabled,
            )
        return self._control_button("B", "REV", "bold white on rgb(64,46,76)", expanded=False, filled=False)

    def _volume_button_detail(self, snapshot: MixerSnapshot) -> str:
        return f"VOL {int(round(snapshot.volume * 100))}%"

    def _volume_control_button(self, direction: str, snapshot: MixerSnapshot) -> RenderableType:
        control_id = "vol_up" if direction == "up" else "vol_down"
        key_label = "+" if direction == "up" else "-"
        style = "bold black on green" if direction == "up" else "bold black on red"
        return self._control_button(
            key_label,
            self._volume_button_detail(snapshot),
            style,
            expanded=self._control_is_flashed(control_id),
            filled=False,
        )

    def _enter_button_label(self, snapshot: MixerSnapshot, focus_index: int) -> str:
        action = self._track_action_label(snapshot, focus_index)
        return f"ENTER {action}"

    def _space_button_label(self, snapshot: MixerSnapshot, focus_index: int) -> str:
        if snapshot.playback_rate > 1.0:
            return f"SPACE HOLD {snapshot.playback_rate:.2f}X"
        if is_together_strategy(snapshot.strategy):
            if snapshot.playing:
                return "SPACE PAUSE"
            if snapshot.paused and snapshot.playhead_frame > 0:
                return "SPACE RESUME"
            return "SPACE PLAY"
        return f"SPACE {self._track_action_label(snapshot, focus_index)}"

    def _track_action_label(self, snapshot: MixerSnapshot, track_index: int) -> str:
        if is_together_strategy(snapshot.strategy):
            track = snapshot.tracks[track_index]
            return "UNMUTE" if track.muted else "MUTE"
        if snapshot.current_track_index == track_index and snapshot.playing:
            return "PAUSE"
        if snapshot.current_track_index == track_index and snapshot.paused:
            return "RESUME"
        return "PLAY"

    def _track_action_button(self, snapshot: MixerSnapshot, track_index: int) -> Text:
        action = self._track_action_label(snapshot, track_index)
        if is_together_strategy(snapshot.strategy):
            track = snapshot.tracks[track_index]
            style = "bold black on yellow" if track.muted else "bold black on white"
            return button_text(action, style)
        if action == "PAUSE":
            return button_text(action, "bold black on yellow")
        return button_text(action, "bold black on green")

    def _build_table(self, snapshot: MixerSnapshot, focus_index: int, current_seconds: float) -> Panel:
        tree_rows, track_row_map = self._build_tree_rows(snapshot)
        focus_row_index = track_row_map.get(focus_index, 0)
        visible_rows = self._visible_track_rows()
        start_index, end_index = self._resolve_scroll_window(tree_rows, focus_row_index, visible_rows)
        visible_tree_rows = tree_rows[start_index:end_index]
        scrollbar = self._scrollbar_cells(len(tree_rows), len(visible_tree_rows), start_index)

        table = Table(
            expand=True,
            box=None,
            show_edge=False,
            pad_edge=False,
            header_style="bold bright_cyan",
        )
        table.add_column(">", width=1, no_wrap=True)
        table.add_column("#", justify="right", width=4, no_wrap=True)
        table.add_column("Tree", ratio=1, no_wrap=True, overflow="ellipsis")
        table.add_column("Aud", width=5, justify="center", no_wrap=True)
        table.add_column("State", width=7, justify="center", no_wrap=True)
        table.add_column("Len", width=8, no_wrap=True, justify="right")
        table.add_column("Timeline", width=17, no_wrap=True, overflow="crop")
        table.add_column("Act", width=9, no_wrap=True, justify="center")
        table.add_column("", width=1, no_wrap=True, justify="center")

        for offset, row in enumerate(visible_tree_rows):
            if row.kind == "group":
                group = snapshot.groups[row.group_index]
                live_in_group = sum(1 for index in group.track_indices if not self._track_is_muted(snapshot, snapshot.tracks[index]))
                group_muted = is_together_strategy(snapshot.strategy) and live_in_group == 0
                table.add_row(
                    Text(" ", style="dim"),
                    Text(" ", style="dim"),
                    Text(row.tree_label, style="bold bright_white"),
                    Text("MUTE" if group_muted else f"{live_in_group}", style="bold yellow" if group_muted else "bold green"),
                    Text("GROUP", style="bold cyan"),
                    Text(f"{len(group.track_indices)}trk", style="dim"),
                    Text("", style="dim"),
                    button_text("SOURCE", "bold black on blue"),
                    scrollbar[offset],
                    style="bold white on rgb(35,35,55)",
                )
                continue

            assert row.track_index is not None
            track = snapshot.tracks[row.track_index]
            focused = row.track_index == focus_index
            marker = Text(">", style="bold cyan") if focused else Text(" ")
            audible = Text("MUTE", style="bold yellow") if self._track_is_muted(snapshot, track) else Text("LIVE", style="bold green")
            state = self._track_state(snapshot, track, current_seconds)
            timeline = self._track_timeline(snapshot, track, current_seconds)
            action = self._track_action_button(snapshot, row.track_index)
            row_style = "bold white on rgb(24,51,77)" if focused else ""
            table.add_row(
                marker,
                str(track.index + 1),
                row.tree_label,
                audible,
                state,
                format_duration_label(track.duration_seconds),
                Text(timeline, no_wrap=True),
                action,
                scrollbar[offset],
                style=row_style,
            )

        title = f"Library {start_index + 1}-{max(start_index + len(visible_tree_rows), 1)}/{max(len(tree_rows), 1)}"
        return Panel(
            table,
            title=title,
            border_style="bright_blue",
            box=box.ROUNDED,
            padding=(0, 1),
        )

    def _build_tree_rows(self, snapshot: MixerSnapshot) -> tuple[list[TreeRow], dict[int, int]]:
        rows: list[TreeRow] = []
        track_row_map: dict[int, int] = {}
        for group in snapshot.groups:
            group_prefix = "[DIR]" if group.kind == "dir" else "[FILES]"
            rows.append(
                TreeRow(
                    kind="group",
                    group_index=group.index,
                    track_index=None,
                    tree_label=f"{group_prefix} {group.label}",
                )
            )
            for offset, track_index in enumerate(group.track_indices):
                track = snapshot.tracks[track_index]
                branch = "`--" if offset == len(group.track_indices) - 1 else "|--"
                row_position = len(rows)
                rows.append(
                    TreeRow(
                        kind="track",
                        group_index=group.index,
                        track_index=track_index,
                        tree_label=f"  {branch} {track.label}",
                    )
                )
                track_row_map[track.index] = row_position
        return rows, track_row_map

    def _group_header_row_index(self, tree_rows: list[TreeRow], row_index: int) -> int | None:
        for index in range(row_index, -1, -1):
            if tree_rows[index].kind == "group":
                return index
        return None

    def _track_state(self, snapshot: MixerSnapshot, track: AudioTrack, current_seconds: float) -> Text:
        if self._track_is_muted(snapshot, track):
            return Text("MUTE", style="bold yellow")
        if is_together_strategy(snapshot.strategy):
            shown_time = min(current_seconds, track.duration_seconds)
            if snapshot.reverse_enabled:
                if current_seconds > track.duration_seconds:
                    return Text("WAIT", style="dim")
                if shown_time <= 0 and track.duration_seconds > 0:
                    return Text("DONE", style="dim")
            elif shown_time >= track.duration_seconds and track.duration_seconds > 0:
                return Text("DONE", style="dim")
            if snapshot.playing:
                return Text("PLAY", style="bold green")
            if snapshot.paused and snapshot.playhead_frame > 0:
                return Text("PAUSE", style="bold cyan")
            return Text("READY", style="bold cyan")
        if snapshot.current_track_index == track.index:
            if snapshot.playing:
                return Text("PLAY", style="bold green")
            if snapshot.paused and 0 < snapshot.playhead_frame < track.frame_count:
                return Text("PAUSE", style="bold cyan")
            if snapshot.reverse_enabled and snapshot.playhead_frame <= 0 and track.frame_count > 0:
                return Text("DONE", style="dim")
            if not snapshot.reverse_enabled and snapshot.playhead_frame >= track.frame_count and track.frame_count > 0:
                return Text("DONE", style="dim")
            return Text("READY", style="bold cyan")
        return Text("WAIT", style="dim")

    def _track_timeline(self, snapshot: MixerSnapshot, track: AudioTrack, current_seconds: float) -> str:
        if is_together_strategy(snapshot.strategy):
            if self._track_is_muted(snapshot, track):
                return f"--:--.--/{format_compact_seconds(track.duration_seconds)}"
            shown_time = min(current_seconds, track.duration_seconds)
            return f"{format_compact_seconds(shown_time)}/{format_compact_seconds(track.duration_seconds)}"
        if snapshot.current_track_index == track.index and snapshot.playhead_frame > 0:
            shown_time = min(snapshot.playhead_frame / self.mixer.sample_rate, track.duration_seconds)
            return f"{format_compact_seconds(shown_time)}/{format_compact_seconds(track.duration_seconds)}"
        return f"--:--.--/{format_compact_seconds(track.duration_seconds)}"

    def _track_is_muted(self, snapshot: MixerSnapshot, track: AudioTrack) -> bool:
        return is_together_strategy(snapshot.strategy) and track.muted

    def _visible_track_rows(self) -> int:
        available_height = self.console.size.height - HEADER_HEIGHT - self.current_control_height - STATUS_HEIGHT
        return max(3, available_height - TABLE_CHROME_HEIGHT)

    def _resolve_scroll_window(
        self,
        tree_rows: list[TreeRow],
        focus_row_index: int,
        visible_rows: int,
    ) -> tuple[int, int]:
        total_rows = len(tree_rows)
        if total_rows <= visible_rows:
            self.table_scroll_top = 0
            return 0, total_rows

        max_start = max(0, total_rows - visible_rows)
        start_index = max(0, min(self.table_scroll_top, max_start))
        end_index = start_index + visible_rows

        if focus_row_index < start_index:
            start_index = focus_row_index
        elif focus_row_index >= end_index:
            start_index = focus_row_index - visible_rows + 1

        group_header_index = self._group_header_row_index(tree_rows, focus_row_index)
        if group_header_index is not None and group_header_index < start_index:
            if focus_row_index - group_header_index < visible_rows:
                start_index = group_header_index

        start_index = max(0, min(start_index, max_start))
        self.table_scroll_top = start_index
        return start_index, min(total_rows, start_index + visible_rows)

    def _scrollbar_cells(self, total_tracks: int, visible_rows: int, start_index: int) -> list[Text]:
        if visible_rows <= 0:
            return []
        if total_tracks <= visible_rows:
            return [Text(" ", style="on rgb(38,38,48)") for _ in range(visible_rows)]

        thumb_height = max(1, round((visible_rows * visible_rows) / total_tracks))
        max_start = max(1, total_tracks - visible_rows)
        thumb_top = round((visible_rows - thumb_height) * (start_index / max_start))
        cells: list[Text] = []
        for row in range(visible_rows):
            if thumb_top <= row < thumb_top + thumb_height:
                cells.append(Text(" ", style="on bright_cyan"))
            else:
                cells.append(Text(" ", style="on rgb(38,38,48)"))
        return cells

    def _build_status_panel(
        self,
        snapshot: MixerSnapshot,
        focus_index: int,
        status_message: str,
        failures: list[LoadFailure],
        warnings: list[str],
    ) -> Panel:
        lines = [Text(f"Status: {status_message}", style="bold"), self._build_transport_progress(snapshot)]
        if snapshot.tracks:
            track = snapshot.tracks[focus_index]
            group = snapshot.groups[track.source_index]
            live_in_group = sum(1 for index in group.track_indices if not self._track_is_muted(snapshot, snapshot.tracks[index]))
            lines.append(
                Text(
                    f"Focus: [{track.index + 1}] {track.label} | Source: {group.label} "
                    f"({group.kind}, {len(group.track_indices)} track(s), {live_in_group} live)",
                    style="dim",
                )
            )
        else:
            lines.append(Text("Focus: playlist is empty", style="dim"))
        lines.append(Text("Help: U/F1 | Close/Quit: Esc/Ctrl+C/Q", style="dim"))
        if snapshot.audio_error:
            lines.append(Text(f"Audio output warning: {snapshot.audio_error}", style="bold red"))
        if warnings:
            lines.append(Text(f"Input warning: {warnings[0]}", style="yellow"))
        if failures:
            preview = ", ".join(failure.path.name for failure in failures[:3])
            suffix = " ..." if len(failures) > 3 else ""
            lines.append(Text(f"Decode failures ({len(failures)}): {preview}{suffix}", style="yellow"))
        body = Table.grid(expand=True)
        body.add_column(ratio=1)
        body.add_column(width=6)
        body.add_row(Group(*lines), Align.center(self._build_volume_meter(snapshot), vertical="middle"))
        return Panel(body, title="Status", border_style="white", box=box.ROUNDED)

    def _build_help_screen(
        self,
        snapshot: MixerSnapshot,
        focus_index: int,
        failures: list[LoadFailure],
        warnings: list[str],
    ) -> RenderableType:
        lines: list[RenderableType] = [
            Text("U / F1 closes this help. Esc / Ctrl+C / Q closes popups first, then exits TAP.", style="bold"),
            Text(""),
            Text("Keys", style="bold bright_cyan"),
        ]
        for part in self._key_hint_parts(snapshot):
            lines.append(Text(f"- {part}", style="white"))
        lines.extend(
            [
                Text(""),
                Text("Behavior", style="bold bright_cyan"),
                Text(self._hold_help_text(snapshot), style="dim"),
                Text(self._strategy_help_text(), style="dim"),
                Text(self._time_mode_help_text(snapshot), style="dim"),
                Text(self._enter_help_text(snapshot, focus_index), style="dim"),
                Text(self._space_help_text(snapshot, focus_index), style="dim"),
                Text(self._list_modify_help_text(snapshot, focus_index), style="dim"),
            ]
        )
        if not self.disable_reverse:
            lines.append(Text(self._reverse_help_text(snapshot), style="dim"))
        lines.extend(
            [
                Text(""),
                Text(
                    (
                        f"Focus: [{snapshot.tracks[focus_index].index + 1}] {snapshot.tracks[focus_index].label} | "
                        f"Strategy: {snapshot.strategy.label} | Mode: {self.generic_mode.label} | Time: {snapshot.time_mode.label}"
                    )
                    if snapshot.tracks
                    else f"Focus: playlist is empty | Strategy: {snapshot.strategy.label} | "
                    f"Mode: {self.generic_mode.label} | Time: {snapshot.time_mode.label}",
                    style="dim",
                ),
            ]
        )
        if snapshot.audio_error:
            lines.append(Text(f"Audio output warning: {snapshot.audio_error}", style="bold red"))
        if warnings:
            lines.append(Text(f"Input warning: {warnings[0]}", style="yellow"))
        if failures:
            preview = ", ".join(failure.path.name for failure in failures[:3])
            suffix = " ..." if len(failures) > 3 else ""
            lines.append(Text(f"Decode failures ({len(failures)}): {preview}{suffix}", style="yellow"))
        return Align.center(
            Panel(
                Group(*lines),
                title="Help",
                subtitle="Press U or F1 to return",
                border_style="bright_cyan",
                box=box.ROUNDED,
                padding=(1, 2),
                width=min(108, max(72, self.console.size.width - 6)),
            ),
            vertical="middle",
            width=self.console.size.width,
            height=self.console.size.height,
        )

    def _build_transport_progress(self, snapshot: MixerSnapshot) -> Table:
        completed_frames = max(0.0, min(snapshot.playhead_frame, float(snapshot.transport_end_frame)))
        total_frames = max(1.0, float(snapshot.transport_end_frame or 1))
        completed_seconds = completed_frames / self.mixer.sample_rate
        total_seconds = snapshot.transport_end_frame / self.mixer.sample_rate
        bar_style, label_style = self._transport_progress_styles(snapshot)

        grid = Table.grid(expand=True)
        grid.add_column(width=8, no_wrap=True)
        grid.add_column(ratio=1)
        grid.add_column(justify="right", no_wrap=True)
        grid.add_row(
            Text("Global", style=label_style),
            ProgressBar(
                total=total_frames,
                completed=completed_frames,
                width=None,
                complete_style=bar_style,
                finished_style=bar_style,
                pulse_style=bar_style,
            ),
            Text(
                f"{format_compact_seconds(completed_seconds)}/{format_compact_seconds(total_seconds)}",
                style="dim",
            ),
        )
        return grid

    def _transport_progress_styles(self, snapshot: MixerSnapshot) -> tuple[str, str]:
        if snapshot.paused:
            return "yellow", "bold yellow"
        if snapshot.playback_rate > 1.0:
            return "green", "bold green"
        if 0 < snapshot.playback_rate < 1.0:
            return "rgb(168,85,247)", "bold rgb(168,85,247)"
        return "bright_cyan", "bold cyan"

    def _build_volume_meter(self, snapshot: MixerSnapshot) -> Group:
        height = 7
        filled = max(0, min(height, int(round(snapshot.volume * height))))
        lines: list[RenderableType] = [Align.center(Text("V", style="bold bright_green"))]
        for row in range(height):
            active = row >= height - filled
            style = "on green" if active else "on rgb(38,38,48)"
            lines.append(Align.center(Text(" ", style=style)))
        lines.append(Align.center(Text(f"{int(round(snapshot.volume * 100)):>3d}%", style="bold")))
        return Group(*lines)

    def _key_hint_parts(self, snapshot: MixerSnapshot) -> list[str]:
        parts = [f"{self._focus_key_hint()} Move", "P Play/Pause", "S Stop"]
        if self._strategy_change_available():
            parts.append("D Strategy")
            parts.append("Hold D Modes")
        parts.extend(["Space", "Enter"])
        if not self.disable_backward:
            parts.append(f"{self._seek_key_label('left')} -{self.backward_seconds:.1f}s")
        if not self.disable_forward:
            parts.append(f"{self._seek_key_label('right')} +{self.forward_seconds:.1f}s")
        if not self.disable_reverse:
            parts.append("B Reverse")
        if self._list_modify_available():
            parts.append("Bksp/Delete Remove")
        if is_together_strategy(snapshot.strategy):
            parts.append("T/M/G")
        parts.append("+/- Volume")
        parts.append("U/F1 Help")
        parts.append("Esc/Ctrl+C/Q Close/Quit")
        return parts

    def _group_is_muted(self, snapshot: MixerSnapshot, group_index: int) -> bool:
        group = snapshot.groups[group_index]
        return bool(group.track_indices) and all(self._track_is_muted(snapshot, snapshot.tracks[index]) for index in group.track_indices)

    def _enter_help_text(self, snapshot: MixerSnapshot, focus_index: int) -> str:
        if is_together_strategy(snapshot.strategy):
            return "Enter toggles mute for the focused track in TOGETHER modes."
        if snapshot.current_track_index == focus_index and snapshot.playing:
            return "Enter pauses the focused current track."
        if snapshot.current_track_index == focus_index and snapshot.paused:
            return "Enter resumes the focused current track."
        return "Enter stops the current transport and starts playback from the focused track."

    def _space_help_text(self, snapshot: MixerSnapshot, focus_index: int) -> str:
        if snapshot.reverse_enabled and snapshot.playback_rate > 1.0:
            labels = ["Space"]
            if not self.disable_backward:
                labels.append(self._seek_key_label("left"))
            return f"{'/'.join(labels)} hold is currently driving reverse playback at x{snapshot.playback_rate:.2f}."
        if snapshot.reverse_enabled and 0 < snapshot.playback_rate < 1.0:
            return f"{self._seek_key_label('right')} hold is currently driving reverse playback at x{snapshot.playback_rate:.2f}."
        if snapshot.playback_rate > 1.0:
            labels = ["Space"]
            if not self.disable_forward:
                labels.append(self._seek_key_label("right"))
            return f"{'/'.join(labels)} hold is currently driving playback at x{snapshot.playback_rate:.2f}."
        if 0 < snapshot.playback_rate < 1.0:
            return f"{self._seek_key_label('left')} hold is currently driving playback at x{snapshot.playback_rate:.2f}."
        if is_together_strategy(snapshot.strategy):
            if snapshot.playing:
                return "Space pauses the full TOGETHER mix."
            if snapshot.paused and snapshot.playhead_frame > 0:
                return "Space resumes the full TOGETHER mix."
            return "Space starts the full TOGETHER mix."
        if snapshot.current_track_index == focus_index and snapshot.playing:
            return "Space pauses the focused current track, same as Enter in single-track modes."
        if snapshot.current_track_index == focus_index and snapshot.paused:
            return "Space resumes the focused current track, same as Enter in single-track modes."
        return "Space stops the current transport and starts playback from the focused track."

    def _time_mode_help_text(self, snapshot: MixerSnapshot) -> str:
        if not is_together_strategy(snapshot.strategy):
            return "Time mode is only used by TOGETHER modes."
        return "Tap T to cycle AUTO-MAX/AUTO-MIN."

    def _strategy_help_text(self) -> str:
        if not self._strategy_change_available():
            return "Strategy changes are disabled."
        return "Tap D to cycle strategies. Hold D to open the mode menu."

    def _list_modify_help_text(self, snapshot: MixerSnapshot, focus_index: int) -> str:
        if not self._list_modify_available():
            return "Playlist deletion is disabled."
        if not snapshot.tracks:
            return "Backspace/Delete opens a confirmation dialog to remove the focused track from the current playlist."
        if (
            not is_together_strategy(snapshot.strategy)
            and snapshot.current_track_index == focus_index
            and snapshot.playing
        ):
            return (
                "Backspace/Delete opens a confirmation dialog. If the focused solo track is currently playing, "
                "it pauses until you confirm or cancel."
            )
        return "Backspace/Delete opens a confirmation dialog to remove the focused track from the current playlist."

    def _reverse_help_text(self, snapshot: MixerSnapshot) -> str:
        if snapshot.reverse_enabled:
            parts: list[str] = []
            if not self.disable_backward:
                parts.append(f"{self._seek_key_label('left')} seeks toward the start")
            if not self.disable_forward:
                parts.append(f"{self._seek_key_label('right')} seeks toward the end")
            if not self.disable_fast_forward:
                fast_keys = ["Space"]
                if not self.disable_backward:
                    fast_keys.append(self._seek_key_label("left"))
                parts.append(f"hold {'/'.join(fast_keys)} for fast reverse")
            if not self.disable_fast_backward and not self.disable_forward:
                parts.append(f"hold {self._seek_key_label('right')} for slow reverse")
            if parts:
                return "Reverse controls: " + ". ".join(parts) + "."
            return "Reverse playback is active."
        return "B toggles the whole transport into reverse playback mode."

    def _hold_help_text(self, snapshot: MixerSnapshot) -> str:
        fast_labels: list[str] = []
        slow_labels: list[str] = []
        if snapshot.reverse_enabled:
            if not self.disable_fast_forward:
                fast_labels.append("Space")
                if not self.disable_backward:
                    fast_labels.append(self._seek_key_label("left"))
            if not self.disable_fast_backward and not self.disable_forward:
                slow_labels.append(self._seek_key_label("right"))
        else:
            if not self.disable_fast_forward:
                fast_labels.append("Space")
                if not self.disable_forward:
                    fast_labels.append(self._seek_key_label("right"))
            if not self.disable_fast_backward and not self.disable_backward:
                slow_labels.append(self._seek_key_label("left"))

        if not fast_labels and not slow_labels:
            return "Hold controls are disabled."

        segments = []
        if fast_labels:
            segments.append(f"Hold {'/'.join(fast_labels)} for x{self.rate_fast_forward:.2f}")
        if slow_labels:
            segments.append(f"hold {'/'.join(slow_labels)} for x{self.rate_slow_forward:.2f}")
        prefix = "P/S"
        if not self.disable_reverse:
            prefix += "/B"
        return f"{prefix} stay global. {'; '.join(segments)}. Seek still follows the forward timeline."


def prompt_for_inputs(console: Console) -> list[str]:
    while True:
        raw = console.input("[bold cyan]Files/Folders[/bold cyan] > ").strip()
        if not raw:
            return ["."]
        try:
            items = shlex.split(raw, posix=(os.name != "nt"))
        except ValueError as exc:
            console.print(f"[red]Parse error:[/red] {exc}")
            continue
        if items:
            return items


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(actual_argv)
    allowed_strategies = allowed_strategies_for_generic_mode(args.generic_mode)
    if args.strategy is None:
        args.strategy = allowed_strategies[0]
    elif args.strategy not in allowed_strategies:
        allowed = ", ".join(strategy.value for strategy in allowed_strategies)
        parser.error(f"--strategy {args.strategy.value} is incompatible with --generic-mode {args.generic_mode.value}. Allowed: {allowed}")
    console = Console()
    try:
        raw_inputs = args.files or prompt_for_inputs(console)
        app = MixListenApp(
            console,
            strategy=args.strategy,
            time_mode=args.time_mode,
            arrow_mode=args.arrow_mode,
            decode_backend=args.decode_backend,
            generic_mode=args.generic_mode,
            forward_seconds=args.forward_seconds,
            backward_seconds=args.backward_seconds,
            rate_fast_forward=args.rate_fast_forward,
            rate_slow_forward=args.rate_slow_forward,
            disable_forward=args.disable_forward,
            disable_fast_forward=args.disable_fast_forward,
            disable_backward=args.disable_backward,
            disable_fast_backward=args.disable_fast_backward,
            disable_reverse=args.disable_reverse,
            disable_strategy_change=args.disable_strategy_change,
            disable_list_modify=args.disable_list_modify,
        )
        app.load_inputs(raw_inputs)
        app.run()
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Exited.[/bold yellow]")
    except Exception as exc:
        console.print(f"[bold red]Startup failed:[/bold red] {exc}")
        raise SystemExit(1) from exc

if __name__ == "__main__":
    main()
    
