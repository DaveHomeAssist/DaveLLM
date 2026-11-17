"""Reproducible image-generation check for DaveLLM (draft, quality, policy, storage).

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
                        and manifest["model"].get("workflow_version") == "sdxl-turbo-draft-v1",
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
                            and qp["manifest"]["model"]["workflow_version"] == "sdxl-turbo-quality-v1")
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
        lines += [f"| {t['case']} | {t['stage']} | {t['size']} | {t['generator_ms']} | {t['round_trip_ms']} |" for t in timings]
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
