"""The publish step picks the next version and replaces the old installer file."""

import json
import os
import shutil
import tempfile
import unittest

from _load import ROOT, load

rv = load("installer", "release_version")


class NextVersion(unittest.TestCase):
    def test_an_unchanged_version_takes_the_next_patch(self):
        version, bumped = rv.next_version("2.4.3", "2.4.3")
        self.assertEqual(version, "2.4.4")
        self.assertTrue(bumped)

    def test_a_push_that_already_bumped_keeps_its_version(self):
        version, bumped = rv.next_version("2.5.0", "2.4.3")
        self.assertEqual(version, "2.5.0")
        self.assertFalse(bumped)

    def test_a_missing_parent_keeps_the_current_version(self):
        version, bumped = rv.next_version("2.4.3", None)
        self.assertEqual((version, bumped), ("2.4.3", False))

    def test_a_version_that_is_not_three_numbers_is_refused(self):
        with self.assertRaises(SystemExit):
            rv.next_version("2.4", "2.4")


class Notes(unittest.TestCase):
    def test_the_new_section_lands_above_the_current_one(self):
        text = "# Log\n\n## 2.4.3 (2026-10-23)\n\n- older\n"
        out = rv.insert_notes(text, "2.4.4", "2026-10-23", "- newer\n")
        self.assertLess(out.index("## 2.4.4"), out.index("## 2.4.3"))
        self.assertIn("- newer", out)

    def test_the_same_version_is_not_inserted_twice(self):
        text = "## 2.4.4 (2026-10-23)\n\n- already\n"
        self.assertEqual(rv.insert_notes(text, "2.4.4", "2026-10-24", "- again"), text)


class Prepare(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bot = os.path.join(self.tmp, "bot.py")
        self.log = os.path.join(self.tmp, "CHANGELOG.md")
        with open(self.bot, "w", encoding="utf-8", newline="\n") as fh:
            fh.write('"""x"""\nVERSION = "2.4.3"\n')
        with open(self.log, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# Log\n\n## 2.4.3 (2026-10-23)\n\n- FLATTEN ALL closes everything.\n")
        self.saved = (rv.BOT, rv.CHANGELOG)
        rv.BOT, rv.CHANGELOG = self.bot, self.log

    def tearDown(self):
        rv.BOT, rv.CHANGELOG = self.saved
        shutil.rmtree(self.tmp)

    def test_prepare_writes_the_next_patch_and_keeps_unix_newlines(self):
        version = rv.prepare(previous="2.4.3", today="2026-10-23")
        self.assertEqual(version, "2.4.4")
        with open(self.bot, "rb") as fh:
            raw = fh.read()
        self.assertIn(b'VERSION = "2.4.4"', raw)
        self.assertNotIn(b"\r\n", raw)
        with open(self.log, encoding="utf-8") as fh:
            notes = fh.read()
        self.assertIn("## 2.4.4 (2026-10-23)", notes)
        self.assertIn("FLATTEN ALL", notes)
        self.assertLess(notes.index("## 2.4.4"), notes.index("## 2.4.3"))

    def test_prepare_leaves_a_hand_bumped_version_alone(self):
        version = rv.prepare(previous="2.4.2", today="2026-10-23")
        self.assertEqual(version, "2.4.3")
        with open(self.bot, encoding="utf-8") as fh:
            self.assertIn('VERSION = "2.4.3"', fh.read())


class Stage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.releases = os.path.join(self.tmp, "releases")
        os.makedirs(self.releases)
        self.readme = os.path.join(self.releases, "README.md")
        with open(self.readme, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(
                "# Releases\n\n"
                "| file | version | SHA-256 |\n| --- | --- | --- |\n"
                "| DayTraderBot-Setup-2.4.3.exe | 2.4.3 | `aaa` |\n"
                "| DayTraderBot-Setup-2.4.2.exe | 2.4.2 | `bbb` |\n"
            )
        old = os.path.join(self.releases, "DayTraderBot-Setup-2.4.3.exe")
        with open(old, "wb") as fh:
            fh.write(b"old")
        built = os.path.join(self.tmp, "out")
        os.makedirs(built)
        self.exe = os.path.join(built, "DayTraderBot-Setup-2.4.4.exe")
        with open(self.exe, "wb") as fh:
            fh.write(b"new-installer")
        with open(os.path.join(built, "latest.json"), "w", encoding="utf-8") as fh:
            json.dump({"installer": {"sha256": "abc123"}}, fh)
        self.saved = (rv.RELEASES, rv.README)
        rv.RELEASES, rv.README = self.releases, self.readme

    def tearDown(self):
        rv.RELEASES, rv.README = self.saved
        shutil.rmtree(self.tmp)

    def test_stage_replaces_the_old_setup_file(self):
        rv.stage(self.exe)
        names = sorted(os.listdir(self.releases))
        self.assertIn("DayTraderBot-Setup-2.4.4.exe", names)
        self.assertNotIn("DayTraderBot-Setup-2.4.3.exe", names)
        with open(os.path.join(self.releases, "latest.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["installer"]["sha256"], "abc123")
        with open(self.readme, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("| DayTraderBot-Setup-2.4.4.exe | 2.4.4 | `abc123` |", text)
        self.assertNotIn("DayTraderBot-Setup-2.4.3.exe", text)
        self.assertIn("DayTraderBot-Setup-2.4.2.exe", text)


class ScriptLivesInTheInstaller(unittest.TestCase):
    def test_the_publisher_is_the_module_under_test(self):
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "installer", "release_version.py")))
        self.assertTrue(rv.NOTES.strip().startswith("- This build replaces"))
        self.assertIn("FLATTEN ALL", rv.NOTES)
