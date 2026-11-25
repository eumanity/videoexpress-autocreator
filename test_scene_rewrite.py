import json
import unittest

from brain import Brain, BrainError


class _RewriteBrain(Brain):
    def __init__(self, response, provider="groq"):
        super().__init__({"provider": provider})
        self.response = response
        self.request = None

    def ask(self, system, prompt, max_tokens=16000, log=print, json_mode=True, research=False):
        self.request = (system, prompt, max_tokens)
        return self.response


class SceneRewriteTests(unittest.TestCase):
    def test_rewrites_only_prompt_and_includes_context_and_feedback(self):
        brain = _RewriteBrain(json.dumps({"scenes": [{"n": 4, "image": "A fresh factory floor at sunrise."}]}))
        scene = {"n": 4, "text": "The old factory reopens.", "image": "A closed factory at night."}

        result = brain.rewrite_scene_prompt(
            {"title": "A New Start", "tokens": []}, scene, "Show workers celebrating."
        )

        self.assertEqual(result, "A fresh factory floor at sunrise.")
        self.assertIn("The old factory reopens.", brain.request[1])
        self.assertIn("A closed factory at night.", brain.request[1])
        self.assertIn("Show workers celebrating.", brain.request[1])

    def test_rejects_unchanged_prompt(self):
        brain = _RewriteBrain(json.dumps({"scenes": [{"n": 4, "image": "same prompt"}]}))

        with self.assertRaisesRegex(BrainError, "same image prompt"):
            brain.rewrite_scene_prompt(
                {"tokens": []}, {"n": 4, "text": "A scene", "image": "same prompt"}
            )

    def test_requires_connected_ai_provider(self):
        for provider in ("manual", "offline"):
            with self.subTest(provider=provider):
                brain = _RewriteBrain("{}", provider=provider)
                with self.assertRaisesRegex(BrainError, "connected AI provider"):
                    brain.rewrite_scene_prompt({"tokens": []}, {"n": 4, "image": "old"})


if __name__ == "__main__":
    unittest.main()
