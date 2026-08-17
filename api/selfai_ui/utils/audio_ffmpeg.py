"""Direct ffmpeg/ffprobe helpers — the replacement for pydub (#106).

pydub was never a decoder: it is a thin wrapper that shells out to the ffmpeg
binary this image already installs (``api/Dockerfile`` apt-installs ffmpeg
explicitly). Calling ffmpeg ourselves therefore keeps the *same* underlying
engine and behaviour while dropping two dependencies:

* ``pydub`` — unmaintained (last release 0.25.1, March 2021) and unpinned
* ``audioop-lts`` — the shim only needed because pydub imports the ``audioop``
  stdlib module that Python 3.13 deleted (PEP 594)

Security notes, because this is a subprocess surface (see #58):

* every invocation passes an explicit argument **list**, never a shell string,
  and ``shell=True`` is never used — so a path can never be word-split or
  interpreted as a shell token
* input paths are passed as the value of ``-i``, so they cannot be parsed as
  options; output paths (which ffmpeg requires positionally) are rejected if
  they begin with ``-``
* every call is bounded by a timeout, so a malformed upload cannot pin a worker
  forever
"""

import json
import logging
import os
import subprocess

log = logging.getLogger(__name__)

# Generous, but finite. Transcode work here is small (single uploads), so a call
# running longer than this means something is wrong rather than slow.
FFMPEG_TIMEOUT_SECONDS = 300
FFPROBE_TIMEOUT_SECONDS = 30


class AudioConversionError(RuntimeError):
    """ffmpeg/ffprobe was unavailable, timed out, or exited non-zero."""


def _checked_path(path, label: str) -> str:
    """Reject a path that ffmpeg would parse as an option instead of a file."""
    path = os.fspath(path)
    if path.startswith("-"):
        raise AudioConversionError(f"{label} must not start with '-': {path!r}")
    return path


def _run(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=True)
    except FileNotFoundError as e:
        raise AudioConversionError(f"{argv[0]} not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise AudioConversionError(f"{argv[0]} timed out after {timeout}s") from e
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        raise AudioConversionError(f"{argv[0]} failed (exit {e.returncode}): {stderr[:500]}") from e


def probe_audio_stream(file_path) -> dict:
    """Return the first audio stream's metadata, or ``{}`` if there is none.

    Replaces ``pydub.utils.mediainfo``, which ran ffprobe and parsed its output.
    Returning ``{}`` rather than raising keeps the ``is_mp4_audio`` contract:
    "not a probeable audio file" is an answer, not an error.
    """
    file_path = _checked_path(file_path, "input")
    if not os.path.isfile(file_path):
        log.debug("probe_audio_stream: not a file: %s", file_path)
        return {}

    proc = _run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name,codec_type,codec_tag_string",
            "-of", "json",
            "-i", file_path,
        ],
        timeout=FFPROBE_TIMEOUT_SECONDS,
    )
    try:
        streams = json.loads(proc.stdout).get("streams") or []
    except json.JSONDecodeError:
        log.warning("probe_audio_stream: ffprobe returned unparseable JSON for %s", file_path)
        return {}
    return streams[0] if streams else {}


def convert_mp4_to_wav(file_path, output_path) -> None:
    """Decode an MP4 audio file to WAV.

    ``-f mp4`` mirrors pydub's ``AudioSegment.from_file(..., format="mp4")``:
    the container is asserted rather than sniffed, which is the point of the
    ``is_mp4_audio`` check that gates this call.
    """
    file_path = _checked_path(file_path, "input")
    output_path = _checked_path(output_path, "output")
    _run(
        ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "mp4", "-i", file_path, output_path],
        timeout=FFMPEG_TIMEOUT_SECONDS,
    )


def compress_to_opus(
    file_path, output_path, *, sample_rate: int = 16000, channels: int = 1, bitrate: str = "32k"
) -> None:
    """Downmix/resample to Opus.

    Equivalent to the previous
    ``AudioSegment.set_frame_rate(16000).set_channels(1).export(format="opus", bitrate="32k")``
    chain, but in one ffmpeg pass instead of decoding the whole file into memory
    first — which also means a large upload no longer has to fit in RAM.
    """
    file_path = _checked_path(file_path, "input")
    output_path = _checked_path(output_path, "output")
    _run(
        [
            "ffmpeg", "-nostdin", "-y", "-v", "error",
            "-i", file_path,
            "-ar", str(int(sample_rate)),
            "-ac", str(int(channels)),
            "-c:a", "libopus",
            "-b:a", str(bitrate),
            output_path,
        ],
        timeout=FFMPEG_TIMEOUT_SECONDS,
    )
