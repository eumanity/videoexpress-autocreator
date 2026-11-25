"""Music + sound effects: built-in mood music, music-folder picking, and the final mix (ffmpeg).
The exported video only contains the narration, so the mix is: voice + ducked music + timed sound effects."""
from __future__ import annotations

import math
import random
import re
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

MOODS = ["tense", "dark", "epic", "sad", "hopeful", "mysterious", "calm", "reverent"]
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"}
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # don't flash console windows on Windows


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return "ffmpeg"


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([ffmpeg_exe(), "-hide_banner", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)


def duration(path: str | Path) -> float:
    err = _run(["-i", str(path)]).stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


def concatenate(videos: list[Path], out: Path, log=print) -> Path:
    """Join VideoExpress segments locally without re-encoding their video or audio."""
    if len(videos) < 2 or any(not path.is_file() for path in videos):
        raise RuntimeError("Joining requires at least two downloaded video segments.")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".joining" + out.suffix)
    manifest_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".ffconcat",
                                         dir=out.parent, delete=False) as manifest:
            manifest_path = Path(manifest.name)
            for path in videos:
                escaped = str(path.resolve()).replace("\\", "/").replace("'", "'\\''")
                manifest.write(f"file '{escaped}'\n")
        log(f"Joining {len(videos)} video segments with ffmpeg…")
        result = _run(["-y", "-f", "concat", "-safe", "0", "-i", str(manifest_path),
                       "-c", "copy", "-movflags", "+faststart", str(tmp)])
        if result.returncode or not tmp.is_file():
            raise RuntimeError("ffmpeg could not join the video segments: " + result.stderr[-600:])
        expected = sum(duration(path) for path in videos)
        actual = duration(tmp)
        if expected <= 0 or actual <= 0 or abs(actual - expected) > 2:
            raise RuntimeError(f"Joined video length ({actual:.1f}s) does not match the segments "
                               f"({expected:.1f}s).")
        tmp.replace(out)
        return out
    finally:
        if manifest_path and manifest_path.exists():
            manifest_path.unlink()
        if tmp.exists():
            tmp.unlink()


def mean_volume(path: str | Path, seconds: float = 90) -> float:
    """Mean loudness in dBFS (ignores digital silence well enough for our levels)."""
    err = _run(["-t", str(seconds), "-i", str(path), "-af", "volumedetect", "-f", "null", "-"]).stderr
    m = re.search(r"mean_volume: (-?[\d.]+) dB", err)
    return float(m.group(1)) if m else -20.0


def level_to_db(level_pct: float) -> float:
    """Level slider (1-100 %) → target mean loudness. 100 % = as loud as typical narration (-18 dBFS)."""
    level_pct = max(1.0, min(100.0, float(level_pct)))
    return -18.0 + 20 * math.log10(level_pct / 100.0)


# --------------------------------------------------------------------- music
def list_music(folder: str | Path) -> list[Path]:
    f = Path(folder).expanduser()
    if not f.is_dir():
        return []
    return sorted(p for p in f.rglob("*") if p.suffix.lower() in AUDIO_EXT and p.is_file())


def pick_music(folder: str | Path, mood: str, keywords: str = "", seed: str = "") -> Path | None:
    files = list_music(folder)
    if not files:
        return None
    words = {w for w in re.findall(r"[a-z]+", f"{mood} {keywords}".lower()) if len(w) > 2}
    scored = sorted(files, key=lambda p: -sum(w in p.stem.lower() for w in words))
    best = [p for p in scored if sum(w in p.stem.lower() for w in words) == sum(w in scored[0].stem.lower() for w in words)]
    return random.Random(seed or mood).choice(best)


_SCALES = {   # (root midi, chord list as semitone offsets from root, tempo of chord changes in s, pulse)
    "tense":      (45, [[0, 3, 7], [1, 5, 8], [0, 3, 7], [-2, 2, 5]], 8.0, True),
    "dark":       (40, [[0, 3, 7], [-4, 0, 3], [-2, 2, 5], [0, 3, 7]], 10.0, False),
    "epic":       (43, [[0, 4, 7], [-4, 0, 3], [-5, -1, 2], [-2, 2, 5]], 6.0, True),
    "sad":        (45, [[0, 3, 7], [-4, 0, 3], [-7, -3, 0], [-5, -1, 2]], 9.0, False),
    "hopeful":    (48, [[0, 4, 7], [7, 11, 14], [9, 12, 16], [5, 9, 12]], 8.0, False),
    "mysterious": (47, [[0, 3, 6], [1, 5, 8], [-1, 2, 6], [0, 3, 6]], 10.0, False),
    "calm":       (48, [[0, 4, 7], [5, 9, 12], [-3, 0, 4], [7, 11, 14]], 10.0, False),
    "reverent":   (46, [[0, 4, 7], [5, 9, 12], [0, 4, 7], [7, 11, 14]], 10.0, False),
}


def make_mood_music(mood: str, out: Path, sr: int = 44100) -> Path:
    """Generate a seamless ambient pad loop (royalty-free, made on your PC) for the given mood."""
    import numpy as np
    root, chords, step, pulse = _SCALES.get(mood, _SCALES["calm"])
    n_ch = len(chords)
    length = step * n_ch
    n = int(length * sr)
    t = np.arange(n) / sr
    rng = np.random.default_rng(sum(ord(c) * 31 ** i for i, c in enumerate(mood)) % (2 ** 32))
    left = np.zeros(n)
    right = np.zeros(n)

    def hz(m):
        return 440.0 * 2 ** ((m - 69) / 12)

    for i, ch in enumerate(chords):
        centre = (i + 0.5) * step
        # periodic raised-cosine envelope (wraps around the loop, so the loop is seamless)
        d = (t - centre + length / 2) % length - length / 2
        env = np.where(np.abs(d) < step * 0.85, 0.5 * (1 + np.cos(np.pi * d / (step * 0.85))), 0.0)
        for k, semi in enumerate(ch + [ch[0] + 12]):
            f = hz(root + 12 + semi)
            for det, pan in ((-0.6, 0.3), (0.6, 0.7)):
                # partials snapped to whole cycles per loop → seamless
                ff = round((f + det) * length) / length
                ph = rng.uniform(0, 2 * np.pi)
                tone = np.sin(2 * np.pi * ff * t + ph) + 0.25 * np.sin(2 * np.pi * 2 * ff * t + ph)
                g = 0.10 / (1 + k * 0.3)
                left += env * tone * g * (1 - pan)
                right += env * tone * g * pan
    # low drone on the root
    fd = round(hz(root - 12) * length) / length
    drone = 0.09 * np.sin(2 * np.pi * fd * t) * (0.8 + 0.2 * np.sin(2 * np.pi * t / length))
    left += drone
    right += drone
    if pulse:  # soft heartbeat-like pulse for tension / action
        beat = 60 / (72 if mood == "tense" else 96)
        nb = max(1, round(length / beat))
        beat = length / nb
        ph = (t % beat) / beat
        thump = np.sin(2 * np.pi * round(hz(root - 12) * length) / length * t) * np.exp(-ph * 9) * 0.22
        left += thump
        right += thump
    # cheap seamless "room": circular delays
    for delay, gain in ((0.113, 0.35), (0.271, 0.25), (0.419, 0.18)):
        s = int(delay * sr)
        left += np.roll(right, s) * gain * 0.5
        right += np.roll(left, s) * gain * 0.5
    peak = max(np.abs(left).max(), np.abs(right).max(), 1e-6)
    stereo = (np.stack([left, right], axis=1) / peak * 0.8 * 32767).astype(np.int16)
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(stereo.tobytes())
    return out


# ----------------------------------------------------------------------- mix
def mix(video: Path, out: Path, music: Path | None, sfx: list[dict], music_level: float = 15,
        sfx_level: float = 45, duck: bool = True, log=print) -> Path:
    """sfx: [{'file': path, 'at': seconds, 'gain_db': optional}]."""
    D = duration(video)
    if D <= 0:
        raise RuntimeError("Could not read the video length.")
    args = ["-y", "-i", str(video)]
    fl = ["[0:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,asplit=2[voice][sc]"]
    mixins = ["[voice]"]
    idx = 1
    if music:
        g = level_to_db(music_level) - mean_volume(music)
        args += ["-stream_loop", "-1", "-i", str(music)]
        fade_out = max(0.0, D - 4)
        fl.append(f"[{idx}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
                  f"atrim=0:{D:.3f},asetpts=PTS-STARTPTS,volume={g:.2f}dB,"
                  f"afade=t=in:d=3,afade=t=out:st={fade_out:.3f}:d=4[mus]")
        if duck:
            fl.append("[mus][sc]sidechaincompress=threshold=0.03:ratio=5:attack=40:release=700:makeup=1[mduck]")
            mixins.append("[mduck]")
        else:
            fl.append("[sc]anullsink")
            mixins.append("[mus]")
        idx += 1
    else:
        fl.append("[sc]anullsink")
    for k, s in enumerate(sfx):
        at = max(0.0, min(float(s["at"]), D - 0.5))
        g = s.get("gain_db")
        if g is None:
            g = level_to_db(sfx_level) - mean_volume(s["file"], 15) + float(s.get("boost_db", 0))
        g = max(-40.0, min(12.0, g))
        args += ["-i", str(s["file"])]
        ms = int(at * 1000)
        fl.append(f"[{idx}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
                  f"volume={g:.2f}dB,adelay={ms}|{ms}[s{k}]")
        mixins.append(f"[s{k}]")
        idx += 1
    if len(mixins) == 1:
        raise RuntimeError("Nothing to mix (music and sound effects are both off).")
    fl.append(f"{''.join(mixins)}amix=inputs={len(mixins)}:duration=first:dropout_transition=0:normalize=0,"
              f"alimiter=limit=0.95[aout]")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".mixing" + out.suffix)
    args += ["-filter_complex", ";".join(fl), "-map", "0:v", "-map", "[aout]", "-c:v", "copy",
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(tmp)]
    log(f"Mixing narration + {'music + ' if music else ''}{len(sfx)} sound effects…")
    r = _run(args)
    if r.returncode != 0 or not tmp.exists():
        raise RuntimeError("ffmpeg mix failed: " + r.stderr[-600:])
    if abs(duration(tmp) - D) > 1.0:
        raise RuntimeError("Mixed video length does not match the original.")
    tmp.replace(out)
    return out
