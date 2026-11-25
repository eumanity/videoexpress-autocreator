"""Local web server for the VideoExpress AutoCreator window (only listens on 127.0.0.1)."""
from __future__ import annotations

from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from . import pipeline as PL
from . import planner as P
from .brain import STYLE_PRESETS, build_image_prompt, build_video_prompt
from .project import MAX_VIDEO_MINUTES, Project, list_projects, load_settings, open_project, save_settings

STATIC = Path(__file__).parent / "static"
app = Flask(__name__, static_folder=None)
MASK = "••••"


def _proj(slug: str) -> Project:
    return open_project(slug)


@app.errorhandler(Exception)
def _err(e):
    code = 404 if isinstance(e, FileNotFoundError) else 400
    return jsonify({"error": str(e)}), code


@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.get("/static/<path:f>")
def static_files(f):
    return send_from_directory(STATIC, f)


# ---------------------------------------------------------------- settings
@app.get("/api/settings")
def get_settings():
    s = load_settings()
    for k in ("groq_key", "anthropic_key", "gemini_key"):
        if s.get(k):
            s[k] = MASK + s[k][-4:]
    return jsonify({"settings": s, "presets": {k: v["label"] for k, v in STYLE_PRESETS.items()},
                    "preset_types": {k: v["image_type"] for k, v in STYLE_PRESETS.items()}})


@app.post("/api/settings")
def post_settings():
    new = dict(request.json or {})
    for k in ("groq_key", "anthropic_key", "gemini_key"):
        if str(new.get(k, "")).startswith(MASK):
            new.pop(k)
    save_settings(new)
    return get_settings()


@app.get("/api/version")
def version():
    from . import __version__
    s = load_settings()
    return jsonify({"version": __version__, "provider": s["provider"],
                    "model": s.get({"groq": "groq_model", "gemini": "gemini_model", "claude": "claude_model"}.get(s["provider"], ""), ""),
                    "has_key": bool(s.get({"groq": "groq_key", "gemini": "gemini_key", "claude": "anthropic_key"}.get(s["provider"], "x"), "ok")),
                    "scene_rewrite": True})


@app.post("/api/open_keys")
def open_keys():
    import os
    import subprocess
    import sys
    from .project import ensure_keys_file
    path = ensure_keys_file()
    if sys.platform == "win32":
        subprocess.Popen(["notepad.exe", str(path)])
    elif hasattr(os, "startfile"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    return jsonify({"path": str(path)})


@app.post("/api/test_ai")
def test_ai():
    from .brain import Brain
    return jsonify({"message": Brain(load_settings(), on_settings=save_settings).test()})


# ---------------------------------------------------------------- projects
@app.get("/api/projects")
def projects():
    return jsonify(list_projects())


@app.post("/api/projects")
def new_project():
    d = request.json or {}
    script = (d.get("script") or "").strip()
    story_type = d.get("story_type") or "narrated"
    topic = (d.get("topic") or "").strip()
    if len(script) < 40 and len(topic) < 8 and not (story_type != "narrated" and len(script) >= 15):
        raise ValueError("Type a topic for the AI to write about, or paste your own script.")
    name = (d.get("name") or "").strip() or (topic or script.split("\n")[0])[:60]
    p = Project.create(name, script, d.get("preset") or load_settings()["preset"])
    p.state["story_type"] = story_type if story_type in ("narrated", "dialogue", "both") else "narrated"
    p.state["topic"] = topic
    p.state["reference"] = (d.get("reference") or "").strip()
    p.state["notes"] = (d.get("notes") or "").strip()
    p.state["minutes"] = max(0.5, min(float(MAX_VIDEO_MINUTES), float(d.get("minutes") or 3)))
    p.state["estimate_s"] = (round(P.estimate_seconds(P.clean_script(script), load_settings()["wpm"]))
                             if p.state["story_type"] == "narrated" and script else int(p.state["minutes"] * 60))
    p.save()
    if d.get("start"):
        PL.RUN.start("all", p, lambda: PL.step_all(p))
    return jsonify(p.summary())


@app.get("/api/project/<slug>")
def get_project(slug):
    p = _proj(slug)
    st = dict(p.state)
    a = st.get("analysis")
    if a and st.get("scenes"):
        for sc in st["scenes"]:
            if sc.get("image"):
                sc["full_image"] = build_image_prompt(sc, a, P.expand)
                sc["full_video"] = build_video_prompt(sc, a, st["preset"], P.expand, P.shots_text)
    st.pop("log", None)
    return jsonify(st)


@app.post("/api/project/<slug>/step/<step>")
def run_step(slug, step):
    p = _proj(slug)
    if step == "script":
        PL.RUN.start("script", p, lambda: PL.step_script(p, force=True))
        return jsonify({"started": step})
    if step == "plan":
        if PL.RUN.busy:
            raise RuntimeError("Another job is running. Stop it first.")
        PL.RUN.project = p
        return jsonify({"message": PL.step_plan(p)})
    fn = {"analyse": PL.step_analyse, "narrate": PL.step_narrate, "prompts": PL.step_prompts,
          "render": PL.step_render, "assemble": PL.step_assemble, "sound": PL.step_sound, "all": PL.step_all}[step]
    if step == "render":
        p.state["review_first"] = False
        p.save()
    PL.RUN.start(step, p, lambda: fn(p))
    return jsonify({"started": step})


@app.post("/api/project/<slug>/manual")
def manual(slug):
    p = _proj(slug)
    return jsonify({"message": PL.apply_manual(p, (request.json or {}).get("reply", ""))})


@app.post("/api/project/<slug>/update")
def update(slug):
    p = _proj(slug)
    d = request.json or {}
    for k in ("name", "export_name", "preset"):
        if k in d and d[k]:
            p.state[k] = d[k]
    if "script" in d:
        if PL.stage_at_least(p, "narrated"):
            raise ValueError("The narration was already made from this script; start a new video to use a different one.")
        p.state["script"] = d["script"]
        p.state["parts"] = []
    if "budget_ok" in d:
        p.state["budget_ok"] = bool(d["budget_ok"])
    if "analysis" in d and isinstance(d["analysis"], dict):
        p.state["analysis"] = d["analysis"]
    p.save()
    return jsonify({"ok": True})


@app.post("/api/project/<slug>/scene/<int:n>")
def edit_scene(slug, n):
    p = _proj(slug)
    d = request.json or {}
    sc = next(s for s in p.state["scenes"] if s["n"] == n)
    if sc.get("status") == "rendering":
        raise ValueError("This scene is rendering right now.")
    for k in ("image", "ctx", "delivery"):
        if k in d:
            sc[k] = d[k]
    if "line" in d and d["line"].strip() != (sc.get("line") or ""):
        sc["line"] = d["line"].strip()
        sc.pop("audio", None)                     # the voice must be re-made for the new words
        if PL.stage_at_least(p, "narrated"):
            p.state["stage"] = "analysed"
    if "continue" in d:
        sc["continue"] = bool(d["continue"])
    if "shots" in d:
        sc["shots"] = d["shots"]
    if d.get("redo"):
        sc.update(status="pending", tries=0)
        for k in ("img", "img_ok", "frame", "ref", "lib_id", "fileName", "preview", "video_url", "dur_ms", "error"):
            sc.pop(k, None)
        if PL.stage_at_least(p, "rendered"):
            p.state["stage"] = "prompted"
    p.save()
    return jsonify({"ok": True})


@app.post("/api/project/<slug>/scene/<int:n>/rewrite")
def rewrite_scene_prompt(slug, n):
    p = _proj(slug)
    if PL.RUN.busy:
        raise RuntimeError("Another job is running. Stop it first.")
    sc = next((s for s in p.state.get("scenes", []) if s["n"] == n), None)
    if sc is None:
        raise ValueError(f"Scene {n} was not found.")
    if sc.get("status") == "rendering":
        raise ValueError("This scene is rendering right now.")
    d = request.json or {}
    feedback = str(d.get("feedback", "")).strip()[:1000]
    PL.RUN.start("rewrite scene prompt", p, lambda: PL.step_rewrite_scene_prompt(p, n, feedback))
    return jsonify({"started": True})


@app.get("/api/music_files")
def music_files():
    from .sound import list_music
    folder = load_settings().get("music_folder", "")
    return jsonify({"folder": folder, "files": [str(p) for p in list_music(folder)]})


@app.post("/api/open_music_folder")
def open_music_folder():
    import subprocess
    import sys
    from .project import ensure_keys_file
    ensure_keys_file()
    folder = load_settings().get("music_folder", "")
    Path(folder).mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        subprocess.Popen(["explorer", folder])
    return jsonify({"folder": folder})


@app.post("/api/project/<slug>/sound")
def edit_sound(slug):
    p = _proj(slug)
    d = request.json or {}
    if d.get("replan"):
        p.state.pop("sound", None)
    else:
        snd = p.state.get("sound") or {"mood": "calm", "music_keywords": "", "sfx": []}
        if d.get("mood"):
            snd["mood"] = d["mood"]
        if "sfx" in d and isinstance(d["sfx"], list):
            old = {(c.get("prompt"), c.get("seconds")): c for c in snd.get("sfx", [])}
            new = []
            for c in d["sfx"]:
                prev = old.get((c.get("prompt"), int(c.get("seconds", 4))), {})
                item = dict(prev)  # keep generated audio when the prompt did not change
                item.update(n=int(c.get("n", 0)), at=float(c.get("at", 0)), prompt=str(c.get("prompt", "")),
                            seconds=max(2, min(7, int(c.get("seconds", 4)))), level=c.get("level", "low"),
                            enabled=bool(c.get("enabled", True)))
                if not prev:
                    item.pop("audio", None)
                    item.pop("file", None)
                new.append(item)
            snd["sfx"] = sorted(new, key=lambda c: c["at"])
        p.state["sound"] = snd
    for k in ("music_file", "music_mode"):
        if k in d:
            p.state[k] = d[k]
    if PL.stage_at_least(p, "mixed"):
        p.state["stage"] = "exported"
    p.save()
    return jsonify({"ok": True})


@app.get("/api/project/<slug>/pack.txt")
def pack(slug):
    """Everything as plain text: SEO, thumbnails and every prompt (works without VideoExpress)."""
    st = _proj(slug).state
    a = st.get("analysis") or {}
    seo = a.get("seo", {})
    out = [f"# {st.get('name')}", "", "## YouTube titles", *[f"- {t}" for t in seo.get("titles", [])], "",
           "## Description", seo.get("description", ""), "", "## Tags", ", ".join(seo.get("tags", [])), "",
           " ".join(seo.get("hashtags", [])), "", "## Thumbnails"]
    for i, t in enumerate(a.get("thumbnails", []), 1):
        out += [f"{i}. {t.get('concept', '')} - text: {t.get('text', '')}", t.get("prompt", ""), ""]
    snd = st.get("sound") or {}
    if snd:
        out += ["## Sound design", f"Mood: {snd.get('mood')} ({snd.get('music_keywords', '')})"]
        out += [f"- {c['at']:.1f}s ({c['seconds']}s, {c.get('level', 'low')}): {c['prompt']}" for c in snd.get("sfx", [])]
        out += [""]
    out += ["## Narration parts"]
    for x in st.get("parts", []):
        out += [f"--- Part {x['k']} ---", x["text"], ""]
    out += ["## Scenes"]
    for sc in st.get("scenes", []):
        if sc.get("image"):
            out += [f"--- Scene {sc['n']} ({sc['start']}s, {sc['len']}s) ---", f"NARRATION: {sc['text']}",
                    "IMAGE PROMPT:", build_image_prompt(sc, a, P.expand), "VIDEO PROMPT:",
                    build_video_prompt(sc, a, st["preset"], P.expand, P.shots_text), ""]
    return Response("\n".join(out), mimetype="text/plain",
                    headers={"Content-Disposition": f'attachment; filename="{slug}-prompts.txt"'})


# ------------------------------------------------------------------ runner
@app.get("/api/status")
def status():
    return jsonify(PL.RUN.status())


@app.post("/api/continue")
def cont():
    PL.RUN.resume()
    return jsonify({"ok": True})


@app.post("/api/stop")
def stop():
    PL.RUN.stop()
    return jsonify({"ok": True})


@app.post("/api/browser")
def open_browser():
    """Open VideoExpress (user logs in), then run the free self-test."""
    async def go():
        ve = await PL.RUN.browser()
        s = load_settings()
        PL.RUN.log("Running the free VideoExpress self-test (nothing is generated)…")
        results = await ve.diagnose(s.get("tts_voice", ""), s.get("image_type", ""))
        for ok, msg in results:
            PL.RUN.log(("✔ " if ok else "✖ ") + msg)
        PL.RUN.image_types = await ve.image_types()
        await ve.close_dialog()
        try:
            import json as _j
            v = await ve.list_voices()
            save_settings({"voices_cache": _j.dumps(v)})
            PL.RUN.log(f"✔ {len(v)} Text-to-Speech voices available for your characters")
        except Exception as e:  # noqa: BLE001
            PL.RUN.log(f"✖ Could not read the voice list: {e}")
        bad = [m for ok, m in results if not ok]
        PL.RUN.log("Self-test passed - ready to make videos." if not bad else f"Self-test found {len(bad)} problem(s) - see above.")
    PL.RUN.start("VideoExpress check", None, go)
    return jsonify({"started": True})


@app.get("/api/voices")
def voices():
    import json as _j
    from .voices import voice_choices
    try:
        cached = _j.loads(load_settings().get("voices_cache") or "[]")
    except ValueError:
        cached = []
    return jsonify(voice_choices(cached))


@app.post("/api/project/<slug>/characters")
def edit_characters(slug):
    """Change a character's voice (their lines are re-made) or voice description."""
    p = _proj(slug)
    a = p.state.get("analysis") or {}
    changed = set()
    for upd in (request.json or {}).get("characters", []):
        t = next((x for x in a.get("tokens", []) if x["key"] == upd.get("key")), None)
        if not t:
            continue
        if upd.get("voice") and upd["voice"] != t.get("voice"):
            t["voice"] = upd["voice"]
            changed.add(t["key"])
        for k in ("look", "voice_hint", "name"):
            if k in upd:
                t[k] = upd[k]
    for sc in p.state.get("scenes", []):
        if sc.get("speaker") in changed and sc.get("status") != "done":
            sc.pop("audio", None)
    if changed and PL.stage_at_least(p, "narrated") and not PL.stage_at_least(p, "rendered"):
        p.state["stage"] = "analysed"
    p.save()
    return jsonify({"ok": True, "changed": sorted(changed)})


@app.get("/api/image_types")
def image_types():
    return jsonify(getattr(PL.RUN, "image_types", []))
