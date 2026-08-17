"""ffmpeg/ffprobe helpers that replaced pydub (#106).

Covers argv construction, error mapping, and the option-injection guard.

These deliberately do NOT require the ffmpeg binary: the test:api image is
python:3.13-slim-bookworm, which has no ffmpeg, while the runtime image
apt-installs it. Asserting on the argv we build is what is actually ours to
get right; whether ffmpeg then behaves is ffmpeg's contract. The one real
round-trip test at the bottom skips itself when the binary is absent, so it
exercises the whole path locally and in the runtime image without making the
suite depend on it.
"""

import json
import shutil
import subprocess
from unittest.mock import patch

import pytest

from selfai_ui.utils.audio_ffmpeg import (
    AudioConversionError,
    compress_to_opus,
    convert_mp4_to_wav,
    probe_audio_stream,
)


def _completed(stdout=""):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


@pytest.mark.tier0
def test_probe_returns_first_audio_stream(tmp_path):
    f = tmp_path / "a.m4a"
    f.write_bytes(b"x")
    payload = json.dumps({"streams": [{"codec_name": "aac", "codec_type": "audio", "codec_tag_string": "mp4a"}]})
    with patch("selfai_ui.utils.audio_ffmpeg.subprocess.run", return_value=_completed(payload)) as run:
        assert probe_audio_stream(str(f))["codec_name"] == "aac"
    argv = run.call_args[0][0]
    assert argv[0] == "ffprobe"
    # the path must be the VALUE of -i, never a bare positional that ffprobe
    # could parse as an option
    assert argv[argv.index("-i") + 1] == str(f)
    assert run.call_args.kwargs["timeout"] > 0
    assert "shell" not in run.call_args.kwargs


@pytest.mark.tier0
def test_probe_missing_file_returns_empty_without_invoking_ffprobe(tmp_path):
    with patch("selfai_ui.utils.audio_ffmpeg.subprocess.run") as run:
        assert probe_audio_stream(str(tmp_path / "nope.m4a")) == {}
    run.assert_not_called()


@pytest.mark.tier0
@pytest.mark.parametrize("payload", ["not json", "{}", '{"streams": []}'])
def test_probe_unusable_output_returns_empty(tmp_path, payload):
    """"Not a probeable audio file" is an answer, not an exception."""
    f = tmp_path / "a.bin"
    f.write_bytes(b"x")
    with patch("selfai_ui.utils.audio_ffmpeg.subprocess.run", return_value=_completed(payload)):
        assert probe_audio_stream(str(f)) == {}


@pytest.mark.tier0
def test_convert_mp4_to_wav_asserts_the_container(tmp_path):
    """-f mp4 mirrors pydub's format="mp4": asserted, not sniffed."""
    with patch("selfai_ui.utils.audio_ffmpeg.subprocess.run", return_value=_completed()) as run:
        convert_mp4_to_wav(str(tmp_path / "in.mp4"), str(tmp_path / "out.wav"))
    argv = run.call_args[0][0]
    assert argv[0] == "ffmpeg"
    assert argv[argv.index("-f") + 1] == "mp4"
    assert argv[argv.index("-i") + 1] == str(tmp_path / "in.mp4")
    assert argv[-1] == str(tmp_path / "out.wav")
    assert "-nostdin" in argv  # never block waiting on stdin in a worker


@pytest.mark.tier0
def test_compress_to_opus_matches_the_previous_pydub_chain(tmp_path):
    """set_frame_rate(16000).set_channels(1).export(opus, 32k), in one pass."""
    with patch("selfai_ui.utils.audio_ffmpeg.subprocess.run", return_value=_completed()) as run:
        compress_to_opus(str(tmp_path / "in.wav"), str(tmp_path / "out.opus"))
    argv = run.call_args[0][0]
    assert argv[argv.index("-ar") + 1] == "16000"
    assert argv[argv.index("-ac") + 1] == "1"
    assert argv[argv.index("-c:a") + 1] == "libopus"
    assert argv[argv.index("-b:a") + 1] == "32k"


@pytest.mark.tier0
@pytest.mark.parametrize("bad", ["-i", "--output", "-rf"])
def test_paths_that_look_like_options_are_rejected(tmp_path, bad):
    """A filename must never be able to become an ffmpeg flag (#58)."""
    with patch("selfai_ui.utils.audio_ffmpeg.subprocess.run") as run:
        with pytest.raises(AudioConversionError):
            convert_mp4_to_wav(str(tmp_path / "in.mp4"), bad)
    run.assert_not_called()


@pytest.mark.tier0
@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError("ffmpeg"),
        subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1),
        subprocess.CalledProcessError(returncode=1, cmd="ffmpeg", stderr="boom"),
    ],
)
def test_subprocess_failures_become_AudioConversionError(tmp_path, exc):
    """Callers get one exception type, not three unrelated ones."""
    with patch("selfai_ui.utils.audio_ffmpeg.subprocess.run", side_effect=exc):
        with pytest.raises(AudioConversionError):
            convert_mp4_to_wav(str(tmp_path / "in.mp4"), str(tmp_path / "out.wav"))


@pytest.mark.tier0
def test_called_process_error_keeps_stderr_for_diagnosis(tmp_path):
    err = subprocess.CalledProcessError(returncode=1, cmd="ffmpeg", stderr="Invalid data found")
    with patch("selfai_ui.utils.audio_ffmpeg.subprocess.run", side_effect=err):
        with pytest.raises(AudioConversionError, match="Invalid data found"):
            convert_mp4_to_wav(str(tmp_path / "in.mp4"), str(tmp_path / "out.wav"))


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed (absent from the test image)")
@pytest.mark.tier0
def test_real_roundtrip_generate_then_compress(tmp_path):
    """End-to-end against the actual binary, where one is available."""
    src = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=1", str(src)],
        check=True, capture_output=True,
    )
    assert src.exists()

    info = probe_audio_stream(str(src))
    assert info.get("codec_type") == "audio"

    out = tmp_path / "tone.opus"
    compress_to_opus(str(src), str(out))
    assert out.exists() and out.stat().st_size > 0
