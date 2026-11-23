"""PNG metadata and zip export for the DaveLLM image library.

Everything here is pure standard-library Python, so it needs no Pillow:

* ``strip_metadata`` drops every text, time and EXIF chunk. ComfyUI writes its
  full workflow (including the prompt) into the PNGs it returns, so the stored
  file can carry the prompt even when the user asked for a clean export.
* ``embed_metadata`` strips first, then writes DaveLLM's own record as ``tEXt``
  chunks: ``parameters`` (a short readable summary) and ``davellm`` (the manifest as
  ASCII-only JSON, so it stays valid Latin-1 as the PNG spec requires).

Thumbnails are not made here: ComfyUI renders a small copy in the same
workflow (see ``davellm_comfy.build_workflow``), so no image decoding runs in
the router.
"""

from __future__ import annotations

import io
import json
import struct
import zipfile
import zlib
from typing import Any, Iterable, Optional

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
METADATA_CHUNKS = {b"tEXt", b"zTXt", b"iTXt", b"tIME", b"eXIf"}
MANIFEST_KEY = "davellm"
PARAMETERS_KEY = "parameters"


class PngError(ValueError):
    pass


def chunks(png: bytes) -> list[tuple[bytes, bytes]]:
    if not png.startswith(PNG_SIGNATURE):
        raise PngError("not a PNG file")
    out, pos = [], len(PNG_SIGNATURE)
    while pos + 8 <= len(png):
        (length,) = struct.unpack(">I", png[pos:pos + 4])
        tag = png[pos + 4:pos + 8]
        data = png[pos + 8:pos + 8 + length]
        if len(data) != length:
            raise PngError("truncated PNG chunk")
        out.append((tag, data))
        pos += 12 + length
        if tag == b"IEND":
            break
    if not out or out[0][0] != b"IHDR" or out[-1][0] != b"IEND":
        raise PngError("malformed PNG")
    return out


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def build(parts: Iterable[tuple[bytes, bytes]]) -> bytes:
    return PNG_SIGNATURE + b"".join(_chunk(tag, data) for tag, data in parts)


def text_chunks(png: bytes) -> dict[str, str]:
    found = {}
    for tag, data in chunks(png):
        if tag == b"tEXt" and b"\x00" in data:
            key, value = data.split(b"\x00", 1)
            found[key.decode("latin-1")] = value.decode("latin-1")
    return found


def strip_metadata(png: bytes) -> bytes:
    return build((tag, data) for tag, data in chunks(png) if tag not in METADATA_CHUNKS)


def parameters_line(manifest: dict[str, Any]) -> str:
    settings = manifest.get("settings") or {}
    output = manifest.get("output") or {}
    parts = [
        manifest.get("structured_prompt") or manifest.get("original_prompt") or "",
        f"Negative prompt: {manifest.get('negative_prompt') or ''}",
        ", ".join([
            f"Steps: {settings.get('steps')}", f"Sampler: {settings.get('sampler')}",
            f"Scheduler: {settings.get('scheduler')}", f"CFG scale: {settings.get('cfg')}",
            f"Seed: {manifest.get('seed')}", f"Size: {output.get('width')}x{output.get('height')}",
            f"Model: {(manifest.get('model') or {}).get('checkpoint')}",
        ]),
    ]
    return "\n".join(parts)


def _latin1(text: str) -> bytes:
    return text.encode("latin-1", errors="replace")


def embed_metadata(png: bytes, manifest: dict[str, Any]) -> bytes:
    parts = [(tag, data) for tag, data in chunks(png) if tag not in METADATA_CHUNKS]
    record = json.dumps(manifest, ensure_ascii=True, sort_keys=True)
    extra = [
        (b"tEXt", PARAMETERS_KEY.encode() + b"\x00" + _latin1(parameters_line(manifest))),
        (b"tEXt", MANIFEST_KEY.encode() + b"\x00" + record.encode("ascii")),
    ]
    return build(parts[:1] + extra + parts[1:])


def export_png(png: bytes, manifest: dict[str, Any], metadata: str) -> bytes:
    if metadata == "strip":
        return strip_metadata(png)
    if metadata == "embed":
        return embed_metadata(png, manifest)
    raise ValueError("metadata must be 'embed' or 'strip'")


def export_zip(items: Iterable[tuple[str, bytes, dict[str, Any]]], metadata: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for image_id, png, manifest in items:
            archive.writestr(f"{image_id}.png", export_png(png, manifest, metadata))
            if metadata == "embed":
                archive.writestr(f"{image_id}.json", json.dumps(manifest, indent=2, sort_keys=True))
    return buffer.getvalue()


def png_size(png: bytes) -> Optional[tuple[int, int]]:
    if len(png) < 24 or not png.startswith(PNG_SIGNATURE) or png[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", png[16:24])
