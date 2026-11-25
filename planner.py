"""Pure planning logic (no browser, no AI): split the script for Text to Speech,
map sentences onto the real narration timing, and group them into timed scenes."""
from __future__ import annotations

import math
import re

_SENT = re.compile(r"(?<=[.!?…])[\"”’')\]]*\s+(?=[\"“‘'(\[]?[A-Z0-9])")


def clean_script(text: str) -> str:
    """Drop markdown headings / scene labels the narrator must not read, normalise spaces."""
    keep = []
    for line in text.replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if re.match(r"^(#{1,6}\s|\[.*\]$|\(.*\)$|(scene|part|chapter)\s*\d+\s*[:.-]?\s*$)", s, re.I):
            continue
        keep.append(re.sub(r"[ \t]+", " ", s))
    out = "\n".join(keep)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


def sentences(text: str) -> list[str]:
    flat = re.sub(r"\s+", " ", text).strip()
    return [s.strip() for s in _SENT.split(flat) if s.strip()]


def chunk_for_tts(text: str, limit: int = 2400, soft: int = 2200) -> list[str]:
    """Split into TTS parts at paragraph boundaries when possible, else sentence boundaries.
    Every part stays under `limit` characters (VideoExpress TTS limit)."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    units: list[str] = []
    for p in paras:
        if len(p) <= soft:
            units.append(p)
        else:
            cur = ""
            for s in sentences(p):
                if cur and len(cur) + 1 + len(s) > soft:
                    units.append(cur)
                    cur = ""
                cur = (cur + " " + s).strip()
            if cur:
                units.append(cur)
    parts, cur = [], ""
    for u in units:
        u = re.sub(r"\s+", " ", u)
        if cur and len(cur) + 2 + len(u) > soft:
            parts.append(cur)
            cur = ""
        cur = (cur + " " + u).strip()
    if cur:
        parts.append(cur)
    for p in parts:
        if len(p) > limit:  # one giant sentence: hard split on commas / spaces
            raise ValueError(f"A single sentence is longer than {limit} characters; please add full stops.")
    return parts


def estimate_seconds(text: str, wpm: float = 175) -> float:
    return len(re.findall(r"\b\w[\w'’-]*\b", text)) / wpm * 60


def sentence_ends(part_text: str, duration: float, pauses: list[list[float]], snap: float = 2.5) -> list[float]:
    """End time (s) of each sentence in one narration part.
    Estimate by character share, then snap to the nearest detected pause."""
    sents = sentences(part_text)
    total_chars = sum(len(s) + 1 for s in sents) or 1
    mids = [(a + b) / 2 for a, b in pauses]
    pos, ends, last = 0, [], 0.0
    used: set[int] = set()
    for s in sents:
        pos += len(s) + 1
        est = pos / total_chars * duration
        best, best_d = None, snap
        for i, m in enumerate(mids):
            if i in used or m <= last + 0.4:
                continue
            d = abs(m - est)
            if d < best_d:
                best, best_d = i, d
        if best is not None:
            used.add(best)
            t = mids[best]
        else:
            t = max(est, last + 0.5)
        t = min(t, duration)
        ends.append(round(t, 2))
        last = t
    if ends:
        ends[-1] = round(duration, 2)
    return ends


def plan_scenes(parts: list[dict], target: float = 7.0, min_len: int = 3, max_len: int = 10) -> list[dict]:
    """parts: [{text, duration, pauses}] in order (narration placed back to back from 0).
    Returns scenes [{n, text, start, end, len, part}] with integer `len` summing to ceil(total)."""
    max_len = max(1, min(int(max_len), 10))
    min_len = max(1, min(int(min_len), max_len))
    target = max(min_len, min(float(target), max_len))
    timeline: list[tuple[str, float, float, int]] = []  # sentence, start, end, part index
    offset = 0.0
    for k, p in enumerate(parts):
        ends = sentence_ends(p["text"], p["duration"], p.get("pauses") or [])
        start = 0.0
        for s, e in zip(sentences(p["text"]), ends):
            timeline.append((s, offset + start, offset + e, k))
            start = e
        offset += p["duration"]
    total = offset

    groups: list[dict] = []
    cur: dict | None = None
    for s, a, b, k in timeline:
        d = b - a
        if d > max_len:  # long sentence: several scenes showing successive moments of it
            if cur:
                groups.append(cur)
                cur = None
            pieces = math.ceil(d / max_len)
            step = d / pieces
            for i in range(pieces):
                groups.append({"text": s, "start": a + i * step, "end": a + (i + 1) * step, "part": k,
                               "beat": f"moment {i + 1} of {pieces} of this sentence"})
            continue
        if cur is None:
            cur = {"text": s, "start": a, "end": b, "part": k}
        elif k == cur["part"] and (b - cur["start"]) <= max_len and (cur["end"] - cur["start"]) < target - 1.5:
            cur["text"] += " " + s
            cur["end"] = b
        else:
            groups.append(cur)
            cur = {"text": s, "start": a, "end": b, "part": k}
    if cur:
        groups.append(cur)

    # merge too-short scenes into a neighbour when it fits
    i = 0
    while i < len(groups):
        g = groups[i]
        if g["end"] - g["start"] < min_len and len(groups) > 1:
            if (i > 0 and groups[i]["part"] == groups[i - 1]["part"]
                    and groups[i]["end"] - groups[i - 1]["start"] <= max_len and "beat" not in groups[i - 1]):
                groups[i - 1]["text"] += " " + g["text"]
                groups[i - 1]["end"] = g["end"]
                groups.pop(i)
                continue
            if (i + 1 < len(groups) and groups[i]["part"] == groups[i + 1]["part"]
                    and groups[i + 1]["end"] - g["start"] <= max_len and "beat" not in groups[i + 1]):
                groups[i + 1]["text"] = g["text"] + " " + groups[i + 1]["text"]
                groups[i + 1]["start"] = g["start"]
                groups.pop(i)
                continue
        i += 1

    # integer clip lengths whose running total tracks the real timing
    scenes, prev = [], 0
    for idx, g in enumerate(groups):
        last = idx == len(groups) - 1
        bound = math.ceil(total) if last else int(round(g["end"]))
        ln = bound - prev
        ln = max(min_len, min(max_len, ln))
        scenes.append({"n": idx + 1, "text": g["text"], "beat": g.get("beat", ""), "part": g["part"] + 1,
                       "start": prev, "len": ln, "t_start": round(g["start"], 2), "t_end": round(g["end"], 2)})
        prev += ln
    # if clamping left the video shorter than the audio, extend the last scenes (≤ max_len)
    short = math.ceil(total) - prev
    j = len(scenes) - 1
    while short > 0 and j >= 0:
        add = min(short, max_len - scenes[j]["len"])
        scenes[j]["len"] += add
        short -= add
        j -= 1
    s = 0
    for sc in scenes:
        sc["start"] = s
        s += sc["len"]
    return scenes


def split_overlong_scenes(scenes: list[dict], max_len: int = 10) -> tuple[list[dict], int]:
    """Split legacy scenes that exceed VideoExpress' per-clip duration limit."""
    max_len = max(1, min(int(max_len), 10))
    result: list[dict] = []
    split_count = 0
    media_keys = (
        "img", "img_ok", "frame", "ref", "lib_id", "fileName", "preview",
        "video_url", "dur_ms", "error", "submitted",
    )

    for scene in scenes:
        duration = int(float(scene.get("len") or 0))
        if duration <= max_len:
            result.append(dict(scene))
            continue

        count = math.ceil(duration / max_len)
        base, remainder = divmod(duration, count)
        lengths = [base + (1 if index < remainder else 0) for index in range(count)]
        original_start = float(scene.get("t_start", scene.get("start", 0)))
        original_end = float(scene.get("t_end", original_start + duration))
        elapsed = 0
        split_count += 1

        for index, length in enumerate(lengths):
            piece = dict(scene)
            piece["len"] = length
            piece["beat"] = f"{scene.get('beat', '')} (segment {index + 1} of {count})".strip()
            piece["status"] = "pending"
            piece["tries"] = 0
            if index:
                piece["continue"] = True
                piece.pop("line", None)
                piece.pop("audio", None)
                image_prompt = str(scene.get("image") or "").rstrip()
                if image_prompt:
                    piece["image"] = (
                        f"{image_prompt} Continue the same scene in its next moment, "
                        "preserving the characters, setting, and visual style."
                    )
            piece["t_start"] = round(
                original_start + (original_end - original_start) * elapsed / duration, 2
            )
            elapsed += length
            piece["t_end"] = round(
                original_start + (original_end - original_start) * elapsed / duration, 2
            )
            for key in media_keys:
                piece.pop(key, None)
            result.append(piece)

    start = 0
    for index, scene in enumerate(result, start=1):
        scene["n"] = index
        scene["start"] = start
        start += int(scene["len"])
    return result, split_count


def partition_narration_exports(parts: list[dict], scenes: list[dict], limit: float = 600) -> list[dict]:
    """Group whole TTS parts and their scenes into exports no longer than the VideoExpress cap."""
    groups: list[dict] = []
    start = 0
    seconds = 0.0
    for i, part in enumerate(parts):
        length = float(part.get("duration") or 0)
        if length <= 0 or length > limit:
            raise ValueError(f"Narration part {i + 1} must be between 0 and {limit:g} seconds to export.")
        if seconds and seconds + length > limit:
            groups.append({"part_start": start, "part_end": i, "duration_s": seconds})
            start, seconds = i, 0.0
        seconds += length
    if seconds:
        groups.append({"part_start": start, "part_end": len(parts), "duration_s": seconds})

    for group in groups:
        part_numbers = set(range(group["part_start"] + 1, group["part_end"] + 1))
        group["scene_numbers"] = [scene["n"] for scene in scenes if scene.get("part") in part_numbers]
        if not group["scene_numbers"]:
            raise ValueError("A narration export segment has no matching scenes; re-plan the scenes before exporting.")
    return groups


def partition_scene_exports(scenes: list[dict], limit: float = 600) -> list[dict]:
    """Group consecutive dialogue scenes into exports no longer than the VideoExpress cap."""
    groups: list[dict] = []
    current: list[int] = []
    seconds = 0.0
    for scene in scenes:
        length = float(scene.get("len") or 0)
        if length <= 0 or length > limit:
            raise ValueError(f"Scene {scene.get('n')} must be between 0 and {limit:g} seconds to export.")
        if seconds and seconds + length > limit:
            groups.append({"scene_numbers": current, "duration_s": seconds})
            current, seconds = [], 0.0
        current.append(scene["n"])
        seconds += length
    if current:
        groups.append({"scene_numbers": current, "duration_s": seconds})
    return groups


def expand(text: str, tokens: dict[str, str]) -> str:
    """Replace <KEY> tokens with their locked descriptions (unknown tokens are removed)."""
    def rep(m):
        return tokens.get(m.group(1), tokens.get(m.group(1).upper(), ""))
    out = re.sub(r"<([A-Za-z0-9_]{1,24})>", rep, text)
    return re.sub(r"\s{2,}", " ", out).replace(" ,", ",").replace(" .", ".").strip()


def shots_text(shots: list[dict], length: int) -> str:
    if not shots:
        return ""
    n = len(shots)
    cuts = [round(length * i / n) for i in range(n + 1)]
    lines = []
    for i, sh in enumerate(shots):
        cam = (sh.get("camera") or "MEDIUM SHOT").upper().rstrip(":")
        act = (sh.get("action") or "").strip()
        prefix = "" if i == 0 else "Hard cut. "
        lines.append(f"SHOT {i + 1} ({cuts[i]}-{cuts[i + 1]}s) — {cam}:\n{prefix}{act}")
    return "\n".join(lines)
