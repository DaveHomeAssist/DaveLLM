"""The ten image upgrades: gallery, remix data, variations, img2img, inpaint,
aspect presets, style/negative presets, job queue, export, favorites."""

import base64
import importlib.util
import io
import json
import socket
import struct
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import davellm_image_export as image_export
import davellm_image_presets as presets
from conftest import TEST_API_KEY
from davellm_comfy import ImageSpec, ProgressWatcher, QualityPass, build_workflow, thumb_size
from davellm_image_jobs import JobQueue, QueueFull
from davellm_image_store import ImageStore

ROOT = Path(__file__).resolve().parents[1]
AUTH = {"X-API-Key": TEST_API_KEY}

_spec = importlib.util.spec_from_file_location("fake_comfyui", ROOT / "scripts" / "fake_comfyui.py")
fake_comfyui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake_comfyui)


def _router(router_factory, monkeypatch, fake, **env):
    monkeypatch.setenv("DAVE_COMFY_URL", fake.url)
    monkeypatch.delenv("DAVE_IMAGE_DIR", raising=False)
    monkeypatch.delenv("DAVE_IMAGE_RETENTION_DAYS", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return router_factory()


@pytest.fixture
def fake():
    server = fake_comfyui.FakeComfy().start()
    yield server
    server.stop()


@pytest.fixture
def slow_fake():
    server = fake_comfyui.FakeComfy(delay=0.6).start()
    yield server
    server.stop()


@pytest.fixture
def image_router(router_factory, monkeypatch, fake):
    router, client, data_dir = _router(router_factory, monkeypatch, fake)
    return router, client, data_dir, fake


def _gen(client, **body):
    body = {"prompt": "stage lights", "width": 256, "height": 256, **body}
    res = client.post("/image/generate", headers=AUTH, json=body)
    assert res.status_code == 200, res.text
    return res.json()


def _data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode()


def _wait_jobs(client, ids, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = {j["id"]: j for j in client.get("/image/jobs", headers=AUTH).json()["jobs"]}
        if all(jobs.get(i, {}).get("status") in ("done", "failed", "cancelled") for i in ids):
            return jobs
        time.sleep(0.05)
    raise AssertionError("jobs did not finish")


# 1. Gallery ------------------------------------------------------------------------


def test_gallery_search_filters_facets_and_thumbnails(image_router):
    _router, client, data_dir, _fake = image_router
    a = _gen(client, prompt="red bicycle by a brick wall", seed=11)
    b = _gen(client, prompt="blue boat at dawn", seed=22)
    lib = client.get("/image/library", headers=AUTH).json()
    assert lib["total"] == 2 and lib["facets"]["models"] == ["sd_xl_turbo_1.0_fp16.safetensors"]
    assert [i["id"] for i in client.get("/image/library?q=bicycle%20BRICK", headers=AUTH).json()["images"]] == [a["id"]]
    assert client.get("/image/library?q=bicycle%20boat", headers=AUTH).json()["total"] == 0
    assert [i["id"] for i in client.get("/image/library?seed=22", headers=AUTH).json()["images"]] == [b["id"]]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    assert client.get(f"/image/library?since={today}&until={today}", headers=AUTH).json()["total"] == 2
    assert client.get(f"/image/library?since={tomorrow}", headers=AUTH).json()["total"] == 0
    assert client.get("/image/library?model=other.safetensors", headers=AUTH).json()["total"] == 0
    assert client.get("/image/library?since=yesterday", headers=AUTH).status_code == 422
    assert client.get("/image/library?limit=1&offset=1", headers=AUTH).json()["images"][0]["id"] == a["id"]
    thumb = client.get(f"/image/{a['id']}/thumb", headers=AUTH)
    assert thumb.status_code == 200 and image_export.png_size(thumb.content) == (256, 256)
    big = _gen(client, width=1024, height=576, seed=3)
    assert image_export.png_size(client.get(f"/image/{big['id']}/thumb", headers=AUTH).content) == (256, 144)
    # Records saved before previews existed fall back to the full image.
    (data_dir / "images" / f"{b['id']}.thumb.png").unlink()
    assert client.get(f"/image/{b['id']}/thumb", headers=AUTH).content == client.get(f"/image/{b['id']}/file", headers=AUTH).content
    assert client.get("/image/img-20990101T000000Z-00000000/thumb", headers=AUTH).status_code == 404


def test_thumbnail_size_keeps_aspect():
    assert thumb_size(1536, 1536) == (256, 256)
    assert thumb_size(1344, 768) == (256, 146)
    assert thumb_size(128, 64) == (128, 64)


# 2. Remix data ---------------------------------------------------------------------


def test_manifest_holds_everything_remix_needs(image_router):
    _router, client, _data_dir, _fake = image_router
    body = _gen(client, prompt="foggy pier", negative_prompt="text", seed=5, width=512, height=320,
                steps=6, cfg=1.5, sampler="euler_ancestral", scheduler="karras", style_preset="cinematic")
    m = client.get(f"/image/{body['id']}/manifest", headers=AUTH).json()
    assert m["original_prompt"] == "foggy pier" and m["negative_prompt"] == "text" and m["seed"] == 5
    assert {k: m["settings"][k] for k in ("width", "height", "steps", "cfg", "sampler", "scheduler", "mode")} == {
        "width": 512, "height": 320, "steps": 6, "cfg": 1.5, "sampler": "euler_ancestral",
        "scheduler": "karras", "mode": "txt2img"}
    assert m["style_preset"]["id"] == "cinematic"


# 3. Variations ---------------------------------------------------------------------


def test_variations_queue_four_seeds_one_at_a_time(router_factory, monkeypatch):
    fake = fake_comfyui.FakeComfy(delay=0.15).start()
    try:
        _r, client, _d = _router(router_factory, monkeypatch, fake)
        res = client.post("/image/jobs", headers=AUTH, json={"prompt": "lighthouse", "width": 256, "height": 256,
                                                             "seed": 100, "count": 4})
        assert res.status_code == 202
        jobs = res.json()["jobs"]
        assert [j["meta"]["seed"] for j in jobs] == [100, 101, 102, 103]
        # Each snapshot is taken as the job is queued, so the first may already be running.
        assert all(j["status"] in ("queued", "running") and 0 <= j["position"] <= 4 for j in jobs)
        done = _wait_jobs(client, [j["id"] for j in jobs])
        results = [done[j["id"]]["result"] for j in jobs]
        assert all(done[j["id"]]["status"] == "done" for j in jobs)
        groups = {r["manifest"]["variation"]["group"] for r in results}
        assert len(groups) == 1 and [r["manifest"]["variation"]["index"] for r in results] == [0, 1, 2, 3]
        assert len({r["manifest"]["png_sha256"] for r in results}) == 4
        # Never two prompts on the GPU at once: each one was queued after the previous finished.
        for (_q0, done0), (q1, _d1) in zip(fake.timeline, fake.timeline[1:]):
            assert q1 >= done0
        assert client.post("/image/generate", headers=AUTH, json={"prompt": "x", "count": 2}).status_code == 422
        assert client.post("/image/jobs", headers=AUTH, json={"prompt": "x", "count": 5}).status_code == 422
    finally:
        fake.stop()


# 4. Image to image -----------------------------------------------------------------


def test_img2img_from_library_and_upload(image_router):
    _router, client, _data_dir, fake = image_router
    source = _gen(client, seed=1)
    lib = _gen(client, mode="img2img", source_image_id=source["id"], strength=0.35, seed=2)
    graph = fake.graphs[-1]
    assert graph["3"]["inputs"]["denoise"] == 0.35 and graph["3"]["inputs"]["latent_image"] == ["13", 0]
    assert graph["12"]["inputs"]["image"].endswith(" [temp]") and fake.upload_types[-1] == "temp"
    assert graph["14"]["inputs"]["crop"] == "center" and "5" not in graph
    m = lib["manifest"]
    assert m["settings"]["mode"] == "img2img" and m["settings"]["strength"] == 0.35
    assert m["source"]["kind"] == "library" and m["source"]["id"] == source["id"]
    assert m["model"]["workflow_version"] == "sdxl-turbo-img2img-v1"
    upload = fake_comfyui.make_png(300, 200, b"upload")
    up = _gen(client, mode="img2img", source_image=_data_url(upload), strength=0.8)
    assert up["manifest"]["source"]["kind"] == "upload" and len(up["manifest"]["source"]["sha256"]) == 64
    for bad in ({"mode": "img2img"}, {"mode": "img2img", "source_image": "data:text/plain;base64,aGk="},
                {"mode": "img2img", "source_image_id": source["id"], "strength": 0},
                {"mode": "img2img", "source_image_id": source["id"], "source_image": _data_url(upload)}):
        assert client.post("/image/generate", headers=AUTH, json={"prompt": "x", **bad}).status_code == 422, bad
    assert client.post("/image/generate", headers=AUTH, json={
        "prompt": "x", "mode": "img2img", "source_image_id": "img-20990101T000000Z-00000000"}).status_code == 404


def test_img2img_quality_pass_refines_the_saved_draft(image_router):
    _router, client, _data_dir, fake = image_router
    source = _gen(client, seed=1, width=512, height=512)
    draft = _gen(client, mode="img2img", source_image_id=source["id"], strength=0.5, seed=9, width=512, height=512)
    res = client.post(f"/image/{draft['id']}/quality", headers=AUTH, json={})
    assert res.status_code == 200, res.text
    q = res.json()["manifest"]
    assert q["quality_method"] == "refine-saved-draft" and q["source"]["id"] == draft["id"]
    assert (q["output"]["width"], q["output"]["height"]) == (768, 768)
    assert fake.graphs[-1]["3"]["inputs"]["denoise"] == 0.45


# 5. Inpainting ---------------------------------------------------------------------


def test_inpaint_graph_mask_alignment_and_validation(image_router):
    _router, client, _data_dir, fake = image_router
    source = _gen(client, seed=1)
    mask = fake_comfyui.make_png(256, 256, b"mask")
    res = _gen(client, mode="inpaint", source_image_id=source["id"], mask=_data_url(mask), strength=0.9)
    graph = fake.graphs[-1]
    assert graph["16"]["class_type"] == "SetLatentNoiseMask" and graph["3"]["inputs"]["latent_image"] == ["16", 0]
    assert graph["15"]["inputs"]["channel"] == "red" and graph["15"]["inputs"]["image"].endswith(" [temp]")
    # The mask is scaled and cropped exactly like the image.
    assert {k: graph["18"]["inputs"][k] for k in ("width", "height", "crop")} == \
        {k: graph["14"]["inputs"][k] for k in ("width", "height", "crop")}
    assert res["manifest"]["model"]["workflow_version"] == "sdxl-turbo-inpaint-v1"
    assert len(res["manifest"]["source"]["mask_sha256"]) == 64
    jpeg = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0rest").decode()
    for bad in ({"mode": "inpaint", "source_image_id": source["id"]},
                {"mode": "inpaint", "source_image_id": source["id"], "mask": jpeg},
                {"mode": "img2img", "source_image_id": source["id"], "mask": _data_url(mask)}):
        assert client.post("/image/generate", headers=AUTH, json={"prompt": "x", **bad}).status_code == 422, bad


def test_inpaint_workflow_requires_inputs():
    with pytest.raises(ValueError):
        build_workflow(ImageSpec(prompt="p", seed=1, mode="inpaint", source_image="a [temp]"), "ckpt")
    with pytest.raises(ValueError):
        build_workflow(ImageSpec(prompt="p", seed=1, mode="img2img"), "ckpt")
    g = build_workflow(ImageSpec(prompt="p", seed=1, mode="inpaint", source_image="a [temp]", mask_image="m [temp]",
                                 strength=0.7, quality=QualityPass()), "ckpt")
    assert g["10"]["inputs"]["samples"] == ["3", 0] and g["3"]["inputs"]["denoise"] == 0.7


# 6. Aspect presets -----------------------------------------------------------------


@pytest.mark.parametrize("max_pixels", [1024 * 1024, 512 * 512, 640 * 640])
def test_aspect_presets_snap_to_64_and_respect_limits(max_pixels):
    items = presets.aspect_presets(max_pixels)
    ratios = {p["ratio"] for p in items}
    assert {"1:1", "4:3", "3:2", "16:9", "9:16"} <= ratios
    for p in items:
        assert p["width"] % 64 == 0 and p["height"] % 64 == 0
        assert p["width"] * p["height"] <= max_pixels
        rw, rh = (int(x) for x in p["ratio"].split(":"))
        assert abs(p["width"] / p["height"] - rw / rh) / (rw / rh) < 0.2, p


def test_aspect_presets_route_uses_hardware_limits(router_factory, monkeypatch, fake):
    _r, client, _d = _router(router_factory, monkeypatch, fake, DAVE_IMAGE_MAX_PIXELS=str(512 * 512))
    body = client.get("/image/presets", headers=AUTH).json()
    assert max(p["width"] * p["height"] for p in body["aspects"]) <= 512 * 512
    square = next(p for p in body["aspects"] if p["ratio"] == "1:1")
    assert (square["width"], square["height"]) == (512, 512)


# 7. Style and negative presets -----------------------------------------------------


def test_style_and_negative_presets_are_local_and_editable(image_router):
    _router, client, data_dir, fake = image_router
    body = client.get("/image/presets", headers=AUTH).json()
    assert {"photo", "cinematic"} <= {p["id"] for p in body["styles"]} and body["negatives"]
    saved = client.put("/image/presets", headers=AUTH, json={
        "styles": [{"id": "neon", "name": "Neon", "text": "neon glow, night"}],
        "negatives": [{"name": "No people", "text": "people, crowd"}]}).json()
    assert saved["styles"] == [{"id": "neon", "name": "Neon", "text": "neon glow, night"}]
    assert saved["negatives"][0]["id"] == "no-people"
    assert json.loads((data_dir / "image_presets.json").read_text())["styles"][0]["id"] == "neon"
    # A chosen style is the user's own choice, not a rewrite: no review even in "always review material".
    res = _gen(client, prompt="a city street", style_preset="neon", approval_mode="material")
    assert fake.graphs[-1]["6"]["inputs"]["text"] == "a city street, neon glow, night"
    assert res["manifest"]["original_prompt"] == "a city street" and res["manifest"]["style_preset"]["name"] == "Neon"
    assert client.post("/image/generate", headers=AUTH, json={"prompt": "x", "style_preset": "photo"}).status_code == 422
    assert client.put("/image/presets", headers=AUTH, json={"styles": [{"name": "", "text": "x"}]}).status_code == 422
    assert client.put("/image/presets", headers=AUTH, json={"styles": [{"id": "a", "name": "A", "text": "x"}] * 2}).status_code == 422
    reset = client.post("/image/presets/reset", headers=AUTH).json()
    assert "photo" in {p["id"] for p in reset["styles"]}


# 8. Job queue ----------------------------------------------------------------------


def test_queue_positions_cancel_queued_and_running(router_factory, monkeypatch, slow_fake):
    _r, client, _d = _router(router_factory, monkeypatch, slow_fake)
    ids = []
    for seed in (1, 2, 3):
        res = client.post("/image/jobs", headers=AUTH, json={"prompt": "p", "width": 256, "height": 256, "seed": seed})
        ids.append(res.json()["jobs"][0]["id"])
    time.sleep(0.2)
    listed = {j["id"]: j for j in client.get("/image/jobs", headers=AUTH).json()["jobs"]}
    assert [listed[i]["position"] for i in ids] == [0, 1, 2]
    assert listed[ids[0]]["status"] == "running" and listed[ids[2]]["status"] == "queued"
    assert client.get("/image/status", headers=AUTH).json()["queue"] == {"running": 1, "queued": 2, "max_queued": 16}
    queued_cancel = client.post(f"/image/jobs/{ids[2]}/cancel", headers=AUTH).json()
    assert queued_cancel["status"] == "cancelled"
    running_cancel = client.post(f"/image/jobs/{ids[0]}/cancel", headers=AUTH)
    assert running_cancel.status_code == 200
    jobs = _wait_jobs(client, ids)
    assert jobs[ids[0]]["status"] == "cancelled" and slow_fake.interrupt_calls >= 1
    assert jobs[ids[1]]["status"] == "done" and jobs[ids[2]]["status"] == "cancelled"
    assert len(slow_fake.graphs) == 2  # the cancelled queued job never reached ComfyUI
    assert client.get(f"/image/jobs/{ids[1]}", headers=AUTH).json()["result"]["saved"] is True
    assert client.post("/image/jobs/job-nope/cancel", headers=AUTH).status_code == 404
    assert client.get("/image/jobs/job-nope", headers=AUTH).status_code == 404


def test_queue_unit_limits_and_progress():
    queue = JobQueue(max_queued=2)
    gate = threading.Event()

    def runner(progress, cancel):
        progress(3, 8)
        gate.wait(5)
        return {"ok": True}

    first = queue.submit("draft", "a", runner)
    time.sleep(0.1)
    assert queue.get(first.id)["progress"] == {"value": 3, "max": 8}
    queue.submit("draft", "b", runner)
    queue.submit("draft", "c", runner)
    with pytest.raises(QueueFull):
        queue.submit("draft", "d", runner)
    gate.set()
    assert queue.wait(first.id, 5)["status"] == "done"
    assert queue.get(first.id)["progress"] == {"value": 8, "max": 8}


def test_progress_watcher_reads_comfy_websocket():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve():
        conn, _ = server.accept()
        request = b""
        while b"\r\n\r\n" not in request:
            request += conn.recv(4096)
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
        time.sleep(0.2)  # the client learns its prompt id from /prompt first
        for event in ({"type": "progress", "data": {"value": 2, "max": 4, "node": "3", "prompt_id": "p1"}},
                      {"type": "progress", "data": {"value": 1, "max": 4, "node": "3", "prompt_id": "other"}},
                      {"type": "progress", "data": {"value": 3, "max": 4, "node": "11", "prompt_id": "p1"}}):
            payload = json.dumps(event).encode()
            conn.sendall(bytes([0x81, len(payload)]) if len(payload) < 126 else bytes([0x81, 126]) + struct.pack(">H", len(payload)))
            conn.sendall(payload)
        time.sleep(0.8)
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    seen = []
    spec = ImageSpec(prompt="p", steps=4, quality=QualityPass(steps=4))
    watcher = ProgressWatcher(f"http://127.0.0.1:{port}", spec, lambda v, m: seen.append((v, m)))
    watcher.prompt_id = "p1"
    assert watcher.connected
    time.sleep(0.6)
    watcher.stop()
    server.close()
    assert seen == [(2, 8), (7, 8)]


# 9. Export -------------------------------------------------------------------------


def test_export_embeds_or_strips_metadata(image_router):
    _router, client, _data_dir, _fake = image_router
    body = _gen(client, prompt="café sign at night", seed=8)
    stored = client.get(f"/image/{body['id']}/file", headers=AUTH).content
    # ComfyUI-style workflow chunks, as real ComfyUI writes them into its PNGs.
    noisy = image_export.build([image_export.chunks(stored)[0], (b"tEXt", b"prompt\x00{\"secret\": 1}")]
                               + image_export.chunks(stored)[1:])
    embedded = image_export.embed_metadata(noisy, {"seed": 8})
    assert set(image_export.text_chunks(embedded)) == {"parameters", "davellm"}
    res = client.get(f"/image/{body['id']}/export?metadata=embed", headers=AUTH)
    assert res.status_code == 200 and "attachment" in res.headers["content-disposition"]
    text = image_export.text_chunks(res.content)
    record = json.loads(text["davellm"])
    assert record["seed"] == 8 and record["original_prompt"] == "café sign at night"
    assert "Seed: 8" in text["parameters"]
    clean = client.get(f"/image/{body['id']}/export?metadata=strip", headers=AUTH)
    assert image_export.text_chunks(clean.content) == {} and image_export.png_size(clean.content) == (256, 256)
    assert clean.headers["content-disposition"].endswith('-clean.png"')
    assert client.get(f"/image/{body['id']}/export?metadata=maybe", headers=AUTH).status_code == 422


def test_zip_export_of_selected_images(image_router):
    _router, client, _data_dir, _fake = image_router
    a, b = _gen(client, seed=1), _gen(client, seed=2)
    res = client.post("/image/export", headers=AUTH, json={"ids": [a["id"], b["id"], a["id"]], "metadata": "embed"})
    assert res.status_code == 200 and res.headers["content-type"] == "application/zip"
    names = sorted(zipfile.ZipFile(io.BytesIO(res.content)).namelist())
    assert names == sorted([f"{a['id']}.png", f"{a['id']}.json", f"{b['id']}.png", f"{b['id']}.json"])
    stripped = zipfile.ZipFile(io.BytesIO(client.post("/image/export", headers=AUTH, json={
        "ids": [a["id"]], "metadata": "strip"}).content))
    assert stripped.namelist() == [f"{a['id']}.png"]
    assert image_export.text_chunks(stripped.read(f"{a['id']}.png")) == {}
    assert client.post("/image/export", headers=AUTH, json={"ids": ["img-20990101T000000Z-00000000"]}).status_code == 404
    assert client.post("/image/export", headers=AUTH, json={"ids": []}).status_code == 422


# 10. Favorites and collections -----------------------------------------------------


def test_favorites_and_collections(image_router):
    _router, client, _data_dir, _fake = image_router
    a, b = _gen(client, seed=1), _gen(client, seed=2)
    res = client.patch(f"/image/{a['id']}", headers=AUTH, json={"favorite": True, "collections": ["Poster ideas", "Poster ideas", "Q4"]})
    assert res.status_code == 200 and res.json()["library"] == {"favorite": True, "collections": ["Poster ideas", "Q4"]}
    assert [i["id"] for i in client.get("/image/library?favorite=true", headers=AUTH).json()["images"]] == [a["id"]]
    assert client.get("/image/library?favorite=false", headers=AUTH).json()["images"][0]["id"] == b["id"]
    assert client.get("/image/library?collection=Q4", headers=AUTH).json()["total"] == 1
    assert client.get("/image/collections", headers=AUTH).json()["collections"] == [
        {"name": "Poster ideas", "count": 1}, {"name": "Q4", "count": 1}]
    assert client.patch(f"/image/{a['id']}", headers=AUTH, json={"collections": ["../etc"]}).status_code == 422
    assert client.patch("/image/img-20990101T000000Z-00000000", headers=AUTH, json={"favorite": True}).status_code == 404
    assert client.get("/image/status", headers=AUTH).json()["storage"]["favorites"] == 1


def test_favorites_are_exempt_from_retention_but_not_purge(tmp_path):
    store = ImageStore(tmp_path / "images", retention_days=30)
    old = datetime.now(timezone.utc) - timedelta(days=45)
    keep = store.save(b"keep", {"stage": "draft"}, now=datetime.now(timezone.utc))
    store.update_library(keep["id"], favorite=True)
    # Backdate the favorite past retention, and add an old non-favorite.
    path = tmp_path / "images" / f"{keep['id']}.json"
    record = json.loads(path.read_text())
    record["created_utc"] = old.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    path.write_text(json.dumps(record))
    gone = store.save(b"gone", {"stage": "draft"}, now=old)
    assert [r["id"] for r in store.list()] == [keep["id"]]
    assert gone["id"] not in {r["id"] for r in store.list()}
    store.update_library(keep["id"], favorite=False)
    assert store.prune() == 1 and store.list() == []
    again = store.save(b"x", {}, thumbnail=b"t")
    store.update_library(again["id"], favorite=True)
    assert store.purge() == 1 and list((tmp_path / "images").iterdir()) == []
