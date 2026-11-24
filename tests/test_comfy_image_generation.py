import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import davellm_image_policy as policy
from conftest import TEST_API_KEY
from davellm_comfy import ImageSpec, QualityPass, build_workflow
from davellm_image_store import ImageNotFound, ImageStore, parse_retention_days

ROOT = Path(__file__).resolve().parents[1]
AUTH = {"X-API-Key": TEST_API_KEY}


def test_turbo_default_and_explicit_scheduler():
    checkpoint = "sd_xl_turbo_1.0_fp16.safetensors"
    spec = ImageSpec(prompt="road case", seed=3406450846279331269)
    graph = build_workflow(spec, checkpoint)
    assert graph["3"]["class_type"] == "SamplerCustom"
    assert graph["3"]["inputs"]["noise_seed"] == spec.seed
    assert graph["3"]["inputs"]["sigmas"] == ["22", 0]
    assert graph["22"]["inputs"] == {"model": ["4", 0], "steps": 4, "denoise": 1.0}
    assert graph["3"]["inputs"]["sampler"] == ["23", 0]
    assert graph["23"]["inputs"]["sampler_name"] == "euler"
    assert build_workflow(spec, "other.safetensors")["3"]["inputs"]["scheduler"] == "simple"
    spec.scheduler = "simple"
    assert build_workflow(spec, checkpoint)["3"]["class_type"] == "KSampler"

_spec = importlib.util.spec_from_file_location("fake_comfyui", ROOT / "scripts" / "fake_comfyui.py")
fake_comfyui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake_comfyui)


@pytest.fixture
def fake_comfy():
    fake = fake_comfyui.FakeComfy().start()
    yield fake
    fake.stop()


@pytest.fixture
def image_router(router_factory, monkeypatch, fake_comfy):
    monkeypatch.setenv("DAVE_COMFY_URL", fake_comfy.url)
    monkeypatch.delenv("DAVE_IMAGE_DIR", raising=False)
    monkeypatch.delenv("DAVE_IMAGE_RETENTION_DAYS", raising=False)
    router, client, data_dir = router_factory()
    return router, client, data_dir, fake_comfy


# -- routes: auth and bounds -------------------------------------------------------


def test_image_routes_require_authentication(router_factory):
    _router, client, _data_dir = router_factory()
    assert client.get("/image/status").status_code == 401
    assert client.post("/image/generate", json={"prompt": "test"}).status_code == 401
    assert client.get("/image/library").status_code == 401
    assert client.delete("/image/img-20261117T000000Z-00000000").status_code == 401
    assert client.post("/image/purge", json={"confirm": "DELETE ALL IMAGES"}).status_code == 401
    assert client.post("/image/enhance", json={"prompt": "x"}).status_code == 401


def test_image_generation_rejects_invalid_values(router_factory):
    _router, client, _data_dir = router_factory()
    for body in (
        {"prompt": "x", "width": 4096},
        {"prompt": "x", "width": 770},
        {"prompt": "x", "steps": 100},
        {"prompt": "x", "sampler": "made_up"},
        {"prompt": ""},
    ):
        assert client.post("/image/generate", headers=AUTH, json=body).status_code == 422, body


def test_status_reports_defaults_limits_and_local_storage(image_router):
    _router, client, _data_dir, _fake = image_router
    status = client.get("/image/status", headers=AUTH).json()
    assert status["available"] is True
    assert status["checkpoint"] == "sd_xl_turbo_1.0_fp16.safetensors"
    assert status["defaults"]["prompt_behavior"] == "review"
    assert status["defaults"]["approval_mode"] == "material"
    assert status["limits"]["max_pixels"] == 1024 * 1024
    assert status["storage"] == {"count": 0, "favorites": 0, "retention_days": 90,
                                 "favorites_exempt_from_retention": True, "location": "local-disk"}


def test_status_when_generator_is_down(router_factory, monkeypatch):
    monkeypatch.setenv("DAVE_COMFY_URL", "http://127.0.0.1:9")
    _router, client, _data_dir = router_factory()
    assert client.get("/image/status", headers=AUTH).json()["available"] is False
    res = client.post("/image/generate", headers=AUTH, json={"prompt": "x", "width": 256, "height": 256})
    assert res.status_code == 503
    assert res.json()["detail"] == "Local image generator is unavailable"


# -- generation, provenance and storage ---------------------------------------------


def test_generate_saves_image_and_manifest_on_local_disk(image_router):
    _router, client, data_dir, fake = image_router
    res = client.post("/image/generate", headers=AUTH, json={
        "prompt": "stage lights", "negative_prompt": "text", "width": 512, "height": 768, "seed": 7})
    assert res.status_code == 200
    body = res.json()
    assert body["saved"] is True and body["seed"] == 7 and (body["width"], body["height"]) == (512, 768)
    assert body["image"].startswith("data:image/png;base64,")
    image_id = body["id"]
    files = sorted(p.name for p in (data_dir / "images").iterdir())
    assert files == [f"{image_id}.json", f"{image_id}.png", f"{image_id}.thumb.png"]
    manifest = json.loads((data_dir / "images" / f"{image_id}.json").read_text())
    assert manifest["original_prompt"] == "stage lights"
    assert manifest["structured_prompt"] == "stage lights"
    assert manifest["negative_prompt"] == "text"
    assert manifest["settings"]["width"] == 512 and manifest["settings"]["sampler"] == "euler"
    assert manifest["model"]["checkpoint"] == "sd_xl_turbo_1.0_fp16.safetensors"
    assert manifest["model"]["workflow_version"] == "sdxl-turbo-draft-v2"
    assert manifest["created_utc"].endswith("Z")
    # The graph wrote to ComfyUI's temp output, never its permanent output folder.
    assert fake.graphs[-1]["9"]["class_type"] == "PreviewImage"
    assert fake.graphs[-1]["7"]["inputs"]["text"] == "text"
    file_res = client.get(f"/image/{image_id}/file", headers=AUTH)
    assert file_res.status_code == 200 and file_res.headers["cache-control"] == "no-store"


def test_save_false_keeps_nothing(image_router):
    _router, client, data_dir, _fake = image_router
    res = client.post("/image/generate", headers=AUTH, json={"prompt": "x", "width": 256, "height": 256, "save": False})
    assert res.status_code == 200 and res.json()["id"] is None
    assert not (data_dir / "images").exists() or list((data_dir / "images").iterdir()) == []


def test_image_dir_and_retention_are_configurable(router_factory, monkeypatch, fake_comfy, tmp_path):
    custom = tmp_path / "elsewhere"
    monkeypatch.setenv("DAVE_COMFY_URL", fake_comfy.url)
    monkeypatch.setenv("DAVE_IMAGE_DIR", str(custom))
    monkeypatch.setenv("DAVE_IMAGE_RETENTION_DAYS", "7")
    _router, client, _data_dir = router_factory()
    res = client.post("/image/generate", headers=AUTH, json={"prompt": "x", "width": 256, "height": 256})
    assert (custom / f"{res.json()['id']}.png").is_file()
    assert client.get("/image/status", headers=AUTH).json()["storage"]["retention_days"] == 7


def test_delete_purge_and_library(image_router):
    _router, client, data_dir, _fake = image_router
    ids = [client.post("/image/generate", headers=AUTH, json={"prompt": f"p{i}", "width": 256, "height": 256}).json()["id"]
           for i in range(3)]
    assert {i["id"] for i in client.get("/image/library", headers=AUTH).json()["images"]} == set(ids)
    assert client.delete(f"/image/{ids[0]}", headers=AUTH).status_code == 200
    assert client.delete(f"/image/{ids[0]}", headers=AUTH).status_code == 404
    assert client.get(f"/image/{ids[0]}/manifest", headers=AUTH).status_code == 404
    assert client.post("/image/purge", headers=AUTH, json={"confirm": "yes"}).status_code == 400
    assert len(client.get("/image/library", headers=AUTH).json()["images"]) == 2
    purged = client.post("/image/purge", headers=AUTH, json={"confirm": "DELETE ALL IMAGES"})
    assert purged.json() == {"deleted": 2}
    assert list((data_dir / "images").iterdir()) == []


def test_image_ids_cannot_escape_the_store(image_router):
    _router, client, _data_dir, _fake = image_router
    for bad in ("..%2F..%2Fconversations", "img-1", "img-20261117T000000Z-0000000g"):
        assert client.get(f"/image/{bad}/file", headers=AUTH).status_code == 404
        assert client.delete(f"/image/{bad}", headers=AUTH).status_code in (404, 405)


# -- quality pass --------------------------------------------------------------------


def test_quality_pass_reuses_seed_and_links_draft(image_router):
    _router, client, _data_dir, fake = image_router
    draft = client.post("/image/generate", headers=AUTH, json={"prompt": "x", "width": 512, "height": 512, "seed": 99}).json()
    q = client.post(f"/image/{draft['id']}/quality", headers=AUTH, json={})
    assert q.status_code == 200
    body = q.json()
    assert body["seed"] == 99 and (body["width"], body["height"]) == (768, 768)
    assert body["manifest"]["stage"] == "quality" and body["manifest"]["parent_id"] == draft["id"]
    graph = fake.graphs[-1]
    assert graph["3"]["inputs"]["noise_seed"] == graph["11"]["inputs"]["seed"] == 99
    assert graph["11"]["inputs"]["denoise"] == 0.45 and graph["8"]["inputs"]["samples"] == ["11", 0]


def test_quality_pass_over_limit_asks_first(image_router):
    _router, client, _data_dir, fake = image_router
    draft = client.post("/image/generate", headers=AUTH, json={"prompt": "x", "width": 1024, "height": 1024, "seed": 1}).json()
    sent = len(fake.graphs)
    over = client.post(f"/image/{draft['id']}/quality", headers=AUTH, json={"quality": {"scale": 2.0}})
    assert over.status_code == 409 and over.json()["detail"]["code"] == "hardware_limit"
    assert len(fake.graphs) == sent
    ok = client.post(f"/image/{draft['id']}/quality", headers=AUTH, json={"quality": {"scale": 2.0}, "accept_reduction": True})
    assert ok.status_code == 200
    assert ok.json()["width"] * ok.json()["height"] <= 1536 * 1536
    assert ok.json()["manifest"]["reduced_from"] == {"quality_scale": 2.0, "quality_steps": 4}


def test_quality_pass_needs_saved_draft(image_router):
    _router, client, _data_dir, _fake = image_router
    assert client.post("/image/img-20261117T000000Z-00000000/quality", headers=AUTH, json={}).status_code == 404


# -- hardware envelope (7B) ----------------------------------------------------------


def test_over_limit_generation_pauses_until_reduction_is_accepted(image_router):
    _router, client, _data_dir, fake = image_router
    res = client.post("/image/generate", headers=AUTH, json={"prompt": "x", "width": 1536, "height": 1536, "steps": 12})
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["code"] == "hardware_limit" and set(detail["exceeded"]) == {"resolution", "steps"}
    assert detail["proposed"] == {"width": 1024, "height": 1024, "steps": 8}
    assert fake.graphs == []
    ok = client.post("/image/generate", headers=AUTH, json={
        "prompt": "x", "width": 1536, "height": 1536, "steps": 12, "accept_reduction": True})
    assert ok.status_code == 200 and (ok.json()["width"], ok.json()["height"]) == (1024, 1024)
    assert ok.json()["manifest"]["reduced_from"] == {"width": 1536, "height": 1536, "steps": 12}


def test_hardware_fit_keeps_aspect_ratio():
    limits = policy.Limits(1024 * 1024, 8, 1536 * 1536, 8)
    over = policy.hardware_check(width=1920, height=1088, steps=4, limits=limits)
    w, h = over["proposed"]["width"], over["proposed"]["height"]
    assert w * h <= 1024 * 1024 and w % 64 == 0 and h % 64 == 0 and w > h
    assert policy.hardware_check(width=768, height=768, steps=4, limits=limits) is None


# -- review and approval modes (1, 6, and their interactions) --------------------------


@pytest.mark.parametrize("behavior,approval,approved,expected", [
    ("review", "material", False, "review_required"),
    ("review", "material", True, "ok"),
    ("review", "none", False, "review_required"),   # stricter setting wins
    ("auto", "material", False, "review_required"),  # approval policy still applies
    ("auto", "none", False, "ok"),                   # only full opt-out skips review
    ("auto", "always", False, "review_required"),
    ("suggest", "none", True, "suggest_only"),       # suggestions never reach the model
])
def test_mode_matrix_for_material_rewrite(behavior, approval, approved, expected):
    decision = policy.decide(original="a red bicycle", structured="a red bicycle on a beach",
                             prompt_behavior=behavior, approval_mode=approval, approved=approved)
    assert decision.code == expected
    if expected == "suggest_only":
        assert decision.prompt_for_model == "a red bicycle"


@pytest.mark.parametrize("approval,approved,expected", [
    ("material", False, "ok"), ("none", False, "ok"), ("always", False, "review_required"), ("always", True, "ok"),
])
def test_unchanged_prompt(approval, approved, expected):
    for structured in (None, "A red bicycle.", "  a red   bicycle "):
        assert policy.decide(original="a red bicycle", structured=structured,
                             approval_mode=approval, approved=approved).code == expected


def test_material_change_detection_is_conservative():
    assert not policy.is_material_change("A cat", "a cat.")
    assert policy.is_material_change("a cat", "a cat, no text")
    assert policy.is_material_change("a cat", "a kitten")


def test_generate_route_enforces_review(image_router):
    _router, client, _data_dir, fake = image_router
    body = {"prompt": "a red bicycle", "structured_prompt": "a red bicycle, golden hour", "width": 256, "height": 256}
    blocked = client.post("/image/generate", headers=AUTH, json=body)
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "review_required"
    assert fake.graphs == []
    ok = client.post("/image/generate", headers=AUTH, json={**body, "approved": True, "enhanced": True})
    assert ok.status_code == 200
    manifest = ok.json()["manifest"]
    assert manifest["original_prompt"] == "a red bicycle"
    assert manifest["structured_prompt"] == "a red bicycle, golden hour"
    assert manifest["material_change"] is True and manifest["approved"] is True
    assert fake.graphs[-1]["6"]["inputs"]["text"] == "a red bicycle, golden hour"


# -- enhancement (5B) ------------------------------------------------------------------


def test_enhance_uses_local_model_and_never_generates(image_router, monkeypatch):
    router, client, _data_dir, fake = image_router
    router.MODEL_INVENTORY[router.NODE_CONFIGS[0].id] = {"llama3:latest"}

    class Result:
        content = '"A red bicycle leaning on a brick wall, warm evening light"'

    seen = {}

    def fake_chat(url, model, messages, **_kw):
        seen["model"] = model
        seen["messages"] = messages
        return Result()

    monkeypatch.setattr(router, "ollama_chat", fake_chat)
    res = client.post("/image/enhance", headers=AUTH, json={"prompt": "a red bicycle"})
    assert res.status_code == 200
    body = res.json()
    assert body["proposed"] == "A red bicycle leaning on a brick wall, warm evening light"
    assert body["material_change"] is True and body["usable_for_generation"] is True
    assert seen["model"] == "llama3:latest" and "Preserve the subject" in seen["messages"][0]["content"]
    assert fake.graphs == []
    suggest = client.post("/image/enhance", headers=AUTH, json={"prompt": "a red bicycle", "prompt_behavior": "suggest"})
    assert suggest.json()["usable_for_generation"] is False


def test_enhance_model_setting_is_honored(router_factory, monkeypatch, fake_comfy):
    monkeypatch.setenv("DAVE_COMFY_URL", fake_comfy.url)
    monkeypatch.setenv("DAVE_IMAGE_ENHANCE_MODEL", "llama3:latest")
    router, client, _data_dir = router_factory()
    node_id = router.NODE_CONFIGS[0].id

    class Result:
        content = "a red bicycle, golden hour"

    seen = []
    monkeypatch.setattr(router, "ollama_chat", lambda url, model, messages, **_kw: seen.append(model) or Result())
    router.MODEL_INVENTORY[node_id] = {"gpt-oss:20b", "llama3:latest"}
    assert client.post("/image/enhance", headers=AUTH, json={"prompt": "a red bicycle"}).json()["model"] == "llama3:latest"
    assert client.post("/image/enhance", headers=AUTH, json={"prompt": "x", "model": "gpt-oss:20b"}).json()["model"] == "gpt-oss:20b"
    router.MODEL_INVENTORY[node_id] = {"gpt-oss:20b"}
    missing = client.post("/image/enhance", headers=AUTH, json={"prompt": "x"})
    assert missing.status_code == 503 and "llama3:latest" in missing.json()["detail"]
    assert seen == ["llama3:latest", "gpt-oss:20b"]


def test_enhance_without_a_model_is_explicit(image_router):
    router, client, _data_dir, _fake = image_router
    router.MODEL_INVENTORY.clear()
    res = client.post("/image/enhance", headers=AUTH, json={"prompt": "x"})
    assert res.status_code == 503


# -- workflow and store units ---------------------------------------------------------


def test_workflow_shapes():
    draft = build_workflow(ImageSpec(prompt="p", seed=3), "ckpt")
    assert set(draft) == {"3", "4", "5", "6", "7", "8", "9", "20", "21"}
    assert draft["20"]["inputs"]["width"] == 256 and draft["21"]["class_type"] == "PreviewImage"
    quality = build_workflow(ImageSpec(prompt="p", seed=3, quality=QualityPass()), "ckpt")
    assert quality["10"]["inputs"]["width"] == 1152 and quality["11"]["inputs"]["seed"] == 3
    with pytest.raises(ValueError):
        build_workflow(ImageSpec(prompt="p"), "ckpt")


def test_store_retention_and_ids(tmp_path):
    store = ImageStore(tmp_path / "images", retention_days=30)
    now = datetime.now(timezone.utc)
    old = store.save(b"old", {"stage": "draft"}, now=now - timedelta(days=31))
    assert store.list() == []  # saving prunes, and "old" was already past retention
    fresh = store.save(b"new", {"stage": "draft"}, now=now)
    assert [i["id"] for i in store.list()] == [fresh["id"]]
    with pytest.raises(ImageNotFound):
        store.manifest(old["id"])
    with pytest.raises(ImageNotFound):
        store.image_path("../../etc/passwd")
    keep = ImageStore(tmp_path / "keep", retention_days=0)
    keep.save(b"x", {}, now=now - timedelta(days=3650))
    assert keep.prune() == 0 and len(keep.list()) == 1
    assert parse_retention_days(None) == 90 and parse_retention_days("0") == 0
    assert parse_retention_days("junk") == 90 and parse_retention_days("-4") == 0


def test_reproducible_check_script_passes_in_stub_mode(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_image_generation.py"), "--stub", "--out", str(tmp_path)],
        capture_output=True, text=True, timeout=300, cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    report = json.loads((tmp_path / "RESULTS.json").read_text())
    assert report["passed"] == report["total"] >= 20
