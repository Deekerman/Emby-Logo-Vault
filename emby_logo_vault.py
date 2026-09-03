#!/usr/bin/env python3
"""
emby_logo_vault.py

Back up, preview and restore Emby live TV channel logos across all three
Emby channel image slots: Primary, LogoLight and LogoLightColor.

Runs a small local web interface so you can see every logo on both a light
and a dark background before pushing anything back into Emby, and drop in
your own replacement artwork.

Stdlib only. Python 3.8+.

    export EMBY_URL="http://192.168.1.10:8096"
    export EMBY_API_KEY="your_key_here"
    ./emby_logo_vault.py

Then open http://localhost:8077

Command line, no browser:
    ./emby_logo_vault.py backup
    ./emby_logo_vault.py status
    ./emby_logo_vault.py restore [--preview] [--force]
"""

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IMAGE_TYPES = ["Primary", "LogoLight", "LogoLightColor"]

TYPE_LABEL = {
    "Primary": "Primary",
    "LogoLight": "Logo light",
    "LogoLightColor": "Logo light color",
}

TYPE_NOTE = {
    "Primary": "shown on light backgrounds",
    "LogoLight": "dark backgrounds, plain",
    "LogoLightColor": "dark backgrounds, default",
}

MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/bmp": ".bmp",
}
EXT_MIME = {v: k for k, v in MIME_EXT.items()}

MANIFEST_NAME = "manifest.json"
IMAGE_DIR = "images"

STATE = {"base": None, "key": None, "vault": None}
LOCK = threading.Lock()


# ----------------------------------------------------------------------
# Emby API
# ----------------------------------------------------------------------

def api_call(path, method="GET", params=None, body=None, content_type=None,
             timeout=60):
    params = dict(params or {})
    params["api_key"] = STATE["key"]
    url = "{}/emby{}?{}".format(
        STATE["base"], path, urllib.parse.urlencode(params)
    )
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Accept", "application/json")
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})
    except urllib.error.URLError as e:
        raise RuntimeError("Cannot reach Emby at {}: {}".format(STATE["base"], e.reason))


def api_json(path, params=None):
    status, raw, _ = api_call(path, params=params)
    if status != 200:
        raise RuntimeError("Emby returned {} for {}: {}".format(
            status, path, raw[:300].decode("utf8", "replace")))
    return json.loads(raw.decode("utf8"))


def fetch_channels():
    data = api_json("/LiveTv/Channels", {
        "Limit": 10000,
        "Fields": "ImageTags,ChannelNumber,Tags",
        "EnableImageTypes": ",".join(IMAGE_TYPES),
    })
    return data.get("Items", [])


def fetch_image(item_id, image_type):
    status, raw, headers = api_call("/Items/{}/Images/{}".format(item_id, image_type))
    if status != 200 or not raw:
        return None, None
    ctype = (headers.get("Content-Type") or "image/png").split(";")[0].strip()
    return raw, ctype


def probe_image(item_id, image_type):
    """Ask the image endpoint directly rather than trusting the tag list."""
    try:
        status, raw, _ = api_call(
            "/Items/{}/Images/{}".format(item_id, image_type),
            params={"maxHeight": 1}, timeout=20)
        return status == 200 and bool(raw)
    except RuntimeError:
        return False


def upload_image(item_id, image_type, data, content_type):
    """Emby wants the body as base64 text with the real mime type in the header."""
    api_call("/Items/{}/Images/{}".format(item_id, image_type), method="DELETE")
    status, raw, _ = api_call(
        "/Items/{}/Images/{}".format(item_id, image_type),
        method="POST", body=base64.b64encode(data), content_type=content_type
    )
    if status not in (200, 204):
        return False, "{} {}".format(status, raw[:200].decode("utf8", "replace"))
    return True, ""


# ----------------------------------------------------------------------
# Vault
# ----------------------------------------------------------------------

def norm(name):
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def safe_stem(name):
    s = re.sub(r"[^A-Za-z0-9 ._]+", "", name or "").strip()
    return re.sub(r"\s+", "_", s) or "unnamed"


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


RULES_NAME = "exclusions.json"


def rules_path():
    return os.path.join(STATE["vault"], RULES_NAME)


def read_rules():
    p = rules_path()
    if not os.path.exists(p):
        return {"groups": [], "patterns": []}
    try:
        with open(p, "r", encoding="utf8") as f:
            r = json.load(f)
    except (ValueError, OSError):
        return {"groups": [], "patterns": []}
    return {"groups": list(r.get("groups") or []),
            "patterns": list(r.get("patterns") or [])}


def write_rules(rules):
    os.makedirs(STATE["vault"], exist_ok=True)
    tmp = rules_path() + ".tmp"
    with open(tmp, "w", encoding="utf8") as f:
        json.dump({"groups": sorted(set(rules.get("groups") or [])),
                   "patterns": [x for x in (rules.get("patterns") or []) if x.strip()]},
                  f, indent=2)
    os.replace(tmp, rules_path())


def channel_groups(ch):
    """M3U group titles arrive as Emby tags."""
    tags = ch.get("Tags") or ch.get("TagItems") or []
    out = []
    for t in tags:
        name = t.get("Name") if isinstance(t, dict) else t
        if name:
            out.append(str(name))
    return out


def matches_pattern(name, pattern):
    pattern = pattern.strip()
    if not pattern:
        return False
    if pattern.lower().startswith("re:"):
        try:
            return re.search(pattern[3:], name or "", re.I) is not None
        except re.error:
            return False
    return pattern.lower() in (name or "").lower()


def is_excluded(ch, rules):
    if set(channel_groups(ch)) & set(rules.get("groups") or []):
        return True
    name = ch.get("Name") or ""
    return any(matches_pattern(name, p) for p in rules.get("patterns") or [])


def manifest_path():
    return os.path.join(STATE["vault"], MANIFEST_NAME)


def read_manifest():
    p = manifest_path()
    if not os.path.exists(p):
        return {"created": None, "updated": None, "channels": {}}
    with open(p, "r", encoding="utf8") as f:
        m = json.load(f)
    m.setdefault("channels", {})
    return m


def write_manifest(m, snapshot=False):
    os.makedirs(STATE["vault"], exist_ok=True)
    p = manifest_path()
    if snapshot and os.path.exists(p):
        stamp = time.strftime("%Y%m%d_%H%M%S")
        os.rename(p, os.path.join(STATE["vault"], "manifest.{}.json".format(stamp)))
    m["updated"] = now_iso()
    m.setdefault("created", m["updated"])
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf8") as f:
        json.dump(m, f, indent=2)
    os.replace(tmp, p)


def vault_write_image(key, image_type, data, content_type, tag=None, origin="emby",
                      source=None):
    """Save one image into the vault and return its manifest record."""
    imgdir = os.path.join(STATE["vault"], IMAGE_DIR)
    os.makedirs(imgdir, exist_ok=True)
    fname = "{}__{}{}".format(key, image_type, MIME_EXT.get(content_type, ".png"))
    with open(os.path.join(imgdir, fname), "wb") as f:
        f.write(data)
    return {
        "file": fname,
        "content_type": content_type,
        "sha256": sha256(data),
        "tag": tag,
        "bytes": len(data),
        "origin": origin,
        "source": source,
        "saved": now_iso(),
    }


def vault_read_image(rec):
    p = os.path.join(STATE["vault"], IMAGE_DIR, rec["file"])
    if not os.path.exists(p):
        return None
    with open(p, "rb") as f:
        return f.read()


# ----------------------------------------------------------------------
# State assembly
# ----------------------------------------------------------------------

def build_state(deep=False):
    """Join live Emby channels against the vault. Uses image tags for speed."""
    channels = fetch_channels()
    if deep:
        deep_probe(channels)
    m = read_manifest()
    rules = read_rules()
    group_counts = {}
    vault_channels = m["channels"]

    rows = []
    seen = set()

    for ch in channels:
        key = norm(ch.get("Name"))
        seen.add(key)
        vc = vault_channels.get(key, {})
        vimages = vc.get("images", {})
        live_tags = ch.get("ImageTags") or {}

        slots = {}
        for t in IMAGE_TYPES:
            live_tag = live_tags.get(t)
            rec = vimages.get(t)
            if live_tag and rec:
                rtag = rec.get("tag")
                if rtag and rtag != "probed" and live_tag != "probed":
                    status = "match" if rtag == live_tag else "differs"
                else:
                    # No usable tag on either side, so compare the bytes.
                    live_data, _ = fetch_image(ch["Id"], t)
                    status = ("match" if live_data and sha256(live_data) == rec.get("sha256")
                              else "differs")
            elif live_tag and not rec:
                status = "unsaved"
            elif rec and not live_tag:
                status = "gone"
            else:
                status = "empty"
            slots[t] = {
                "status": status,
                "live": bool(live_tag),
                "live_tag": live_tag,
                "vault": bool(rec),
                "vault_sha": (rec or {}).get("sha256"),
                "origin": (rec or {}).get("origin"),
                "source": (rec or {}).get("source"),
                "saved": (rec or {}).get("saved"),
            }

        groups = channel_groups(ch)
        for g in (groups or ["Ungrouped"]):
            group_counts[g] = group_counts.get(g, 0) + 1

        rows.append({
            "key": key,
            "name": ch.get("Name") or "Unnamed",
            "number": ch.get("ChannelNumber") or "",
            "id": ch.get("Id"),
            "in_emby": True,
            "groups": groups,
            "excluded": is_excluded(ch, rules),
            "slots": slots,
        })

    # Channels that exist only in the vault, so you can still see what you hold.
    for key, vc in vault_channels.items():
        if key in seen:
            continue
        slots = {}
        for t in IMAGE_TYPES:
            rec = (vc.get("images") or {}).get(t)
            slots[t] = {
                "status": "orphan" if rec else "empty",
                "live": False, "live_tag": None,
                "vault": bool(rec),
                "vault_sha": (rec or {}).get("sha256"),
                "origin": (rec or {}).get("origin"),
                "source": (rec or {}).get("source"),
                "saved": (rec or {}).get("saved"),
            }
        rows.append({
            "key": key,
            "name": vc.get("name") or key,
            "number": vc.get("number") or "",
            "id": vc.get("emby_id"),
            "in_emby": False,
            "groups": [],
            "excluded": is_excluded({"Name": vc.get("name") or key}, rules),
            "slots": slots,
        })

    rows.sort(key=lambda r: (chan_sort(r["number"]), r["name"].lower()))
    groups = [{"name": g, "count": c, "excluded": g in (rules.get("groups") or [])}
              for g, c in sorted(group_counts.items(), key=lambda kv: -kv[1])]
    return {
        "channels": rows,
        "groups": groups,
        "rules": rules,
        "excluded_count": sum(1 for r in rows if r["excluded"]),
        "types": IMAGE_TYPES,
        "labels": TYPE_LABEL,
        "notes": TYPE_NOTE,
        "manifest": {"created": m.get("created"), "updated": m.get("updated")},
        "emby_url": STATE["base"],
        "vault": os.path.abspath(STATE["vault"]),
    }


def deep_probe(channels):
    """Some Emby builds leave LogoLight tags out of the channel list response.
    Ask each image endpoint directly and fill in what the list did not report."""
    from concurrent.futures import ThreadPoolExecutor

    jobs = []
    for ch in channels:
        tags = ch.setdefault("ImageTags", {}) or {}
        ch["ImageTags"] = tags
        for t in IMAGE_TYPES:
            if not tags.get(t):
                jobs.append((ch, t))
    if not jobs:
        return
    with ThreadPoolExecutor(max_workers=12) as pool:
        found = list(pool.map(lambda j: probe_image(j[0]["Id"], j[1]), jobs))
    for (ch, t), ok in zip(jobs, found):
        if ok:
            # Synthetic marker. Real tags come back on the next backup.
            ch["ImageTags"][t] = "probed"


def chan_sort(num):
    try:
        return (0, float(str(num).replace(" ", "") or 1e9))
    except ValueError:
        return (1, 1e9)


def do_backup(targets=None, deep=True):
    """targets: list of (key, type) or None for everything currently in Emby."""
    channels = fetch_channels()
    if deep:
        deep_probe(channels)
    by_key = {norm(c.get("Name")): c for c in channels}
    m = read_manifest()

    if targets is None:
        rules = read_rules()
        targets = []
        for c in channels:
            if is_excluded(c, rules):
                continue
            tags = c.get("ImageTags") or {}
            for t in IMAGE_TYPES:
                if tags.get(t):
                    targets.append((norm(c.get("Name")), t))

    saved, failed = 0, 0
    for key, t in targets:
        ch = by_key.get(key)
        if not ch:
            failed += 1
            continue
        tag = (ch.get("ImageTags") or {}).get(t)
        if not tag:
            continue
        data, ctype = fetch_image(ch["Id"], t)
        if not data:
            failed += 1
            continue
        entry = m["channels"].setdefault(key, {
            "name": ch.get("Name"), "emby_id": ch.get("Id"),
            "number": ch.get("ChannelNumber"), "images": {},
        })
        entry["name"] = ch.get("Name")
        entry["emby_id"] = ch.get("Id")
        entry["number"] = ch.get("ChannelNumber")
        entry["images"][t] = vault_write_image(
            safe_stem(ch.get("Name")) + "_" + key[:8], t, data, ctype,
            tag=(None if tag == "probed" else tag), origin="emby")
        saved += 1

    write_manifest(m)
    return {"saved": saved, "failed": failed}


def do_copy_slot(keys, source="Primary", targets=None, overwrite=False):
    """Duplicate one slot's artwork into the other slots for each channel.

    Prefers the vault copy of the source, falling back to whatever Emby holds.
    Writes into the vault only, so nothing reaches Emby until you push.
    """
    if source not in IMAGE_TYPES:
        source = "Primary"
    targets = targets or [t for t in IMAGE_TYPES if t != source]
    channels = fetch_channels()
    deep_probe(channels)
    by_key = {norm(c.get("Name")): c for c in channels}
    m = read_manifest()

    copied = skipped = no_source = 0
    for key in keys:
        vc = m["channels"].get(key) or {}
        rec = (vc.get("images") or {}).get(source)
        data = vault_read_image(rec) if rec else None
        ctype = rec["content_type"] if rec else None

        if data is None:
            ch = by_key.get(key)
            if ch:
                data, ctype = fetch_image(ch["Id"], source)
        if data is None:
            no_source += 1
            continue

        ch = by_key.get(key)
        entry = m["channels"].setdefault(key, {
            "name": (ch or {}).get("Name") or vc.get("name") or key,
            "emby_id": (ch or {}).get("Id") or vc.get("emby_id"),
            "number": (ch or {}).get("ChannelNumber") or vc.get("number"),
            "images": {},
        })
        stem = safe_stem(entry.get("name")) + "_" + key[:8]
        for t in targets:
            if t == source:
                continue
            if entry["images"].get(t) and not overwrite:
                skipped += 1
                continue
            entry["images"][t] = vault_write_image(
                stem, t, data, ctype, tag=None, origin="copy", source=source)
            copied += 1

    write_manifest(m)
    return {"copied": copied, "skipped": skipped, "no_source": no_source}


def build_library():
    """Every distinct image in the vault, folded by content hash."""
    m = read_manifest()
    lib = {}
    for key, vc in m["channels"].items():
        for t, rec in (vc.get("images") or {}).items():
            sha = rec.get("sha256")
            if not sha:
                continue
            e = lib.setdefault(sha, {
                "sha": sha, "file": rec["file"],
                "content_type": rec.get("content_type", "image/png"),
                "channels": [], "types": [],
            })
            nm = vc.get("name") or key
            if nm not in e["channels"]:
                e["channels"].append(nm)
            if t not in e["types"]:
                e["types"].append(t)
    out = list(lib.values())
    out.sort(key=lambda e: (e["channels"][0] or "").lower())
    return out


def library_lookup(sha):
    for e in build_library():
        if e["sha"] == sha:
            return e
    return None


def do_assign_from_library(key, image_type, sha, name=None, cid=None, number=None):
    """Point one channel slot at artwork already held in the vault."""
    src = library_lookup(sha)
    if not src:
        return {"ok": False, "msg": "that image is no longer in the vault"}
    path = os.path.join(STATE["vault"], IMAGE_DIR, src["file"])
    if not os.path.exists(path):
        return {"ok": False, "msg": "vault file missing"}
    with open(path, "rb") as f:
        data = f.read()

    m = read_manifest()
    entry = m["channels"].setdefault(key, {
        "name": name or key, "emby_id": cid, "number": number, "images": {},
    })
    if name:
        entry["name"] = name
    if cid:
        entry["emby_id"] = cid
    entry["images"][image_type] = vault_write_image(
        safe_stem(entry.get("name")) + "_" + key[:8], image_type, data,
        src["content_type"], tag=None, origin="library",
        source=src["channels"][0])
    write_manifest(m)
    return {"ok": True, "from": src["channels"][0]}


def do_restore(targets, force=False):
    channels = fetch_channels()
    by_key = {norm(c.get("Name")): c for c in channels}
    by_id = {c.get("Id"): c for c in channels}
    m = read_manifest()

    results = []
    for key, t in targets:
        vc = m["channels"].get(key) or {}
        rec = (vc.get("images") or {}).get(t)
        if not rec:
            results.append({"key": key, "type": t, "ok": False, "msg": "not in vault"})
            continue
        ch = by_key.get(key) or by_id.get(vc.get("emby_id"))
        if not ch:
            results.append({"key": key, "type": t, "ok": False, "msg": "no matching channel in Emby"})
            continue
        data = vault_read_image(rec)
        if data is None:
            results.append({"key": key, "type": t, "ok": False, "msg": "vault file missing"})
            continue
        live_tag = (ch.get("ImageTags") or {}).get(t)
        rtag = rec.get("tag")
        if not force and live_tag and rtag and live_tag == rtag:
            results.append({"key": key, "type": t, "ok": True, "msg": "already matching, skipped"})
            continue
        ok, err = upload_image(ch["Id"], t, data, rec["content_type"])
        results.append({"key": key, "type": t, "ok": ok, "msg": err or "restored"})

    # Refresh tags so the UI shows green straight after a push.
    try:
        fresh = {norm(c.get("Name")): c for c in fetch_channels()}
        for key, t in targets:
            ch = fresh.get(key)
            vc = m["channels"].get(key) or {}
            rec = (vc.get("images") or {}).get(t)
            if ch and rec:
                newtag = (ch.get("ImageTags") or {}).get(t)
                if newtag:
                    rec["tag"] = newtag
        write_manifest(m)
    except RuntimeError:
        pass

    return results


# ----------------------------------------------------------------------
# Web server
# ----------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode("utf8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf8")) if n else {}

    # -- GET ---------------------------------------------------------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path, q = u.path, urllib.parse.parse_qs(u.query)
        try:
            if path == "/":
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if path == "/api/state":
                deep = q.get("deep", ["0"])[0] == "1"
                with LOCK:
                    return self._json(build_state(deep=deep))
            if path.startswith("/img/live/"):
                _, _, _, cid, itype = path.split("/", 4)
                data, ctype = fetch_image(cid, itype)
                if not data:
                    return self._send(404, b"", "text/plain")
                return self._send(200, data, ctype,
                                  {"Cache-Control": "no-cache"})
            if path == "/api/rules":
                return self._json(read_rules())
            if path == "/api/library":
                with LOCK:
                    return self._json({"images": build_library()})
            if path.startswith("/img/lib/"):
                sha = path.rsplit("/", 1)[-1]
                e = library_lookup(sha)
                if not e:
                    return self._send(404, b"", "text/plain")
                fp = os.path.join(STATE["vault"], IMAGE_DIR, e["file"])
                if not os.path.exists(fp):
                    return self._send(404, b"", "text/plain")
                with open(fp, "rb") as fh:
                    return self._send(200, fh.read(), e["content_type"],
                                      {"Cache-Control": "max-age=300"})
            if path.startswith("/img/vault/"):
                _, _, _, key, itype = path.split("/", 4)
                m = read_manifest()
                rec = ((m["channels"].get(key) or {}).get("images") or {}).get(itype)
                if not rec:
                    return self._send(404, b"", "text/plain")
                data = vault_read_image(rec)
                if data is None:
                    return self._send(404, b"", "text/plain")
                return self._send(200, data, rec["content_type"],
                                  {"Cache-Control": "no-cache"})
        except RuntimeError as e:
            return self._json({"error": str(e)}, 502)
        except Exception as e:
            return self._json({"error": repr(e)}, 500)
        self._send(404, b"not found", "text/plain")

    # -- POST --------------------------------------------------------
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/backup":
                items = body.get("items")
                targets = [(i["key"], i["type"]) for i in items] if items else None
                with LOCK:
                    return self._json(do_backup(targets))
            if path == "/api/restore":
                targets = [(i["key"], i["type"]) for i in body.get("items", [])]
                with LOCK:
                    return self._json({"results": do_restore(targets, body.get("force", False))})
            if path in ("/api/copy_slot", "/api/copy_primary"):
                with LOCK:
                    return self._json(do_copy_slot(
                        body.get("keys", []),
                        body.get("source", "Primary"),
                        body.get("targets"),
                        body.get("overwrite", False)))
            if path == "/api/rules":
                with LOCK:
                    write_rules({"groups": body.get("groups", []),
                                 "patterns": body.get("patterns", [])})
                    return self._json(read_rules())
            if path == "/api/assign":
                with LOCK:
                    return self._json(do_assign_from_library(
                        body["key"], body["type"], body["sha"],
                        body.get("name"), body.get("id"), body.get("number")))
            if path == "/api/vault_set":
                return self._json(self._vault_set(body))
            if path == "/api/vault_clear":
                with LOCK:
                    m = read_manifest()
                    imgs = (m["channels"].get(body["key"]) or {}).get("images") or {}
                    imgs.pop(body["type"], None)
                    write_manifest(m)
                return self._json({"ok": True})
        except RuntimeError as e:
            return self._json({"error": str(e)}, 502)
        except Exception as e:
            return self._json({"error": repr(e)}, 500)
        self._send(404, b"not found", "text/plain")

    def _vault_set(self, body):
        """Put custom artwork into the vault for one channel and slot."""
        key, itype = body["key"], body["type"]
        raw = base64.b64decode(body["data"])
        ctype = body.get("content_type") or "image/png"
        if ctype not in MIME_EXT:
            ctype = "image/png"
        with LOCK:
            m = read_manifest()
            entry = m["channels"].setdefault(key, {
                "name": body.get("name") or key, "emby_id": body.get("id"),
                "number": body.get("number"), "images": {},
            })
            entry["images"][itype] = vault_write_image(
                safe_stem(entry.get("name")) + "_" + key[:8], itype,
                raw, ctype, tag=None, origin="custom")
            write_manifest(m)
        return {"ok": True, "bytes": len(raw)}


# ----------------------------------------------------------------------
# Web page
# ----------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Channel logo vault</title>
<style>
:root{
  /* Neutral grey surround, the way image editors frame artwork under review,
     so neither the light nor the dark preview swatch wins on contrast. */
  --surround:#4a4d52;
  --panel:#3e4146;
  --edge:#5e6268;
  --ink:#f1f2f3;
  --ink-dim:#b9bcc1;
  --drift:#f0b429;
  --absent:#e8695d;
  --held:#7fb3a4;
  --focus:#9ec9ff;
  --tile-light:#ffffff;
  --tile-dark:#0d0f12;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  background:var(--surround);color:var(--ink);
  font:400 14px/1.45 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  font-variant-numeric:tabular-nums;
}
button,input,select{font:inherit;color:inherit}
button{cursor:pointer}

header{
  position:sticky;top:0;z-index:20;background:var(--panel);
  border-bottom:1px solid var(--edge);
  padding:12px 20px;display:flex;gap:18px;align-items:center;flex-wrap:wrap;
}
h1{font-size:15px;font-weight:650;margin:0;letter-spacing:.005em}
.meta{font-size:12px;color:var(--ink-dim)}
.spacer{flex:1}

.seg{display:flex;border:1px solid var(--edge);border-radius:7px;overflow:hidden}
.seg button{background:transparent;border:0;padding:6px 13px;font-size:13px;color:var(--ink-dim)}
.seg button[aria-pressed=true]{background:var(--surround);color:var(--ink);font-weight:500}
.seg button+button{border-left:1px solid var(--edge)}

.btn{background:var(--surround);border:1px solid var(--edge);border-radius:7px;
     padding:6px 14px;font-size:13px}
.btn:hover{border-color:#7d828a}
.btn.go{background:#5b7f74;border-color:#6d968a;font-weight:500}
.btn.go:disabled{background:var(--surround);border-color:var(--edge);color:#8a8e94;cursor:default}
.btn:focus-visible,.seg button:focus-visible,input:focus-visible{
  outline:2px solid var(--focus);outline-offset:2px}

input[type=search]{background:var(--surround);border:1px solid var(--edge);
  border-radius:7px;padding:6px 11px;width:200px}

#selbar{position:sticky;top:57px;z-index:12;background:var(--surround);
  border-bottom:1px solid #565a60;padding:9px 20px;display:flex;gap:8px;
  align-items:center;flex-wrap:wrap}
#selbar .lbl{font-size:12.5px;color:var(--ink-dim);margin-right:2px}
#selbar button{background:transparent;border:1px solid var(--edge);border-radius:6px;
  padding:4px 11px;font-size:12.5px;color:var(--ink-dim)}
#selbar button:hover{color:var(--ink);border-color:#7d828a}
#selbar button[aria-pressed=true]{background:var(--panel);color:var(--ink);
  border-color:var(--focus);font-weight:500}
#selbar .scope{font-size:11.5px;color:#8f9399;margin-left:auto}
.chan{display:flex;gap:9px;align-items:flex-start}
.chan .meta{min-width:0;flex:1}
.rowbox{margin-top:5px;width:15px;height:15px;flex:none;accent-color:var(--focus)}
#grid{padding:8px 20px 120px}
.head{display:grid;grid-template-columns:220px repeat(3,1fr);gap:14px;
  padding:14px 0 8px;position:sticky;top:57px;background:var(--surround);z-index:10}
.head div{font-size:13px;font-weight:500}
.head span{display:block;font-size:11.5px;color:var(--ink-dim);font-weight:400}

.row{display:grid;grid-template-columns:220px repeat(3,1fr);gap:14px;
  padding:10px 0;border-top:1px solid #565a60;align-items:start}
.chan{padding-top:4px;min-width:0}
.num{font-size:12px;color:var(--ink-dim)}
.nm{font-weight:500;overflow-wrap:anywhere}
.absent-note{font-size:11.5px;color:var(--absent)}

.cell{border:1px solid transparent;border-radius:9px;padding:6px;position:relative}
.cell.sel{border-color:var(--focus);background:rgba(158,201,255,.09)}
.cell.drop{border-color:var(--held);border-style:dashed}
.pair{display:flex;gap:8px;align-items:stretch}
.tile{flex:1;min-width:0;border-radius:6px;height:66px;display:flex;
  align-items:center;justify-content:center;padding:6px;position:relative}
.tile img{max-width:100%;max-height:100%;object-fit:contain;display:block}
body[data-bg=light] .tile{background:var(--tile-light)}
body[data-bg=dark] .tile{background:var(--tile-dark)}
body[data-bg=check] .tile{
  background-color:#fff;
  background-image:linear-gradient(45deg,#c9ccd1 25%,transparent 25%,transparent 75%,#c9ccd1 75%),
                   linear-gradient(45deg,#c9ccd1 25%,transparent 25%,transparent 75%,#c9ccd1 75%);
  background-size:14px 14px;background-position:0 0,7px 7px}
.tile.void{background:#54585e;border:1px dashed #71767d}
.void em{font-style:normal;font-size:11.5px;color:var(--ink-dim)}
.tag{position:absolute;bottom:3px;left:5px;font-size:10px;padding:0 4px;
  border-radius:3px;background:rgba(0,0,0,.55);color:#fff}
body[data-bg=light] .tag{background:rgba(255,255,255,.75);color:#222}

.state{margin-top:5px;display:flex;gap:8px;align-items:center;font-size:11.5px}
.dot{width:7px;height:7px;border-radius:50%;flex:none}
.s-match .dot{background:var(--held)}
.s-differs .dot{background:var(--drift)}
.s-unsaved .dot{background:var(--ink-dim)}
.s-gone .dot,.s-orphan .dot{background:var(--absent)}
.s-empty{color:#8f9399}
.s-empty .dot{background:#71767d}
.acts{margin-left:auto;display:flex;gap:6px;opacity:0;transition:opacity .12s}
.cell:hover .acts,.cell:focus-within .acts{opacity:1}
.acts button{background:transparent;border:1px solid var(--edge);border-radius:5px;
  font-size:11px;padding:1px 7px;color:var(--ink-dim)}
.acts button:hover{color:var(--ink);border-color:#7d828a}

.pop{position:absolute;right:6px;top:100%;margin-top:2px;z-index:30;
  background:var(--panel);border:1px solid var(--edge);border-radius:8px;
  padding:5px;display:flex;flex-direction:column;gap:3px;min-width:150px;
  box-shadow:0 6px 18px rgba(0,0,0,.35)}
.pop button{background:transparent;border:0;text-align:left;padding:5px 8px;
  border-radius:5px;font-size:12.5px;color:var(--ink)}
.pop button:hover{background:var(--surround)}
.pop .hint{font-size:11px;color:var(--ink-dim);padding:2px 8px 4px}

.row.off{opacity:.42}
.row.off .cell{pointer-events:none}
.exnote{font-size:11.5px;color:var(--drift)}
.grouplist{overflow:auto;padding:4px 18px 14px;display:grid;gap:2px}
.grow{display:flex;align-items:center;gap:10px;padding:7px 9px;border-radius:7px}
.grow:hover{background:var(--surround)}
.grow input{accent-color:var(--drift);width:15px;height:15px;flex:none}
.grow .gn{flex:1;overflow-wrap:anywhere}
.grow .gc{font-size:12px;color:var(--ink-dim)}
.patbox{padding:12px 18px 16px;border-top:1px solid var(--edge)}
.patbox textarea{width:100%;min-height:66px;background:var(--surround);
  border:1px solid var(--edge);border-radius:7px;padding:8px 10px;resize:vertical;
  font:inherit;color:var(--ink)}
.patbox p{font-size:12px;color:var(--ink-dim);margin:0 0 7px}
.veil{position:fixed;inset:0;background:rgba(15,17,20,.62);z-index:60;
  display:flex;align-items:center;justify-content:center;padding:24px}
[hidden]{display:none!important}
body.locked{overflow:hidden}
.smsg{font-size:12.5px;color:var(--drift);padding:9px 18px 0;overflow-wrap:anywhere}
.sheet{background:var(--panel);border:1px solid var(--edge);border-radius:12px;
  width:min(920px,100%);max-height:86vh;display:flex;flex-direction:column}
.sheet header{position:static;background:transparent;border:0;
  border-bottom:1px solid var(--edge);padding:14px 18px}
.sheet h2{font-size:14px;font-weight:650;margin:0}
.sheet .sub{font-size:12px;color:var(--ink-dim);margin-top:2px}
.libgrid{overflow:auto;padding:14px 18px 18px;display:grid;gap:12px;
  grid-template-columns:repeat(auto-fill,minmax(132px,1fr))}
.lib{border:1px solid var(--edge);border-radius:9px;padding:7px;background:transparent;
  display:flex;flex-direction:column;gap:6px;text-align:left}
.lib:hover,.lib:focus-visible{border-color:var(--focus)}
.lib .swatch{height:62px;border-radius:6px;display:flex;align-items:center;
  justify-content:center;padding:6px}
body[data-bg=light] .lib .swatch{background:var(--tile-light)}
body[data-bg=dark] .lib .swatch{background:var(--tile-dark)}
body[data-bg=check] .lib .swatch{background-color:#fff;
  background-image:linear-gradient(45deg,#c9ccd1 25%,transparent 25%,transparent 75%,#c9ccd1 75%),
                   linear-gradient(45deg,#c9ccd1 25%,transparent 25%,transparent 75%,#c9ccd1 75%);
  background-size:14px 14px;background-position:0 0,7px 7px}
.lib img{max-width:100%;max-height:100%;object-fit:contain}
.lib .cap{font-size:11.5px;line-height:1.3;overflow-wrap:anywhere;color:var(--ink-dim)}
footer{position:fixed;bottom:0;left:0;right:0;background:var(--panel);
  border-top:1px solid var(--edge);padding:11px 20px;display:flex;gap:14px;
  align-items:center;z-index:20}
#log{font-size:12px;color:var(--ink-dim);max-height:34px;overflow:auto;flex:1}
.empty{padding:60px 20px;text-align:center;color:var(--ink-dim)}
@media (max-width:900px){
  .head,.row{grid-template-columns:1fr}
  .head{display:none}
  .cell::before{content:attr(data-label);font-size:11.5px;color:var(--ink-dim);
    display:block;margin-bottom:4px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
</head>
<body data-bg="dark">

<header>
  <h1>Channel logo vault</h1>
  <div class="seg" role="group" aria-label="Preview background">
    <button data-bg="light">Light</button>
    <button data-bg="dark" aria-pressed="true">Dark</button>
    <button data-bg="check">Checker</button>
  </div>
  <div class="seg" role="group" aria-label="Show">
    <button data-view="show" data-v="all" aria-pressed="true">All</button>
    <button data-view="show" data-v="drift">Changed</button>
    <button data-view="show" data-v="unsaved">Not saved</button>
    <button data-view="show" data-v="edited">Edited</button>
    <button data-view="show" data-v="excluded">Excluded</button>
  </div>
  <input type="search" id="q" placeholder="Filter channels" aria-label="Filter channels">
  <div class="spacer"></div>
  <button class="btn" id="groups">Groups</button>
  <button class="btn" id="fill">Fill blanks from Primary</button>
  <button class="btn" id="deep">Deep scan</button>
  <button class="btn" id="backupAll">Back up everything</button>
  <div class="meta" id="mstamp"></div>
</header>

<div id="selbar">
  <span class="lbl">Select</span>
  <button data-sel="all">Every cell</button>
  <button data-sel="Primary">All primary</button>
  <button data-sel="LogoLight">All logo light</button>
  <button data-sel="LogoLightColor">All logo light color</button>
  <button data-sel="none">Clear</button>
  <span class="scope" id="scope"></span>
</div>

<div id="grid"><div class="empty">Loading channels from Emby...</div></div>

<footer>
  <button class="btn go" id="push" disabled>Push selected to Emby</button>
  <button class="btn" id="save" disabled>Save selected to vault</button>
  <button class="btn" id="clear" disabled>Clear selection</button>
  <div id="log">Select any cell to stage it. Drop an image file on a cell to replace the stored artwork.</div>
</footer>

<input type="file" id="picker" accept="image/*" hidden>
<div id="veil" hidden></div>

<script>
const TYPES=["Primary","LogoLight","LogoLightColor"];
let DATA=null, SEL=new Set(), SHOW="all", Q="", pickTarget=null;

function say(t){
  document.getElementById("log").textContent=t;
  const m=document.getElementById("smsg");   // footer is behind the dim layer
  if(m){m.textContent=t; m.hidden=!t;}
}
const cellId=(k,t)=>k+"|"+t;
const hasArt=s=>!!(s.vault||s.live);
let BYKEY={};

// Cells worth acting on, limited to what the current filter shows.
function cellsFor(type){
  const out=[];
  for(const ch of DATA.channels.filter(visible))
    for(const t of TYPES)
      if((!type||t===type)&&hasArt(ch.slots[t])) out.push(cellId(ch.key,t));
  return out;
}

function bulkSelect(type){
  const ids=cellsFor(type);
  if(!ids.length){say("Nothing on screen to select. Try clearing the filter.");return;}
  const allOn=ids.every(i=>SEL.has(i));
  for(const i of ids) allOn?SEL.delete(i):SEL.add(i);
  render();
  say(allOn?("Deselected "+ids.length+" cells."):(SEL.size+" cells selected."));
}

// How many of the selection each action can actually act on.
function actionable(kind){
  let n=0;
  for(const id of SEL){
    const i=id.lastIndexOf("|");
    const ch=BYKEY[id.slice(0,i)];
    if(!ch) continue;
    const s=ch.slots[id.slice(i+1)];
    if(s && (kind==="push"?s.vault:s.live)) n++;
  }
  return n;
}

async function jget(u){const r=await fetch(u);const j=await r.json();
  if(j.error)throw new Error(j.error);return j;}
async function jpost(u,b){const r=await fetch(u,{method:"POST",
  headers:{"Content-Type":"application/json"},body:JSON.stringify(b||{})});
  const j=await r.json(); if(j.error)throw new Error(j.error); return j;}

let DEEP=false;
async function load(){
  try{ DATA=await jget("/api/state"+(DEEP?"?deep=1":"")); render(); }
  catch(e){ document.getElementById("grid").innerHTML=
    '<div class="empty">Could not read from Emby.<br>'+e.message+'</div>'; }
}

function visible(ch){
  if(ch.excluded && SHOW!=="excluded")return false;
  if(SHOW==="excluded")return ch.excluded;
  if(Q && !(ch.name.toLowerCase().includes(Q)||String(ch.number).includes(Q)))return false;
  if(SHOW==="all")return true;
  return TYPES.some(t=>{
    const s=ch.slots[t];
    if(SHOW==="drift")return s.status==="differs"||s.status==="gone";
    if(SHOW==="unsaved")return s.status==="unsaved";
    if(SHOW==="edited")return ["custom","copy","library"].includes(s.origin);
    return true;
  });
}

const WORD={match:"Matches vault",differs:"Changed in Emby",unsaved:"Not in vault",
  gone:"Missing from Emby",orphan:"Vault only",empty:"No artwork"};

function tile(ch,t,which){
  const s=ch.slots[t], bust="?v="+Date.now();
  if(which==="live"){
    if(!s.live)return '<div class="tile void"><em>none in Emby</em></div>';
    return '<div class="tile"><img loading="lazy" alt="'+esc(ch.name)+' '+t+' in Emby" src="/img/live/'+ch.id+'/'+t+'?t='+encodeURIComponent(s.live_tag||"")+'"><span class="tag">Emby</span></div>';
  }
  if(!s.vault)return '<div class="tile void"><em>none in vault</em></div>';
  return '<div class="tile"><img loading="lazy" alt="'+esc(ch.name)+' '+t+' in vault" src="/img/vault/'+ch.key+'/'+t+bust+'"><span class="tag">'+esc(ORIGIN(s.origin,s.source))+'</span></div>';
}

const SHORT={Primary:"Primary",LogoLight:"Light",LogoLightColor:"Light color"};
function ORIGIN(o,src){
  if(o==="custom")return "Custom";
  if(o==="copy")return "From "+(SHORT[src]||"another slot");
  if(o==="library")return "From "+(src||"vault");
  return "Vault";
}
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function render(){
  const g=document.getElementById("grid");
  BYKEY={}; for(const c of DATA.channels) BYKEY[c.key]=c;
  const rows=DATA.channels.filter(visible);
  const sc=document.getElementById("scope");
  if(sc) sc.textContent=rows.length+" channels on screen"+
    (rows.length<DATA.channels.length?" of "+DATA.channels.length:"");
  document.getElementById("mstamp").textContent =
    DATA.manifest.updated ? "Vault updated "+DATA.manifest.updated.replace("T"," ").replace("+00:00"," UTC")
                          : "Vault is empty";
  if(!rows.length){g.innerHTML='<div class="empty">Nothing matches that filter.</div>';return;}

  let h='<div class="head"><div>Channel</div>';
  for(const t of TYPES)h+='<div>'+DATA.labels[t]+'<span>'+DATA.notes[t]+'</span></div>';
  h+='</div>';

  for(const ch of rows){
    const rowIds=TYPES.filter(t=>hasArt(ch.slots[t])).map(t=>cellId(ch.key,t));
    const rowOn=rowIds.length&&rowIds.every(i=>SEL.has(i));
    h+='<div class="row'+(ch.excluded?" off":"")+'"><div class="chan">'+
       '<input type="checkbox" class="rowbox" data-row="'+esc(ch.key)+'"'+
       (rowOn?" checked":"")+(rowIds.length?"":" disabled")+
       ' aria-label="Select every slot for '+esc(ch.name)+'">'+
       '<div class="meta"><div class="num">'+esc(ch.number||"")+'</div>'+
       '<div class="nm">'+esc(ch.name)+'</div>'+
       (ch.in_emby?"":'<div class="absent-note">not in Emby now</div>')+
       (ch.excluded?'<div class="exnote">excluded</div>':'')+'</div></div>';
    for(const t of TYPES){
      const s=ch.slots[t], id=cellId(ch.key,t);
      h+='<div class="cell'+(SEL.has(id)?" sel":"")+'" data-id="'+id+'" data-label="'+DATA.labels[t]+'" tabindex="0" role="button" aria-pressed="'+SEL.has(id)+'">'+
         '<div class="pair">'+tile(ch,t,"live")+tile(ch,t,"vault")+'</div>'+
         '<div class="state s-'+s.status+'"><span class="dot"></span>'+WORD[s.status]+
         '<span class="acts">'+
           '<button data-act="copy" title="Copy this artwork into another slot">Copy to</button>'+
           '<button data-act="library" title="Pick artwork already held in the vault">From vault</button>'+
           '<button data-act="replace" title="Upload a file from this computer">'+(s.vault?'Replace':'Upload')+'</button>'+
           (s.vault?'<button data-act="clear" title="Drop this from the vault">Forget</button>':'')+
         '</span></div></div>';
    }
    h+='</div>';
  }
  g.innerHTML=h;
  syncButtons();
}

function syncButtons(){
  const n=SEL.size, np=actionable("push"), ns=actionable("save");
  document.getElementById("push").disabled=!np;
  document.getElementById("save").disabled=!ns;
  document.getElementById("clear").disabled=!n;
  document.getElementById("push").textContent = n?("Push "+np+" to Emby"):"Push selected to Emby";
  document.getElementById("save").textContent = n?("Save "+ns+" to vault"):"Save selected to vault";
  for(const b of document.querySelectorAll("#selbar button[data-sel]")){
    const k=b.dataset.sel;
    if(k==="none"){b.setAttribute("aria-pressed","false");continue;}
    const ids=cellsFor(k==="all"?null:k);
    b.setAttribute("aria-pressed", ids.length&&ids.every(i=>SEL.has(i)));
  }
}

function toggle(id){ SEL.has(id)?SEL.delete(id):SEL.add(id);
  const el=document.querySelector('[data-id="'+CSS.escape(id)+'"]');
  if(el){el.classList.toggle("sel",SEL.has(id));el.setAttribute("aria-pressed",SEL.has(id));}
  syncButtons(); }

for(const b of document.querySelectorAll("#selbar button[data-sel]")){
  b.addEventListener("click",()=>{
    const k=b.dataset.sel;
    if(k==="none"){SEL.clear();render();say("Selection cleared.");return;}
    bulkSelect(k==="all"?null:k);
  });
}

document.getElementById("grid").addEventListener("change",e=>{
  const box=e.target.closest(".rowbox"); if(!box)return;
  const ch=BYKEY[box.dataset.row]; if(!ch)return;
  const ids=TYPES.filter(t=>hasArt(ch.slots[t])).map(t=>cellId(ch.key,t));
  for(const i of ids) box.checked?SEL.add(i):SEL.delete(i);
  render();
});

document.getElementById("grid").addEventListener("click",e=>{
  if(e.target.closest(".rowbox"))return;
  const act=e.target.closest("button[data-act]");
  const cell=e.target.closest(".cell");
  if(!cell)return;
  if(act){
    e.stopPropagation();
    const [k,t]=cell.dataset.id.split("|");
    const a=act.dataset.act;
    if(a==="replace"){pickTarget=[k,t];document.getElementById("picker").click();}
    else if(a==="copy"){openCopyMenu(cell,k,t);}
    else if(a==="library"){openLibrary(k,t);}
    else jpost("/api/vault_clear",{key:k,type:t}).then(()=>{say("Removed from vault.");load();});
    return;
  }
  toggle(cell.dataset.id);
});
document.getElementById("grid").addEventListener("keydown",e=>{
  if(e.key!==" "&&e.key!=="Enter")return;
  const cell=e.target.closest(".cell"); if(!cell)return;
  e.preventDefault(); toggle(cell.dataset.id);
});

function closePop(){document.querySelectorAll(".pop").forEach(p=>p.remove());}
document.addEventListener("click",e=>{
  if(!e.target.closest(".pop")&&!e.target.closest('[data-act="copy"]'))closePop();},true);

function openCopyMenu(cell,key,srcType){
  closePop();
  const ch=DATA.channels.find(c=>c.key===key);
  const p=document.createElement("div");
  p.className="pop";
  const has=ch.slots[srcType].vault||ch.slots[srcType].live;
  if(!has){
    p.innerHTML='<div class="hint">This slot has no artwork to copy.</div>';
  }else{
    p.innerHTML='<div class="hint">Copy this artwork into</div>'+
      TYPES.filter(t=>t!==srcType).map(t=>'<button data-t="'+t+'">'+DATA.labels[t]+'</button>').join("");
    p.addEventListener("click",async ev=>{
      const b=ev.target.closest("button[data-t]"); if(!b)return;
      closePop();
      say("Copying...");
      try{
        const r=await jpost("/api/copy_slot",{keys:[key],source:srcType,targets:[b.dataset.t],overwrite:true});
        say(r.copied?("Copied into "+DATA.labels[b.dataset.t].toLowerCase()+
             ". Check it against the background it will actually appear on, then push."):
             "Nothing to copy from that slot.");
        load();
      }catch(e){say("Copy failed. "+e.message);}
    });
  }
  cell.appendChild(p);
}

// ---- groups panel ----
document.getElementById("groups").addEventListener("click",openGroups);

function openGroups(){
  const gs=DATA.groups||[], pats=(DATA.rules&&DATA.rules.patterns)||[];
  openSheet(
    '<header><h2>Groups to skip</h2><div class="sub">'+
    (gs.length? 'Ticked groups are left out of Back up everything, Fill blanks and the command line. '+
                'Per cell buttons still work on them.'
              : 'Emby is not reporting any groups for your channels, so use name patterns below.')+
    '</div></header>'+
    (gs.length?'<div class="grouplist" id="glist">'+gs.map(g=>
      '<label class="grow"><input type="checkbox" data-g="'+esc(g.name)+'"'+(g.excluded?" checked":"")+'>'+
      '<span class="gn">'+esc(g.name)+'</span><span class="gc">'+g.count+'</span></label>').join("")+'</div>':"")+
    '<div class="patbox"><p>Skip by name as well. One rule per line, matched anywhere in the channel name. '+
    'Start a line with re: for a regular expression.</p>'+
    '<textarea id="pats" spellcheck="false" placeholder="24/7&#10;PPV&#10;re:^VOD ">'+esc(pats.join("\n"))+'</textarea>'+
    '<div style="display:flex;gap:10px;margin-top:11px">'+
    '<button class="btn go" id="gsave">Save and reload</button>'+
    '<button class="btn" id="gx">Cancel</button></div></div>',
    "Choose which groups to skip");
  document.getElementById("gx").onclick=closeSheet;
  document.getElementById("gsave").onclick=async()=>{
    const groups=[...document.querySelectorAll('#glist input:checked')].map(i=>i.dataset.g);
    const patterns=document.getElementById("pats").value.split("\n").map(x=>x.trim()).filter(Boolean);
    try{
      await jpost("/api/rules",{groups,patterns});
      closeSheet(); SEL.clear(); await load();
      say(DATA.excluded_count+" channels are now skipped by bulk operations.");
    }catch(e){say("Could not save those rules. "+e.message);}
  };
}

// ---- vault picker ----
let LIB=[];
async function openLibrary(key,type){
  const ch=DATA.channels.find(c=>c.key===key)||{};
  openSheet(
    '<header><h2>Artwork in the vault</h2>'+
    '<div class="sub">Choosing one sets the '+DATA.labels[type].toLowerCase()+' slot for '+esc(ch.name||key)+'. '+
    'Nothing reaches Emby until you push.</div>'+
    '<div style="margin-top:10px;display:flex;gap:10px">'+
    '<input type="search" id="libq" placeholder="Filter by channel name" style="flex:1" aria-label="Filter artwork">'+
    '<button class="btn" id="libx">Close</button></div></header>'+
    '<div class="libgrid" id="libgrid">Loading...</div>',
    "Pick artwork from the vault");
  libTarget=[key,type];                 // after openSheet, which clears stale targets
  document.getElementById("libx").onclick=closeSheet;
  document.getElementById("libq").oninput=e=>drawLibrary(e.target.value.trim().toLowerCase());
  try{ LIB=(await jget("/api/library")).images; drawLibrary(""); }
  catch(e){ document.getElementById("libgrid").textContent="Could not read the vault. "+e.message; }
}
let libTarget=null;
const veilEl=()=>document.getElementById("veil");
const sheetOpen=()=>!veilEl().hidden;
let lastFocus=null;

const FOCUSABLE='a[href],button:not([disabled]),input:not([disabled]),'+
                'textarea:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])';

function openSheet(inner,label){
  closePop();
  libTarget=null;                       // never inherit a target from a previous sheet
  const v=veilEl();
  if(!sheetOpen()) lastFocus=document.activeElement;
  v.hidden=false; v.className="veil";
  v.innerHTML='<div class="sheet" role="dialog" aria-modal="true" aria-label="'+label+'">'+
              inner+'<div class="smsg" id="smsg" hidden></div></div>';
  document.body.classList.add("locked");
  v.onclick=e=>{if(e.target===v)closeSheet();};
  const first=v.querySelector(FOCUSABLE);
  if(first) first.focus();
}

function closeSheet(){
  const v=veilEl();
  if(v.hidden) return;
  v.hidden=true; v.className=""; v.innerHTML=""; v.onclick=null;
  document.body.classList.remove("locked");
  libTarget=null;
  if(lastFocus && lastFocus.isConnected) lastFocus.focus();
  lastFocus=null;
}
const closeLibrary=closeSheet;

document.addEventListener("keydown",e=>{
  if(e.key==="Escape"){closePop(); closeSheet(); return;}
  if(e.key!=="Tab" || !sheetOpen()) return;
  const f=[...veilEl().querySelectorAll(FOCUSABLE)].filter(x=>x.offsetParent!==null);
  if(!f.length) return;
  const first=f[0], last=f[f.length-1];
  if(e.shiftKey && document.activeElement===first){e.preventDefault(); last.focus();}
  else if(!e.shiftKey && document.activeElement===last){e.preventDefault(); first.focus();}
});

function drawLibrary(q){
  const g=document.getElementById("libgrid"); if(!g)return;
  const rows=LIB.filter(e=>!q||e.channels.some(c=>c.toLowerCase().includes(q)));
  if(!rows.length){g.innerHTML='<div style="color:var(--ink-dim)">Nothing in the vault matches. Back up some logos first.</div>';return;}
  g.innerHTML=rows.map(e=>
    '<button class="lib" data-sha="'+e.sha+'">'+
    '<div class="swatch"><img loading="lazy" alt="'+esc(e.channels[0])+'" src="/img/lib/'+e.sha+'"></div>'+
    '<div class="cap">'+esc(e.channels.slice(0,2).join(", "))+
      (e.channels.length>2?" +"+(e.channels.length-2):"")+'</div></button>').join("");
  g.onclick=async ev=>{
    const b=ev.target.closest(".lib"); if(!b||!libTarget)return;
    const [k,t]=libTarget;
    const ch=DATA.channels.find(c=>c.key===k)||{};
    try{
      const r=await jpost("/api/assign",{key:k,type:t,sha:b.dataset.sha,
        name:ch.name,id:ch.id,number:ch.number});
      if(!r.ok){say(r.msg);return;}
      closeSheet(); load();
      say("Set from "+r.from+". Select the cell and push it when it looks right.");
    }catch(e){say("Could not assign that. "+e.message);}
  };
}

// Drop artwork straight onto a cell.
document.getElementById("grid").addEventListener("dragover",e=>{
  const c=e.target.closest(".cell"); if(!c)return;
  e.preventDefault(); c.classList.add("drop");});
document.getElementById("grid").addEventListener("dragleave",e=>{
  const c=e.target.closest(".cell"); if(c)c.classList.remove("drop");});
document.getElementById("grid").addEventListener("drop",e=>{
  const c=e.target.closest(".cell"); if(!c)return;
  e.preventDefault(); c.classList.remove("drop");
  const f=e.dataTransfer.files[0]; if(!f)return;
  const [k,t]=c.dataset.id.split("|"); sendFile(k,t,f);});

document.getElementById("picker").addEventListener("change",e=>{
  const f=e.target.files[0]; if(f&&pickTarget)sendFile(pickTarget[0],pickTarget[1],f);
  e.target.value=""; pickTarget=null;});

function sendFile(key,type,file){
  const ch=DATA.channels.find(c=>c.key===key)||{};
  const fr=new FileReader();
  fr.onload=async()=>{
    try{
      await jpost("/api/vault_set",{key,type,name:ch.name,id:ch.id,number:ch.number,
        content_type:file.type||"image/png",data:fr.result.split(",")[1]});
      say("Stored "+file.name+" as the vault copy. Select the cell and push it to apply.");
      load();
    }catch(err){say("Could not store that file. "+err.message);}
  };
  fr.readAsDataURL(file);
}

function staged(kind){
  const out=[];
  for(const id of SEL){
    const i=id.lastIndexOf("|"), k=id.slice(0,i), t=id.slice(i+1);
    const ch=BYKEY[k]; if(!ch) continue;
    const s=ch.slots[t]; if(!s) continue;
    if(kind==="push"?s.vault:s.live) out.push({key:k,type:t});
  }
  return out;
}

document.getElementById("push").addEventListener("click",async()=>{
  const items=staged("push");
  const dropped=SEL.size-items.length;
  if(!items.length){say("None of the selected cells hold vault artwork to push.");return;}
  say("Pushing "+items.length+" to Emby"+(dropped?" ("+dropped+" skipped, no vault artwork)":"")+"...");
  try{
    const r=await jpost("/api/restore",{items,force:true});
    const bad=r.results.filter(x=>!x.ok);
    say("Pushed "+(r.results.length-bad.length)+" of "+r.results.length+
        (bad.length?". Failed: "+bad.map(b=>b.key+" "+b.type+" ("+b.msg+")").join(", "):"."));
    SEL.clear(); load();
  }catch(e){say("Push failed. "+e.message);}
});

document.getElementById("save").addEventListener("click",async()=>{
  const items=staged("save");
  const dropped=SEL.size-items.length;
  if(!items.length){say("None of the selected cells have artwork in Emby to save.");return;}
  say("Saving "+items.length+" to the vault"+(dropped?" ("+dropped+" skipped, nothing in Emby)":"")+"...");
  try{const r=await jpost("/api/backup",{items});
    say("Saved "+r.saved+", skipped "+r.failed+".");SEL.clear();load();}
  catch(e){say("Save failed. "+e.message);}
});

document.getElementById("clear").addEventListener("click",()=>{SEL.clear();render();});

document.getElementById("fill").addEventListener("click",async()=>{
  const rows=DATA.channels.filter(visible);
  const keys=rows.filter(c=>TYPES.some(t=>t!=="Primary"&&!c.slots[t].vault)).map(c=>c.key);
  if(!keys.length){say("Every channel on screen already has artwork in both logo slots.");return;}
  say("Copying Primary into empty logo slots for "+keys.length+" channels...");
  try{
    const r=await jpost("/api/copy_slot",{keys,source:"Primary",overwrite:false});
    say("Filled "+r.copied+" empty slots. "+r.no_source+" channels had no Primary to copy. "+
        "Switch the preview to Dark and check them before pushing.");
    await load();
  }catch(e){say("Fill failed. "+e.message);}
});

document.getElementById("deep").addEventListener("click",async()=>{
  DEEP=true;
  say("Checking every image slot directly. Slower, but it catches logos Emby leaves out of the channel list...");
  await load();
  say("Deep scan done. Slots are now read from Emby itself rather than its summary.");
});

document.getElementById("backupAll").addEventListener("click",async()=>{
  say("Reading every logo out of Emby. This takes a moment on a large guide...");
  try{const r=await jpost("/api/backup",{});say("Vault now holds "+r.saved+" images. "+r.failed+" could not be read.");load();}
  catch(e){say("Backup failed. "+e.message);}
});

for(const b of document.querySelectorAll("[data-bg]")){
  b.addEventListener("click",()=>{
    document.body.dataset.bg=b.dataset.bg;
    document.querySelectorAll("[data-bg]").forEach(x=>x.setAttribute("aria-pressed",x===b));
  });
}
for(const b of document.querySelectorAll("[data-view=show]")){
  b.addEventListener("click",()=>{
    SHOW=b.dataset.v;
    document.querySelectorAll("[data-view=show]").forEach(x=>x.setAttribute("aria-pressed",x===b));
    render();
  });
}
let qt; document.getElementById("q").addEventListener("input",e=>{
  clearTimeout(qt); qt=setTimeout(()=>{Q=e.target.value.trim().toLowerCase();render();},160);});

load();
</script>
</body>
</html>
"""


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def configure(args):
    STATE["base"] = (args.url or os.environ.get("EMBY_URL") or "").rstrip("/")
    STATE["key"] = args.key or os.environ.get("EMBY_API_KEY")
    STATE["vault"] = args.vault
    if not STATE["base"] or not STATE["key"]:
        raise SystemExit("Set EMBY_URL and EMBY_API_KEY, or pass --url and --key.")
    os.makedirs(STATE["vault"], exist_ok=True)


def cmd_serve(args):
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = "http://{}:{}".format(
        "localhost" if args.host in ("0.0.0.0", "") else args.host, args.port)
    print("Emby      {}".format(STATE["base"]))
    print("Vault     {}".format(os.path.abspath(STATE["vault"])))
    print("Interface {}".format(url))
    print("Ctrl+C to stop.")
    if args.open:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


def cmd_backup(args):
    r = do_backup()
    print("Saved {} images, {} could not be read.".format(r["saved"], r["failed"]))
    print("Vault: {}".format(os.path.abspath(STATE["vault"])))


def cmd_fill(args):
    st = build_state()
    others = [t for t in IMAGE_TYPES if t != args.source]
    keys = [ch["key"] for ch in st["channels"] if not ch["excluded"]
            and (args.overwrite or any(not ch["slots"][t]["vault"] for t in others))]
    r = do_copy_slot(keys, source=args.source, overwrite=args.overwrite)
    print("Copied {} slots, skipped {} already filled, {} channels had no {}."
          .format(r["copied"], r["skipped"], r["no_source"], TYPE_LABEL[args.source]))
    print("Nothing has reached Emby yet. Review, then run: restore --force")


def cmd_groups(args):
    st = build_state()
    if not st["groups"]:
        print("Emby reports no groups. Your M3U may not carry group titles.")
        print("Use name patterns in the web interface instead.")
        return
    print("{:<40} {:>7}  {}".format("Group", "Channels", "State"))
    for g in st["groups"]:
        print("{:<40} {:>7}  {}".format(
            g["name"][:40], g["count"], "excluded" if g["excluded"] else ""))
    pats = st["rules"].get("patterns") or []
    if pats:
        print("\nName patterns excluded: {}".format(", ".join(pats)))
    print("\n{} channels are excluded from bulk operations.".format(st["excluded_count"]))


def cmd_exclude(args):
    r = read_rules()
    r["groups"] = sorted(set(r["groups"]) | set(args.group))
    write_rules(r)
    print("Excluded: {}".format(", ".join(args.group)))
    cmd_groups(args)


def cmd_include(args):
    r = read_rules()
    r["groups"] = [g for g in r["groups"] if g not in args.group]
    write_rules(r)
    print("Included again: {}".format(", ".join(args.group)))
    cmd_groups(args)


def cmd_status(args):
    st = build_state()
    counts = {}
    for ch in st["channels"]:
        for t in IMAGE_TYPES:
            s = ch["slots"][t]["status"]
            counts[s] = counts.get(s, 0) + 1
    print("Vault updated: {}".format(st["manifest"]["updated"] or "never"))
    for k in ("match", "differs", "unsaved", "gone", "orphan", "empty"):
        print("  {:<9} {}".format(k, counts.get(k, 0)))
    drift = [(ch["name"], t) for ch in st["channels"] for t in IMAGE_TYPES
             if ch["slots"][t]["status"] in ("differs", "gone")]
    if drift:
        print("\nChanged since backup:")
        for name, t in drift:
            print("  {:<38} {}".format(name[:38], TYPE_LABEL[t]))


def cmd_restore(args):
    st = build_state()
    targets, why = [], []
    for ch in st["channels"]:
        if ch["excluded"]:
            continue
        for t in IMAGE_TYPES:
            s = ch["slots"][t]
            if not s["vault"]:
                continue
            if args.force or s["status"] in ("differs", "gone"):
                targets.append((ch["key"], t))
                why.append((ch["name"], t, s["status"]))

    if args.preview:
        print("Would push {} images:".format(len(targets)))
        for name, t, s in why:
            print("  {:<38} {:<18} {}".format(name[:38], TYPE_LABEL[t], s))
        print("\nPreview only. Drop --preview to apply.")
        return

    if not targets:
        print("Nothing to restore.")
        return
    ok = 0
    for r in do_restore(targets, force=args.force):
        if r["ok"]:
            ok += 1
        else:
            print("  failed  {} {}  ({})".format(r["key"], r["type"], r["msg"]))
    print("Pushed {} of {}.".format(ok, len(targets)))


def main():
    p = argparse.ArgumentParser(description="Emby channel logo vault.")
    p.add_argument("--url", help="Emby base URL (or EMBY_URL)")
    p.add_argument("--key", help="Emby API key (or EMBY_API_KEY)")
    p.add_argument("--vault", default=os.environ.get("EMBY_LOGO_VAULT", "./emby_logo_vault"))
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="Run the web interface (default)")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8077)
    s.add_argument("--open", action="store_true", help="Open a browser on start")

    sub.add_parser("backup", help="Save every logo to the vault")
    sub.add_parser("groups", help="List channel groups and which are excluded")
    x = sub.add_parser("exclude", help="Stop backing up one or more groups")
    x.add_argument("group", nargs="+")
    i = sub.add_parser("include", help="Start backing up one or more groups again")
    i.add_argument("group", nargs="+")
    f = sub.add_parser("fill", help="Copy one slot into the others in the vault")
    f.add_argument("--source", default="Primary", choices=IMAGE_TYPES,
                   help="Slot to copy from (default: Primary)")
    f.add_argument("--overwrite", action="store_true",
                   help="Also replace slots that already hold artwork")
    sub.add_parser("status", help="Report drift, change nothing")
    r = sub.add_parser("restore", help="Push vault logos back into Emby")
    r.add_argument("--preview", action="store_true")
    r.add_argument("--force", action="store_true")

    args = p.parse_args()
    if not args.cmd:
        args.cmd = "serve"
        args.host, args.port, args.open = "0.0.0.0", 8077, False
    configure(args)
    {"serve": cmd_serve, "backup": cmd_backup, "fill": cmd_fill,
     "groups": cmd_groups, "exclude": cmd_exclude, "include": cmd_include,
     "status": cmd_status, "restore": cmd_restore}[args.cmd](args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except RuntimeError as e:
        raise SystemExit(str(e))
