"""Pipeline: script → analysis → narration → timed scenes → prompts → renders → timeline → export → download.
Every step saves to state.json first, so Stop / crash / restart resumes without paying twice."""
from __future__ import annotations

import asyncio
import threading
import time
import traceback
from pathlib import Path

from . import planner as P
from . import screenplay as SP
from . import scriptwriter as SW
from . import voices as VO
from . import sound as SND
from .brain import (Brain, BrainError, build_image_prompt, build_video_prompt, normalise_analysis, normalise_sound,
                    parse_scene_reply, _extract_json)
from .project import (HOME, MAX_EXPORT_SEGMENT_SECONDS, MAX_VIDEO_CLIP_SECONDS, MAX_VIDEO_SECONDS, Project, load_settings,
                      save_settings)
from .ve import Stop, VEError, VideoExpress, download

def _brain(s: dict) -> Brain:
    b = Brain(s, on_settings=save_settings)
    b.log = RUN.log
    return b


STAGES = ["new", "analysed", "narrated", "planned", "prompted", "rendered", "exported", "mixed"]


def stage_at_least(p: Project, st: str) -> bool:
    return STAGES.index(p.state.get("stage", "new")) >= STAGES.index(st)


def set_stage(p: Project, st: str):
    if STAGES.index(st) > STAGES.index(p.state.get("stage", "new")):
        p.state["stage"] = st
    p.save()


class Runner:
    """Owns one asyncio loop thread + one browser session; runs one job at a time."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.ve: VideoExpress | None = None
        self.busy = False
        self.job = ""
        self.waiting: str | None = None
        self._event: asyncio.Event | None = None
        self._stop = False
        self.progress = {"done": 0, "total": 0, "label": ""}
        self.lines: list[str] = []
        self.project: Project | None = None
        self.image_types: list[dict] = []

    # ------------------------------------------------------------- plumbing
    def log(self, msg: str):
        line = time.strftime("%H:%M:%S ") + msg
        self.lines = (self.lines + [line])[-300:]
        if self.project:
            try:
                self.project.log(msg)
            except Exception:
                pass

    async def ask(self, msg: str):
        self.waiting = msg
        self.log("⏸ " + msg)
        self._event = asyncio.Event()
        while not self._event.is_set():
            if self._stop:
                raise Stop()
            try:
                await asyncio.wait_for(self._event.wait(), 1.0)
            except asyncio.TimeoutError:
                pass
        self.waiting = None

    def resume(self):
        if self._event:
            self.loop.call_soon_threadsafe(self._event.set)

    def stop(self):
        self._stop = True
        self.resume()

    def status(self) -> dict:
        return {"busy": self.busy, "job": self.job, "waiting": self.waiting, "progress": self.progress,
                "log": self.lines[-120:], "project": self.project.slug if self.project else None}

    def start(self, job: str, project: Project | None, coro_fn):
        if self.busy:
            raise RuntimeError("Another job is running. Stop it first.")
        self.busy, self.job, self._stop, self.project = True, job, False, project
        self.progress = {"done": 0, "total": 0, "label": ""}

        async def wrap():
            try:
                await coro_fn()
                self.log(f"✔ {job} finished.")
            except Stop:
                self.log("■ Stopped. Press the same button to resume where it left off.")
            except (BrainError, VEError) as e:
                self.log(f"✖ {e}")
                await self._shot()
            except Exception as e:  # noqa: BLE001
                self.log(f"✖ Unexpected error: {e}")
                self.log(traceback.format_exc()[-1500:])
                await self._shot()
            finally:
                self.busy, self.job, self.waiting = False, "", None

        asyncio.run_coroutine_threadsafe(wrap(), self.loop)

    async def _shot(self):
        if self.ve and self.ve.page and self.project:
            path = self.project.dir / f"error_{int(time.time())}.png"
            await self.ve.screenshot(path)
            self.log(f"Screenshot saved: {path}")

    async def browser(self) -> VideoExpress:
        s = load_settings()
        if self.ve and self.ve.ready and self.ve.page and not self.ve.page.is_closed():
            self.ve.s = s
            return self.ve
        if self.ve:
            try:
                await self.ve.stop()
            except Exception as e:
                self.log(f"Could not close the incomplete VideoExpress browser session: {e}")
            self.ve = None
        self.ve = VideoExpress(s, HOME / "browser-profile", self.log, self.ask, lambda: self._stop)
        self.log("Opening VideoExpress…")
        try:
            await self.ve.start()
        except Exception:
            try:
                await self.ve.stop()
            except Exception:
                pass
            self.ve = None
            raise
        return self.ve


RUN = Runner()


# ====================================================================== steps
async def step_script(p: Project, force: bool = False):
    """Step 0: the AI writes the script from your topic, following the formula of your reference transcripts."""
    s = load_settings()
    if p.state.get("script", "").strip() and not force:
        return
    topic = (p.state.get("topic") or "").strip()
    if not topic:
        raise VEError("Give the video a topic (or paste a script).")
    if stage_at_least(p, "narrated"):
        raise VEError("The narration is already made from the current script; start a new video to change it.")
    brain = _brain(s)
    if brain.provider in ("manual", "offline"):
        raise VEError("The AI script writer needs an AI key (Groq, Claude or Gemini) - add one in your keys file.")
    if p.state.get("reference") and not p.state.get("formula"):
        p.state["formula"] = await asyncio.to_thread(SW.study_formula, brain, p.state["reference"], RUN.log)
        p.save()
        RUN.log("Formula learned: " + str(p.state["formula"].get("summary", ""))[:200])
    if is_dialogue(p):
        return                                   # movies: the screenplay writer uses topic + formula directly
    formula = p.state.get("formula") or SW.DEFAULT_FORMULA
    text = await asyncio.to_thread(SW.write_script, brain, topic, formula, float(p.state.get("minutes", 5)),
                                   float(s.get("wpm", 165)), p.state.get("notes", ""), s.get("channel_name", ""),
                                   bool(s.get("script_research", True)), RUN.log)
    if len(text.split()) < 60:
        raise VEError("The AI returned a script that is too short. Run it again.")
    p.state["script"] = text
    p.state["script_ai"] = True
    p.state["estimate_s"] = round(P.estimate_seconds(text, float(s["wpm"])))
    p.state["parts"] = []
    p.save()
    RUN.log(f"Script written: {len(text.split())} words (≈ {p.state['estimate_s'] / 60:.1f} min). "
            "You can read and edit it in the 'Script & narration' tab.")


async def step_analyse(p: Project):
    if not (p.state.get("script") or "").strip() or (is_dialogue(p) and p.state.get("reference") and not p.state.get("formula")):
        await step_script(p)
    if is_dialogue(p):
        return await dlg_screenplay(p)
    s = load_settings()
    script = P.clean_script(p.state["script"])
    p.state["clean_script"] = script
    est = P.estimate_seconds(script, float(s["wpm"]))
    if est > MAX_VIDEO_SECONDS:
        raise VEError(f"This script is about {est / 60:.1f} minutes; the maximum video length is 18 minutes. "
                      "Shorten the script before generating narration.")
    tts_limit = int(s["tts_limit"])
    tts_soft = 2200
    if est > MAX_EXPORT_SEGMENT_SECONDS:
        tts_limit = min(tts_limit, 1800)
        tts_soft = min(tts_soft, 1600, tts_limit)
    parts = P.chunk_for_tts(script, tts_limit, tts_soft)
    if not p.state.get("parts") or not any(x.get("audio") for x in p.state["parts"]):
        p.state["parts"] = [{"k": i + 1, "text": t} for i, t in enumerate(parts)]
    p.state["estimate_s"] = round(est)
    RUN.log(f"Script: {len(P.sentences(script))} sentences, {len(parts)} narration parts, "
            f"about {est / 60:.1f} min of narration.")
    brain = _brain(s)
    if brain.provider == "manual":
        sys_, task = brain.analysis_prompt(script, p.state["preset"])
        p.state["manual"] = {"step": "analysis", "prompt": sys_ + "\n\n" + task}
        p.save()
        RUN.log("Manual AI mode: copy the prompt shown in the app into Claude or ChatGPT, then paste the reply back.")
        return
    model = s.get({"groq": "groq_model", "gemini": "gemini_model", "claude": "claude_model"}.get(brain.provider, ""), "")
    RUN.log(f"Studying the script with {brain.provider} ({model})…")
    a = await asyncio.to_thread(brain.analyse, script, p.state["preset"])
    if brain.provider == "offline" or not a.get("title"):
        a["title"] = p.state["name"]
    p.state["analysis"] = a
    p.state.pop("manual", None)
    RUN.log(f"Visual bible ready: '{a['title']}', {len(a['tokens'])} locked characters/places.")
    set_stage(p, "analysed")


def apply_manual(p: Project, reply: str):
    m = p.state.get("manual") or {}
    if m.get("step") == "analysis":
        p.state["analysis"] = normalise_analysis(_extract_json(reply), p.state["preset"])
        p.state.pop("manual", None)
        set_stage(p, "analysed")
        return "Visual bible saved."
    if m.get("step") == "scenes":
        got = parse_scene_reply(reply)
        _merge_prompts(p, got)
        missing = [s["n"] for s in p.state["scenes"] if not s.get("image")]
        if missing:
            nxt = [s for s in p.state["scenes"] if not s.get("image")]
            b = _brain(load_settings())
            sys_, task = b.scenes_prompt(p.state["analysis"], nxt[:40])
            p.state["manual"] = {"step": "scenes", "prompt": sys_ + "\n\n" + task}
            p.save()
            return f"Saved {len(got)} scenes. {len(missing)} still need prompts - copy the new prompt."
        p.state.pop("manual", None)
        set_stage(p, "prompted")
        return "All scene prompts saved. Review them, then start rendering."
    if m.get("step") == "screenplay":
        _store_screenplay(p, _extract_json(reply))
        b = _brain(load_settings())
        sys_, task = SP.shots_prompt(p.state["analysis"], [x["b"] for x in p.state["analysis"]["beats"]],
                                     float(p.state.get("minutes", 3)))
        p.state["manual"] = {"step": "shots", "prompt": sys_ + "\n\n" + task}
        p.save()
        return "Screenplay saved. Now copy the SHOTS prompt and paste its reply."
    if m.get("step") == "shots":
        _store_shots(p, SP.parse_shots(reply, _keys(p)))
        p.state.pop("manual", None)
        set_stage(p, "analysed")
        return f"{len(p.state['scenes'])} shots saved. Next: step 2 makes every character's voice lines."
    if m.get("step") == "sound":
        s = load_settings()
        p.state["sound"] = normalise_sound(_extract_json(reply), _scenes_for_sound(p),
                                           int(s["sfx_max"]) if s.get("sfx_on") else 0, SND.MOODS)
        p.state.pop("manual", None)
        p.save()
        return "Sound plan saved. Run step 7 again to make the sounds and mix."
    raise ValueError("Nothing is waiting for a manual reply.")


async def step_narrate(p: Project):
    if is_dialogue(p):
        return await dlg_voices(p)
    if not stage_at_least(p, "analysed"):
        raise VEError("Analyse the script first.")
    ve = await RUN.browser()
    parts = p.state["parts"]
    voice = (load_settings().get("tts_voice") or "").strip()
    if not voice and load_settings().get("autopilot", True):
        voice = "Andrew Multilingual"
    if not voice and not p.state.get("voice_confirmed") and not any(x.get("audio") for x in parts):
        await ve.open_tts()
        await RUN.ask("In the VideoExpress window, choose the narrator voice and language in the Text to Speech panel "
                      "(tip: type the voice name in Settings to skip this). Then click Continue.")
        p.state["voice_confirmed"] = True
        p.save()
    RUN.progress = {"done": sum(1 for x in parts if x.get("audio")), "total": len(parts), "label": "Narration"}
    for part in parts:
        await ve.check_stop()
        if not part.get("audio"):
            RUN.log(f"Narration part {part['k']}/{len(parts)} ({len(part['text'])} chars)…")
            part["audio"] = await ve.tts(part["text"], voice)
            p.save()
        if not part.get("pauses_done"):
            m = await ve.measure(part["audio"]["url"])
            part["duration"] = m["dur"]
            part["pauses"] = m["pauses"]
            part["pauses_done"] = True
            p.save()
            RUN.log(f"  part {part['k']}: {m['dur']:.1f}s, {len(m['pauses'])} pauses")
        RUN.progress["done"] = sum(1 for x in parts if x.get("audio"))
    total = sum(x["duration"] for x in parts)
    if total > MAX_VIDEO_SECONDS:
        raise VEError(f"The narration is {total / 60:.1f} minutes; the maximum video length is 18 minutes. "
                      "Shorten the script and regenerate the narration.")
    p.state["narration_s"] = round(total, 2)
    RUN.log(f"Narration total {total / 60:.2f} min.")
    set_stage(p, "narrated")


def step_plan(p: Project) -> str:
    if is_dialogue(p):
        return dlg_timing(p)
    s = load_settings()
    if not stage_at_least(p, "narrated"):
        raise VEError("Generate the narration first.")
    if any(sc.get("img") or sc.get("ref") for sc in p.state.get("scenes", [])):
        raise VEError("Scenes have already started rendering; re-planning would waste credits.")
    parts = [{"text": x["text"], "duration": x["duration"], "pauses": x.get("pauses", [])} for x in p.state["parts"]]
    scenes = P.plan_scenes(parts, float(s["scene_target"]), int(s["scene_min"]), int(s["scene_max"]))
    p.state["scenes"] = [dict(sc, status="pending", tries=0) for sc in scenes]
    p.state["budget_ok"] = len(scenes) <= int(s["max_clips"])
    set_stage(p, "planned")
    msg = f"{len(scenes)} scenes planned (= {len(scenes)} images + {len(scenes)} video clips)."
    if not p.state["budget_ok"]:
        msg += f" ⚠ More than your limit of {s['max_clips']} clips - approve the budget before rendering."
    RUN.log(msg)
    return msg


def _merge_prompts(p: Project, got: dict[int, dict]):
    for sc in p.state["scenes"]:
        g = got.get(sc["n"])
        if g and g.get("image"):
            sc.update(image=g["image"], ctx=g["ctx"], shots=g["shots"])
    p.save()


async def step_prompts(p: Project):
    if is_dialogue(p):
        if not stage_at_least(p, "planned"):
            dlg_timing(p)
        set_stage(p, "prompted")
        RUN.log("Shot prompts were written with the screenplay. Review them in the Scenes tab, then press Render.")
        return
    if not stage_at_least(p, "planned"):
        step_plan(p)
    s = load_settings()
    brain = _brain(s)
    todo = [sc for sc in p.state["scenes"] if not sc.get("image")]
    if not todo:
        set_stage(p, "prompted")
        return
    if brain.provider == "manual":
        sys_, task = brain.scenes_prompt(p.state["analysis"], todo[:40])
        p.state["manual"] = {"step": "scenes", "prompt": sys_ + "\n\n" + task}
        p.save()
        RUN.log("Manual AI mode: copy the scene prompt into Claude or ChatGPT and paste the JSON reply back "
                "(long scripts come in batches of 40 scenes).")
        return
    got = await asyncio.to_thread(brain.scene_prompts, p.state["analysis"], todo, 0, RUN.log)
    _merge_prompts(p, got)
    left = [sc["n"] for sc in p.state["scenes"] if not sc.get("image")]
    if left:
        RUN.log(f"⚠ {len(left)} scenes came back without prompts ({left[:10]}). Run 'Write prompts' again.")
        return
    set_stage(p, "prompted")
    RUN.log("All prompts written. Review/edit them in the Scenes tab, then press Render.")


async def step_rewrite_scene_prompt(p: Project, scene_n: int, feedback: str = ""):
    scene = next((sc for sc in p.state.get("scenes", []) if sc["n"] == scene_n), None)
    if scene is None:
        raise ValueError(f"Scene {scene_n} was not found.")
    if scene.get("continue") and is_dialogue(p):
        raise ValueError("This scene continues from the previous clip and does not use a new image prompt.")
    brain = _brain(load_settings())
    prompt = await asyncio.to_thread(
        brain.rewrite_scene_prompt, p.state.get("analysis") or {}, scene, feedback, RUN.log
    )
    scene["image"] = prompt
    scene.update(status="pending", tries=0)
    for key in ("img", "img_ok", "frame", "ref", "lib_id", "fileName", "preview",
                "video_url", "dur_ms", "error", "submitted"):
        scene.pop(key, None)
    p.state.pop("export", None)
    if stage_at_least(p, "rendered"):
        p.state["stage"] = "prompted"
    p.save()
    RUN.log(f"Scene {scene_n} prompt rewritten. Review it in the Scenes tab, then press Render.")


# ============================================================ dialogue movies
def _keys(p: Project) -> set[str]:
    return {t["key"] for t in (p.state.get("analysis") or {}).get("tokens", [])}


def _store_screenplay(p: Project, raw: dict):
    a = SP.normalise_beats(normalise_analysis(raw, p.state["preset"]))
    if not a.get("title") or a["title"] == "Untitled":
        a["title"] = p.state["name"]
    s = load_settings()
    VO.cast_voices(a["tokens"], s.get("tts_voice", ""))
    p.state["analysis"] = a
    p.save()


def _store_shots(p: Project, shots: list[dict]):
    shots = SP.fix_continuity(SP.split_long_lines(shots))
    p.state["scenes"] = SP.to_scenes(shots, p.state["analysis"])
    SP.time_scenes(p.state["scenes"])
    duration = sum(float(sc.get("len") or 0) for sc in p.state["scenes"])
    if duration > MAX_VIDEO_SECONDS:
        raise VEError(f"The screenplay is about {duration / 60:.1f} minutes; the maximum video length is 18 minutes.")
    p.save()


async def dlg_screenplay(p: Project):
    """Step 1 (dialogue): idea/script → screenplay with characters, voices, beats and shots."""
    s = load_settings()
    brain = _brain(s)
    minutes = float(p.state.get("minutes", 3))
    source = (p.state.get("script") or "").strip() or (p.state.get("topic") or "").strip()
    if p.state.get("notes"):
        source += "\n\nNotes: " + p.state["notes"]
    if p.state.get("formula"):
        source += "\n\n" + SW.formula_for_story(p.state["formula"])
    sys_, task = SP.screenplay_prompt(source, p.state["story_type"], minutes, p.state["preset"])
    if brain.provider in ("manual", "offline"):
        if brain.provider == "offline":
            raise VEError("Dialogue movies need an AI (Groq, Claude, Gemini or Manual) - Offline mode cannot write dialogue.")
        p.state["manual"] = {"step": "screenplay", "prompt": sys_ + "\n\n" + task}
        p.save()
        RUN.log("Manual AI mode: copy the SCREENPLAY prompt into Claude or ChatGPT, then paste the reply back.")
        return
    RUN.log(f"Writing the screenplay ({SP.STORY_TYPES[p.state['story_type']]}, about {minutes:g} min)…")
    raw = await asyncio.to_thread(lambda: _extract_json(brain.ask(sys_, task, 8000, log=RUN.log)))
    _store_screenplay(p, raw)
    a = p.state["analysis"]
    chars = [t for t in a["tokens"] if t.get("kind") == "character"]
    RUN.log(f"Screenplay: '{a['title']}', {len(a['beats'])} beats, {len(chars)} characters: "
            + ", ".join(f"{t['name']} ({VO.voice_name_of(t.get('voice', ''))})" for t in chars))
    shots: list[dict] = []
    beats = [b["b"] for b in a["beats"]]
    step = 2 if brain.provider == "groq" else 4
    for i in range(0, len(beats), step):
        await asyncio.sleep(0)
        if RUN._stop:
            raise Stop()
        part = beats[i:i + step]
        RUN.log(f"Writing shots and dialogue for beats {part[0]}–{part[-1]}…")
        sys2, task2 = SP.shots_prompt(a, part, minutes)
        got = await asyncio.to_thread(lambda: SP.parse_shots(brain.ask(sys2, task2, 6000, log=RUN.log), _keys(p)))
        for x in got:
            if x["b"] not in part:
                x["b"] = part[0]
        shots.extend(sorted(got, key=lambda x: part.index(x["b"])))
    if not shots:
        raise VEError("The AI returned no shots. Run step 1 again.")
    _store_shots(p, shots)
    lines = sum(1 for x in p.state["scenes"] if x.get("line"))
    RUN.log(f"{len(p.state['scenes'])} shots ({lines} spoken lines). Check the Characters tab to change any voice.")
    p.state.pop("manual", None)
    set_stage(p, "analysed")


async def dlg_voices(p: Project):
    """Step 2 (dialogue): every spoken line is made with VideoExpress Text to Speech in that character's voice."""
    if not stage_at_least(p, "analysed") or not p.state.get("scenes"):
        raise VEError("Write the screenplay first (step 1).")
    s = load_settings()
    ve = await RUN.browser()
    a = p.state["analysis"]
    voices = {t["key"]: t.get("voice") for t in a["tokens"]}
    narrator = s.get("tts_voice") or "Andrew Multilingual"
    voice_mode = s.get("dialogue_voice_mode", "exact")
    i = 0
    while i < len(p.state["scenes"]):
        await ve.check_stop()
        sc = p.state["scenes"][i]
        RUN.progress = {"done": i, "total": len(p.state["scenes"]), "label": "Voice lines"}
        needs_tts = sc.get("line") and (sc.get("speaker") == "NARRATOR" or voice_mode == "exact")
        if needs_tts and not sc.get("audio"):
            v = narrator if sc["speaker"] == "NARRATOR" else (voices.get(sc["speaker"]) or narrator)
            RUN.log(f"Line {sc['n']} - {VO.voice_name_of(v)}: {sc['line'][:70]}")
            sc["audio"] = await ve.tts(sc["line"], v)
            p.save()
            if sc["audio"]["dur_ms"] > 9800 and len(sc["line"].split()) > 6:
                RUN.log(f"  line {sc['n']} is {sc['audio']['dur_ms'] / 1000:.1f}s - splitting it into two shots (10 s limit).")
                words = sc["line"].split()
                half = SP.split_long_lines([dict(sc, **{"line": sc["line"], "b": sc.get("b", 0), "continue": sc.get("continue"),
                                                       "image": sc.get("image", ""), "camera": sc["shots"][0]["camera"],
                                                       "action": sc["shots"][0]["action"], "seconds": sc.get("seconds", 4)})],
                                           max(4, len(words) // 2))
                new = SP.to_scenes([{k: h[k] for k in ("b", "continue", "image", "camera", "action", "speaker", "line",
                                                       "delivery", "seconds")} for h in half], a)
                p.state["scenes"][i:i + 1] = new
                for k, x in enumerate(p.state["scenes"], 1):
                    x["n"] = k
                p.save()
                continue
        i += 1
    SP.time_scenes(p.state["scenes"])
    total = sum(x["len"] for x in p.state["scenes"])
    p.state["narration_s"] = total
    RUN.log(f"All voice lines ready. Movie length about {total / 60:.1f} min.")
    set_stage(p, "narrated")
    dlg_timing(p)
    set_stage(p, "prompted")


def dlg_timing(p: Project) -> str:
    s = load_settings()
    SP.time_scenes(p.state["scenes"])
    duration = sum(float(sc.get("len") or 0) for sc in p.state["scenes"])
    if duration > MAX_VIDEO_SECONDS:
        raise VEError(f"The screenplay is about {duration / 60:.1f} minutes; the maximum video length is 18 minutes.")
    n = len(p.state["scenes"])
    p.state["budget_ok"] = p.state.get("budget_ok") or n <= int(s["max_clips"])
    set_stage(p, "planned")
    fresh = sum(1 for x in p.state["scenes"] if not x.get("continue"))
    msg = f"{n} shots timed to the voices ({fresh} new keyframes, {n - fresh} continue from the previous clip's last frame)."
    if not p.state["budget_ok"]:
        msg += f" ⚠ More than your limit of {s['max_clips']} clips - approve the budget before rendering."
    RUN.log(msg)
    return msg


def is_dialogue(p: Project) -> bool:
    return p.state.get("story_type", "narrated") in ("dialogue", "both")


async def step_render(p: Project):
    """One scheduler for both modes. Never more clips rendering than VideoExpress allows (counted from the
    library itself), continuity shots wait for the previous clip, failed clips retry with the SAME start frame."""
    s = load_settings()
    if not stage_at_least(p, "prompted"):
        raise VEError("Write the scene prompts first.")
    scenes, split_count = P.split_overlong_scenes(
        p.state.get("scenes", []), MAX_VIDEO_CLIP_SECONDS
    )
    if split_count:
        p.state["scenes"] = scenes
        p.state["budget_ok"] = len(scenes) <= int(s["max_clips"])
        p.save()
        RUN.log(
            f"Split {split_count} legacy scene(s) over {MAX_VIDEO_CLIP_SECONDS}s into "
            f"VideoExpress-compatible clips; existing completed clips were kept."
        )
    if not p.state.get("budget_ok"):
        raise VEError("Approve the budget first (Scenes tab).")
    ve = await RUN.browser()
    a, preset = p.state["analysis"], p.state["preset"]
    scenes = p.state["scenes"]
    dialogue = is_dialogue(p)
    voice_mode = s.get("dialogue_voice_mode", "exact")
    retries = int(s["image_retries"])
    limit = max(1, int(s.get("max_parallel", 5)))
    image_type = s.get("image_type", "")
    RUN.progress = {"done": 0, "total": len(scenes), "label": "Preparing scenes"}
    last = {"poll": 0.0, "wait_log": 0.0}

    async def poll(force=False):
        if not force and time.time() - last["poll"] < 12:
            return
        last["poll"] = time.time()
        refs = {sc["n"]: sc["ref"] for sc in scenes if sc.get("status") == "rendering"}
        if refs:
            res = await ve.video_status(refs)
            for sc in scenes:
                r = res.get(sc["n"])
                if not r:
                    continue
                if r["state"] == "done":
                    it = r["item"]
                    sc.update(status="done", lib_id=it["id"], fileName=it.get("fileName", ""),
                              video_url=it.get("mediaPath", ""), dur_ms=it.get("duration"))
                elif r["state"] == "failed":
                    sc["status"] = "failed"
                    RUN.log(f"Scene {sc['n']} render failed in VideoExpress (try {sc['tries']}).")
                elif time.time() - sc.get("submitted", time.time()) > 45 * 60:
                    sc["status"] = "failed"
                    RUN.log(f"Scene {sc['n']} took over 45 min; will retry.")
            p.save()
        RUN.progress["done"] = sum(1 for x in scenes if x["status"] == "done")
        active = sum(1 for x in scenes if x["status"] == "rendering")
        RUN.progress["label"] = f"{active} clips rendering" if active else "Scenes complete"

    async def free_slot():
        while True:
            await ve.check_stop()
            await poll(force=True)
            ours = sum(1 for x in scenes if x["status"] == "rendering")
            busy = max(ours, await ve.pending_videos())
            if busy < limit:
                return
            if time.time() - last["wait_log"] > 60:
                RUN.log(f"Waiting for a free render slot ({busy}/{limit} clips rendering)…")
                last["wait_log"] = time.time()
            await ve.sleep(15)

    def prev_of(sc):
        i = scenes.index(sc)
        return scenes[i - 1] if i > 0 else None

    def ready(sc):
        if sc["status"] in ("done", "rendering"):
            return False
        if sc["status"] == "failed" and sc["tries"] > retries:
            return False
        if dialogue and sc.get("continue"):
            pv = prev_of(sc)
            return bool(pv and pv.get("status") == "done" and pv.get("lib_id"))
        return True

    async def start_frame(sc) -> dict:
        """Returns the source for the clip: a fresh keyframe, a reused one, or the previous clip's last frame."""
        if dialogue and sc.get("continue"):
            pv = prev_of(sc)
            if not sc.get("frame") or sc["frame"].get("from") != pv["lib_id"]:
                fr = await ve.last_frame(pv["lib_id"], pv.get("dur_ms") or pv["len"] * 1000,
                                         f"{p.state['name'][:30]} - frame for shot {sc['n']}")
                sc["frame"] = dict(fr, **{"from": pv["lib_id"]})
                sc["preview"] = fr.get("url", "")
                p.save()
            return {"lib_id": sc["frame"]["id"], "lib_file": sc["frame"]["fileName"]}
        if sc.get("img") and sc.get("img_ok"):
            return {"uuid": sc["img"]}
        sc["img"] = await ve.keyframe(build_image_prompt(sc, a, P.expand), image_type)
        sc["img_ok"] = True
        sc["preview"] = ve.preview_url(sc["img"])
        p.save()
        return {"uuid": sc["img"]}

    async def submit(sc):
        sc["tries"] += 1
        RUN.progress["label"] = f"Scene {sc['n']}/{len(scenes)} · generating keyframe"
        kind = "line" if sc.get("line") and sc.get("speaker") not in ("", "NARRATOR") else "silent"
        RUN.log(f"Scene {sc['n']}/{len(scenes)} ({sc['len']}s{', continues' if sc.get('continue') and dialogue else ''}"
                f"{', talking' if kind == 'line' else ''}): start frame…")
        try:
            src = await start_frame(sc)
            RUN.progress["label"] = f"Scene {sc['n']}/{len(scenes)} · submitting clip"
            await free_slot()
            if dialogue:
                prompt = SP.shot_video_prompt(sc, a, preset, P.expand, voice_mode)
                if kind == "line" and voice_mode == "exact" and sc.get("audio", {}).get("id"):
                    sc["ref"] = await ve.lipsync_clip(src, prompt, sc["audio"]["id"])
                else:
                    sc["ref"] = await ve.clip(src, prompt, sc["len"], with_sound=(kind == "line"))
            else:
                sc["ref"] = await ve.clip(src, build_video_prompt(sc, a, preset, P.expand, P.shots_text), sc["len"])
            sc.update(status="rendering", submitted=time.time())
            sc.pop("error", None)
            active = sum(1 for x in scenes if x["status"] == "rendering")
            RUN.progress["label"] = f"{active} clips rendering"
            RUN.log(f"  submitted clip ({sc['ref']})")
        except VEError as e:
            sc["status"] = "failed"
            sc["error"] = str(e)
            RUN.progress["label"] = f"Scene {sc['n']}/{len(scenes)} failed; see log"
            if "KEYFRAME_MISSING" in str(e):
                sc["img_ok"] = False          # the old keyframe is gone from the dialog: make a new one next time
            RUN.log(f"  scene {sc['n']} problem: {e}")
            if sc["tries"] > retries:
                RUN.log(f"  scene {sc['n']} skipped after {sc['tries']} tries - fix it in the Scenes tab and press Render again.")
            await RUN._shot()
            await ve.sleep(3)
        p.save()

    while True:
        await ve.check_stop()
        todo = [sc for sc in scenes if ready(sc)]
        if todo:
            await submit(todo[0])
            continue
        if any(x["status"] == "rendering" for x in scenes):
            await ve.sleep(15)
            await poll(force=True)
            continue
        break

    bad = [x["n"] for x in scenes if x["status"] != "done"]
    if bad:
        raise VEError(f"{len(bad)} scenes did not render: {bad}. Edit them in the Scenes tab and press Render again.")
    set_stage(p, "rendered")
    RUN.log("Every clip is rendered.")


async def _assemble_long_video(p: Project, ve: VideoExpress, name: str, ex: dict):
    segment_limit = MAX_EXPORT_SEGMENT_SECONDS - 10
    dialogue = is_dialogue(p)
    try:
        if dialogue:
            groups = P.partition_scene_exports(p.state["scenes"], segment_limit)
        else:
            groups = P.partition_narration_exports(p.state["parts"], p.state["scenes"], segment_limit)
    except ValueError as e:
        raise VEError(str(e)) from e
    if len(groups) < 2:
        raise VEError("This video cannot be divided into exports under 10 minutes. Re-plan the narration into "
                      "shorter parts and try again.")

    segments = ex.get("segments") or []
    same_plan = len(segments) == len(groups) and all(
        old.get("scene_numbers") == new.get("scene_numbers")
        and old.get("part_start") == new.get("part_start")
        and old.get("part_end") == new.get("part_end")
        for old, new in zip(segments, groups)
    )
    if not same_plan:
        segments = []
        for i, group in enumerate(groups, 1):
            part_name = f"{_safe(name)[:42]} - Part {i} of {len(groups)}"
            segments.append({**group, "name": part_name})
        ex["segments"] = segments
        p.state["export"] = ex
        p.save()

    for i, segment in enumerate(segments, 1):
        await ve.check_stop()
        segment_path = Path(segment.get("file") or "")
        if segment.get("file") and segment_path.is_file():
            continue
        if not segment.get("url"):
            await ve.close_dialog()
            copy = await ve.ensure_blank()
            if copy:
                RUN.log(f"The editor had unsaved work - it was saved as '{copy}' before starting the next segment.")
            scenes = [sc for sc in p.state["scenes"] if sc["n"] in segment["scene_numbers"]]
            items = [{"id": sc["lib_id"], "fileName": sc.get("fileName", "")} for sc in scenes]
            RUN.log(f"Exporting segment {i}/{len(segments)} ({segment['duration_s'] / 60:.1f} min)…")
            RUN.progress = {"done": 0, "total": len(items), "label": f"Segment {i}/{len(segments)} clips"}
            await ve.add_clips(items, lambda d, t: RUN.progress.update(done=d))
            rows = await ve.timeline()
            bricks = sorted(rows[0], key=lambda b: b["left"])
            if len(bricks) != len(scenes):
                raise VEError(f"Segment {i}: expected {len(scenes)} video clips, found {len(bricks)}.")
            first_len = (scenes[0].get("dur_ms") or scenes[0]["len"] * 1000) / 1000
            pxs = bricks[0]["width"] / first_len
            gaps = await ve.check_gaps()
            if gaps:
                raise VEError(f"Gaps in segment {i} at positions {gaps[:10]}")
            if dialogue:
                narr = [(sc["audio"]["id"], b["left"]) for sc, b in zip(scenes, bricks)
                        if sc.get("speaker") == "NARRATOR" and sc.get("audio", {}).get("id")]
                if narr:
                    await ve.place_audio_at(narr)
            else:
                parts = p.state["parts"][segment["part_start"]:segment["part_end"]]
                end_px = await ve.place_narration([part["audio"]["id"] for part in parts])
                await ve.trim_video(end_px)
            await ve.save_new(segment["name"])
            result = await ve.export(segment["name"])
            segment.update(result)
            p.state["export"] = ex
            p.save()
        if not segment.get("file") or not Path(segment["file"]).is_file():
            dest = p.dir / "exports" / f"segment-{i:02d}.mp4"
            RUN.log(f"Downloading segment {i}/{len(segments)}…")
            segment["file"] = str(await asyncio.to_thread(download, segment["url"], dest, RUN.log))
            p.state["export"] = ex
            p.save()

    s = load_settings()
    out = Path(s["download_dir"]).expanduser() / (_safe(name) + ".mp4")
    out = _unique(out)
    RUN.log("Joining the exported segments on this PC…")
    final = await asyncio.to_thread(SND.concatenate, [Path(x["file"]) for x in segments], out, RUN.log)
    ex["file"] = str(final)
    ex["duration_s"] = round(SND.duration(final), 2)
    ex["stitched"] = True
    p.state["export"] = ex
    set_stage(p, "exported")
    RUN.log(f"✔ Joined video saved: {final}")


async def step_assemble(p: Project):
    s = load_settings()
    if not stage_at_least(p, "rendered"):
        raise VEError("Render all scenes first.")
    ve = await RUN.browser()
    await ve.close_dialog()
    name = p.state.get("export_name") or p.state["name"]
    ex = p.state.get("export") or {}
    duration_s = (sum(float(sc.get("len") or 0) for sc in p.state.get("scenes", []))
                  if is_dialogue(p) else sum(float(part.get("duration") or 0) for part in p.state.get("parts", [])))
    if duration_s > MAX_EXPORT_SEGMENT_SECONDS:
        if ex.get("file") and Path(ex["file"]).is_file():
            set_stage(p, "exported")
            RUN.log(f"✔ Joined video already exists: {ex['file']}")
            return
        await _assemble_long_video(p, ve, name, ex)
        return
    if not ex.get("url"):
        if not ex.get("saved"):
            RUN.log("Starting a new blank project in VideoExpress…")
            copy = await ve.ensure_blank()
            if copy:
                RUN.log(f"The editor had unsaved work - it was saved as '{copy}' before starting fresh.")
            scenes = p.state["scenes"]
            RUN.progress = {"done": 0, "total": len(scenes), "label": "Clips on timeline"}
            items = [{"id": sc["lib_id"], "fileName": sc.get("fileName", "")} for sc in scenes]
            RUN.log("Adding clips to the timeline in order…")
            await ve.add_clips(items, lambda d, t: RUN.progress.update(done=d))
            rows = await ve.timeline()
            bricks = sorted(rows[0], key=lambda b: b["left"])
            first_len = (scenes[0].get("dur_ms") or scenes[0]["len"] * 1000) / 1000
            pxs = bricks[0]["width"] / first_len
            RUN.log(f"Timeline scale {pxs:.2f} px/s.")
            gaps = await ve.check_gaps()
            if gaps:
                raise VEError(f"Gaps between clips at positions {gaps[:10]}")
            if is_dialogue(p):
                # real positions of every clip; characters' voices are inside their clips,
                # narrator lines go on track 2 exactly where their shot starts
                for sc, b in zip(scenes, bricks):
                    sc["start"] = round(b["left"] / pxs, 2)
                    sc["t_start"] = sc["start"]
                narr = [(sc["audio"]["id"], b["left"]) for sc, b in zip(scenes, bricks)
                        if sc.get("speaker") == "NARRATOR" and sc.get("audio", {}).get("id")]
                if narr:
                    RUN.log(f"Placing {len(narr)} narrator lines on track 2…")
                    await ve.place_audio_at(narr)
                end_px = bricks[-1]["left"] + bricks[-1]["width"]
                p.save()
            else:
                RUN.log("Placing narration on track 2…")
                end_px = await ve.place_narration([x["audio"]["id"] for x in p.state["parts"]])
                RUN.log(f"Narration ends at {end_px / pxs:.2f}s; trimming the video to match…")
                await ve.trim_video(end_px)
            await ve.save_new(name)
            ex = {"saved": True, "duration_s": round(end_px / pxs, 2)}
            p.state["export"] = ex
            p.save()
            RUN.log(f"Saved as new project '{name}'.")
        r = await ve.export(name)
        ex.update(r)
        p.state["export"] = ex
        p.save()
        RUN.log(f"Export ready: {r['url']}")
    suffix = " (voice only)" if sound_enabled(s) else ""
    dest = Path(s["download_dir"]).expanduser() / (_safe(name) + suffix + ".mp4")
    RUN.log(f"Downloading to {dest}…")
    path = await asyncio.to_thread(download, ex["url"], dest, RUN.log)
    ex["file"] = str(path)
    p.state["export"] = ex
    set_stage(p, "exported")
    RUN.log(f"✔ Video saved: {path}")


async def step_all(p: Project):
    """'Run everything': from the script (or topic) to the finished video. With Autopilot on (default) it never
    stops to wait for you - prompts are not paused for review, the clip budget is approved automatically and
    failed clips get an extra round of retries."""
    s = load_settings()
    auto = bool(s.get("autopilot", True))
    if not stage_at_least(p, "analysed"):
        await step_analyse(p)
        if p.state.get("manual"):
            return
    if not stage_at_least(p, "narrated"):
        await step_narrate(p)
        if not auto and is_dialogue(p) and p.state.get("review_first", True):
            RUN.log("Screenplay, voices and shots are ready. Check the Scenes and Characters tabs, then press "
                    "'Run everything' (or Render) to make the movie.")
            p.state["review_first"] = False
            p.save()
            return
    if not stage_at_least(p, "planned"):
        step_plan(p)
    if not stage_at_least(p, "prompted"):
        for _ in range(3):
            await step_prompts(p)
            if p.state.get("manual"):
                return
            if stage_at_least(p, "prompted"):
                break
            RUN.log("Some prompts are still missing - asking the AI again…")
        else:
            raise VEError("The AI kept skipping some scene prompts. Run 'Write prompts' again.")
        if not auto and p.state.get("review_first", True):
            RUN.log("Prompts are ready for review. Press 'Render' (or 'Run everything') when happy.")
            p.state["review_first"] = False
            p.save()
            return
    if not p.state.get("budget_ok"):
        if not auto:
            RUN.log("⚠ Scene count is above your clip limit - approve the budget in the Scenes tab.")
            return
        p.state["budget_ok"] = True
        p.save()
        RUN.log(f"Autopilot: {len(p.state['scenes'])} clips is above your limit of {s['max_clips']} - continuing anyway.")
    if not stage_at_least(p, "rendered"):
        try:
            await step_render(p)
        except VEError as e:
            if not auto:
                raise
            RUN.log(f"{e} Autopilot: one more round for the failed clips…")
            for sc in p.state["scenes"]:
                if sc.get("status") == "failed":
                    sc["tries"] = 0
            p.save()
            await step_render(p)
    if not stage_at_least(p, "exported"):
        await step_assemble(p)
    if sound_enabled(load_settings()) and not stage_at_least(p, "mixed"):
        await step_sound(p)
    RUN.log("🎬 All done - your video is in your Downloads folder.")


# ====================================================== music + sound effects
def sound_enabled(s: dict) -> bool:
    return s.get("music_mode", "auto") != "off" or bool(s.get("sfx_on"))


def _scenes_for_sound(p: Project) -> list[dict]:
    """Scenes with the time the narration actually reaches them (more precise than the clip grid)."""
    return [dict(sc, start=round(sc.get("t_start", sc["start"]), 1)) for sc in p.state.get("scenes", [])]


def _unique(path: Path) -> Path:
    out, i = path, 2
    while out.exists():
        out = path.with_name(f"{path.stem} ({i}){path.suffix}")
        i += 1
    return out


async def step_sound(p: Project):
    s = load_settings()
    if not p.state.get("scenes") or not p.state.get("analysis"):
        raise VEError("Plan the scenes first (steps 1-3).")
    music_mode = p.state.get("music_mode") or s.get("music_mode", "auto")
    sfx_on = bool(s.get("sfx_on"))
    if music_mode == "off" and not sfx_on:
        RUN.log("Music and sound effects are both switched off in Settings.")
        return
    # 1. sound plan (mood + timed effects)
    if not p.state.get("sound"):
        brain = _brain(s)
        mx = int(s.get("sfx_max", 8)) if sfx_on else 0
        if brain.provider == "manual":
            sys_, task = brain.sound_prompt(p.state["analysis"], _scenes_for_sound(p), mx, SND.MOODS)
            p.state["manual"] = {"step": "sound", "prompt": sys_ + "\n\n" + task}
            p.save()
            RUN.log("Manual AI mode: copy the sound prompt into Claude or ChatGPT and paste the JSON reply back.")
            return
        RUN.log("Choosing the music mood and sound-effect moments…")
        p.state["sound"] = await asyncio.to_thread(brain.sound_plan, p.state["analysis"], _scenes_for_sound(p), mx, SND.MOODS)
        p.save()
        plan = p.state["sound"]
        RUN.log(f"Mood: {plan['mood']} · {len(plan['sfx'])} sound effects planned.")
    plan = p.state["sound"]
    # 2. sound effects with VideoExpress' AI Sound Maker
    cues = [c for c in plan.get("sfx", []) if c.get("enabled", True)] if sfx_on else []
    todo = [c for c in cues if not (c.get("file") and Path(c["file"]).exists())]
    if todo:
        ve = await RUN.browser()
        await ve.close_dialog()
        RUN.progress = {"done": len(cues) - len(todo), "total": len(cues), "label": "Sound effects"}
        for c in todo:
            await ve.check_stop()
            if not c.get("audio"):
                RUN.log(f"Sound effect at {c['at']:.1f}s ({c['seconds']}s): {c['prompt']}")
                try:
                    c["audio"] = await ve.make_sound(c["prompt"], c["seconds"])
                    c.pop("error", None)
                except VEError as e:
                    c["error"] = str(e)
                    RUN.log(f"  skipped this effect: {e}")
                    p.save()
                    continue
                p.save()
            dest = p.dir / "sfx" / f"sfx_{c['n']:03d}_{int(c['at'] * 10)}.mp3"
            if not dest.exists():
                dest = await asyncio.to_thread(download, c["audio"]["url"], dest, lambda m: None)
            c["file"] = str(dest)
            p.save()
            RUN.progress["done"] += 1
        await ve.close_sound_maker()
    # 3. music
    music = None
    if music_mode != "off":
        folder = s.get("music_folder", "")
        chosen = p.state.get("music_file") or ""
        if chosen and Path(chosen).exists():
            music = Path(chosen)
        elif music_mode in ("auto", "folder"):
            music = SND.pick_music(folder, plan.get("mood", "calm"), plan.get("music_keywords", ""), p.slug)
            if music is None and music_mode == "folder":
                RUN.log(f"No music files in {folder} - using built-in mood music instead.")
        if music is None:
            music = await asyncio.to_thread(SND.make_mood_music, plan.get("mood", "calm"),
                                            p.dir / f"music_{plan.get('mood', 'calm')}.wav")
            RUN.log(f"Music: built-in '{plan.get('mood', 'calm')}' mood music (made on your PC, royalty-free).")
        else:
            RUN.log(f"Music: {music.name}")
        p.state["music_used"] = str(music)
    # 4. mix
    if not stage_at_least(p, "exported") or not (p.state.get("export") or {}).get("file"):
        p.save()
        RUN.log("Music and sound effects are ready. They will be mixed in after the video is exported (step 6).")
        return
    voice = Path(p.state["export"]["file"])
    name = _safe(p.state.get("export_name") or p.state["name"])
    final = _unique(voice.with_name(name + ".mp4"))
    fx = [{"file": c["file"], "at": c["at"], "boost_db": 5.0 if c.get("level") == "medium" else 0.0}
          for c in cues if c.get("file") and Path(c["file"]).exists()]
    out = await asyncio.to_thread(SND.mix, voice, final, music, fx, float(s.get("music_level", 15)),
                                  float(s.get("sfx_level", 45)), bool(s.get("duck", True)), RUN.log)
    p.state["export"]["final"] = str(out)
    set_stage(p, "mixed")
    RUN.log(f"✔ Final video with music and sound effects: {out}")


def _safe(name: str) -> str:
    return "".join(c for c in name if c not in '<>:"/\\|?*').strip() or "video"
