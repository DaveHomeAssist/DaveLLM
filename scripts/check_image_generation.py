"""Reproducible image-generation check for DaveLLM (draft, quality, policy, storage,
and the upgrades U1-U10: gallery, remix, variations, image to image, inpaint,
aspect presets, style presets, job queue, export, favorites).

Drives the real router routes in-process against a ComfyUI endpoint:

    # Offline, deterministic (CI): starts scripts/fake_comfyui.py in-process
    python scripts/check_image_generation.py --stub --out /tmp/imagecheck

    # Live on the GPU host (Walter), ComfyUI on loopback
    python scripts/check_image_generation.py --comfy-url http://127.0.0.1:8188 --out benchmarks/imagegen-YYYY-MM-DD

It uses a throwaway data directory and API key, so it never touches the
user's real image library. Results go to <out>/RESULTS.json and RESULTS.md,
and each generated PNG is copied into <out> for review. Exit status is 0 only
when every check passes.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import platform
import secrets
import shutil
import struct
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

CASES = [
    {"id": "C1", "prompt": "Watercolor illustration of a rainy loading dock at dusk, stagehands unloading road cases",
     "seed": 1101, "width": 768, "height": 768},
    {"id": "C2", "prompt": "Macro food photograph of a glazed cinnamon roll on a slate plate, soft window light",
     "negative_prompt": "text, watermark", "seed": 2202, "width": 768, "height": 768},
    {"id": "C3", "prompt": "Cinematic wide shot of an outdoor festival stage at sunset with a large crowd",
     "seed": 3303, "width": 1024, "height": 576},
]


def png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def decode_data_url(url: str) -> bytes:
    import base64
    return base64.b64decode(url.split(",", 1)[1])


class Checker:
    def __init__(self):
        self.results: list[dict] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append({"check": name, "pass": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}", flush=True)
        return ok

    @property
    def passed(self) -> bool:
        return all(item["pass"] for item in self.results)


def mask_png(width: int, height: int, box: tuple[float, float, float, float]) -> bytes:
    """White rectangle (the area to repaint) on black, as an RGB PNG."""
    import zlib
    x0, y0, x1, y1 = (int(box[0] * width), int(box[1] * height), int(box[2] * width), int(box[3] * height))
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            v = 255 if x0 <= x < x1 and y0 <= y < y1 else 0
            rows += bytes((v, v, v))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b""))


def wait_jobs(client, headers, ids, timeout=600, watch=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get("/image/status", headers=headers).json().get("queue", {})
        if watch is not None:
            watch.append(status.get("running", 0))
        jobs = {j["id"]: j for j in client.get("/image/jobs", headers=headers).json()["jobs"]}
        if all(jobs.get(i, {}).get("status") in ("done", "failed", "cancelled") for i in ids):
            return jobs
        time.sleep(0.25)
    raise TimeoutError("image jobs did not finish")


def upgrades(client, headers, c, out, timings, router, data_dir, live=True):
    """Checks for the ten image upgrades (gallery ... favorites)."""
    import base64
    from davellm_image_export import png_size as _png_size, text_chunks
    to_url = lambda b: "data:image/png;base64," + base64.b64encode(b).decode()  # noqa: E731

    def timed(name, body, path="/image/generate"):
        t0 = time.monotonic()
        res = client.post(path, headers=headers, json=body)
        elapsed = int((time.monotonic() - t0) * 1000)
        if res.status_code == 200:
            p = res.json()
            timings.append({"case": name, "stage": p["stage"], "size": f"{p['width']}x{p['height']}",
                            "generator_ms": p["duration_ms"], "round_trip_ms": elapsed})
        return res

    # 6. Aspect presets, and a max 16:9 draft at the limit.
    aspects = client.get("/image/presets", headers=headers).json()["aspects"]
    limit = router.IMAGE_LIMITS.max_pixels
    c.check("U6 aspect presets snap to 64 and stay within the draft limit",
            all(a["width"] % 64 == 0 and a["height"] % 64 == 0 and a["width"] * a["height"] <= limit for a in aspects),
            f"{len(aspects)} presets")
    wide = next(a for a in aspects if a["ratio"] == "16:9" and a["tier"] == "max")
    res = timed("U6-16:9", {"prompt": "Cinematic wide shot of a mountain road at dawn", "seed": 606,
                            "width": wide["width"], "height": wide["height"]})
    ok = res.status_code == 200
    if c.check(f"U6 16:9 max preset {wide['width']}x{wide['height']} generated", ok, f"HTTP {res.status_code}"):
        png = decode_data_url(res.json()["image"])
        c.check("U6 output size matches the preset", png_size(png) == (wide["width"], wide["height"]), f"got {png_size(png)}")
        (out / "U6-16x9.png").write_bytes(png)

    # 7. Style preset (user's choice, no review) and editable presets.
    saved = client.put("/image/presets", headers=headers, json={"styles": [
        {"id": "check-ink", "name": "Ink", "text": "black ink line drawing, white background"}]})
    styled = timed("U7-style", {"prompt": "a fox sitting in tall grass", "seed": 707, "style_preset": "check-ink"})
    sm = styled.json().get("manifest", {}) if styled.status_code == 200 else {}
    c.check("U7 style preset is appended to the model prompt and recorded",
            saved.status_code == 200 and styled.status_code == 200 and sm.get("style_preset", {}).get("id") == "check-ink"
            and sm.get("original_prompt") == "a fox sitting in tall grass", f"HTTP {styled.status_code}")
    if styled.status_code == 200:
        (out / "U7-style-ink.png").write_bytes(decode_data_url(styled.json()["image"]))
    reset = client.post("/image/presets/reset", headers=headers).json()
    c.check("U7 presets reset to defaults", "photo" in {p["id"] for p in reset["styles"]} and reset["negatives"])

    # 1. Gallery: thumbnails, search, seed filter.
    lib = client.get("/image/library", headers=headers).json()
    first = lib["images"][0]
    thumb = client.get(f"/image/{first['id']}/thumb", headers=headers)
    tsize = _png_size(thumb.content) if thumb.status_code == 200 else None
    c.check("U1 gallery thumbnail rendered by ComfyUI (long side 256)", bool(tsize) and max(tsize) == 256, f"{tsize}")
    found = client.get("/image/library?q=fox%20grass", headers=headers).json()
    by_seed = client.get("/image/library?seed=606", headers=headers).json()
    c.check("U1 gallery search and seed filter", found["total"] == 1 and by_seed["total"] == 1
            and lib["facets"]["models"], f"search {found['total']}, seed {by_seed['total']}")

    # 2. Remix: the manifest carries the full recipe; rerunning it reproduces the image.
    m = client.get(f"/image/{styled.json()['id']}/manifest", headers=headers).json() if styled.status_code == 200 else {}
    s = m.get("settings", {})
    recipe = {"prompt": m.get("original_prompt"), "negative_prompt": m.get("negative_prompt", ""), "seed": m.get("seed"),
              "width": s.get("width"), "height": s.get("height"), "steps": s.get("steps"), "cfg": s.get("cfg"),
              "sampler": s.get("sampler"), "scheduler": s.get("scheduler"),
              "style_preset": (m.get("style_preset") or {}).get("id"), "save": False}
    client.put("/image/presets", headers=headers, json={"styles": [m["style_preset"]]} if m.get("style_preset") else {})
    remix = client.post("/image/generate", headers=headers, json=recipe)
    c.check("U2 remix recipe from the manifest reproduces the image",
            remix.status_code == 200 and hashlib.sha256(decode_data_url(remix.json()["image"])).hexdigest() == m.get("png_sha256"),
            f"HTTP {remix.status_code}")
    client.post("/image/presets/reset", headers=headers)

    # 3. Variations: four seeds, queued one after another.
    running_seen: list[int] = []
    t0 = time.monotonic()
    sub = client.post("/image/jobs", headers=headers, json={"prompt": "a paper boat on a puddle, macro photo",
                                                            "seed": 300, "count": 4})
    ids = [j["id"] for j in sub.json().get("jobs", [])]
    jobs = wait_jobs(client, headers, ids, watch=running_seen) if ids else {}
    wall = int((time.monotonic() - t0) * 1000)
    results = [jobs[i]["result"] for i in ids if jobs.get(i, {}).get("status") == "done"]
    c.check("U3 four variations finished", sub.status_code == 202 and len(results) == 4,
            f"{len(results)}/4 in {wall} ms")
    c.check("U3 variations use consecutive seeds and differ", [r["seed"] for r in results] == [300, 301, 302, 303]
            and len({r["manifest"]["png_sha256"] for r in results}) == 4)
    c.check("U8 never more than one GPU job running", max(running_seen or [0]) <= 1, f"max running {max(running_seen or [0])}")
    for r in results:
        (out / f"U3-variation-{r['seed']}.png").write_bytes(client.get(f"/image/{r['id']}/file", headers=headers).content)
        timings.append({"case": f"U3-seed{r['seed']}", "stage": "draft", "size": f"{r['width']}x{r['height']}",
                        "generator_ms": r["duration_ms"], "round_trip_ms": None})

    # 4. Image to image from a library image.
    source = results[0] if results else first
    i2i = timed("U4-img2img", {"prompt": "a paper boat on a puddle, oil painting", "seed": 404, "mode": "img2img",
                               "source_image_id": source["id"], "strength": 0.55})
    if c.check("U4 image to image from a library image", i2i.status_code == 200, f"HTTP {i2i.status_code}"):
        im = i2i.json()["manifest"]
        c.check("U4 records source and strength", im["source"]["id"] == source["id"] and im["settings"]["strength"] == 0.55
                and im["model"]["workflow_version"] == "sdxl-turbo-img2img-v1")
        (out / "U4-img2img.png").write_bytes(decode_data_url(i2i.json()["image"]))
    upload = client.get(f"/image/{source['id']}/export?metadata=strip", headers=headers).content
    i2u = timed("U4-upload", {"prompt": "a paper boat on a puddle, pencil sketch", "seed": 405, "mode": "img2img",
                              "source_image": to_url(upload), "strength": 0.7})
    c.check("U4 image to image from an upload", i2u.status_code == 200
            and i2u.json()["manifest"]["source"]["kind"] == "upload", f"HTTP {i2u.status_code}")

    # 5. Inpaint the middle of the source.
    w, h = source.get("width") or 768, source.get("height") or 768
    mask = mask_png(w, h, (0.3, 0.3, 0.7, 0.7))
    inp = timed("U5-inpaint", {"prompt": "a small yellow rubber duck floating on the water", "seed": 505, "mode": "inpaint",
                               "source_image_id": source["id"], "mask": to_url(mask), "strength": 1.0})
    if c.check("U5 inpaint generated", inp.status_code == 200, f"HTTP {inp.status_code}"):
        ipng = decode_data_url(inp.json()["image"])
        (out / "U5-inpaint.png").write_bytes(ipng)
        (out / "U5-inpaint-mask.png").write_bytes(mask)
        (out / "U5-inpaint-source.png").write_bytes(upload)
        c.check("U5 inpaint manifest records the mask", len(inp.json()["manifest"]["source"].get("mask_sha256", "")) == 64)

    # 8. Queue: cancel a running job (interrupts ComfyUI) and a queued one.
    sub = client.post("/image/jobs", headers=headers, json={"prompt": "a busy night market, wide shot", "seed": 808,
                                                            "count": 3, "width": 1024, "height": 1024})
    ids = [j["id"] for j in sub.json().get("jobs", [])]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and client.get(f"/image/jobs/{ids[0]}", headers=headers).json()["status"] != "running":
        time.sleep(0.05)
    positions = [client.get(f"/image/jobs/{i}", headers=headers).json()["position"] for i in ids]
    client.post(f"/image/jobs/{ids[2]}/cancel", headers=headers)
    client.post(f"/image/jobs/{ids[0]}/cancel", headers=headers)
    jobs = wait_jobs(client, headers, ids)
    c.check("U8 queue reports positions 0/1/2", positions == [0, 1, 2], f"{positions}")
    c.check("U8 cancel stops a running job and drops a queued one",
            [jobs[i]["status"] for i in ids] == ["cancelled", "done", "cancelled"], f"{[jobs[i]['status'] for i in ids]}")
    prog = jobs[ids[1]]["progress"]
    if live:  # the stub has no websocket, so step progress only exists against real ComfyUI
        c.check("U8 progress reported in steps", prog.get("max", 0) > 0 and prog.get("value") == prog.get("max"), f"{prog}")

    # 9. Export with embedded metadata, stripped, and a zip of a selection.
    target = i2i.json()["id"] if i2i.status_code == 200 else first["id"]
    raw = client.get(f"/image/{target}/file", headers=headers).content
    embedded = client.get(f"/image/{target}/export?metadata=embed", headers=headers).content
    stripped = client.get(f"/image/{target}/export?metadata=strip", headers=headers).content
    raw_keys, emb = sorted(text_chunks(raw)), text_chunks(embedded)
    c.check("U9 export embeds DaveLLM tEXt and drops ComfyUI's workflow chunks",
            set(emb) == {"parameters", "davellm"} and json.loads(emb["davellm"])["id"] == target,
            f"stored file had {raw_keys or 'no text chunks'}")
    c.check("U9 strip-metadata export has no text chunks", text_chunks(stripped) == {} and png_size(stripped) == png_size(raw))
    (out / "U9-export-embedded.png").write_bytes(embedded)
    zipped = client.post("/image/export", headers=headers, json={"ids": [r["id"] for r in results[:3]], "metadata": "embed"})
    import io
    import zipfile
    names = zipfile.ZipFile(io.BytesIO(zipped.content)).namelist() if zipped.status_code == 200 else []
    c.check("U9 zip export of selected images", len(names) == 6, f"{len(names)} entries")

    # 10. Favorites survive retention; unfavorited old images do not.
    fav, plain = (results + [first, first])[:2]
    client.patch(f"/image/{fav['id']}", headers=headers, json={"favorite": True, "collections": ["Check"]})
    store = router.IMAGE_STORE
    for rec in (fav, plain):
        path = store.root / f"{rec['id']}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["created_utc"] = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        path.write_text(json.dumps(record), encoding="utf-8")
    store.prune()
    left = {i["id"] for i in store.list()}
    c.check("U10 favorite kept past retention, non-favorite pruned", fav["id"] in left and plain["id"] not in left)
    coll = client.get("/image/library?collection=Check&favorite=true", headers=headers).json()
    c.check("U10 favorite and collection filters", coll["total"] == 1)


def run(args) -> int:
    fake = None
    comfy_url = args.comfy_url
    if args.stub:
        from fake_comfyui import FakeComfy
        fake = FakeComfy().start()
        comfy_url = fake.url
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    data_dir = Path(tempfile.mkdtemp(prefix="davellm-imagecheck-"))
    api_key = secrets.token_hex(16)
    os.environ.update({
        "DAVE_DATA_DIR": str(data_dir), "DAVE_API_KEY": api_key, "DAVE_COMFY_URL": comfy_url,
        "DAVE_NODES": json.dumps([{"id": "none", "name": "none", "url": "http://127.0.0.1:9"}]),
        "DAVE_IMAGE_RETENTION_DAYS": "90",
    })
    if args.checkpoint:
        os.environ["DAVE_COMFY_CHECKPOINT"] = args.checkpoint
    os.environ.pop("DAVE_IMAGE_DIR", None)
    sys.modules.pop("app", None)
    router = importlib.import_module("app")
    from fastapi.testclient import TestClient

    c = Checker()
    timings: list[dict] = []
    started_utc = datetime.now(timezone.utc)
    headers = {"X-API-Key": api_key}
    try:
        with TestClient(router.app) as client:
            status = client.get("/image/status", headers=headers).json()
            if not c.check("generator reachable", status.get("available") is True,
                           f"comfyui {status.get('comfyui_version')} on {status.get('device')}"):
                return finish(c, out, args, status, timings, started_utc)

            first = None
            for case in CASES:
                body = {k: v for k, v in case.items() if k != "id"}
                t0 = time.monotonic()
                res = client.post("/image/generate", headers=headers, json=body)
                elapsed = int((time.monotonic() - t0) * 1000)
                ok = res.status_code == 200
                if not c.check(f"{case['id']} draft generated", ok, f"HTTP {res.status_code}"):
                    continue
                payload = res.json()
                png = decode_data_url(payload["image"])
                size = png_size(png)
                c.check(f"{case['id']} PNG is {case['width']}x{case['height']}",
                        size == (case["width"], case["height"]), f"got {size}")
                manifest = payload["manifest"]
                fields = ["original_prompt", "structured_prompt", "negative_prompt", "seed", "settings", "model", "created_utc", "png_sha256"]
                missing = [f for f in fields if f not in manifest]
                c.check(f"{case['id']} manifest has provenance", not missing and manifest["seed"] == case["seed"]
                        and manifest["model"].get("workflow_version") == "sdxl-turbo-draft-v2",
                        "missing " + ", ".join(missing) if missing else f"id {payload['id']}")
                stored = client.get(f"/image/{payload['id']}/file", headers=headers)
                c.check(f"{case['id']} saved file matches", stored.status_code == 200
                        and hashlib.sha256(stored.content).hexdigest() == manifest["png_sha256"])
                (out / f"{case['id']}-draft.png").write_bytes(png)
                timings.append({"case": case["id"], "stage": "draft", "size": f"{case['width']}x{case['height']}",
                                "generator_ms": payload["duration_ms"], "round_trip_ms": elapsed})
                if first is None:
                    first = (case, payload, png)

            if first:
                case, payload, png = first
                again = client.post("/image/generate", headers=headers, json={
                    "prompt": case["prompt"], "seed": case["seed"], "width": case["width"], "height": case["height"], "save": False})
                same = again.status_code == 200 and decode_data_url(again.json()["image"]) == png
                c.check("same seed and settings reproduce the draft byte-for-byte", same,
                        "identical" if same else "differs (non-deterministic backend)")
                c.check("save=false leaves no library entry", again.status_code == 200 and again.json()["id"] is None
                        and len(client.get("/image/library", headers=headers).json()["images"]) == len(CASES))

                t0 = time.monotonic()
                q = client.post(f"/image/{payload['id']}/quality", headers=headers, json={})
                elapsed = int((time.monotonic() - t0) * 1000)
                if c.check("quality pass generated", q.status_code == 200, f"HTTP {q.status_code}"):
                    qp = q.json()
                    qpng = decode_data_url(qp["image"])
                    c.check("quality pass keeps seed and links its draft",
                            qp["seed"] == case["seed"] and qp["manifest"]["parent_id"] == payload["id"]
                            and qp["manifest"]["model"]["workflow_version"] == "sdxl-turbo-quality-v2")
                    c.check("quality pass is 1.5x the draft", png_size(qpng) == (1152, 1152), f"got {png_size(qpng)}")
                    (out / f"{case['id']}-quality.png").write_bytes(qpng)
                    timings.append({"case": case["id"], "stage": "quality", "size": "1152x1152",
                                    "generator_ms": qp["duration_ms"], "round_trip_ms": elapsed})

            count_before = len(client.get("/image/library", headers=headers).json()["images"])
            big = client.post("/image/generate", headers=headers, json={"prompt": "test", "width": 1536, "height": 1536})
            detail = big.json().get("detail", {}) if big.status_code == 409 else {}
            c.check("over-limit request pauses for approval and generates nothing",
                    big.status_code == 409 and detail.get("code") == "hardware_limit"
                    and len(client.get("/image/library", headers=headers).json()["images"]) == count_before,
                    f"proposed {detail.get('proposed')}")
            review = client.post("/image/generate", headers=headers, json={
                "prompt": "a red bicycle", "structured_prompt": "a red bicycle leaning on a brick wall, golden hour"})
            c.check("material rewrite needs review by default", review.status_code == 409
                    and review.json()["detail"]["code"] == "review_required")
            fmt = client.post("/image/generate", headers=headers, json={
                "prompt": "a red bicycle", "structured_prompt": "A red bicycle.", "seed": 5, "width": 512, "height": 512, "save": False})
            c.check("formatting-only change generates without review", fmt.status_code == 200, f"HTTP {fmt.status_code}")
            suggest = client.post("/image/generate", headers=headers, json={
                "prompt": "a red bicycle", "structured_prompt": "a blue bicycle", "prompt_behavior": "suggest", "approved": True})
            c.check("suggest mode never sends a rewrite", suggest.status_code == 422)

            upgrades(client, headers, c, out, timings, router, data_dir, live=not args.stub)

            library = client.get("/image/library", headers=headers).json()["images"]
            victim = library[-1]["id"]
            deleted = client.delete(f"/image/{victim}", headers=headers)
            c.check("per-image delete removes file and manifest", deleted.status_code == 200
                    and client.get(f"/image/{victim}/file", headers=headers).status_code == 404
                    and not (data_dir / "images" / f"{victim}.png").exists())
            store = router.IMAGE_STORE
            old = store.save(b"\x89PNG\r\n\x1a\n", {"stage": "draft"}, now=datetime.now(timezone.utc) - timedelta(days=120))
            store.prune()
            c.check("retention prunes images past 90 days", old["id"] not in {i["id"] for i in store.list()}
                    and not (store.root / f"{old['id']}.png").exists())
            refused = client.post("/image/purge", headers=headers, json={"confirm": "yes"})
            purged = client.post("/image/purge", headers=headers, json={"confirm": "DELETE ALL IMAGES"})
            c.check("purge-all needs the exact confirmation and empties the library",
                    refused.status_code == 400 and purged.status_code == 200
                    and client.get("/image/library", headers=headers).json()["images"] == [],
                    f"deleted {purged.json().get('deleted') if purged.status_code == 200 else '?'}")
            leftovers = [p.name for p in (data_dir / "images").glob("*")]
            c.check("nothing left on disk after purge", leftovers == [], ", ".join(leftovers))
        return finish(c, out, args, status, timings, started_utc)
    finally:
        if fake:
            fake.stop()
        shutil.rmtree(data_dir, ignore_errors=True)
        sys.modules.pop("app", None)


def finish(c: Checker, out: Path, args, status: dict, timings: list, started_utc: datetime) -> int:
    report = {
        "started_utc": started_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": "stub" if args.stub else "live",
        "host": platform.node() if not args.stub else "stub",
        "generator": {k: status.get(k) for k in ("available", "comfyui_version", "device", "checkpoint")},
        "limits": status.get("limits"),
        "timings": timings,
        "checks": c.results,
        "passed": sum(1 for r in c.results if r["pass"]),
        "total": len(c.results),
    }
    (out / "RESULTS.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = [f"# DaveLLM image check ({report['mode']})", "",
             f"Started {report['started_utc']} UTC. Generator: {report['generator']}.", "",
             f"**{report['passed']}/{report['total']} checks passed.**", "",
             "| Check | Result | Detail |", "|---|---|---|"]
    lines += [f"| {r['check']} | {'PASS' if r['pass'] else 'FAIL'} | {r['detail']} |" for r in c.results]
    if timings:
        lines += ["", "| Case | Stage | Size | Generator ms | Round trip ms |", "|---|---|---|---:|---:|"]
        lines += [f"| {t['case']} | {t['stage']} | {t['size']} | {t['generator_ms']} | {t['round_trip_ms'] if t['round_trip_ms'] is not None else 'queued'} |" for t in timings]
    (out / "RESULTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{report['passed']}/{report['total']} checks passed; report in {out}", flush=True)
    return 0 if c.passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stub", action="store_true", help="use the in-process fake ComfyUI")
    parser.add_argument("--comfy-url", default=os.getenv("DAVE_COMFY_URL", "http://127.0.0.1:8188"))
    parser.add_argument("--checkpoint", default=None)
    parser.add_argument("--out", required=True)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
