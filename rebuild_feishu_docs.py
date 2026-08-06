#!/usr/bin/env python3
"""Rebuild Feishu source docs as new editable wiki docs from captured block data."""

import argparse
import asyncio
import json
import random
import string
import sys
import uuid
from pathlib import Path

from feishu_client import FeishuClient, open_session


ALPHANUM = string.ascii_letters + string.digits
MAX_BLOCKS_PER_REQUEST = 300
MAX_IMAGE_UPDATES_PER_REQUEST = 20

REPLACEMENTS = []
TARGET_URL = ""
TENANT_ID = None
MEMBER_ID = ""
WORK = Path(".")
RESULTS_FILE = None
OLD_PHRASES = []


def make_block_id():
    return "doxcn" + "".join(random.choice(ALPHANUM) for _ in range(22))


def replace_text(value):
    for old, new in REPLACEMENTS:
        value = value.replace(old, new)
    return value


def replace_deep(obj):
    if isinstance(obj, str):
        return replace_text(obj)
    if isinstance(obj, list):
        return [replace_deep(v) for v in obj]
    if isinstance(obj, dict):
        return {k: replace_deep(v) for k, v in obj.items()}
    return obj


def utf16_len(text):
    return len(text.encode("utf-16-le")) // 2


def dec_char(ch):
    if "0" <= ch <= "9":
        return ord(ch) - ord("0")
    if "a" <= ch <= "z":
        return ord(ch) - ord("a") + 10
    if "A" <= ch <= "Z":
        return ord(ch) - ord("A") + 10
    return 0


def enc_len(n):
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n < 36:
        return chars[n]
    out = []
    while n:
        out.append(chars[n % 36])
        n //= 36
    return "".join(reversed(out))


def parse_attribs(attribs):
    runs = []
    cur = []
    line_marker = None
    i = 0
    n = len(attribs)
    while i < n:
        ch = attribs[i]
        if ch == "*":
            j = i + 1
            while j < n and attribs[j] != "*" and attribs[j] != "+":
                j += 1
            segment = attribs[i + 1 : j]
            if "|" in segment:
                attrs_part, marker_part = segment.split("|", 1)
                line_marker = int(marker_part)
                if attrs_part:
                    cur.append(int(attrs_part))
            else:
                cur.append(int(segment))
            i = j
        elif ch == "+":
            j = i + 1
            while j < n and attribs[j] != "*" and attribs[j] != "+":
                j += 1
            length = 0
            for c in attribs[i + 1 : j]:
                length = length * 36 + dec_char(c)
            runs.append((sorted(set(cur)), length, line_marker))
            cur = []
            line_marker = None
            i = j
        else:
            i += 1
    return runs


def encode_runs(runs):
    out = []
    for attrs, length, line_marker in runs:
        if length <= 0:
            continue
        for a in attrs:
            out.append("*" + str(a))
        if line_marker is not None:
            out.append("|" + str(line_marker))
        out.append("+" + enc_len(length))
    return "".join(out)


def apply_text_replacements(text, attribs):
    runs = parse_attribs(attribs)
    unit_attrs = []
    for attrs, length, _line_marker in runs:
        unit_attrs.extend([list(attrs)] * max(0, length))
    total_units = utf16_len(text)
    if len(unit_attrs) < total_units:
        unit_attrs.extend([[]] * (total_units - len(unit_attrs)))
    unit_attrs = unit_attrs[:total_units]

    spans = []
    i = 0
    n = len(text)
    while i < n:
        best = None
        for phrase, repl in REPLACEMENTS:
            if text.startswith(phrase, i) and (
                best is None or len(phrase) > len(best[0])
            ):
                best = (phrase, repl)
        if best:
            phrase, repl = best
            start_cu = utf16_len(text[:i])
            end_cu = utf16_len(text[: i + len(phrase)])
            spans.append((start_cu, end_cu, repl, i, i + len(phrase)))
            i += len(phrase)
        else:
            i += 1

    marker_orig = set()
    pos_cu = 0
    for attrs, length, line_marker in runs:
        if line_marker is not None and length > 0:
            marker_orig.add(pos_cu + length - 1)
        pos_cu += length

    out_units = []
    new_marker = set()
    new_pos = 0
    old_pos = 0
    pieces = []
    pos_cp = 0
    for start_cu, end_cu, repl, start_cp, end_cp in spans:
        for idx in range(old_pos, start_cu):
            if idx in marker_orig:
                new_marker.add(new_pos)
            out_units.append(unit_attrs[idx])
            new_pos += 1
        attrs = unit_attrs[start_cu] if start_cu < len(unit_attrs) else []
        span_units = utf16_len(repl)
        for idx in range(start_cu, end_cu):
            if idx in marker_orig:
                new_marker.add(new_pos + span_units - 1)
        out_units.extend([list(attrs)] * span_units)
        new_pos += span_units
        old_pos = end_cu
        pieces.append(text[pos_cp:start_cp])
        pieces.append(repl)
        pos_cp = end_cp
    for idx in range(old_pos, len(unit_attrs)):
        if idx in marker_orig:
            new_marker.add(new_pos)
        out_units.append(unit_attrs[idx])
        new_pos += 1
    pieces.append(text[pos_cp:])
    new_text = "".join(pieces)

    new_runs = []
    pos_cu = 0
    for attrs in out_units:
        marker = 1 if pos_cu in new_marker else None
        if new_runs and new_runs[-1][0] == attrs and new_runs[-1][2] == marker:
            new_runs[-1][1] += 1
        else:
            new_runs.append([list(attrs), 1, marker])
        pos_cu += 1
    return new_text, new_runs


def make_text_block(parent_id, member_id, text, bold=False):
    pool = {"0": ["author", member_id]}
    if bold:
        pool["1"] = ["bold", "true"]
    attrs = "*0" + (("*1" if bold else "")) + "+" + enc_len(utf16_len(text))
    return {
        "type": "text",
        "parent_id": parent_id,
        "children": [],
        "author": member_id,
        "hidden": False,
        "locked": False,
        "text": {
            "apool": {"nextNum": len(pool), "numToAttrib": pool},
            "initialAttributedTexts": {
                "text": {"0": text},
                "attribs": {"0": attrs},
            },
        },
    }


def format_size(size):
    size = size or 0
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f}MB"
    if size >= 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size}B"


def video_meta(source, view_data):
    bm = source["blockMap"]
    for child in view_data.get("children") or []:
        data = bm.get(child, {}).get("data", {})
        if data.get("type") == "file":
            file_data = data.get("file") or {}
            return file_data.get("name", "视频"), format_size(file_data.get("size", 0))
    return "视频", ""


def remap_mention(comp, target):
    data = dict(comp.get("data") or {})
    data["token"] = target["obj_token"]
    data["raw_url"] = f"{TARGET_URL}/docx/{target['obj_token']}"
    if TENANT_ID:
        data["tenant_id"] = TENANT_ID
    data["file_type"] = 22
    data["icon_type"] = 22
    data["title"] = target["title"]
    data.pop("icon_info", None)
    comp = dict(comp)
    comp["data"] = data
    return comp


def transform_text_block(data, member_id, remap):
    text = data.get("text")
    if not isinstance(text, dict):
        return data

    apool = text.get("apool") or {}
    nta_raw = apool.get("numToAttrib") or {}
    nta = {}
    comps = {}
    for num, pair in nta_raw.items():
        if isinstance(pair, list) and pair and pair[0] == "inline-component":
            try:
                comps[num] = json.loads(pair[1])
            except Exception:
                comps[num] = None
        else:
            nta[num] = list(pair)

    iat = text.get("initialAttributedTexts") or {}
    raw_text = ((iat.get("text") or {}).get("0")) or ""
    raw_attribs = ((iat.get("attribs") or {}).get("0")) or ""
    new_raw_text, replaced_runs = apply_text_replacements(raw_text, raw_attribs)

    new_comps = {}
    dropped_titles = []
    for num, comp in comps.items():
        if not isinstance(comp, dict):
            continue
        if comp.get("type") == "mention_doc":
            token = ((comp.get("data") or {}).get("token") or "")
            if token in remap:
                new_comps[int(num)] = remap_mention(comp, remap[token])
            else:
                dropped_titles.append(
                    ((comp.get("data") or {}).get("title") or "").strip()
                )

    mention_only_drop = (
        dropped_titles
        and len(comps) == 1
        and not new_comps
        and raw_text.strip() == ""
    )
    if mention_only_drop:
        title = replace_text(dropped_titles[0]) or "文档链接"
        keep_nums = {
            num
            for num, pair in nta.items()
            if pair and pair[0] not in ("inline-component", "link-id")
        }
        canonical = {}
        old_to_new = {}
        new_pool = {}
        for old in sorted(keep_nums, key=int):
            pair = list(nta[old])
            if pair and pair[0] == "author":
                pair[1] = member_id
            key = json.dumps(pair, ensure_ascii=False, sort_keys=True)
            if key not in canonical:
                new_id = len(new_pool)
                canonical[key] = new_id
                new_pool[str(new_id)] = pair
            old_to_new[old] = canonical[key]
        if old_to_new:
            all_attrs = sorted(set(old_to_new.values()))
            new_attribs = "".join("*" + str(a) for a in all_attrs) + "+" + enc_len(utf16_len(title))
        else:
            new_pool = {"0": ["author", member_id]}
            new_attribs = "*0+" + enc_len(utf16_len(title))
        text["apool"] = {"nextNum": len(new_pool), "numToAttrib": new_pool}
        text["initialAttributedTexts"] = {
            "text": {"0": title},
            "attribs": {"0": new_attribs},
        }
        data["text"] = text
        return data

    keep_nums = set(int(num) for num in nta)
    for num in new_comps:
        keep_nums.add(num)

    link_id_nums = [
        int(num)
        for num, pair in nta.items()
        if pair and pair[0] == "link-id"
    ]
    if new_comps:
        keep_nums.update(link_id_nums)
    else:
        for num in link_id_nums:
            keep_nums.discard(num)

    canonical = {}
    old_to_new = {}
    new_pool = {}
    for old in sorted(keep_nums, key=int):
        if old in new_comps:
            pair = ["inline-component", json.dumps(new_comps[old], ensure_ascii=False)]
        else:
            pair = list(nta.get(str(old), ["author", member_id]))
            if pair and pair[0] == "author":
                pair[1] = member_id
        key = json.dumps(pair, ensure_ascii=False, sort_keys=True)
        if key not in canonical:
            new_id = len(new_pool)
            canonical[key] = new_id
            new_pool[str(new_id)] = pair
        old_to_new[old] = canonical[key]

    runs = replaced_runs
    author_id = next(
        (
            int(num)
            for num, pair in new_pool.items()
            if pair and pair[0] == "author"
        ),
        None,
    )
    if new_raw_text and author_id is None:
        author_id = len(new_pool)
        new_pool[str(author_id)] = ["author", member_id]
    new_runs = []
    for attrs, length, line_marker in runs:
        na = sorted(old_to_new[a] for a in attrs if a in old_to_new)
        if not na and author_id is not None:
            na = [author_id]
        if na:
            if (
                new_runs
                and new_runs[-1][0] == na
                and new_runs[-1][2] == line_marker
            ):
                new_runs[-1][1] += length
            else:
                new_runs.append([na, length, line_marker])

    if not new_pool and new_raw_text:
        new_pool = {"0": ["author", member_id]}
        new_runs = [[[0], utf16_len(new_raw_text), None]]

    text["apool"] = {"nextNum": len(new_pool), "numToAttrib": new_pool}
    text["initialAttributedTexts"] = {
        "text": {"0": new_raw_text},
        "attribs": {"0": encode_runs(new_runs)},
    }
    data["text"] = text
    return data


def collect_subtree_ids(source, old_root):
    bm = source["blockMap"]
    seen = set()
    stack = [old_root]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        data = bm[cur]["data"]
        for c in data.get("children") or []:
            stack.append(c)
        if data.get("type") == "table":
            for info in (data.get("cell_set") or {}).values():
                if info.get("block_id"):
                    stack.append(info["block_id"])
    return list(seen)


def build_subtree_blocks(source, old_root, new_root_id, member_id, remap):
    bm = source["blockMap"]
    old_ids = collect_subtree_ids(source, old_root)
    new_ids = {old: make_block_id() for old in old_ids}
    skipped = set()
    change_map = {}
    pending_images = {}

    for old in old_ids:
        src_data = bm[old]["data"]
        stype = src_data.get("type")
        if stype == "file":
            skipped.add(old)
            continue

        parent_old = src_data.get("parent_id") or source["sequence"][0]
        parent_new = new_ids.get(parent_old, new_root_id)

        if stype == "view":
            name, size = video_meta(source, src_data)
            data = make_text_block(
                parent_new,
                member_id,
                f"视频文件：{name}（{size}，需在飞书中手动上传）",
            )
        elif stype == "sheet":
            data = make_text_block(
                parent_new,
                member_id,
                "电子表格（原文档嵌入，需手动重建）",
            )
        else:
            data = json.loads(json.dumps(src_data))
            if isinstance(data.get("text"), dict):
                text_part = data["text"]
                rest = {k: v for k, v in data.items() if k != "text"}
                data = replace_deep(rest)
                data["text"] = text_part
            else:
                data = replace_deep(data)
            data["parent_id"] = parent_new
            data["children"] = [
                new_ids[c]
                for c in (src_data.get("children") or [])
                if c in new_ids and c not in skipped
            ]
            if stype == "table":
                cell_set = {}
                for key, info in (data.get("cell_set") or {}).items():
                    info = dict(info)
                    if info.get("block_id") in new_ids:
                        info["block_id"] = new_ids[info["block_id"]]
                    cell_set[key] = info
                data["cell_set"] = cell_set
            if stype == "image":
                image = dict(data.get("image") or {})
                image["token"] = ""
                image["src"] = ""
                pending_images[new_ids[old]] = (
                    image,
                    dict(src_data.get("image") or {}),
                )
                data["image"] = image
            if isinstance(data.get("text"), dict):
                data = transform_text_block(data, member_id, remap)

        data["author"] = member_id
        data.pop("comments", None)
        data.pop("area_comments", None)
        data.pop("revisions", None)
        change_map[new_ids[old]] = {
            "id": new_ids[old],
            "version": 0,
            "payload": {"ops": [{"p": [], "action": {"oi": data}}]},
        }

    return change_map, new_ids[old_root], pending_images, len(old_ids) - len(skipped)


def load_results():
    if RESULTS_FILE.exists():
        return json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    return []


def save_results(results):
    RESULTS_FILE.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def build_remap(results, link_remap):
    by_name = {
        r["name"]: r
        for r in results
        if r.get("status") in ("created", "complete")
    }
    remap = {}
    for source_token, target_name in (link_remap or {}).items():
        target = by_name.get(target_name)
        if target:
            remap[source_token] = target
    return remap, by_name


class Client(FeishuClient):
    async def change(self, change_map, page_id):
        return await self.change_blocks(page_id, change_map)

    async def fetch_state(self, obj_token):
        return await self.fetch_doc_state(obj_token)

    async def fetch_blocks(self, obj_token):
        return await self.fetch_doc_blocks(obj_token)


async def insert_all_blocks(client, source, obj_token, member_id, remap):
    state = await client.fetch_state(obj_token)
    root_id = state["root_id"]
    root_version = state["root_version"]
    child_count = state["child_count"]
    top_roots = source["blockMap"][source["sequence"][0]]["data"].get("children") or []

    batch_changes = {}
    batch_roots = []
    batch_blocks = 0
    pending_images = {}

    async def flush():
        nonlocal root_version, child_count, batch_changes, batch_roots, batch_blocks
        if not batch_changes:
            return
        ops = [
            {"p": ["children", child_count + i], "action": {"li": rid}}
            for i, rid in enumerate(batch_roots)
        ]
        batch_changes[root_id] = {
            "id": root_id,
            "version": root_version,
            "payload": {"ops": ops},
        }
        ok, res = await client.change(batch_changes, obj_token)
        if not ok:
            (WORK / "failed_batch.json").write_text(
                json.dumps(
                    {
                        "root_id": root_id,
                        "root_version": root_version,
                        "batch_changes": batch_changes,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            raise RuntimeError(f"insert failed: {res['body'][:1200]}")
        state = await client.fetch_state(obj_token)
        root_version = state["root_version"]
        child_count = state["child_count"]
        batch_changes = {}
        batch_roots = []
        batch_blocks = 0

    for old_root in top_roots:
        cm, rid, pend, nblocks = build_subtree_blocks(
            source, old_root, root_id, member_id, remap
        )
        batch_changes.update(cm)
        pending_images.update(pend)
        batch_roots.append(rid)
        batch_blocks += nblocks
        if batch_blocks >= MAX_BLOCKS_PER_REQUEST:
            await flush()

    await flush()
    state = await client.fetch_state(obj_token)
    return state, pending_images


def collect_source_images(source):
    bm = source["blockMap"]
    top_roots = bm[source["sequence"][0]]["data"].get("children") or []
    out = []
    for old_root in top_roots:
        for old in collect_subtree_ids(source, old_root):
            data = bm[old]["data"]
            if data.get("type") == "image":
                out.append((old, data))
    return out


async def pending_images_from_doc(client, source, obj_token):
    src_images = collect_source_images(source)
    blocks = await client.fetch_blocks(obj_token)
    seq = blocks["seq"]
    bm = blocks["bm"]
    cur_images = [
        bid for bid in seq if bm[bid]["data"].get("type") == "image"
    ]
    if len(cur_images) != len(src_images):
        raise RuntimeError(
            f"image count mismatch: source={len(src_images)} current={len(cur_images)}"
        )
    pending = {}
    for cur_id, (old_id, src_data) in zip(cur_images, src_images):
        image = dict(bm[cur_id]["data"].get("image") or {})
        pending[cur_id] = (image, dict(src_data.get("image") or {}))
    return pending


async def upload_all_images(client, obj_token, member_id, pending_images, manifest):
    by_token = {e["token"]: e for e in manifest}
    state = await client.fetch_state(obj_token)
    versions = state["block_versions"]
    updates = {}
    uploaded = 0

    for block_id, (image, source_image) in pending_images.items():
        old_token = source_image.get("token")
        entry = by_token.get(old_token)
        if not entry:
            print("MISSING_IMAGE", old_token, flush=True)
            continue
        path = WORK / "images" / f"{entry['token']}.{entry['ext']}"
        raw = path.read_bytes()
        new_token = await client.upload_image(
            block_id,
            obj_token,
            source_image,
            raw,
        )
        od = dict(image)
        image["token"] = new_token
        updates[block_id] = {
            "id": block_id,
            "version": versions.get(block_id, 1),
            "payload": {
                "ops": [{"p": ["image"], "action": {"od": od, "oi": image}}]
            },
        }
        uploaded += 1
        if len(updates) >= MAX_IMAGE_UPDATES_PER_REQUEST:
            ok, res = await client.change(updates, obj_token)
            if not ok:
                raise RuntimeError(f"image update failed: {res['body'][:1200]}")
            updates = {}
            state = await client.fetch_state(obj_token)
            versions = state["block_versions"]
        print("IMG_PROGRESS", obj_token, uploaded, len(pending_images), flush=True)

    if updates:
        ok, res = await client.change(updates, obj_token)
        if not ok:
            raise RuntimeError(f"image update failed: {res['body'][:1200]}")
    return uploaded


async def verify_doc(client, source, result):
    obj_token = result["obj_token"]
    state = await client.fetch_state(obj_token)
    bm = await client.page.evaluate(
        """async (obj) => {
            const r = await fetch(`/space/api/docx/pages/client_vars?id=${obj}&mode=7&limit=10000`, {credentials: 'include'});
            const j = await r.json();
            const seq = j.data.block_sequence;
            const b = j.data.block_map;
            return {
                total: seq.length,
                images: seq.filter(x => b[x].data.type === 'image').length,
                image_tokens: seq.filter(x => b[x].data.type === 'image' && (b[x].data.image || {}).token).length,
                texts: seq.map(x => ((b[x].data.text || {}).initialAttributedTexts || {}).text?.['0'] || '').join('\\n')
            };
        }""",
        obj_token,
    )
    print("VERIFY_STATE", result["name"], state, flush=True)
    for phrase in OLD_PHRASES:
        if phrase in bm["texts"]:
            raise RuntimeError(f"{result['name']}: still contains {phrase!r}")
    if bm["images"] != bm["image_tokens"]:
        raise RuntimeError(
            f"{result['name']}: image tokens missing {bm['images'] - bm['image_tokens']}"
        )

    page = await client.page.context.new_page()
    try:
        await page.goto(
            f"{TARGET_URL}/wiki/{result['wiki_token']}",
            wait_until="domcontentloaded",
            timeout=90000,
        )
        await page.wait_for_timeout(6000)
        dom = await page.evaluate(
            """() => {
                const main = document.querySelector('.page-main') || document.body;
                const imgs = [...document.querySelectorAll('img')];
                return {
                    title: document.title,
                    text: main.innerText,
                    imgs_ok: imgs.filter(i => i.naturalWidth > 0).length,
                    imgs_total: imgs.length,
                    links: [...document.querySelectorAll('a[data-token]')].map(a => ({text:(a.innerText||'').trim(), href:a.href, token:a.getAttribute('data-token')}))
                };
            }"""
        )
        print("VERIFY_DOM", result["name"], json.dumps(dom, ensure_ascii=False)[:1200], flush=True)
        for phrase in OLD_PHRASES:
            if phrase in dom["text"]:
                raise RuntimeError(f"{result['name']}: page text still contains {phrase!r}")
        await page.screenshot(path=str(WORK / f"verify_{result['name']}.png"), full_page=False)
    finally:
        await page.close()


async def rebuild_doc(client, source, name, member_id, remap, manifest, results):
    source_title = replace_text((source.get("title") or "").strip())
    result = next((r for r in results if r.get("name") == name), None)
    if result and result.get("status") in ("created", "complete"):
        obj_token = result["obj_token"]
        wiki_token = result["wiki_token"]
        print("RESUME", name, source_title, wiki_token, obj_token, flush=True)
    else:
        created = await client.create_doc(source_title)
        obj_token = created["obj_token"]
        wiki_token = created["wiki_token"]
        print("CREATED", name, source_title, wiki_token, obj_token, flush=True)
        result = {
            "name": name,
            "title": source_title,
            "wiki_token": wiki_token,
            "obj_token": obj_token,
            "url": f"{TARGET_URL}/wiki/{wiki_token}",
            "status": "created",
            "blocks": 0,
            "images": 0,
        }
        results.append(result)
        save_results(results)

    state = await client.fetch_state(obj_token)
    if state["child_count"] == 0:
        state, pending_images = await insert_all_blocks(
            client, source, obj_token, member_id, remap
        )
    else:
        pending_images = await pending_images_from_doc(
            client, source, obj_token
        )
    print("BLOCKS_DONE", name, state["child_count"], "images", len(pending_images), flush=True)

    uploaded = await upload_all_images(
        client, obj_token, member_id, pending_images, manifest
    )
    print("IMAGES_DONE", name, uploaded, flush=True)

    result["status"] = "complete"
    result["blocks"] = state["child_count"]
    result["images"] = uploaded
    save_results(results)
    return result


async def main():
    parser = argparse.ArgumentParser(description="Rebuild Feishu docs from captured data.")
    parser.add_argument("--config", required=True, help="Path to config JSON.")
    parser.add_argument("--only", help="Rebuild only this doc name.")
    parser.add_argument("--force", action="store_true", help="Ignore completed results.")
    parser.add_argument("--skip-verify", action="store_true", help="Skip DOM verification.")
    args = parser.parse_args()

    global WORK, RESULTS_FILE, TARGET_URL, TENANT_ID, MEMBER_ID, REPLACEMENTS, OLD_PHRASES
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    TARGET_URL = config["tenant_url"].rstrip("/")
    TENANT_ID = config.get("tenant_id")
    MEMBER_ID = config.get("member_id")
    REPLACEMENTS = [tuple(item) for item in config.get("replacements", [])]
    OLD_PHRASES = [item[0] for item in REPLACEMENTS]
    WORK = Path(config.get("output_dir", "work"))
    WORK.mkdir(parents=True, exist_ok=True)
    RESULTS_FILE = WORK / "rebuild_results.json"
    manifest = json.loads(
        (WORK / "images" / "manifest.json").read_text(encoding="utf-8")
    )
    results = load_results()

    pw, browser, page, csrf = await open_session(
        config.get("cdp_url", "http://127.0.0.1:9222"),
        TARGET_URL,
    )
    client = Client(
        page,
        csrf,
        TARGET_URL,
        MEMBER_ID,
        tenant_id=TENANT_ID,
        space_id=config.get("space_id"),
    )
    try:
        for name in [doc["name"] for doc in config.get("docs", [])]:
            if args.only and name != args.only:
                continue
            if args.force:
                results = [r for r in results if r.get("name") != name]
                save_results(results)
            if any(
                r.get("name") == name and r.get("status") == "complete"
                for r in results
            ) and not args.force:
                print("SKIP", name, flush=True)
                continue
            source = json.loads((WORK / f"full_{name}.json").read_text(encoding="utf-8"))
            remap, by_name = build_remap(results, config.get("link_remap"))
            missing = [
                target
                for source_token, target in (config.get("link_remap") or {}).items()
                if target not in by_name
            ]
            if missing:
                print("WARN_MISSING_LINK_TARGETS", sorted(set(missing)), flush=True)
            await rebuild_doc(
                client,
                source,
                name,
                MEMBER_ID,
                remap,
                manifest,
                results,
            )

        if not args.skip_verify:
            for result in results:
                if result.get("status") != "complete":
                    continue
                source = json.loads(
                    (WORK / f"full_{result['name']}.json").read_text(encoding="utf-8")
                )
                await verify_doc(client, source, result)
    finally:
        await pw.stop()

    print("ALL_DONE")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
