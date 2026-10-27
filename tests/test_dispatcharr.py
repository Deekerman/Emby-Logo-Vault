"""Tests for the Dispatcharr side of emby_logo_vault.

No Dispatcharr server is involved. Most tests stub the one transport function,
dis_raw; TestTransport instead stubs urlopen so the real URL and header
building is exercised.
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (os.path.dirname(HERE), HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import emby_logo_vault as elv  # noqa: E402
from test_vault import PNG, PNG2, VaultCase  # noqa: E402

DIS = elv.DISPATCHARR


def logo(lid, url, name="Logo"):
    return {"id": lid, "name": name, "url": url,
            "cache_url": "/api/channels/logos/{}/cache".format(lid)}


def channel(cid, name, logo_id=None, auto=True, number=1, effective=None):
    """A Dispatcharr channel as its API serializes one."""
    return {
        "id": cid, "name": name, "channel_number": number,
        "logo_id": logo_id, "auto_created": auto,
        "effective_name": effective, "effective_logo_id": None,
        "effective_channel_number": None,
    }


class DisCase(VaultCase):
    """A configured Dispatcharr with a scripted set of API responses."""

    def setUp(self):
        super().setUp()
        elv.STATE["dis_base"] = "http://dispatcharr.invalid:9191"
        elv.STATE["dis_key"] = "dis-key"
        elv.STATE["dis_user"] = None
        elv.STATE["dis_pass"] = None
        elv.STATE["dis_token"] = None
        self.calls = []

    def serve(self, channels=(), logos=(), images=None, upload=None,
              patch_status=200):
        """Stub the transport with a small scripted Dispatcharr."""
        images = images or {}

        def fake(path, method="GET", params=None, body=None, content_type=None,
                 timeout=60, authed=True):
            self.calls.append({"path": path, "method": method, "body": body,
                               "content_type": content_type})
            if path.startswith("/api/channels/channels/") and method == "PATCH":
                return patch_status, b"{}", {}
            if path == "/api/channels/channels/":
                return 200, json.dumps(list(channels)).encode(), {}
            if path == "/api/channels/logos/":
                return 200, json.dumps(list(logos)).encode(), {}
            if path == "/api/channels/logos/upload/":
                if upload is None:
                    return 201, json.dumps({"id": 99}).encode(), {}
                return upload
            if path.startswith("/api/channels/logos/"):
                lid = path.split("/")[4]
                if lid in images:
                    return 200, images[lid], {"Content-Type": "image/png"}
                return 404, b"", {}
            return 404, b"", {}

        self.stub("dis_raw", fake)

    def patches(self):
        return [json.loads(c["body"].decode()) for c in self.calls
                if c["method"] == "PATCH"]


class TestConfigured(VaultCase):
    def test_enabled_needs_a_url_and_some_credential(self):
        elv.STATE["dis_base"] = ""
        self.assertFalse(elv.dis_enabled())

        elv.STATE["dis_base"] = "http://d.invalid"
        elv.STATE["dis_key"] = elv.STATE["dis_user"] = None
        self.assertFalse(elv.dis_enabled())

        elv.STATE["dis_key"] = "k"
        self.assertTrue(elv.dis_enabled())

        elv.STATE["dis_key"] = None
        elv.STATE["dis_user"] = "admin"
        self.assertTrue(elv.dis_enabled())

    def test_slots_gain_dispatcharr_only_when_configured(self):
        elv.STATE["dis_base"] = ""
        self.assertEqual(elv.build_state.__doc__ and elv.IMAGE_TYPES,
                         ["Primary", "LogoLight", "LogoLightColor"])
        self.assertEqual(elv.VAULT_SLOTS[-1], DIS)
        self.assertEqual(len(elv.VAULT_SLOTS), 4)


class TestLogoChoice(unittest.TestCase):
    def test_override_logo_wins_over_the_providers(self):
        ch = channel(1, "CBC", logo_id=5)
        ch["effective_logo_id"] = 9
        self.assertEqual(elv.dis_logo_id(ch), 9)

    def test_falls_back_to_the_channels_own_logo(self):
        self.assertEqual(elv.dis_logo_id(channel(1, "CBC", logo_id=5)), 5)

    def test_no_logo_at_all(self):
        self.assertIsNone(elv.dis_logo_id(channel(1, "CBC")))

    def test_name_prefers_the_effective_one(self):
        self.assertEqual(elv.dis_name(channel(1, "raw", effective="Nice")), "Nice")
        self.assertEqual(elv.dis_name(channel(1, "raw")), "raw")


class TestTransport(VaultCase):
    """The real request builder, with only urlopen faked out."""

    def setUp(self):
        super().setUp()
        elv.STATE["dis_base"] = "http://dispatcharr.invalid:9191"
        self.seen = []

        class Resp:
            status = 200
            headers = {"Content-Type": "application/json"}

            def read(self):
                return b'{"access": "tok-from-login"}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        import urllib.request
        original = urllib.request.urlopen
        self.addCleanup(setattr, urllib.request, "urlopen", original)

        def fake(req, timeout=None):
            self.seen.append(req)
            return Resp()

        urllib.request.urlopen = fake

    def test_api_key_goes_in_the_header_not_the_url(self):
        elv.STATE["dis_key"] = "secret-key"
        elv.dis_call("/api/channels/channels/")
        req = self.seen[-1]
        self.assertEqual(req.get_header("X-api-key"), "secret-key")
        self.assertNotIn("secret-key", req.full_url)
        self.assertEqual(req.full_url,
                         "http://dispatcharr.invalid:9191/api/channels/channels/")

    def test_username_and_password_are_traded_for_a_bearer_token(self):
        elv.STATE["dis_key"] = None
        elv.STATE["dis_user"], elv.STATE["dis_pass"] = "admin", "pw"
        elv.dis_call("/api/channels/channels/")

        self.assertEqual(self.seen[0].full_url,
                         "http://dispatcharr.invalid:9191/api/accounts/token/")
        self.assertEqual(json.loads(self.seen[0].data.decode()),
                         {"username": "admin", "password": "pw"})
        self.assertIsNone(self.seen[0].get_header("Authorization"))
        self.assertEqual(self.seen[-1].get_header("Authorization"),
                         "Bearer tok-from-login")

    def test_query_parameters_are_encoded(self):
        elv.STATE["dis_key"] = "k"
        elv.dis_call("/api/channels/logos/", params={"no_pagination": "true"})
        self.assertTrue(self.seen[-1].full_url.endswith(
            "/api/channels/logos/?no_pagination=true"))


class TestMultipart(unittest.TestCase):
    def test_body_carries_the_fields_and_the_file(self):
        body, ctype = elv.dis_multipart(
            {"name": "CBC", "overwrite": "true"}, PNG, "cbc.png", "image/png")
        self.assertTrue(ctype.startswith("multipart/form-data; boundary="))
        boundary = ctype.split("boundary=")[1]

        self.assertIn(boundary.encode(), body)
        self.assertIn(b'name="name"', body)
        self.assertIn(b"CBC", body)
        self.assertIn(b'name="overwrite"', body)
        self.assertIn(b'name="file"; filename="cbc.png"', body)
        self.assertIn(b"Content-Type: image/png", body)
        self.assertIn(PNG, body)
        self.assertTrue(body.rstrip().endswith(
            ("--" + boundary + "--").encode()))

    def test_binary_payload_is_not_mangled(self):
        raw = bytes(range(256))
        body, _ = elv.dis_multipart({}, raw, "x.png", "image/png")
        self.assertIn(raw, body)


class TestUpload(DisCase):
    def test_upload_returns_the_new_logo_id(self):
        self.serve()
        lid, err = elv.dis_upload_logo("CBC", PNG, "image/png")
        self.assertEqual((lid, err), (99, ""))

        call = [c for c in self.calls if c["path"].endswith("/upload/")][0]
        self.assertEqual(call["method"], "POST")
        self.assertTrue(call["content_type"].startswith("multipart/form-data;"))
        # Overwriting by filename keeps the logo manager from filling up with
        # near duplicates on a second push.
        self.assertIn(b'name="overwrite"', call["body"])
        self.assertIn(b"true", call["body"])

    def test_a_type_dispatcharr_rejects_is_refused_before_uploading(self):
        self.serve()
        lid, err = elv.dis_upload_logo("CBC", PNG, "image/bmp")
        self.assertIsNone(lid)
        self.assertIn("image/bmp", err)
        self.assertEqual(self.calls, [])

    def test_oversized_artwork_is_refused_before_uploading(self):
        self.serve()
        lid, err = elv.dis_upload_logo("CBC", b"x" * (5 * 1024 * 1024 + 1),
                                       "image/png")
        self.assertIsNone(lid)
        self.assertIn("5 MB", err)
        self.assertEqual(self.calls, [])

    def test_a_server_error_is_reported(self):
        self.serve(upload=(500, b"boom", {}))
        lid, err = elv.dis_upload_logo("CBC", PNG, "image/png")
        self.assertIsNone(lid)
        self.assertIn("500", err)

    def test_a_response_without_an_id_is_reported(self):
        self.serve(upload=(201, b'{"ok": true}', {}))
        lid, err = elv.dis_upload_logo("CBC", PNG, "image/png")
        self.assertIsNone(lid)
        self.assertIn("no logo id", err)


class TestAssign(DisCase):
    """The behaviour that decides whether a pushed logo survives an M3U sync."""

    def test_an_auto_created_channel_is_written_as_an_override(self):
        self.serve()
        ok, err = elv.dis_assign_logo(channel(7, "CBC", auto=True), 42)
        self.assertTrue(ok, err)
        self.assertEqual(self.patches(), [{"override": {"logo_id": 42}}])

    def test_a_manual_channel_is_written_directly(self):
        self.serve()
        ok, err = elv.dis_assign_logo(channel(7, "CBC", auto=False), 42)
        self.assertTrue(ok, err)
        self.assertEqual(self.patches(), [{"logo_id": 42}])

    def test_the_channel_id_is_in_the_path(self):
        self.serve()
        elv.dis_assign_logo(channel(7, "CBC"), 42)
        self.assertEqual(self.calls[-1]["path"], "/api/channels/channels/7/")

    def test_a_rejected_patch_is_reported(self):
        self.serve(patch_status=400)
        ok, err = elv.dis_assign_logo(channel(7, "CBC"), 42)
        self.assertFalse(ok)
        self.assertIn("400", err)


class TestStateWithDispatcharr(DisCase):
    def setUp(self):
        super().setUp()
        self.stub("fetch_channels", lambda: [
            {"Id": "e1", "Name": "CBC", "ChannelNumber": "1", "ImageTags": {}},
            {"Id": "e2", "Name": "TSN", "ChannelNumber": "2", "ImageTags": {}},
        ])

    def slot(self, st, key):
        return next(c for c in st["channels"] if c["key"] == key)["slots"][DIS]

    def test_matching_logo_url_counts_as_a_match(self):
        self.serve(channels=[channel(1, "CBC", logo_id=10)],
                   logos=[logo(10, "/data/logos/cbc.png")])
        elv.write_manifest({"channels": {"cbc": {"name": "CBC", "images": {
            DIS: dict(elv.vault_write_image("CBC", DIS, PNG, "image/png"),
                      tag="/data/logos/cbc.png")}}}})
        self.assertEqual(self.slot(elv.build_state(), "cbc")["status"], "match")

    def test_a_swapped_logo_url_is_drift(self):
        self.serve(channels=[channel(1, "CBC", logo_id=11)],
                   logos=[logo(11, "http://provider.example/new.png")])
        elv.write_manifest({"channels": {"cbc": {"name": "CBC", "images": {
            DIS: dict(elv.vault_write_image("CBC", DIS, PNG, "image/png"),
                      tag="/data/logos/cbc.png")}}}})
        self.assertEqual(self.slot(elv.build_state(), "cbc")["status"], "differs")

    def test_a_logo_not_held_in_the_vault_is_unsaved(self):
        self.serve(channels=[channel(1, "CBC", logo_id=10)],
                   logos=[logo(10, "/data/logos/cbc.png")])
        st = elv.build_state()
        self.assertEqual(self.slot(st, "cbc")["status"], "unsaved")
        self.assertEqual(self.slot(st, "cbc")["live_id"], 10)

    def test_vault_artwork_with_no_logo_upstream_is_gone(self):
        self.serve(channels=[channel(1, "CBC")], logos=[])
        elv.write_manifest({"channels": {"cbc": {"name": "CBC", "images": {
            DIS: elv.vault_write_image("CBC", DIS, PNG, "image/png")}}}})
        self.assertEqual(self.slot(elv.build_state(), "cbc")["status"], "gone")

    def test_a_channel_dispatcharr_does_not_have_is_empty(self):
        self.serve(channels=[], logos=[])
        self.assertEqual(self.slot(elv.build_state(), "cbc")["status"], "empty")

    def test_the_override_flag_is_reported_per_channel(self):
        self.serve(channels=[channel(1, "CBC", logo_id=10, auto=True),
                             channel(2, "TSN", logo_id=10, auto=False)],
                   logos=[logo(10, "/data/logos/x.png")])
        st = elv.build_state()
        self.assertTrue(self.slot(st, "cbc")["override"])
        self.assertFalse(self.slot(st, "tsn")["override"])

    def test_a_dispatcharr_only_channel_still_gets_a_row(self):
        self.serve(channels=[channel(3, "Sportsnet", logo_id=12)],
                   logos=[logo(12, "/data/logos/sn.png")])
        row = next(c for c in elv.build_state()["channels"]
                   if c["key"] == "sportsnet")
        self.assertFalse(row["in_emby"])
        self.assertEqual(row["dis_id"], 3)
        self.assertEqual(row["slots"][DIS]["status"], "unsaved")
        self.assertEqual(row["slots"]["Primary"]["status"], "empty")

    def test_byte_comparison_when_the_vault_record_has_no_tag(self):
        self.serve(channels=[channel(1, "CBC", logo_id=10)],
                   logos=[logo(10, "/data/logos/cbc.png")],
                   images={"10": PNG2})
        elv.write_manifest({"channels": {"cbc": {"name": "CBC", "images": {
            DIS: dict(elv.vault_write_image("CBC", DIS, PNG, "image/png"),
                      tag=None)}}}})
        self.assertEqual(self.slot(elv.build_state(), "cbc")["status"], "differs")

    def test_dispatcharr_being_down_does_not_break_the_emby_side(self):
        def boom(*a, **k):
            raise RuntimeError("Cannot reach Dispatcharr at http://x: timed out")
        self.stub("dis_raw", boom)

        st = elv.build_state()
        self.assertIn("Cannot reach Dispatcharr", st["dispatcharr_error"])
        self.assertEqual(len([c for c in st["channels"] if c["in_emby"]]), 2)
        self.assertEqual(self.slot(st, "cbc")["status"], "empty")

    def test_the_slot_list_reports_dispatcharr_only_when_configured(self):
        self.serve()
        self.assertEqual(elv.build_state()["types"], elv.VAULT_SLOTS)
        self.assertTrue(elv.build_state()["dispatcharr"])

        elv.STATE["dis_base"] = ""
        st = elv.build_state()
        self.assertEqual(st["types"], elv.IMAGE_TYPES)
        self.assertFalse(st["dispatcharr"])


class TestBackupDispatcharr(DisCase):
    def setUp(self):
        super().setUp()
        self.stub("fetch_channels", list)
        self.serve(channels=[channel(1, "CBC", logo_id=10)],
                   logos=[logo(10, "/data/logos/cbc.png")],
                   images={"10": PNG})

    def test_the_logo_is_saved_with_its_url_as_the_tag(self):
        r = elv.do_backup(deep=False)
        self.assertEqual(r["saved"], 1)

        rec = elv.read_manifest()["channels"]["cbc"]["images"][DIS]
        self.assertEqual(rec["tag"], "/data/logos/cbc.png")
        self.assertEqual(rec["origin"], "dispatcharr")
        self.assertEqual(elv.vault_read_image(rec), PNG)

    def test_the_dispatcharr_channel_id_is_recorded(self):
        elv.do_backup(deep=False)
        self.assertEqual(elv.read_manifest()["channels"]["cbc"]["dis_id"], 1)

    def test_an_excluded_channel_is_skipped(self):
        elv.write_rules({"groups": [], "patterns": ["CBC"]})
        self.assertEqual(elv.do_backup(deep=False)["saved"], 0)

    def test_a_channel_with_no_logo_is_not_a_failure(self):
        self.serve(channels=[channel(1, "CBC")], logos=[])
        r = elv.do_backup(deep=False)
        self.assertEqual((r["saved"], r["failed"]), (0, 0))

    def test_an_unreadable_logo_counts_as_failed(self):
        self.serve(channels=[channel(1, "CBC", logo_id=10)],
                   logos=[logo(10, "/data/logos/cbc.png")], images={})
        r = elv.do_backup(deep=False)
        self.assertEqual((r["saved"], r["failed"]), (0, 1))


class TestPushDispatcharr(DisCase):
    def setUp(self):
        super().setUp()
        self.stub("fetch_channels", list)

    def seed(self, tag="/data/logos/old.png"):
        elv.write_manifest({"channels": {"cbc": {
            "name": "CBC", "dis_id": 1, "images": {
                DIS: dict(elv.vault_write_image("CBC", DIS, PNG, "image/png"),
                          tag=tag)}}}})

    def test_push_uploads_then_assigns_as_an_override(self):
        self.serve(channels=[channel(1, "CBC", logo_id=10, auto=True)],
                   logos=[logo(10, "/data/logos/live.png")])
        self.seed()
        results = elv.do_push_dispatcharr(["cbc"])

        self.assertTrue(results[0]["ok"], results[0]["msg"])
        self.assertIn("override", results[0]["msg"])
        paths = [c["path"] for c in self.calls]
        self.assertIn("/api/channels/logos/upload/", paths)
        self.assertEqual(self.patches(), [{"override": {"logo_id": 99}}])

    def test_a_manual_channel_is_pushed_without_an_override(self):
        self.serve(channels=[channel(1, "CBC", logo_id=10, auto=False)],
                   logos=[logo(10, "/data/logos/live.png")])
        self.seed()
        results = elv.do_push_dispatcharr(["cbc"])
        self.assertTrue(results[0]["ok"])
        self.assertEqual(self.patches(), [{"logo_id": 99}])

    def test_artwork_already_in_place_is_skipped(self):
        self.serve(channels=[channel(1, "CBC", logo_id=10)],
                   logos=[logo(10, "/data/logos/same.png")])
        self.seed(tag="/data/logos/same.png")
        results = elv.do_push_dispatcharr(["cbc"])
        self.assertTrue(results[0]["ok"])
        self.assertIn("skipped", results[0]["msg"])
        self.assertEqual(self.patches(), [])

    def test_force_pushes_even_when_it_already_matches(self):
        self.serve(channels=[channel(1, "CBC", logo_id=10)],
                   logos=[logo(10, "/data/logos/same.png")])
        self.seed(tag="/data/logos/same.png")
        elv.do_push_dispatcharr(["cbc"], force=True)
        self.assertEqual(len(self.patches()), 1)

    def test_the_stored_tag_is_refreshed_after_a_push(self):
        # The second channel read reports the logo the push just set, which is
        # what lets the grid show green without a manual rescan.
        self.serve(channels=[channel(1, "CBC", logo_id=99)],
                   logos=[logo(99, "/data/logos/pushed.png")])
        self.seed()
        elv.do_push_dispatcharr(["cbc"])
        rec = elv.read_manifest()["channels"]["cbc"]["images"][DIS]
        self.assertEqual(rec["tag"], "/data/logos/pushed.png")

    def test_nothing_in_the_vault(self):
        self.serve(channels=[channel(1, "CBC")])
        elv.write_manifest({"channels": {}})
        results = elv.do_push_dispatcharr(["cbc"])
        self.assertFalse(results[0]["ok"])
        self.assertEqual(results[0]["msg"], "not in vault")

    def test_no_matching_channel_in_dispatcharr(self):
        self.serve(channels=[], logos=[])
        self.seed()
        results = elv.do_push_dispatcharr(["cbc"])
        self.assertFalse(results[0]["ok"])
        self.assertIn("no matching channel", results[0]["msg"])
        self.assertEqual(self.patches(), [])

    def test_a_missing_vault_file_is_not_pushed(self):
        self.serve(channels=[channel(1, "CBC")])
        self.seed()
        rec = elv.read_manifest()["channels"]["cbc"]["images"][DIS]
        os.remove(os.path.join(self.dir, elv.IMAGE_DIR, rec["file"]))
        results = elv.do_push_dispatcharr(["cbc"])
        self.assertFalse(results[0]["ok"])
        self.assertEqual(results[0]["msg"], "vault file missing")

    def test_a_failed_upload_does_not_assign(self):
        self.serve(channels=[channel(1, "CBC")], upload=(500, b"nope", {}))
        self.seed()
        results = elv.do_push_dispatcharr(["cbc"])
        self.assertFalse(results[0]["ok"])
        self.assertEqual(self.patches(), [])

    def test_pushing_without_dispatcharr_configured_is_refused(self):
        elv.STATE["dis_base"] = ""
        with self.assertRaises(RuntimeError) as cm:
            elv.do_push_dispatcharr(["cbc"])
        self.assertIn("not configured", str(cm.exception))


class TestDestinationsStaySeparate(DisCase):
    """Emby and Dispatcharr are pushed independently, never as a side effect."""

    def setUp(self):
        super().setUp()
        self.uploaded = []
        self.stub("fetch_channels", lambda: [
            {"Id": "e1", "Name": "CBC", "ChannelNumber": "1",
             "ImageTags": {"Primary": "tag-live"}}])
        self.stub("upload_image", lambda cid, t, data, ctype: (
            self.uploaded.append((cid, t)) or (True, "")))
        self.serve(channels=[channel(1, "CBC", logo_id=10)],
                   logos=[logo(10, "/data/logos/live.png")])
        elv.write_manifest({"channels": {"cbc": {
            "name": "CBC", "emby_id": "e1", "dis_id": 1, "images": {
                "Primary": dict(elv.vault_write_image(
                    "CBC", "Primary", PNG, "image/png"), tag="tag-old"),
                DIS: dict(elv.vault_write_image(
                    "CBC", DIS, PNG2, "image/png"), tag="/data/logos/old.png"),
            }}}})

    def test_an_emby_push_leaves_dispatcharr_alone(self):
        elv.do_restore([("cbc", "Primary")])
        self.assertEqual(self.uploaded, [("e1", "Primary")])
        self.assertEqual(self.patches(), [])

    def test_a_dispatcharr_slot_handed_to_the_emby_push_is_ignored(self):
        results = elv.do_restore([("cbc", DIS)])
        self.assertEqual(results, [])
        self.assertEqual(self.uploaded, [])

    def test_a_dispatcharr_push_leaves_emby_alone(self):
        elv.do_push_dispatcharr(["cbc"])
        self.assertEqual(self.uploaded, [])
        self.assertEqual(len(self.patches()), 1)


class TestCopyFromDispatcharr(DisCase):
    def setUp(self):
        super().setUp()
        self.stub("fetch_channels", list)
        self.stub("deep_probe", lambda channels: None)

    def test_the_dispatcharr_slot_can_seed_the_emby_slots(self):
        self.serve()
        elv.write_manifest({"channels": {"cbc": {"name": "CBC", "images": {
            DIS: elv.vault_write_image("CBC", DIS, PNG, "image/png")}}}})

        r = elv.do_copy_slot(["cbc"], source=DIS)
        self.assertEqual(r["copied"], 3)

        images = elv.read_manifest()["channels"]["cbc"]["images"]
        for t in elv.IMAGE_TYPES:
            self.assertEqual(images[t]["sha256"], elv.sha256(PNG))
            self.assertEqual(images[t]["source"], DIS)

    def test_with_no_vault_copy_it_falls_back_to_dispatcharr_itself(self):
        self.serve(channels=[channel(1, "CBC", logo_id=10)],
                   logos=[logo(10, "/data/logos/cbc.png")],
                   images={"10": PNG2})
        elv.write_manifest({"channels": {"cbc": {"name": "CBC", "images": {}}}})

        r = elv.do_copy_slot(["cbc"], source=DIS)
        self.assertEqual(r["copied"], 3)
        images = elv.read_manifest()["channels"]["cbc"]["images"]
        self.assertEqual(images["Primary"]["sha256"], elv.sha256(PNG2))

    def test_primary_fills_the_dispatcharr_slot_too(self):
        self.serve()
        elv.write_manifest({"channels": {"cbc": {"name": "CBC", "images": {
            "Primary": elv.vault_write_image(
                "CBC", "Primary", PNG, "image/png")}}}})

        r = elv.do_copy_slot(["cbc"], source="Primary")
        self.assertEqual(r["copied"], 3)
        self.assertEqual(
            elv.read_manifest()["channels"]["cbc"]["images"][DIS]["sha256"],
            elv.sha256(PNG))

    def test_without_dispatcharr_configured_only_the_emby_slots_are_filled(self):
        elv.STATE["dis_base"] = ""
        elv.write_manifest({"channels": {"cbc": {"name": "CBC", "images": {
            "Primary": elv.vault_write_image(
                "CBC", "Primary", PNG, "image/png")}}}})

        r = elv.do_copy_slot(["cbc"], source="Primary")
        self.assertEqual(r["copied"], 2)
        self.assertNotIn(DIS, elv.read_manifest()["channels"]["cbc"]["images"])


if __name__ == "__main__":
    unittest.main()
