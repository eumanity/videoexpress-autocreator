import unittest

from ve import VideoExpress


class _ImageButton:
    def __init__(self, page):
        self.page = page
        self.clicked = False

    async def count(self):
        return 1

    async def is_enabled(self):
        return True

    async def click(self, timeout):
        self.clicked = True
        self.page.previews.append(
            "https://user-assets.renderplatform.com/preview/12345678-1234-1234-1234-123456789abc.jpg"
        )

    def filter(self, has_text):
        self.has_text = has_text
        return self

    def locator(self, selector):
        self.page.nested_selector = selector
        return self


class _Page:
    def __init__(self):
        self.previews = []
        self.button = _ImageButton(self)

    def locator(self, selector):
        self.selectors = getattr(self, "selectors", []) + [selector]
        return self.button

    async def evaluate(self, script, arg=None):
        if script == "__ve.previews()":
            return self.previews
        if script.startswith("([t,p]) =>"):
            return {"shared": False}
        if script.startswith("async ([p,n,vo]) =>"):
            return {"shared": False, "enhance_video_prompt": False, "dur": "10"}
        if script == "__ve.footer()":
            return ""
        if script == "__ve.loading()":
            return False
        if script == "__ve.errors()":
            return []
        raise AssertionError(f"Unexpected browser evaluation: {script}")


class _VideoExpress(VideoExpress):
    async def dialog(self):
        return None

    async def sleep(self, seconds):
        return None


class _ClipVideoExpress(_VideoExpress):
    async def _select_source(self, source):
        self.source = source

    async def media(self, media_type, limit):
        return [{"id": 7}]

    async def _video_id(self, before, seconds=None, previous_footer=""):
        self.video_id_args = before, seconds, previous_footer
        return {"uuid": "12345678-1234-1234-1234-123456789abc"}


class _DialogButton:
    def __init__(self, page):
        self.page = page
        self.clicked = False

    async def count(self):
        return 1

    @property
    def last(self):
        return self

    async def click(self, timeout):
        self.clicked = True
        self.page.open = True


class _DialogPage:
    def __init__(self):
        self.open = False
        self.button = _DialogButton(self)

    async def evaluate(self, script, arg=None):
        if "bbm-modal--open" in script:
            return self.open
        return None

    def locator(self, selector):
        self.selector = selector
        return self.button

    async def wait_for_selector(self, selector, state, timeout):
        self.waited_for = (selector, state)
        if not self.open:
            raise AssertionError("Dialog should be opened before waiting")


class _DialogVideoExpress(VideoExpress):
    async def rail(self, label):
        self.rail_label = label


class KeyframeTests(unittest.IsolatedAsyncioTestCase):
    async def test_keyframe_uses_browser_click_and_returns_new_preview(self):
        page = _Page()
        messages = []
        ve = _VideoExpress({}, None, messages.append, None, lambda: False)
        ve.page = page

        image_id = await ve.keyframe("A test image prompt", "photorealistic")

        self.assertEqual(image_id, "12345678-1234-1234-1234-123456789abc")
        self.assertTrue(page.button.clicked)
        self.assertEqual(page.selectors, [".bbm-modal--open"])
        self.assertEqual(page.nested_selector, "button:visible")
        self.assertEqual(page.button.has_text, "Create Image")
        self.assertIn("Image request submitted to VideoExpress", messages[0])


class DialogTests(unittest.IsolatedAsyncioTestCase):
    async def test_reopens_hidden_videoexpress_dialog(self):
        page = _DialogPage()
        ve = _DialogVideoExpress({}, None, lambda _: None, None, lambda: False)
        ve.page = page

        await ve.dialog()

        self.assertEqual(ve.rail_label, "Create with AI")
        self.assertTrue(page.button.clicked)
        self.assertEqual(page.selector, ".button-generate-from-prompt:visible")
        self.assertEqual(page.waited_for, ("#opt_prompt", "visible"))


class ClipTests(unittest.IsolatedAsyncioTestCase):
    async def test_clip_uses_browser_click_and_tracks_new_clip(self):
        page = _Page()
        messages = []
        ve = _ClipVideoExpress({}, None, messages.append, None, lambda: False)
        ve.page = page

        result = await ve.clip("image-uuid", "A test video prompt", 10)

        self.assertEqual(result["uuid"], "12345678-1234-1234-1234-123456789abc")
        self.assertTrue(page.button.clicked)
        self.assertEqual(page.button.has_text, "Create Video")
        self.assertEqual(ve.source, {"uuid": "image-uuid"})
        self.assertEqual(ve.video_id_args, (7, 10, ""))
        self.assertIn("Clip request submitted to VideoExpress", messages[0])


if __name__ == "__main__":
    unittest.main()
