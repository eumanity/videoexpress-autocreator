import unittest

import brain
import planner
import project


class ExportPartitionTests(unittest.TestCase):
    def test_scene_settings_are_capped_at_videoexpress_clip_limit(self):
        settings = project.normalise_scene_bounds({
            "scene_min": 12,
            "scene_target": 18,
            "scene_max": 21,
        })

        self.assertEqual(
            (settings["scene_min"], settings["scene_target"], settings["scene_max"]),
            (10, 10, 10),
        )

    def test_scene_planner_caps_requested_clip_lengths(self):
        parts = [{"text": "A long sentence continues.", "duration": 25, "pauses": []}]

        scenes = planner.plan_scenes(parts, target=21, min_len=3, max_len=21)

        self.assertTrue(all(scene["len"] <= 10 for scene in scenes))
        self.assertEqual(sum(scene["len"] for scene in scenes), 25)

    def test_legacy_overlong_scene_is_split_without_discarding_other_clips(self):
        scenes = [
            {
                "n": 1, "start": 0, "len": 11, "t_start": 0, "t_end": 11,
                "text": "A scene continues.", "image": "A character walks.",
                "status": "failed", "tries": 3, "img": "old-image",
            },
            {"n": 2, "start": 11, "len": 4, "status": "done", "ref": {"lib": 23}},
        ]

        repaired, split_count = planner.split_overlong_scenes(scenes)

        self.assertEqual(split_count, 1)
        self.assertEqual([scene["len"] for scene in repaired], [6, 5, 4])
        self.assertEqual([scene["n"] for scene in repaired], [1, 2, 3])
        self.assertEqual([scene["start"] for scene in repaired], [0, 6, 11])
        self.assertEqual(repaired[0]["status"], "pending")
        self.assertNotIn("img", repaired[0])
        self.assertTrue(repaired[1]["continue"])
        self.assertIn("next moment", repaired[1]["image"])
        self.assertEqual(repaired[2]["ref"], {"lib": 23})

    def test_narration_exports_keep_parts_and_their_scenes_together(self):
        parts = [{"duration": 350}, {"duration": 280}, {"duration": 400}]
        scenes = [
            {"n": 1, "part": 1},
            {"n": 2, "part": 2},
            {"n": 3, "part": 3},
        ]

        groups = planner.partition_narration_exports(parts, scenes)

        self.assertEqual([g["scene_numbers"] for g in groups], [[1], [2], [3]])
        self.assertTrue(all(g["duration_s"] <= 600 for g in groups))

    def test_short_parts_share_an_export_until_the_cap(self):
        parts = [{"duration": 250}, {"duration": 250}, {"duration": 250}]
        scenes = [{"n": i, "part": i} for i in range(1, 4)]

        groups = planner.partition_narration_exports(parts, scenes)

        self.assertEqual([g["scene_numbers"] for g in groups], [[1, 2], [3]])

    def test_scene_planner_does_not_merge_across_tts_part_boundary(self):
        parts = [
            {"text": "The first part ends here.", "duration": 4, "pauses": []},
            {"text": "The next part starts here.", "duration": 4, "pauses": []},
        ]

        scenes = planner.plan_scenes(parts, target=7, min_len=3, max_len=10)

        self.assertEqual([scene["part"] for scene in scenes], [1, 2])

    def test_dialogue_scenes_are_split_below_export_cap(self):
        scenes = [{"n": i, "len": 200} for i in range(1, 5)]

        groups = planner.partition_scene_exports(scenes)

        self.assertEqual([g["scene_numbers"] for g in groups], [[1, 2, 3], [4]])
        self.assertTrue(all(g["duration_s"] <= 600 for g in groups))

    def test_single_narration_part_over_cap_is_rejected(self):
        with self.assertRaises(ValueError):
            planner.partition_narration_exports([{"duration": 601}], [{"n": 1, "part": 1}])

    def test_new_visual_styles_are_complete_presets(self):
        for key in ("stickman", "claymation"):
            with self.subTest(key=key):
                self.assertTrue(brain.STYLE_PRESETS[key]["label"])
                self.assertTrue(brain.STYLE_PRESETS[key]["style"])
                self.assertTrue(brain.STYLE_PRESETS[key]["motion"])


if __name__ == "__main__":
    unittest.main()
