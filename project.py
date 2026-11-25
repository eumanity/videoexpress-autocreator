"""Projects live in Documents/VideoExpress AutoCreator/projects/<slug>/ as plain JSON so any run can resume."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

HOME = Path(os.environ.get("SCRIPTPILOT_HOME") or (Path.home() / "Documents" / "VideoExpress AutoCreator"))
OLD_HOME = Path.home() / "Documents" / "ScriptPilot"   # name used by versions 1.0-1.2


def _migrate_old_home():
    """Bring keys, settings, projects and the VideoExpress login over from the old ScriptPilot folder (once)."""
    if os.environ.get("SCRIPTPILOT_HOME") or HOME.exists() or not OLD_HOME.exists():
        return
    import shutil
    try:
        shutil.copytree(OLD_HOME, HOME, ignore=shutil.ignore_patterns("window-profile"))
    except Exception:
        HOME.mkdir(parents=True, exist_ok=True)
        for name in ("keys.env", "settings.json"):
            try:
                shutil.copy2(OLD_HOME / name, HOME / name)
            except Exception:
                pass


_migrate_old_home()
PROJECTS = HOME / "projects"
SETTINGS_FILE = HOME / "settings.json"
KEYS_FILE = HOME / "keys.env"
KEYS_TEMPLATE = """# AutoCreator keys - paste each key after the = sign, save this file (Ctrl+S),
# then close and reopen VideoExpress AutoCreator. Lines starting with # are ignored.
# Groq (main AI, free key): https://console.groq.com/keys
GROQ_API_KEY=
# Optional backups:
GEMINI_API_KEY=
ANTHROPIC_API_KEY=
# Which AI to use: groq, gemini, claude, manual or offline
AI_PROVIDER=groq
# Optional model overrides (leave empty for the defaults)
GROQ_MODEL=
GEMINI_MODEL=
CLAUDE_MODEL=
"""
_KEYMAP = {"GROQ_API_KEY": "groq_key", "GEMINI_API_KEY": "gemini_key", "ANTHROPIC_API_KEY": "anthropic_key",
           "AI_PROVIDER": "provider", "GROQ_MODEL": "groq_model", "GEMINI_MODEL": "gemini_model",
           "CLAUDE_MODEL": "claude_model"}


def ensure_keys_file() -> Path:
    HOME.mkdir(parents=True, exist_ok=True)
    if not KEYS_FILE.exists():
        KEYS_FILE.write_text(KEYS_TEMPLATE, "utf-8")
    music = HOME / "music"
    if not music.exists():
        music.mkdir(parents=True, exist_ok=True)
        (music / "PUT YOUR MUSIC HERE.txt").write_text(
            "Drop royalty-free background music here (mp3, wav, m4a, ogg, flac).\n"
            "Tip: put the mood in the file name, e.g. 'tense dark strings.mp3', 'hopeful piano.mp3',\n"
            "'epic drums.mp3' - the app picks the file whose name best matches the story's mood.\n"
            "Moods it uses: tense, dark, epic, sad, hopeful, mysterious, calm, reverent.\n"
            "If this folder is empty, the app makes its own soft mood music on your PC.\n", "utf-8")
    return KEYS_FILE


def read_keys_file() -> dict:
    out = {}
    if KEYS_FILE.exists():
        for line in KEYS_FILE.read_text("utf-8-sig", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip().upper(), v.strip().strip('"').strip("'")
            if k in _KEYMAP and v:
                out[_KEYMAP[k]] = v.lower() if k == "AI_PROVIDER" else v
    return out
_lock = threading.RLock()

MAX_VIDEO_MINUTES = 18
MAX_VIDEO_SECONDS = MAX_VIDEO_MINUTES * 60
MAX_EXPORT_SEGMENT_SECONDS = 600
MAX_VIDEO_CLIP_SECONDS = 10

DEFAULT_SETTINGS = {
    "provider": "groq",              # groq | claude | gemini | manual | offline
    "groq_key": "",
    "groq_model": "openai/gpt-oss-120b",
    "anthropic_key": "",
    "gemini_key": "",
    "claude_model": "claude-sonnet-4-5",
    "gemini_model": "gemini-3.8-flash",
    "ve_url": "https://app.videoexpress.ai/",
    "browser_channel": "chrome",     # chrome | msedge | chromium
    "tts_voice": "Andrew Multilingual",  # exact voice name in VideoExpress Text to Speech ('' = ask each time)
    "aspect": "landscape",           # landscape | vertical
    "preset": "photoreal",
    "image_type": "photorealistic",  # value of VideoExpress' image type dropdown
    "scene_target": 7,
    "scene_min": 3,
    "scene_max": 10,
    "max_parallel": 5,
    "max_clips": 90,
    "image_retries": 2,
    "wpm": 175,
    "tts_limit": 2400,
    "export_cap_seconds": 600,
    "download_dir": str(Path.home() / "Downloads"),
    "music_mode": "auto",            # auto (my folder, else built-in) | folder | builtin | off
    "music_folder": str(HOME / "music"),
    "music_level": 15,               # % of narration loudness
    "duck": True,                    # music dips while the narrator speaks
    "sfx_on": True,
    "sfx_max": 8,
    "sfx_level": 45,
    "autopilot": True,
    "channel_name": "",              # used in subscribe lines of AI-written scripts (empty = generic)               # Run everything never stops to wait for you
    "script_research": True,         # let the script writer look facts up on the web (Groq compound / Gemini search)
    "dialogue_voice_mode": "exact",  # exact = each character's chosen TTS voice + lip-sync | ai = video model voices
    "voices_cache": "",              # JSON list of [id, name] read from your account by the self-test
}


def normalise_scene_bounds(settings: dict) -> dict:
    """Keep scene timing settings within VideoExpress' per-clip limit."""
    scene_max = max(1, min(int(settings.get("scene_max", 10)), MAX_VIDEO_CLIP_SECONDS))
    scene_min = max(1, min(int(settings.get("scene_min", 3)), scene_max))
    scene_target = max(scene_min, min(int(settings.get("scene_target", 7)), scene_max))
    settings.update(scene_min=scene_min, scene_max=scene_max, scene_target=scene_target)
    return settings


def load_settings() -> dict:
    HOME.mkdir(parents=True, exist_ok=True)
    s = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            s.update(json.loads(SETTINGS_FILE.read_text("utf-8")))
        except json.JSONDecodeError:
            pass
    s.update(read_keys_file())          # keys.env always wins
    # retired models from older versions
    if s.get("gemini_model", "").startswith(("gemini-2.", "gemini-1.")):
        s["gemini_model"] = DEFAULT_SETTINGS["gemini_model"]
    if s.get("provider") not in ("groq", "claude", "gemini", "manual", "offline"):
        s["provider"] = "groq"
    return normalise_scene_bounds(s)


def save_settings(new: dict) -> dict:
    s = load_settings()
    for k, v in new.items():
        if k in DEFAULT_SETTINGS:
            d = DEFAULT_SETTINGS[k]
            if isinstance(d, bool):
                s[k] = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(d, int):
                s[k] = int(float(v or 0))
            elif isinstance(d, float):
                s[k] = float(v or 0)
            else:
                s[k] = str(v)
    normalise_scene_bounds(s)
    SETTINGS_FILE.write_text(json.dumps(s, indent=1), "utf-8")
    return s


def slugify(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()[:50]
    return s or f"project-{int(time.time())}"


class Project:
    def __init__(self, slug: str):
        self.slug = slug
        self.dir = PROJECTS / slug
        self.file = self.dir / "state.json"
        self.state: dict = {}
        self.load()

    @classmethod
    def create(cls, name: str, script: str, preset: str) -> "Project":
        slug, i = slugify(name), 2
        base = slug
        while (PROJECTS / slug).exists():
            slug = f"{base}-{i}"
            i += 1
        (PROJECTS / slug).mkdir(parents=True)
        p = cls(slug)
        p.state = {"name": name, "script": script, "preset": preset, "created": time.time(),
                   "stage": "new", "analysis": None, "parts": [], "scenes": [], "export": None, "log": []}
        p.save()
        _cache[slug] = p
        return p

    def load(self):
        if self.file.exists():
            self.state = json.loads(self.file.read_text("utf-8"))

    def save(self):
        with _lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            for _ in range(5):  # the UI thread may edit while the job thread saves
                try:
                    data = json.dumps(self.state, indent=1, ensure_ascii=False)
                    break
                except RuntimeError:
                    time.sleep(0.05)
            else:
                return
            tmp.write_text(data, "utf-8")
            os.replace(tmp, self.file)

    def log(self, msg: str):
        line = time.strftime("%H:%M:%S ") + msg
        self.state.setdefault("log", []).append(line)
        self.state["log"] = self.state["log"][-400:]
        with open(self.dir / "run.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
        self.save()

    def summary(self) -> dict:
        st = self.state
        sc = st.get("scenes", [])
        return {"slug": self.slug, "name": st.get("name"), "stage": st.get("stage"), "scenes": len(sc),
                "done": sum(1 for s in sc if s.get("status") == "done"),
                "rendering": sum(1 for s in sc if s.get("status") == "rendering"),
                "created": st.get("created")}


_cache: dict[str, Project] = {}


def open_project(slug: str) -> Project:
    """One shared Project object per slug, so the UI and the background job never overwrite each other."""
    with _lock:
        p = _cache.get(slug)
        if p is None:
            p = Project(slug)
            if not p.state:
                raise FileNotFoundError(f"No project '{slug}'")
            _cache[slug] = p
        return p


def list_projects() -> list[dict]:
    PROJECTS.mkdir(parents=True, exist_ok=True)
    out = []
    for d in PROJECTS.iterdir():
        if (d / "state.json").exists():
            try:
                out.append((_cache.get(d.name) or Project(d.name)).summary())
            except Exception:
                pass
    return sorted(out, key=lambda x: x.get("created") or 0, reverse=True)
