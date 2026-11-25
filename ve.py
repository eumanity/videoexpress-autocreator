"""VideoExpress.ai browser driver (Playwright).
Every selector, endpoint and UI quirk lives in this one file, so a VideoExpress UI change means editing only here.
Learned on real runs (Ephesus, Joseph, Secret Disciples, Blair Mountain - Oct 2026):
  * text set by script needs a real keystroke before the TTS counter registers it
  * at most 5 renders run in parallel; extra submissions fail silently, so we wait for a free slot
  * the Video id sometimes never shows in the dialog footer; fall back to the newest library item
  * library items exist in several hidden panels; always use the visible one; 'Load more' = button.button-more
  * trimming: set the ruler, REAL-click the last clip, press split, delete the tail
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.request
from pathlib import Path

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

HELPERS_JS = r"""
window.__ve = {
 dlg(){const ta=document.getElementById('opt_prompt'); if(!ta) return null; let d=ta;
   for(let i=0;i<14;i++){d=d.parentElement; if(!d) return null; if((d.className||'').includes('bbm-modal__section_inner')) return d;} return null;},
 root(){const d=this.dlg(); return d? d.closest('.bbm-modal') : null;},
 cb(name,val){const c=this.dlg().querySelector(`input[name="${name}"]`); if(!c) return null;
   if(c.checked!==val){(c.closest('label')||c).click(); if(c.checked!==val) c.click();} return c.checked;},
 setText(id,v){const t=document.getElementById(id); Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(t,v);
   t.dispatchEvent(new Event('input',{bubbles:true})); t.dispatchEvent(new Event('change',{bubbles:true}));
   t.dispatchEvent(new KeyboardEvent('keyup',{bubbles:true})); return t.value.length;},
 setDur(n){const r=document.getElementById('opt_video_duration'); Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(r,String(n));
   r.dispatchEvent(new Event('input',{bubbles:true})); r.dispatchEvent(new Event('change',{bubbles:true}));},
 state(){const o={}; this.dlg().querySelectorAll('input[type=checkbox]').forEach(c=>o[c.name]=c.checked);
   const s=this.dlg().querySelector('select[name="select-type"]'); o.type=s?s.value:null;
   const r=document.getElementById('opt_video_duration'); o.dur=r?r.value:null; return o;},
 types(){const s=this.dlg()&&this.dlg().querySelector('select[name="select-type"]'); return s?[...s.options].map(o=>({v:o.value,t:o.text})):[];},
 setType(v){const s=this.dlg().querySelector('select[name="select-type"]'); if(!s||!v) return s?s.value:null;
   if(s.value!==v){s.value=v; s.dispatchEvent(new Event('change',{bubbles:true}));} return s.value;},
 footer(){const r=this.root(); if(!r) return null; return (r.innerText.match(/(Video: )?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g)||[]).pop()||null;},
 button(label){const d=this.dlg(); if(!d) return null; return [...d.querySelectorAll('button')].filter(b=>b.innerText.trim()===label&&b.offsetParent).pop()||null;},
 previews(){const d=this.dlg(); if(!d) return []; return [...d.querySelectorAll('img')].filter(i=>/renderplatform/.test(i.src)).map(i=>i.src);},
 loading(){const d=this.dlg(); if(!d) return false; return [...d.querySelectorAll('img')].some(i=>/loading|spinner/i.test(i.src)&&i.offsetParent);},
 errors(){const sn=[...document.querySelectorAll('.snackbar')].filter(e=>e.querySelector('.bi-exclamation-triangle')).map(e=>e.innerText.trim());
   const md=[...document.querySelectorAll('.bbm-modal--open')].filter(m=>!m.querySelector('#opt_prompt')&&/warning|error|credit|limit/i.test(m.innerText)).map(m=>m.innerText.trim().slice(0,200));
   return sn.concat(md).filter(Boolean).slice(-3);},
 snack(){return [...document.querySelectorAll('.snackbar')].map(e=>e.innerText.trim()).join(' | ');},
 selectImage(id){const d=this.dlg(); const im=[...d.querySelectorAll('img')].find(i=>i.src.includes(id)); if(!im) return 'missing';
   const slide=im.closest('.swiper-slide'); const sw=slide&&slide.closest('.swiper, .swiper-container');
   if(sw&&sw.swiper&&slide){const idx=[...slide.parentElement.children].indexOf(slide); try{sw.swiper.slideTo(idx,0);}catch(e){}}
   const item=im.closest('.swiper-slide-pair-item'); if(!(item&&item.classList.contains('selected'))) im.click(); return 'ok';},
 rows(){return [...document.querySelectorAll('.tracks-wrapper .track-row')];},
 bricks(){return this.rows().map(row=>[...row.querySelectorAll('.brick')].map(b=>({type:b.dataset.type||(b.classList.contains('video')?'video':b.classList.contains('audio')?'audio':''),
   left:parseFloat(b.style.left), width:parseFloat(b.style.width), bg:(b.querySelector('.content')||b).style.backgroundImage||''})));},
 visItem(id){return [...document.querySelectorAll(`.library-item[data-ident="${id}"]`)].find(e=>e.offsetParent)||null;},
 scroller(){let sc=this.rows()[0]; while(sc && !(sc.scrollWidth>sc.clientWidth+5 && getComputedStyle(sc).overflowX!=='visible')) sc=sc.parentElement; return sc;},
};
window.__pauses = async function(url){const buf=await (await fetch(url)).arrayBuffer(); const ac=new OfflineAudioContext(1,1,44100);
  const ab=await ac.decodeAudioData(buf); const d=ab.getChannelData(0), sr=ab.sampleRate, win=Math.round(sr*0.02); const out=[]; let start=null;
  for(let i=0;i<d.length;i+=win){let s=0; for(let j=i;j<Math.min(i+win,d.length);j++) s+=d[j]*d[j]; const rms=Math.sqrt(s/win); const t=i/sr;
    if(rms<0.01){ if(start===null) start=t;} else { if(start!==null && t-start>=0.25) out.push([+start.toFixed(2),+t.toFixed(2)]); start=null;}}
  return {dur:+(d.length/sr).toFixed(3), pauses:out};};
true;
"""

DONE = {"completed", "complete", "done", "finished", "success", "ready"}
FAILED = {"failed", "error", "cancelled", "canceled", "rejected"}


class Stop(Exception):
    """Raised when the user presses Stop."""


class VEError(RuntimeError):
    pass


def voice_name(v: str) -> str:
    """Voice setting can be a VideoExpress voice id ('en-US-AriaNeural') or a display name ('Aria')."""
    from .voices import VOICE_NAMES
    return VOICE_NAMES.get(v, v)


class VideoExpress:
    def __init__(self, settings: dict, profile_dir: Path, log, ask, should_stop):
        self.s = settings
        self.profile_dir = profile_dir
        self.log, self.ask, self.should_stop = log, ask, should_stop
        self.ctx = None
        self.page = None
        self.ready = False
        self.folders: dict[str, int] = {}
        self._pw = None
        self._dumped_item = False

    # ================================================================ session
    async def start(self):
        if self.ready and self.page and not self.page.is_closed():
            return
        if self.ctx or self._pw:
            try:
                await self.stop()
            except Exception as e:
                self.log(f"Could not close the incomplete VideoExpress session: {e}")
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        args = ["--disable-blink-features=AutomationControlled", "--start-maximized", "--mute-audio"]
        channel = self.s.get("browser_channel") or None
        try:
            self.ctx = await self._pw.chromium.launch_persistent_context(
                str(self.profile_dir), headless=False, channel=None if channel == "chromium" else channel,
                args=args, no_viewport=True, accept_downloads=True)
        except Exception as e:
            self.log(f"Could not start {channel} ({str(e)[:120]}); using built-in Chromium.")
            self.ctx = await self._pw.chromium.launch_persistent_context(
                str(self.profile_dir), headless=False, args=args, no_viewport=True, accept_downloads=True)
        await self.ctx.add_init_script(HELPERS_JS)
        self.page = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
        for attempt in range(3):
            try:
                await self.page.goto(self.s["ve_url"], wait_until="domcontentloaded")
                break
            except Exception as e:
                if attempt == 2:
                    raise VEError(f"Could not load VideoExpress after 3 attempts: {e}") from e
                self.log(
                    f"VideoExpress navigation was interrupted (attempt {attempt + 1}/3); retrying."
                )
                await asyncio.sleep(attempt + 1)
        await self.ensure_editor()
        self.ready = True

    async def ensure_editor(self):
        for attempt in range(3):
            try:
                await self.page.wait_for_selector("text=Export Video", timeout=25000)
                break
            except Exception:
                await self.ask("Please log in to VideoExpress in the browser window that just opened "
                               "(the app never types your password). When you can see the video editor, click Continue.")
        else:
            raise VEError("The VideoExpress editor did not load.")
        await self.page.evaluate(HELPERS_JS)
        cats = await self.api("/library/get_categories/4")
        data = cats.get("data", cats) if isinstance(cats, dict) else cats
        self.folders = {c["name"]: c["id"] for c in data}
        self.log("Connected to VideoExpress.")

    async def stop(self):
        try:
            if self.ctx:
                await self.ctx.close()
        finally:
            if self._pw:
                await self._pw.stop()
            self.ctx = self.page = self._pw = None
            self.ready = False

    async def check_stop(self):
        if self.should_stop():
            raise Stop()

    async def sleep(self, s: float):
        end = time.time() + s
        while time.time() < end:
            await self.check_stop()
            await asyncio.sleep(min(1.0, end - time.time()))

    async def api(self, path: str):
        return await self.page.evaluate(
            "async p => { const r = await fetch(p, {credentials:'include'}); const t = await r.text();"
            " try { return JSON.parse(t); } catch(e) { return {__raw: t.slice(0,300), __status: r.status}; } }", path)

    async def media(self, folder: str, limit: int = 40) -> list[dict]:
        cid = self.folders.get(folder)
        if cid is None:
            raise VEError(f"Library folder '{folder}' not found. Folders: {list(self.folders)}")
        j = await self.api(f"/api/library/get_media/4?categoryId={cid}&page=1&start=0&limit={limit}"
                           f"&query=&orderBy=id&orderDir=desc&filter=")
        return j.get("results", []) if isinstance(j, dict) else []

    async def in_flight(self) -> int:
        q = await self.api("/user_queue")
        try:
            return int(q.get("total") or 0)
        except Exception:
            return 0

    RAIL = {"Media Library": "a.button-open-panel-medialibrary", "Create with AI": "a.button-generate-video",
            "Import Media": "a.button-open-panel-import", "Text Animations": "a.button-open-panel-animation"}

    async def rail(self, label: str, force: bool = False):
        """Open a right-rail panel. Rail links TOGGLE, so an already-open panel is left alone unless force=True."""
        res = await self.page.evaluate("""([sel, l, force]) => {
            let a=[...document.querySelectorAll(sel)].find(e=>e.offsetParent);
            if(!a) a=[...document.querySelectorAll('a')].find(e=>e.offsetParent && e.innerText.replace(/\\s+/g,' ').trim().startsWith(l));
            if(!a) return 'missing';
            if(a.classList.contains('active') && !force) return 'open';
            a.click(); return 'clicked'; }""", [self.RAIL.get(label, "a.__none"), label, force])
        if res == "missing":
            raise VEError(f"Side menu item '{label}' not found")
        if res == "clicked":
            await asyncio.sleep(1.5)

    async def screenshot(self, path: Path):
        try:
            await self.page.screenshot(path=str(path))
        except Exception:
            pass

    # ============================================================ narration
    async def open_tts(self):
        if await self.page.evaluate("!!document.querySelector('.drop-panel-wrapper textarea[name=text]')?.offsetParent"):
            return
        await self.rail("Import Media")
        ok = await self.page.evaluate("""(() => { const p=document.querySelector('.drop-panel-wrapper'); if(!p) return false;
            const row=[...p.querySelectorAll('.d-flex')].find(r=>r.innerText.trim()==='Text to Speech');
            const b=row&&row.querySelector('button'); if(!b) return false; b.click(); return true; })()""")
        if not ok:
            raise VEError("Text to Speech option not found in the Import Media panel")
        await self.page.wait_for_selector(".drop-panel-wrapper textarea[name=text]", state="visible", timeout=15000)

    async def set_voice(self, voice: str) -> str:
        """Pick the narrator by name (e.g. 'Andrew Multilingual'); returns the voice now selected."""
        await self.open_tts()
        return await self.page.evaluate("""async v => { const p=document.querySelector('.drop-panel-wrapper');
            const tg=p.querySelector('.dropdown-toggle'); const nm=()=>tg?tg.innerText.split('\\n').map(x=>x.trim()).filter(Boolean).pop()||'':'';
            if(!v) return nm();
            const all=[...p.querySelectorAll('.dropdown-menu a[data-name]')];
            const a=all.find(x=>x.dataset.value===v)||all.find(x=>x.dataset.name.toLowerCase()===v.toLowerCase());
            if(a && tg && nm()!==a.dataset.name){ a.click(); await new Promise(r=>setTimeout(r,700)); }
            return nm(); }""", voice or "")

    async def tts(self, text: str, voice: str = "") -> dict:
        """One narration part with the built-in Text to Speech."""
        before = {m["id"] for m in await self.media("my_ai_audio", 8)}
        await self.open_tts()
        if voice:
            got = await self.set_voice(voice)
            want = voice_name(voice)
            if got.lower() != want.lower():
                raise VEError(f"Voice '{voice}' not found in Text to Speech (selected: '{got}'). Check the voice name in Settings.")
        box = self.page.locator(".drop-panel-wrapper textarea[name=text]").first
        await box.fill(text)
        await box.press("End")
        await box.type(" ")
        await box.press("Backspace")
        await asyncio.sleep(0.5)
        counter = await self.page.evaluate("(document.querySelector('.drop-panel-wrapper .text-count')||{}).innerText||''")
        if counter.strip().startswith("0 /"):
            raise VEError("Text to Speech did not register the text")
        await self.page.locator(".drop-panel-wrapper button.button-import").first.click()
        t0 = time.time()
        while time.time() - t0 < 300:
            await self.sleep(4)
            new = [m for m in await self.media("my_ai_audio", 8) if m["id"] not in before]
            if new and new[0].get("duration") and new[0].get("mediaPath"):
                m = new[0]
                return {"id": m["id"], "dur_ms": m["duration"], "url": m["mediaPath"], "name": m.get("name", "")}
        raise VEError("Text to Speech did not finish within 5 minutes")

    async def measure(self, url: str) -> dict:
        return await self.page.evaluate("u => __pauses(u)", url)

    # ======================================================== create dialog
    async def dialog(self):
        if await self.page.evaluate("""(() => {
            const d=window.__ve&&__ve.dlg(), m=d&&d.closest('.bbm-modal');
            return !!(m&&m.classList.contains('bbm-modal--open')&&m.offsetParent);
        })()"""):
            return
        await self.rail("Create with AI")
        button = self.page.locator(".button-generate-from-prompt:visible")
        if await button.count() == 0:
            raise VEError("'Create Video From Prompt' button not found")
        await button.last.click(timeout=10000)
        await self.page.wait_for_selector("#opt_prompt", state="visible", timeout=20000)
        await asyncio.sleep(0.8)
        aspect = "Vertical" if self.s.get("aspect") == "vertical" else "Landscape"
        await self.page.evaluate("""a => { const d=__ve.dlg(); const b=[...d.querySelectorAll('button')].find(x=>x.innerText.trim().startsWith(a));
            if(b && !b.classList.contains('active')) b.click(); }""", aspect)
        await self.page.evaluate(HELPERS_JS)
        await asyncio.sleep(1)

    async def diagnose(self, voice: str, image_type: str) -> list[tuple[bool, str]]:
        """Free self-test: checks every screen the app uses without generating anything."""
        out: list[tuple[bool, str]] = []

        async def check(label, coro):
            try:
                res = await coro
                out.append((True, f"{label}{': ' + str(res) if res not in (None, True) else ''}"))
            except Exception as e:  # noqa: BLE001
                out.append((False, f"{label}: {str(e)[:160]}"))

        await check("Logged in, library folders found", self._folders_ok())
        await check("Render queue readable", self.in_flight())
        await check("Text to Speech panel", self.open_tts())
        if voice:
            got = await self.set_voice(voice)
            out.append((got.lower() == voice.lower(), f"Narrator voice '{voice}'" + ("" if got.lower() == voice.lower() else f" not found (selected: {got})")))
        await check("Create Video From Prompt dialog", self.dialog())
        types = [t["v"] for t in await self.page.evaluate("__ve.types()")]
        out.append((image_type in types, f"Image type '{image_type}' (available: {', '.join(types)})"))
        btns = await self.page.evaluate("[!!__ve.button('Create Image'), !!__ve.button('Create Video'), !!document.getElementById('opt_video_duration')]")
        out.append((all(btns), "Create Image / Create Video / clip length controls"))
        dl = await self.page.evaluate("""[!!__ve.dlg().querySelector('button.button-image-from-library'), !!__ve.dlg().querySelector('input[name=narration_video]'),
            !!__ve.dlg().querySelector('button.button-generate-talking-video')]""")
        out.append((all(dl), "Dialogue tools: start from a saved frame, lip-sync to my audio"))
        await self.close_dialog()
        await check("AI Sound Maker (for sound effects)", self.open_sound_maker())
        await self.close_sound_maker()
        tl = await self.page.evaluate("[document.querySelectorAll('.tracks-wrapper .track-row').length, !!document.querySelector('button.timeline-button-split'), !!document.querySelector('.timeline-header .ruler.ui-slider'), typeof $]")
        out.append((tl[0] >= 2 and tl[1] and tl[2] and tl[3] == "function", "Timeline, ruler and split tool"))
        menu = await self.page.evaluate("[!!document.querySelector('a.button-new-project'), !!document.querySelector('a.button-save-project-as'), !!document.querySelector('button.button-render-project')]")
        out.append((all(menu), "New / Save Project As / Export Video"))
        return out

    async def _folders_ok(self):
        need = {"my_ai_audio", "my_ai_images", "my_ai_videos"}
        missing = need - set(self.folders)
        if missing:
            raise VEError(f"missing {missing}")
        return None

    # ===================================================== AI Sound Maker
    async def open_sound_maker(self):
        if await self.page.evaluate("!!document.querySelector('#formCreateSound')?.offsetParent"):
            return
        await self.close_dialog()
        await self.rail("Create with AI")
        ok = await self.page.evaluate("""(() => { const b=[...document.querySelectorAll('.button-create-sound')].find(e=>e.offsetParent);
            if(b){b.click(); return true;} return false; })()""")
        if not ok:
            raise VEError("AI Sound Maker not found under Create with AI")
        await self.page.wait_for_selector("#formCreateSound", state="visible", timeout=15000)
        await asyncio.sleep(0.8)

    async def close_sound_maker(self):
        await self.page.evaluate("""(() => { const f=document.querySelector('#formCreateSound'); if(!f) return;
            const m=f.closest('.bbm-modal'); const b=[...m.querySelectorAll('button.button-close')].find(x=>x.offsetParent); if(b) b.click(); })()""")
        await asyncio.sleep(0.8)

    async def make_sound(self, prompt: str, seconds: int, timeout_s: int = 180) -> dict:
        """Generate one sound effect (2-7 s) with VideoExpress' AI Sound Maker and save it to My AI Audio."""
        seconds = max(2, min(7, int(round(seconds))))
        await self.open_sound_maker()
        before = {m["id"] for m in await self.media("my_ai_audio", 10)}
        st = await self.page.evaluate("""([p, n]) => { const f=document.querySelector('#formCreateSound'); const m=f.closest('.bbm-modal');
            const ta=f.querySelector('textarea[name=prompt]');
            Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(ta,p);
            ta.dispatchEvent(new Event('input',{bubbles:true})); ta.dispatchEvent(new Event('change',{bubbles:true}));
            const r=f.querySelector('input[name=audio_duration]');
            Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(r,String(n));
            r.dispatchEvent(new Event('input',{bubbles:true})); r.dispatchEvent(new Event('change',{bubbles:true}));
            const al=m.querySelector('.alerts-bottom'); if(al) al.innerHTML='';
            const b=[...m.querySelectorAll('button.button-submit-create-sound')].find(x=>x.offsetParent); if(!b) return 'nobutton';
            b.click(); return ta.value.length+'/'+r.value; }""", [prompt[:500], seconds])
        if st == "nobutton":
            raise VEError("'Create Sound' button not found")
        t0 = time.time()
        ready = False
        while time.time() - t0 < timeout_s:
            await self.sleep(2)
            r = await self.page.evaluate("""(() => { const m=document.querySelector('#formCreateSound').closest('.bbm-modal');
                const err=m.querySelector('.alerts-bottom .alert-danger, .alerts-bottom .alert-error');
                const save=[...m.querySelectorAll('button.button-submit-save-sound')].find(x=>x.offsetParent);
                return {err: err?err.innerText.trim():'', save: !!save}; })()""")
            if r["err"]:
                raise VEError("Sound Maker: " + r["err"])
            if r["save"]:
                ready = True
                break
        if not ready:
            raise VEError("Sound Maker did not finish in time")
        await self.page.evaluate("""(() => { const m=document.querySelector('#formCreateSound').closest('.bbm-modal');
            [...m.querySelectorAll('button.button-submit-save-sound')].find(x=>x.offsetParent).click(); })()""")
        t0 = time.time()
        while time.time() - t0 < 60:
            await self.sleep(2)
            new = [m for m in await self.media("my_ai_audio", 10) if m["id"] not in before and m.get("mediaPath")]
            if new:
                m = new[0]
                return {"id": m["id"], "url": m["mediaPath"], "dur_ms": m.get("duration")}
        raise VEError("The sound was made but did not appear in My AI Audio")

    async def image_types(self) -> list[dict]:
        await self.dialog()
        return await self.page.evaluate("__ve.types()")

    async def _click_button(self, name: str):
        dialog = self.page.locator(".bbm-modal--open")
        button = dialog.locator("button:visible").filter(has_text=name)
        if await button.count() == 0:
            raise VEError(f"VideoExpress' '{name}' button was not found.")
        if not await button.is_enabled():
            raise VEError(f"VideoExpress' '{name}' button is disabled.")
        await button.click(timeout=10000)

    async def keyframe(self, prompt: str, image_type: str, timeout_s: int = 240) -> str:
        """Generate one keyframe in the Create dialog and return its image UUID."""
        await self.dialog()
        known = set(await self.page.evaluate("__ve.previews()"))
        st = await self.page.evaluate("""([t,p]) => { __ve.setType(t); __ve.cb('shared',false); __ve.cb('image_creative_mode',true);
            __ve.cb('auto_enhance_prompt',false); __ve.cb('use_consistent_character',false);
            __ve.setText('opt_prompt',p); return __ve.state(); }""", [image_type, prompt])
        if st.get("shared"):
            raise VEError("Could not switch off public sharing - stopping for safety")
        await self._click_button("Create Image")
        self.log("Image request submitted to VideoExpress; waiting for its preview…")
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            await self.sleep(3)
            fresh = [u for u in await self.page.evaluate("__ve.previews()") if u not in known]
            if fresh and not await self.page.evaluate("__ve.loading()"):
                m = re.search(UUID, fresh[0])
                if m:
                    await asyncio.sleep(1)
                    return m.group(0)
            errs = await self.page.evaluate("__ve.errors()")
            if errs and any(re.search(r"error|fail|insufficient|credit|limit", e, re.I) for e in errs):
                raise VEError("VideoExpress: " + " | ".join(errs))
        raise VEError(f"VideoExpress did not show a new image preview within {timeout_s} seconds. "
                      "Check the VideoExpress window for an account, service, or generation error, then retry.")

    def preview_url(self, image_uuid: str) -> str:
        return f"https://user-assets.renderplatform.com/preview/{image_uuid}.jpg"

    async def pending_videos(self) -> int:
        """Clips VideoExpress is still rendering (counts every AI video, also ones started outside the app)."""
        n = 0
        for it in await self.media("my_ai_videos", 30):
            st = str(it.get("status") or "").lower()
            if it.get("isPending") or (st and st not in DONE and st not in FAILED):
                n += 1
        return n

    async def list_voices(self) -> list[list[str]]:
        await self.open_tts()
        return await self.page.evaluate("""(() => { const p=document.querySelector('.drop-panel-wrapper');
            const m=new Map(); p.querySelectorAll('.dropdown-menu a[data-name]').forEach(a=>{ if(!m.has(a.dataset.value)) m.set(a.dataset.value, a.dataset.name); });
            return [...m.entries()]; })()""")

    # ------------------------------------------------- continuity (last frame)
    async def last_frame(self, video_lib_id: int, duration_ms: float, name: str) -> dict:
        """Save the last frame of a rendered clip into My AI Images (VideoExpress' own 'save frame' tool)."""
        r = await self.page.evaluate("""async ([id, ms, name]) => { const r = await fetch('/library/extract_last_frame/'+id, {method:'POST',
            headers:{'X-Requested-With':'XMLHttpRequest','Content-Type':'application/x-www-form-urlencoded; charset=UTF-8'},
            body:new URLSearchParams({name:name, frame_time_ms:String(Math.max(0,Math.round(ms)))})}); const t=await r.text();
            try { return JSON.parse(t); } catch(e) { return {success:false, raw:t.slice(0,200)}; } }""",
                                     [video_lib_id, max(0, float(duration_ms or 0) - 80), name[:60]])
        if not r.get("success") or not r.get("id"):
            raise VEError(f"Could not save the last frame of clip {video_lib_id}: {str(r)[:200]}")
        for _ in range(10):
            it = next((m for m in await self.media("my_ai_images", 10) if m["id"] == r["id"]), None)
            if it:
                return {"id": it["id"], "fileName": it.get("fileName", ""), "url": it.get("mediaPath") or it.get("imagePath")}
            await asyncio.sleep(1.5)
        raise VEError("The saved last frame did not appear in My AI Images")

    async def _picker(self, folder: str, lib_id: int):
        """Drive VideoExpress' 'Select image/audio' library picker (the newest modal on screen)."""
        fid = self.folders[folder]
        ok = await self.page.evaluate("""async ([fid, id]) => {
            const vis=e=>!!e.offsetParent; const sleep=ms=>new Promise(r=>setTimeout(r,ms));
            let pk=null; for(let i=0;i<20 && !pk;i++){ await sleep(400);
              pk=[...document.querySelectorAll('.bbm-modal--open')].filter(m=>m.querySelector('.library-folder') && /Select (image|audio)/i.test(m.innerText)).pop(); }
            if(!pk) return 'nopicker';
            const f=[...pk.querySelectorAll(`.library-folder[data-id="${fid}"]`)].find(vis); if(!f) return 'nofolder'; f.click();
            let it=null; for(let i=0;i<25 && !it;i++){ await sleep(500); it=[...pk.querySelectorAll(`.library-item[data-ident="${id}"]`)].find(vis);
              if(!it && i%5===4){ const more=[...pk.querySelectorAll('button.button-more')].find(vis); if(more) more.click(); } }
            if(!it) return 'noitem'; it.click(); await sleep(500);
            const ch=[...pk.querySelectorAll('button')].find(b=>vis(b)&&b.innerText.trim()==='Choose'); if(!ch) return 'nochoose'; ch.click();
            return 'ok'; }""", [fid, lib_id])
        if ok != "ok":
            raise VEError(f"Library picker problem: {ok}")
        await asyncio.sleep(2)

    async def use_library_image(self, image_lib_id: int, file_name: str):
        """Put a library image (e.g. a saved last frame) into the Create dialog as the start frame."""
        await self.dialog()
        await self.page.evaluate("""(() => { const b=[...__ve.dlg().querySelectorAll('button.button-image-from-library')].find(e=>e.offsetParent); b&&b.click(); })()""")
        await self._picker("my_ai_images", image_lib_id)
        for _ in range(10):
            src = await self.page.evaluate("(() => { const a=__ve.dlg().querySelector('.swiper-slide-active img'); return a?a.src:''; })()")
            if file_name and file_name in src:
                return
            await asyncio.sleep(1)
        raise VEError("The last frame did not become the start image")

    async def _select_source(self, source: dict):
        await self.dialog()
        if source.get("lib_file"):
            src = await self.page.evaluate("(() => { const a=__ve.dlg().querySelector('.swiper-slide-active img'); return a?a.src:''; })()")
            if source["lib_file"] not in src:
                await self.use_library_image(source["lib_id"], source["lib_file"])
            return
        uuid = source["uuid"]
        if await self.page.evaluate("id => __ve.selectImage(id)", uuid) == "missing":
            raise VEError("KEYFRAME_MISSING")
        await asyncio.sleep(0.8)
        footer = await self.page.evaluate("__ve.footer()")
        if not footer or uuid not in footer:
            await self.page.evaluate("id => __ve.selectImage(id)", uuid)
            await asyncio.sleep(1.2)
            footer = await self.page.evaluate("__ve.footer()")
            if not footer or uuid not in footer:
                raise VEError(f"Wrong source image selected ({footer})")

    async def _video_id(self, before: int, seconds: int | None = None, previous_footer: str = "") -> dict:
        deadline = time.monotonic() + 120
        last_media_check = 0.0
        old_errors = set(await self.page.evaluate("__ve.errors()"))
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            footer = await self.page.evaluate("__ve.footer()")
            if footer and footer.startswith("Video: ") and footer != previous_footer:
                video_id = footer[7:].strip()
                if re.fullmatch(UUID, video_id):
                    return {"uuid": video_id}
            if time.monotonic() - last_media_check >= 6:
                last_media_check = time.monotonic()
                new = [m for m in await self.media("my_ai_videos", 5) if m["id"] > before]
                if seconds:
                    new = [m for m in new if str(m.get("name", "")).startswith(
                        f"Total duration {int(seconds)}s")] or new
                if new:
                    return {"lib": new[-1]["id"]}
            errors = await self.page.evaluate("__ve.errors()")
            fresh_errors = [e for e in errors if e not in old_errors]
            if fresh_errors and any(re.search(r"error|fail|insufficient|credit|limit", e, re.I)
                                    for e in fresh_errors):
                raise VEError("VideoExpress: " + " | ".join(fresh_errors))
        raise VEError("VideoExpress did not confirm the new clip within 120 seconds. "
                      "Check its editor for an account, service, or generation error, then retry.")

    async def lipsync_clip(self, source: dict, prompt: str, audio_lib_id: int) -> dict:
        """Talking clip: the start frame is animated and lip-synced to OUR audio (a character's TTS line)."""
        await self._select_source(source)
        await self.page.evaluate("""async (p) => { __ve.setText('opt_video_prompt', p); __ve.cb('shared',false); __ve.cb('talking_video',false);
            __ve.cb('narration_video',true); await new Promise(r=>setTimeout(r,500)); }""", prompt)
        st = await self.page.evaluate("__ve.state()")
        if st.get("shared") or not st.get("narration_video"):
            raise VEError(f"Lip-sync settings did not stick: {st}")
        top = await self.media("my_ai_videos", 3)
        before = top[0]["id"] if top else 0
        previous_footer = await self.page.evaluate("__ve.footer()")
        button = self.page.locator("button.button-generate-talking-video:visible")
        if await button.count() == 0:
            raise VEError("Lip-sync 'Create Video' button not found")
        await button.click(timeout=10000)
        # audio modal → Import Audio tab → select our line → Create Narration Video
        r = await self.page.evaluate("""async () => { const vis=e=>!!e.offsetParent; const sleep=ms=>new Promise(r=>setTimeout(r,ms));
            let m=null; for(let i=0;i<20 && !m;i++){ await sleep(400); m=[...document.querySelectorAll('.bbm-modal--open')].find(x=>x.querySelector('#tab-third')); }
            if(!m) return 'nomodal';
            const a=m.querySelector('a[href="#tab-third"]'); a&&a.click(); await sleep(800);
            const b=m.querySelector('#tab-third .button-select-file'); if(!b) return 'noselect'; b.click(); return 'ok'; }""")
        if r != "ok":
            raise VEError(f"Lip-sync audio window problem: {r}")
        await self._picker("my_ai_audio", audio_lib_id)
        r = await self.page.evaluate("""async () => { const vis=e=>!!e.offsetParent; const sleep=ms=>new Promise(r=>setTimeout(r,ms));
            const m=[...document.querySelectorAll('.bbm-modal--open')].find(x=>x.querySelector('#tab-third')); if(!m) return 'gone';
            const b=[...m.querySelectorAll('button.button-submit')].find(vis); if(!b) return 'nosubmit'; b.click(); await sleep(2500);
            const still=[...document.querySelectorAll('.bbm-modal--open')].find(x=>x.querySelector('#tab-third'));
            if(still){ const err=(still.querySelector('.alert-danger, .text-danger, .error')||{}).innerText||''; return 'err:'+err.trim().slice(0,200); }
            return 'ok'; }""")
        if r != "ok":
            await self.page.evaluate("""(() => { const m=[...document.querySelectorAll('.bbm-modal--open')].find(x=>x.querySelector('#tab-third'));
                if(m){ const c=[...m.querySelectorAll('button.button-close')].find(e=>e.offsetParent); c&&c.click(); } })()""")
            raise VEError(f"Lip-sync clip was not accepted: {r}")
        return await self._video_id(before, previous_footer=previous_footer)

    async def clip(self, source, prompt: str, seconds: int, with_sound: bool = False) -> dict:
        """Animate the start frame; returns {'uuid':..} or {'lib':id}. with_sound=True lets the video model
        create its own audio (used for AI-voiced dialogue); otherwise the clip is silent."""
        if isinstance(source, str):
            source = {"uuid": source}
        await self._select_source(source)
        st = await self.page.evaluate("""async ([p,n,vo]) => {
            __ve.setText('opt_video_prompt', p); __ve.cb('shared',false); __ve.cb('talking_video',false);
            __ve.cb('narration_video',false); __ve.cb('advanced_mode',true); await new Promise(r=>setTimeout(r,300));
            __ve.cb('enhance_video_prompt',false); __ve.cb('video_only',vo); __ve.cb('manual_video_length',true);
            await new Promise(r=>setTimeout(r,300)); __ve.setDur(n); return __ve.state(); }""", [prompt, int(seconds), not with_sound])
        if st.get("shared") or st.get("enhance_video_prompt") or str(st.get("dur")) != str(int(seconds)):
            raise VEError(f"Video settings did not stick: {st}")
        top = await self.media("my_ai_videos", 3)
        before = top[0]["id"] if top else 0
        previous_footer = await self.page.evaluate("__ve.footer()")
        await self._click_button("Create Video")
        self.log("Clip request submitted to VideoExpress; waiting for its video ID…")
        return await self._video_id(before, seconds, previous_footer)

    async def close_dialog(self):
        await self.page.evaluate("""(() => { const r=__ve.root(); if(!r) return;
            const b=[...r.querySelectorAll('button')].find(b=>b.innerText.trim()==='Close'); if(b) b.click(); })()""")

    # ============================================================== polling
    def _match(self, item: dict, ref: dict) -> bool:
        if ref.get("lib"):
            return item.get("id") == ref["lib"]
        return bool(ref.get("uuid")) and ref["uuid"] in json.dumps(item)

    async def video_status(self, refs: dict[int, dict]) -> dict[int, dict]:
        """refs: scene n -> {'uuid'| 'lib'}. Returns n -> {'state': done|failed|pending, 'item': ...}."""
        items = await self.media("my_ai_videos", max(60, len(refs) + 40))
        if items and not self._dumped_item:
            self._dumped_item = True
            self.log("Library item fields: " + ", ".join(sorted(items[0].keys())))
        out = {}
        for n, ref in refs.items():
            it = next((m for m in items if self._match(m, ref)), None)
            if not it:
                out[n] = {"state": "pending", "item": None}
                continue
            st = str(it.get("status", "")).lower()
            media = str(it.get("mediaPath") or it.get("url") or "")
            if st in FAILED:
                out[n] = {"state": "failed", "item": it}
            elif st in DONE or (not st and media.endswith(".mp4") and it.get("duration")):
                out[n] = {"state": "done", "item": it}
            else:
                out[n] = {"state": "pending", "item": it}
        return out

    # ============================================================= timeline
    async def timeline(self) -> list[list[dict]]:
        return await self.page.evaluate("__ve.bricks()")

    async def open_folder(self, folder: str):
        try:
            await self.rail("Text Animations")
        except VEError:
            pass
        await self.rail("Media Library")
        ok = await self.page.evaluate("""id => { const f=[...document.querySelectorAll(`.library-folder[data-id="${id}"]`)].find(e=>e.offsetParent);
            if(f){f.click(); return true;} return false; }""", self.folders[folder])
        if not ok:
            raise VEError(f"Media Library folder '{folder}' not visible")
        await asyncio.sleep(2.5)

    async def reveal(self, lib_ids: list[int], rounds: int = 20) -> list[int]:
        """Click 'load more' until every id has a visible library tile. Returns ids still missing."""
        missing = lib_ids
        for _ in range(rounds):
            missing = await self.page.evaluate("ids => ids.filter(id => !__ve.visItem(id))", lib_ids)
            if not missing:
                return []
            more = await self.page.evaluate("""(() => { const m=[...document.querySelectorAll('button.button-more')].find(e=>e.offsetParent);
                if(m){m.click(); return true;} return false; })()""")
            if not more:
                break
            await asyncio.sleep(2.5)
        return missing

    async def add_to_timeline(self, lib_id: int):
        await self.page.evaluate("""async id => {
            const el = __ve.visItem(id); el.scrollIntoView({block:'center'}); await new Promise(r=>setTimeout(r,300));
            const r = el.getBoundingClientRect();
            el.dispatchEvent(new MouseEvent('contextmenu', {bubbles:true, cancelable:true, clientX:r.x+r.width/2, clientY:r.y+r.height/2, button:2}));
            await new Promise(r=>setTimeout(r,600));
            const cand=[...document.querySelectorAll('a, li, button')].filter(e=>e.offsetParent && /^\\s*Add to Timeline\\s*$/i.test(e.innerText));
            const a=cand.find(e=>e.tagName==='A')||cand[0];
            if(!a) throw new Error('Add to Timeline menu not found'); a.click(); }""", lib_id)

    async def add_clips(self, items: list[dict], progress=None):
        await self.open_folder("my_ai_videos")
        missing = await self.reveal([i["id"] for i in items])
        if missing:
            raise VEError(f"{len(missing)} clips not found in the Media Library: {missing[:8]}")
        for k, it in enumerate(items):
            await self.check_stop()
            before = len((await self.timeline())[0])
            await self.add_to_timeline(it["id"])
            rows = []
            for _ in range(30):
                await asyncio.sleep(0.4)
                rows = await self.timeline()
                if len(rows[0]) == before + 1:
                    break
            else:
                raise VEError(f"Clip {k + 1} was not added to the timeline")
            fn = it.get("fileName") or ""
            last = max(rows[0], key=lambda b: b["left"])
            if fn and fn not in last["bg"] and Path(fn).stem not in last["bg"]:
                self.log(f"Note: could not confirm thumbnail of clip {k + 1} (continuing)")
            if progress:
                progress(k + 1, len(items))

    async def drop_audio(self, lib_id: int, target_px: float):
        await self.page.evaluate("""async ([id, targetPx]) => {
          const rows=__ve.rows(); const b0=rows[0].querySelector('.brick'); const sc=__ve.scroller(); if(sc) sc.scrollLeft=Math.max(0,targetPx-200);
          await new Promise(r=>setTimeout(r,400));
          const originX=b0.getBoundingClientRect().left-parseFloat(b0.style.left);
          const el=__ve.visItem(id); el.scrollIntoView({block:'center'}); await new Promise(r=>setTimeout(r,300));
          const er=el.getBoundingClientRect(); const rr=rows[1].getBoundingClientRect();
          const sx=er.left+5, sy=er.top+er.height/2, tx=originX+targetPx+5, ty=rr.top+rr.height/2;
          const f=(t,x,y,tg)=>(tg||document).dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,clientX:x,clientY:y,button:0,buttons:t==='mouseup'?0:1,view:window}));
          f('mousedown',sx,sy,el); for(let i=1;i<=20;i++){f('mousemove',sx+(tx-sx)*i/20,sy+(ty-sy)*i/20); await new Promise(r=>setTimeout(r,30));}
          f('mouseup',tx,ty); }""", [lib_id, target_px])
        await asyncio.sleep(2)

    async def nudge_last_audio(self, target_px: float):
        await self.page.evaluate("""async target => {
          const rows=__ve.rows(); const sc=__ve.scroller();
          const ab=[...rows[1].querySelectorAll('.brick')].sort((a,b)=>parseFloat(b.style.left)-parseFloat(a.style.left))[0];
          const dx=target-parseFloat(ab.style.left); if(Math.abs(dx)<1) return;
          if(sc) sc.scrollLeft=Math.max(0, parseFloat(ab.style.left)-150); await new Promise(r=>setTimeout(r,400));
          const br=ab.getBoundingClientRect(); const sx=br.left+20, sy=br.top+br.height/2;
          const f=(t,x,y,tg)=>(tg||document).dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,clientX:x,clientY:y,button:0,buttons:t==='mouseup'?0:1,view:window}));
          f('mousedown',sx,sy,ab.querySelector('.content')||ab); for(let i=1;i<=15;i++){f('mousemove',sx+dx*i/15,sy); await new Promise(r=>setTimeout(r,30));}
          f('mouseup',sx+dx,sy); }""", target_px)
        await asyncio.sleep(1.5)

    async def place_narration(self, audio_ids: list[int]) -> float:
        await self.open_folder("my_ai_audio")
        missing = await self.reveal(audio_ids)
        if missing:
            raise VEError(f"Narration not found in the Media Library: {missing}")
        x = 0.0
        for k, aid in enumerate(audio_ids):
            await self.check_stop()
            n_before = len((await self.timeline())[1]) if len(await self.timeline()) > 1 else 0
            await self.drop_audio(aid, x)
            rows = await self.timeline()
            if len(rows) < 2 or len(rows[1]) != n_before + 1:
                raise VEError(f"Narration part {k + 1} did not land on track 2")
            for _ in range(3):
                last = max(rows[1], key=lambda b: b["left"])
                if abs(last["left"] - x) < 1:
                    break
                await self.nudge_last_audio(x)
                rows = await self.timeline()
            last = max(rows[1], key=lambda b: b["left"])
            if abs(last["left"] - x) >= 1:
                raise VEError(f"Narration part {k + 1} sits at {last['left']}px, expected {x}px")
            x = last["left"] + last["width"]
        return x

    async def place_audio_at(self, items: list[tuple[int, float]]):
        """Put audio clips on track 2 at exact positions (px). Used for narrator lines in dialogue movies."""
        if not items:
            return
        await self.open_folder("my_ai_audio")
        missing = await self.reveal([i for i, _ in items])
        if missing:
            raise VEError(f"Narration not found in the Media Library: {missing}")
        for k, (aid, x) in enumerate(items):
            await self.check_stop()
            rows = await self.timeline()
            n_before = len(rows[1]) if len(rows) > 1 else 0
            await self.drop_audio(aid, x)
            rows = await self.timeline()
            if len(rows) < 2 or len(rows[1]) != n_before + 1:
                raise VEError(f"Narrator line {k + 1} did not land on track 2")
            for _ in range(3):
                newest = max(rows[1], key=lambda b: b["left"]) if x >= max(b["left"] for b in rows[1]) else \
                    min(rows[1], key=lambda b: abs(b["left"] - x))
                if abs(newest["left"] - x) < 1:
                    break
                await self.nudge_last_audio(x)
                rows = await self.timeline()

    async def trim_video(self, end_px: float):
        rows = await self.timeline()
        vids = sorted(rows[0], key=lambda b: b["left"])
        if vids[-1]["left"] + vids[-1]["width"] <= end_px + 0.5:
            return
        await self.page.evaluate("end => $('.timeline-header .ruler.ui-slider').slider('value', end)", end_px)
        await asyncio.sleep(0.8)
        box = await self.page.evaluate("""async () => { const sc=__ve.scroller(); const row=__ve.rows()[0];
            const b=[...row.querySelectorAll('.brick')].sort((a,b)=>parseFloat(a.style.left)-parseFloat(b.style.left)).pop();
            if(sc) sc.scrollLeft=Math.max(0, parseFloat(b.style.left)-100); await new Promise(r=>setTimeout(r,500));
            const r=b.getBoundingClientRect(); return {x:r.left+Math.min(30,r.width/3), y:r.top+r.height/2}; }""")
        await self.page.mouse.click(box["x"], box["y"])          # a REAL click selects the clip
        await asyncio.sleep(0.6)
        await self.page.evaluate("document.querySelector('button.timeline-button-split').click()")
        await asyncio.sleep(1.5)
        await self.page.evaluate("""end => { const row=__ve.rows()[0];
            const tail=[...row.querySelectorAll('.brick')].find(b=>Math.abs(parseFloat(b.style.left)-end)<1.5);
            if(tail) $(tail).trigger('ctxmenu:delete'); }""", end_px)
        await asyncio.sleep(1.5)
        rows = await self.timeline()
        v = sorted(rows[0], key=lambda b: b["left"])
        v_end = v[-1]["left"] + v[-1]["width"]
        if abs(v_end - end_px) > 1.5:
            raise VEError(f"Trim failed: video ends at {v_end}px, narration at {end_px}px")

    async def check_gaps(self) -> list[int]:
        v = sorted((await self.timeline())[0], key=lambda b: b["left"])
        return [i for i in range(1, len(v)) if abs(v[i]["left"] - (v[i - 1]["left"] + v[i - 1]["width"])) > 0.6]

    # ======================================================== save & export
    async def new_project(self) -> bool:
        """Start a blank project (VideoExpress 'New'). Returns False if VideoExpress asks about unsaved work."""
        await self.page.evaluate("(() => { const a=[...document.querySelectorAll('a.button-new-project')].find(e=>e.offsetParent)||document.querySelector('a.button-new-project'); a&&a.click(); })()")
        await asyncio.sleep(2)
        rows = await self.timeline()
        return not any(rows)

    async def ensure_blank(self) -> str | None:
        """Make the editor timeline empty without asking anyone and without losing work:
        if VideoExpress says the open project has unsaved changes, a safety copy is saved first.
        Returns the name of the safety copy (or None)."""
        if not any(await self.timeline()):
            return None
        if await self.new_project():
            return None
        # the 'save before starting a new one?' question is open: close it, save a copy, try again
        await self.page.evaluate("""(() => { const m=[...document.querySelectorAll('.bbm-modal--open')].find(x=>/save the project before/i.test(x.innerText));
            if(!m) return; const c=[...m.querySelectorAll('.close, .button-close, button')].find(b=>b.offsetParent && /close|cancel|×/i.test((b.innerText||'')+' '+b.className));
            if(c) c.click(); })()""")
        await asyncio.sleep(1)
        copy = time.strftime("AutoCreator safety copy %Y-%m-%d %H-%M")
        await self.save_new(copy)
        await asyncio.sleep(1.5)
        if await self.new_project():
            return copy
        raise VEError("Could not start a new blank project in VideoExpress")

    async def save_new(self, name: str):
        """'Save Project As' always creates a NEW project (save_new=1), so nothing can be overwritten."""
        await self.page.evaluate("document.querySelector('a.button-save-project-as').click()")
        inp = self.page.locator(".bbm-modal--open input[name=project_name]").first
        try:
            await inp.wait_for(state="visible", timeout=10000)
        except Exception:
            raise VEError("The 'Save project' name box did not appear.") from None
        await inp.fill(name)
        await self.page.evaluate("""(() => { const m=document.querySelector('input[name=project_name]').closest('.bbm-modal');
            const b=[...m.querySelectorAll('button')].filter(x=>x.offsetParent && /^\\s*Save\\s*$/.test(x.innerText)).pop(); b.click(); })()""")
        t0 = time.time()
        while time.time() - t0 < 60:
            await asyncio.sleep(1)
            if re.search("successfully saved", await self.page.evaluate("__ve.snack()"), re.I):
                return
        raise VEError("VideoExpress did not confirm the save.")

    async def export(self, name: str, timeout_s: int = 3600) -> dict:
        t0 = time.time()
        while await self.in_flight():            # Export only opens when nothing else is rendering
            if time.time() - t0 > 1800:
                raise VEError("Renders are still running after 30 min; export later.")
            self.log("Waiting for running renders to finish before exporting…")
            await self.sleep(30)
        known = {r.get("id") for r in ((await self.api("/api/get_list_output")) or {}).get("results", [])}
        await self.page.evaluate("document.querySelector('button.button-render-project').click()")
        form = self.page.locator("#formExportProject input[name=name]")
        await form.wait_for(state="visible", timeout=20000)
        await form.fill(name)
        await self.page.evaluate("""(() => { const m=document.querySelector('#formExportProject').closest('.bbm-modal');
            const b=m.querySelector('.button-submit')||[...m.querySelectorAll('button')].filter(x=>/create|export/i.test(x.innerText)).pop(); b.click(); })()""")
        await asyncio.sleep(4)
        errs = await self.page.evaluate("__ve.errors()")
        if errs:
            raise VEError("Export refused: " + " | ".join(errs))
        self.log("Export started (this can take 10–30 minutes)…")
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            await self.sleep(20)
            out = await self.api("/api/get_list_output")
            rows = out.get("results", []) if isinstance(out, dict) else []
            hit = next((r for r in rows if r.get("id") not in known and str(r.get("title", "")).strip() == name), None)
            if hit and hit.get("mediaPath") and not await self.in_flight():
                return {"id": hit.get("id"), "url": hit["mediaPath"], "size": hit.get("filesize")}
        raise VEError("Export did not finish in time (check My Videos in VideoExpress)")


def _find_mp4(obj) -> str | None:
    s = json.dumps(obj)
    m = re.findall(r"https?:\\?/\\?/[^\"\s]+?\.mp4", s)
    return m[0].replace("\\/", "/") if m else None


def download(url: str, dest: Path, log) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    final, i = dest, 2
    while final.exists():
        final = dest.with_name(f"{dest.stem} ({i}){dest.suffix}")
        i += 1
    tmp = final.with_suffix(".part")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 VideoExpressAutoCreator"})
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("content-length") or 0)
        got, last = 0, 0.0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if total and time.time() - last > 5:
                log(f"Downloading… {got * 100 // total}%")
                last = time.time()
    if total and got != total:
        raise VEError(f"Download incomplete ({got} of {total} bytes)")
    tmp.replace(final)
    return final
