"""Tests for emby_logo_vault.

Everything here runs against a temporary vault directory with the Emby API
calls stubbed out, so no network and no real server are involved.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import emby_logo_vault as elv  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"fake png bytes"
PNG2 = b"\x89PNG\r\n\x1a\n" + b"different bytes"


class VaultCase(unittest.TestCase):
    """Points STATE at a throwaway vault for the duration of each test."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="logovault-test-")
        self._saved = dict(elv.STATE)
        elv.STATE["vault"] = self.dir
        elv.STATE["base"] = "http://emby.invalid:8096"
        elv.STATE["key"] = "test-key"

    def tearDown(self):
        elv.STATE.update(self._saved)
        shutil.rmtree(self.dir, ignore_errors=True)

    def stub(self, name, value):
        original = getattr(elv, name)
        setattr(elv, name, value)
        self.addCleanup(setattr, elv, name, original)


class TestNaming(unittest.TestCase):
    def test_norm_ignores_case_spacing_and_punctuation(self):
        self.assertEqual(elv.norm("CBC News HD"), elv.norm("cbc-news_hd"))
        self.assertEqual(elv.norm("TSN 1"), "tsn1")

    def test_norm_handles_missing_name(self):
        self.assertEqual(elv.norm(None), "")

    def test_safe_stem_strips_path_characters(self):
        self.assertNotIn("/", elv.safe_stem("Sports/HD"))
        self.assertEqual(elv.safe_stem("CTV  Two"), "CTV_Two")

    def test_safe_stem_never_returns_empty(self):
        self.assertEqual(elv.safe_stem("///"), "unnamed")
        self.assertEqual(elv.safe_stem(""), "unnamed")

    def test_channel_number_sort_puts_numbers_first(self):
        order = sorted(["abc", "12", "2"], key=elv.chan_sort)
        self.assertEqual(order[-1], "abc")
        self.assertEqual(order[:2], ["2", "12"])


class TestPatterns(unittest.TestCase):
    def test_substring_match_is_case_insensitive(self):
        self.assertTrue(elv.matches_pattern("PPV Boxing 3", "ppv"))
        self.assertFalse(elv.matches_pattern("CBC News", "ppv"))

    def test_regex_prefix(self):
        self.assertTrue(elv.matches_pattern("VOD The Matrix", "re:^VOD "))
        self.assertFalse(elv.matches_pattern("Nickelodeon", "re:^VOD "))

    def test_broken_regex_does_not_raise(self):
        self.assertFalse(elv.matches_pattern("anything", "re:[unclosed"))

    def test_empty_pattern_matches_nothing(self):
        self.assertFalse(elv.matches_pattern("CBC News", "   "))


class TestGroups(unittest.TestCase):
    def test_tags_read_as_plain_strings_or_objects(self):
        self.assertEqual(elv.channel_groups({"Tags": ["Sports"]}), ["Sports"])
        self.assertEqual(
            elv.channel_groups({"TagItems": [{"Name": "News"}]}), ["News"])

    def test_no_tags(self):
        self.assertEqual(elv.channel_groups({}), [])

    def test_exclusion_by_group_and_by_name(self):
        rules = {"groups": ["PPV"], "patterns": ["24/7"]}
        self.assertTrue(elv.is_excluded({"Name": "Fight", "Tags": ["PPV"]}, rules))
        self.assertTrue(elv.is_excluded({"Name": "24/7 Cartoons"}, rules))
        self.assertFalse(elv.is_excluded({"Name": "CBC", "Tags": ["News"]}, rules))


class TestRules(VaultCase):
    def test_roundtrip_deduplicates_and_drops_blanks(self):
        elv.write_rules({"groups": ["PPV", "PPV", "VOD"], "patterns": ["a", "  "]})
        r = elv.read_rules()
        self.assertEqual(r["groups"], ["PPV", "VOD"])
        self.assertEqual(r["patterns"], ["a"])

    def test_missing_file_gives_empty_rules(self):
        self.assertEqual(elv.read_rules(), {"groups": [], "patterns": []})

    def test_corrupt_file_gives_empty_rules_rather_than_raising(self):
        with open(elv.rules_path(), "w", encoding="utf8") as f:
            f.write("{ this is not json")
        self.assertEqual(elv.read_rules(), {"groups": [], "patterns": []})


class TestManifest(VaultCase):
    def test_missing_manifest_is_an_empty_one(self):
        m = elv.read_manifest()
        self.assertEqual(m["channels"], {})

    def test_write_sets_timestamps(self):
        elv.write_manifest({"channels": {}})
        m = elv.read_manifest()
        self.assertTrue(m["created"])
        self.assertEqual(m["created"], m["updated"])

    def test_snapshot_preserves_the_previous_manifest(self):
        elv.write_manifest({"channels": {"cbc": {"name": "CBC"}}})
        elv.write_manifest({"channels": {}}, snapshot=True)

        snaps = [f for f in os.listdir(self.dir)
                 if f.startswith("manifest.") and f != "manifest.json"]
        self.assertEqual(len(snaps), 1)
        with open(os.path.join(self.dir, snaps[0]), encoding="utf8") as f:
            self.assertIn("cbc", json.load(f)["channels"])
        self.assertEqual(elv.read_manifest()["channels"], {})


class TestImageStore(VaultCase):
    def test_write_then_read_returns_the_same_bytes(self):
        rec = elv.vault_write_image("CBC_abc12345", "Primary", PNG, "image/png")
        self.assertEqual(elv.vault_read_image(rec), PNG)
        self.assertEqual(rec["sha256"], elv.sha256(PNG))
        self.assertEqual(rec["bytes"], len(PNG))
        self.assertEqual(rec["origin"], "emby")

    def test_extension_follows_the_content_type(self):
        rec = elv.vault_write_image("x", "Primary", PNG, "image/webp")
        self.assertTrue(rec["file"].endswith(".webp"))

    def test_unknown_content_type_falls_back_to_png(self):
        rec = elv.vault_write_image("x", "Primary", PNG, "application/octet-stream")
        self.assertTrue(rec["file"].endswith(".png"))

    def test_reading_a_record_whose_file_is_gone(self):
        rec = elv.vault_write_image("x", "Primary", PNG, "image/png")
        os.remove(os.path.join(self.dir, elv.IMAGE_DIR, rec["file"]))
        self.assertIsNone(elv.vault_read_image(rec))


class TestLibrary(VaultCase):
    def seed(self):
        """Two channels sharing one image, plus a distinct one."""
        m = {"channels": {}}
        for key, name in (("cbc", "CBC"), ("ctv", "CTV")):
            m["channels"][key] = {
                "name": name, "emby_id": key + "-id", "number": "1",
                "images": {"Primary": elv.vault_write_image(
                    name, "Primary", PNG, "image/png")},
            }
        m["channels"]["tsn"] = {
            "name": "TSN", "emby_id": "tsn-id", "number": "2",
            "images": {"Primary": elv.vault_write_image(
                "TSN", "Primary", PNG2, "image/png")},
        }
        elv.write_manifest(m)

    def test_identical_artwork_is_folded_into_one_entry(self):
        self.seed()
        lib = elv.build_library()
        self.assertEqual(len(lib), 2)
        shared = [e for e in lib if e["sha"] == elv.sha256(PNG)][0]
        self.assertEqual(sorted(shared["channels"]), ["CBC", "CTV"])

    def test_lookup_by_hash(self):
        self.seed()
        self.assertIsNotNone(elv.library_lookup(elv.sha256(PNG2)))
        self.assertIsNone(elv.library_lookup("0" * 64))

    def test_assign_copies_artwork_into_another_slot(self):
        self.seed()
        r = elv.do_assign_from_library("tsn", "LogoLight", elv.sha256(PNG))
        self.assertTrue(r["ok"])

        rec = elv.read_manifest()["channels"]["tsn"]["images"]["LogoLight"]
        self.assertEqual(rec["sha256"], elv.sha256(PNG))
        self.assertEqual(rec["origin"], "library")
        self.assertEqual(elv.vault_read_image(rec), PNG)

    def test_assign_from_an_unknown_hash_fails_cleanly(self):
        self.seed()
        r = elv.do_assign_from_library("tsn", "LogoLight", "0" * 64)
        self.assertFalse(r["ok"])


class TestCopySlot(VaultCase):
    def setUp(self):
        super().setUp()
        # No Emby to talk to: every copy has to come from the vault.
        self.stub("fetch_channels", lambda: [])
        elv.write_manifest({"channels": {"cbc": {
            "name": "CBC", "emby_id": "cbc-id", "number": "1",
            "images": {"Primary": elv.vault_write_image(
                "CBC", "Primary", PNG, "image/png")},
        }}})

    def test_primary_fills_both_logo_slots(self):
        r = elv.do_copy_slot(["cbc"])
        self.assertEqual(r["copied"], 2)

        images = elv.read_manifest()["channels"]["cbc"]["images"]
        for t in ("LogoLight", "LogoLightColor"):
            self.assertEqual(images[t]["sha256"], elv.sha256(PNG))
            self.assertEqual(images[t]["origin"], "copy")
            self.assertEqual(images[t]["source"], "Primary")

    def test_a_filled_slot_is_left_alone_without_overwrite(self):
        elv.do_copy_slot(["cbc"])
        r = elv.do_copy_slot(["cbc"])
        self.assertEqual(r["copied"], 0)
        self.assertEqual(r["skipped"], 2)

    def test_overwrite_replaces_a_filled_slot(self):
        elv.do_copy_slot(["cbc"])
        r = elv.do_copy_slot(["cbc"], overwrite=True)
        self.assertEqual(r["copied"], 2)

    def test_a_channel_with_no_source_artwork_is_counted_not_crashed(self):
        r = elv.do_copy_slot(["nosuchchannel"])
        self.assertEqual(r["no_source"], 1)
        self.assertEqual(r["copied"], 0)


class TestBuildState(VaultCase):
    """Drift detection: what the status column says for each combination."""

    def setUp(self):
        super().setUp()
        self.stub("fetch_channels", lambda: [
            {"Id": "1", "Name": "CBC", "ChannelNumber": "1", "Tags": ["News"],
             "ImageTags": {"Primary": "tag-unchanged", "LogoLight": "tag-new"}},
            {"Id": "2", "Name": "TSN", "ChannelNumber": "2", "Tags": ["Sports"],
             "ImageTags": {"Primary": "tag-fresh"}},
        ])
        elv.write_manifest({"channels": {
            "cbc": {"name": "CBC", "emby_id": "1", "number": "1", "images": {
                "Primary": dict(elv.vault_write_image("CBC", "Primary", PNG,
                                                      "image/png"),
                                tag="tag-unchanged"),
                "LogoLight": dict(elv.vault_write_image("CBC", "LogoLight", PNG,
                                                        "image/png"),
                                  tag="tag-old"),
                "LogoLightColor": dict(elv.vault_write_image(
                    "CBC", "LogoLightColor", PNG, "image/png"), tag="tag-any"),
            }},
            "oldsportsnet": {"name": "Old Sportsnet", "emby_id": "9",
                             "number": "9", "images": {
                                 "Primary": elv.vault_write_image(
                                     "Old", "Primary", PNG2, "image/png")}},
        }})

    def slots(self, state, key):
        return [c for c in state["channels"] if c["key"] == key][0]["slots"]

    def test_statuses(self):
        st = elv.build_state()
        cbc = self.slots(st, "cbc")
        self.assertEqual(cbc["Primary"]["status"], "match")
        self.assertEqual(cbc["LogoLight"]["status"], "differs")
        # Held in the vault, no longer present in Emby.
        self.assertEqual(cbc["LogoLightColor"]["status"], "gone")

        tsn = self.slots(st, "tsn")
        self.assertEqual(tsn["Primary"]["status"], "unsaved")
        self.assertEqual(tsn["LogoLight"]["status"], "empty")

    def test_channel_only_in_the_vault_is_still_listed(self):
        st = elv.build_state()
        orphan = [c for c in st["channels"] if c["key"] == "oldsportsnet"][0]
        self.assertFalse(orphan["in_emby"])
        self.assertEqual(orphan["slots"]["Primary"]["status"], "orphan")

    def test_byte_comparison_when_neither_side_has_a_usable_tag(self):
        """A deep scan marks slots 'probed', so drift falls back to sha256."""
        self.stub("fetch_channels", lambda: [
            {"Id": "1", "Name": "CBC", "ChannelNumber": "1",
             "ImageTags": {"Primary": "probed"}}])
        self.stub("fetch_image", lambda cid, t: (PNG2, "image/png"))
        elv.write_manifest({"channels": {"cbc": {
            "name": "CBC", "emby_id": "1", "number": "1",
            "images": {"Primary": dict(elv.vault_write_image(
                "CBC", "Primary", PNG, "image/png"), tag=None)}}}})

        self.assertEqual(
            self.slots(elv.build_state(), "cbc")["Primary"]["status"], "differs")

        self.stub("fetch_image", lambda cid, t: (PNG, "image/png"))
        self.assertEqual(
            self.slots(elv.build_state(), "cbc")["Primary"]["status"], "match")

    def test_groups_and_exclusions_are_reported(self):
        elv.write_rules({"groups": ["Sports"], "patterns": []})
        st = elv.build_state()
        self.assertEqual(st["excluded_count"], 1)
        self.assertTrue(self.channel(st, "tsn")["excluded"])
        self.assertFalse(self.channel(st, "cbc")["excluded"])
        self.assertIn("Sports", [g["name"] for g in st["groups"]])

    def channel(self, state, key):
        return [c for c in state["channels"] if c["key"] == key][0]


class TestBackup(VaultCase):
    def setUp(self):
        super().setUp()
        self.stub("fetch_channels", lambda: [
            {"Id": "1", "Name": "CBC", "ChannelNumber": "1", "Tags": ["News"],
             "ImageTags": {"Primary": "tag-1"}},
            {"Id": "2", "Name": "PPV Fight", "ChannelNumber": "2",
             "Tags": ["PPV"], "ImageTags": {"Primary": "tag-2"}},
        ])
        self.stub("fetch_image", lambda cid, t: (PNG, "image/png"))

    def test_backup_saves_everything_not_excluded(self):
        r = elv.do_backup(deep=False)
        self.assertEqual(r["saved"], 2)
        self.assertEqual(r["failed"], 0)

        images = elv.read_manifest()["channels"]["cbc"]["images"]
        self.assertEqual(images["Primary"]["tag"], "tag-1")
        self.assertEqual(elv.vault_read_image(images["Primary"]), PNG)

    def test_excluded_groups_are_skipped(self):
        elv.write_rules({"groups": ["PPV"], "patterns": []})
        r = elv.do_backup(deep=False)
        self.assertEqual(r["saved"], 1)
        self.assertNotIn("ppvfight", elv.read_manifest()["channels"])

    def test_explicit_targets_override_the_exclusion_rules(self):
        elv.write_rules({"groups": ["PPV"], "patterns": []})
        elv.do_backup([("ppvfight", "Primary")], deep=False)
        self.assertIn("ppvfight", elv.read_manifest()["channels"])

    def test_an_unreadable_image_is_counted_as_failed(self):
        self.stub("fetch_image", lambda cid, t: (None, None))
        r = elv.do_backup(deep=False)
        self.assertEqual(r["saved"], 0)
        self.assertEqual(r["failed"], 2)


class TestRestore(VaultCase):
    def setUp(self):
        super().setUp()
        self.uploaded = []
        self.stub("fetch_channels", lambda: [
            {"Id": "1", "Name": "CBC", "ChannelNumber": "1",
             "ImageTags": {"Primary": "tag-live"}}])
        self.stub("upload_image", lambda cid, t, data, ctype: (
            self.uploaded.append((cid, t, data, ctype)) or (True, "")))

    def seed(self, tag):
        elv.write_manifest({"channels": {"cbc": {
            "name": "CBC", "emby_id": "1", "number": "1",
            "images": {"Primary": dict(elv.vault_write_image(
                "CBC", "Primary", PNG, "image/png"), tag=tag)}}}})

    def test_drifted_artwork_is_pushed(self):
        self.seed("tag-old")
        results = elv.do_restore([("cbc", "Primary")])
        self.assertTrue(results[0]["ok"])
        self.assertEqual(self.uploaded[0][:2], ("1", "Primary"))
        self.assertEqual(self.uploaded[0][2], PNG)

    def test_a_matching_tag_is_skipped(self):
        self.seed("tag-live")
        results = elv.do_restore([("cbc", "Primary")])
        self.assertTrue(results[0]["ok"])
        self.assertEqual(self.uploaded, [])

    def test_force_pushes_a_matching_tag_anyway(self):
        self.seed("tag-live")
        elv.do_restore([("cbc", "Primary")], force=True)
        self.assertEqual(len(self.uploaded), 1)

    def test_nothing_in_the_vault(self):
        elv.write_manifest({"channels": {}})
        results = elv.do_restore([("cbc", "Primary")])
        self.assertFalse(results[0]["ok"])
        self.assertEqual(results[0]["msg"], "not in vault")

    def test_no_matching_channel_in_emby(self):
        self.seed("tag-old")
        self.stub("fetch_channels", lambda: [])
        results = elv.do_restore([("cbc", "Primary")])
        self.assertFalse(results[0]["ok"])
        self.assertIn("no matching channel", results[0]["msg"])

    def test_a_missing_vault_file_is_reported_not_uploaded(self):
        self.seed("tag-old")
        m = elv.read_manifest()
        os.remove(os.path.join(
            self.dir, elv.IMAGE_DIR, m["channels"]["cbc"]["images"]["Primary"]["file"]))
        results = elv.do_restore([("cbc", "Primary")])
        self.assertFalse(results[0]["ok"])
        self.assertEqual(self.uploaded, [])


if __name__ == "__main__":
    unittest.main()
