#!/usr/bin/env python3
"""Capture Feishu doc block data and images into a local work directory."""

import argparse
import asyncio
import base64
import json
import sys
from urllib.parse import quote
from pathlib import Path

from feishu_client import FeishuClient, open_session


def ext_for(mime_type):
    mime_type = mime_type or ""
    if "png" in mime_type:
        return "png"
    if "jpeg" in mime_type or "jpg" in mime_type:
        return "jpg"
    if "gif" in mime_type:
        return "gif"
    if "webp" in mime_type:
        return "webp"
    return "bin"


def collect_images(docs_dir):
    entries = {}
    for doc_file in sorted(docs_dir.glob("full_*.json")):
        doc = json.loads(doc_file.read_text(encoding="utf-8"))
        for block in (doc.get("blockMap") or {}).values():
            data = block.get("data") or {}
            if data.get("type") != "image":
                continue
            image = data.get("image") or {}
            token = image.get("token")
            if not token:
                continue
            if token in entries:
                if doc.get("name") not in entries[token]["docs"]:
                    entries[token]["docs"].append(doc["name"])
                continue
            entries[token] = {
                "token": token,
                "mimeType": image.get("mimeType"),
                "width": image.get("width"),
                "height": image.get("height"),
                "size": image.get("size"),
                "name": image.get("name"),
                "docs": [doc.get("name")],
                "ext": ext_for(image.get("mimeType")),
            }
    return sorted(entries.values(), key=lambda item: item["token"])


async def fetch_source_doc(client, name, token, out_path):
    pages = []
    cursor = None
    guard = 0
    while guard < 80:
        guard += 1
        url = (
            f"{client.tenant_url}/space/api/docx/pages/client_vars"
            f"?id={token}&mode=7&limit=239"
        )
        if cursor:
            url += f"&cursor={quote(cursor)}"
        res = await client.get_json(url)
        if res["status"] != 200:
            raise RuntimeError(f"HTTP {res['status']} for {name}")
        j = json.loads(res["body"])
        if j.get("code") != 0 or not j.get("data"):
            raise RuntimeError(f"API error for {name}: {j.get('msg') or j.get('code')}")
        data = j["data"]
        pages.append(data)
        if not data.get("has_more"):
            break
        cursor = data.get("cursor")

    seen = set()
    sequence = []
    block_map = {}
    meta = None
    for page_data in pages:
        for block_id in page_data.get("block_sequence") or []:
            if block_id not in seen:
                seen.add(block_id)
                sequence.append(block_id)
        block_map.update(page_data.get("block_map") or {})
        meta = meta or (page_data.get("meta_map") or {}).get(token)

    out = {
        "token": token,
        "name": name,
        "title": (meta or {}).get("title"),
        "pageCount": len(pages),
        "blockCount": len(sequence),
        "sequence": sequence,
        "blockMap": block_map,
    }
    out_path.write_text(
        json.dumps(out, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"CAPTURED {name} blocks={len(sequence)} pages={len(pages)}")


async def download_all_images(client, manifest, image_dir):
    failed = []
    downloaded = 0
    skipped = 0
    for index, entry in enumerate(manifest, 1):
        path = image_dir / f"{entry['token']}.{entry['ext']}"
        if path.exists() and path.stat().st_size > 0:
            skipped += 1
            continue
        result = None
        for attempt in range(3):
            result = await client.download_image(entry["token"])
            if result.get("ok"):
                break
            await client.page.wait_for_timeout(400 * (attempt + 1))
        if not result or not result.get("ok"):
            failed.append({"token": entry["token"], "status": result.get("status")})
            print(f"FAIL_IMAGE {entry['token']} status={result.get('status')}")
            continue
        path.write_bytes(base64.b64decode(result["base64"]))
        downloaded += 1
        if index % 25 == 0 or index == len(manifest):
            print(f"IMAGE_PROGRESS {index}/{len(manifest)} downloaded={downloaded} skipped={skipped} failed={len(failed)}")
    return failed


async def main():
    parser = argparse.ArgumentParser(description="Capture Feishu source docs and images.")
    parser.add_argument("--config", required=True, help="Path to config JSON.")
    parser.add_argument("--skip-images", action="store_true", help="Skip image download.")
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    tenant_url = config["tenant_url"]
    out_dir = Path(config.get("output_dir", "work"))
    out_dir.mkdir(parents=True, exist_ok=True)
    image_dir = out_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)

    pw, browser, page, csrf = await open_session(
        config.get("cdp_url", "http://127.0.0.1:9222"),
        tenant_url,
    )
    client = FeishuClient(
        page,
        csrf,
        tenant_url,
        config.get("member_id"),
        tenant_id=config.get("tenant_id"),
        space_id=config.get("space_id"),
    )
    try:
        for doc in config.get("docs", []):
            name = doc["name"]
            out_path = out_dir / f"full_{name}.json"
            if out_path.exists():
                print(f"SKIP_CAPTURE {name} file exists")
                continue
            token = doc.get("source_token") or doc.get("token")
            if not token:
                raise RuntimeError(f"doc {name} needs source_token")
            await fetch_source_doc(client, name, token, out_path)

        manifest = collect_images(out_dir)
        (image_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"MANIFEST images={len(manifest)}")

        if not args.skip_images:
            failed = await download_all_images(client, manifest, image_dir)
            (image_dir / "download_report.json").write_text(
                json.dumps({"failed": failed}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if failed:
                raise SystemExit(f"image download failed: {len(failed)}")
    finally:
        await pw.stop()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
