"""Shared Feishu web-API client used by the feishu-doc-rebuild skill."""

import base64
import hashlib
import json
import uuid
import zlib


WEB_HEADERS = {
    "Content-Type": "application/json;charset=UTF-8",
    "x-lsc-terminal": "web",
    "x-lsc-version": "1",
    "x-lsc-bizid": "2",
    "x-lgw-terminal-type": "2",
    "x-lgw-app-id": "1161",
    "ccm-scene": "web",
}


async def open_session(cdp_url, tenant_url, start_path="/drive/home/"):
    """Connect to an already-logged-in Chrome via CDP and return an API client."""
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(cdp_url)
    context = browser.contexts[0]
    page = next((p for p in context.pages if tenant_url in p.url), None)
    if page is None:
        page = context.pages[0]
        await page.goto(
            f"{tenant_url.rstrip('/')}{start_path}",
            wait_until="domcontentloaded",
            timeout=60000,
        )
    cookies = await context.cookies(tenant_url)
    csrf = next(
        (c["value"] for c in cookies if c["name"].lower() == "_csrf_token"),
        None,
    )
    if not csrf:
        raise RuntimeError("No _csrf_token cookie found; log in to Feishu in Chrome first.")
    return pw, browser, page, csrf


class FeishuClient:
    def __init__(
        self,
        page,
        csrf,
        tenant_url,
        member_id,
        tenant_id=None,
        space_id=None,
        upload_host="https://internal-api-space.feishu.cn",
        stream_host="https://internal-api-drive-stream.feishu.cn",
    ):
        self.page = page
        self.csrf = csrf
        self.tenant_url = tenant_url.rstrip("/")
        self.member_id = member_id
        self.tenant_id = tenant_id
        self.space_id = space_id
        self.upload_host = upload_host.rstrip("/")
        self.stream_host = stream_host.rstrip("/")

    async def post_json(self, url, data):
        headers = dict(WEB_HEADERS)
        headers["X-CSRFToken"] = self.csrf
        return await self.page.evaluate(
            """async ({url, data, headers}) => {
                const r = await fetch(url, {
                    method: 'POST',
                    credentials: 'include',
                    headers: headers,
                    body: JSON.stringify(data)
                });
                const t = await r.text();
                return {status: r.status, body: t};
            }""",
            {"url": url, "data": data, "headers": headers},
        )

    async def get_json(self, url):
        headers = {"X-CSRFToken": self.csrf}
        return await self.page.evaluate(
            """async ({url, headers}) => {
                const r = await fetch(url, {
                    method: 'GET',
                    credentials: 'include',
                    headers: headers
                });
                return {status: r.status, body: await r.text()};
            }""",
            {"url": url, "headers": headers},
        )

    async def post_bytes(self, url, raw, headers):
        b64 = base64.b64encode(raw).decode("ascii")
        return await self.page.evaluate(
            """async ({url, b64, headers}) => {
                const bin = atob(b64);
                const bytes = new Uint8Array(bin.length);
                for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
                const r = await fetch(url, {
                    method: 'POST',
                    credentials: 'include',
                    headers: headers,
                    body: bytes
                });
                return {status: r.status, body: await r.text()};
            }""",
            {"url": url, "b64": b64, "headers": headers},
        )

    async def fetch_doc_state(self, obj_token):
        return await self.page.evaluate(
            """async (obj) => {
                const r = await fetch(`/space/api/docx/pages/client_vars?id=${obj}&mode=7&limit=10000`, {credentials: 'include'});
                const j = await r.json();
                const seq = j.data.block_sequence;
                const bm = j.data.block_map;
                const root = bm[seq[0]];
                return {
                    root_id: root.id,
                    root_version: root.version,
                    child_count: seq.length - 1,
                    member_id: j.data.member_id || null,
                    block_versions: Object.fromEntries(Object.entries(bm).map(([k, v]) => [k, v.version]))
                };
            }""",
            obj_token,
        )

    async def fetch_doc_blocks(self, obj_token):
        return await self.page.evaluate(
            """async (obj) => {
                const r = await fetch(`/space/api/docx/pages/client_vars?id=${obj}&mode=7&limit=10000`, {credentials: 'include'});
                const j = await r.json();
                return {seq: j.data.block_sequence, bm: j.data.block_map};
            }""",
            obj_token,
        )

    async def create_doc(self, title):
        res = await self.post_json(
            f"{self.tenant_url}/space/api/wiki/v2/tree/create_node/",
            {
                "title": title,
                "ua_type": "Web",
                "scene": "wiki_create",
                "node_type": 0,
                "obj_type": 22,
            },
        )
        data = json.loads(res["body"])
        if data.get("code") != 0:
            raise RuntimeError(f"create failed: {res['body'][:800]}")
        return data["data"]

    async def change_blocks(self, page_id, change_map):
        payload = {
            "member_id": self.member_id,
            "uuid": str(uuid.uuid4()),
            "page_id": page_id,
            "change_map": change_map,
        }
        res = await self.post_json(
            f"{self.tenant_url}/space/api/docx/blocks/user_change",
            payload,
        )
        ok = '"code":0' in res["body"] or '"code": 0' in res["body"]
        return ok, res

    async def fetch_tree(self, space_id):
        url = (
            f"{self.tenant_url}/space/api/wiki/v2/tree/get_info/"
            f"?space_id={space_id}&with_space=false&with_perm=false"
            "&expand_shortcut=true&need_shared=true&exclude_fields=5"
        )
        res = await self.get_json(url)
        j = json.loads(res["body"])
        if j.get("code") != 0:
            raise RuntimeError(f"tree fetch failed: {res['body'][:800]}")
        return j["data"]["tree"]

    async def delete_wiki_node(self, space_id, wiki_token):
        res = await self.post_json(
            f"{self.tenant_url}/space/api/wiki/v2/tree/del_node/",
            {
                "space_id": space_id,
                "wiki_token": wiki_token,
                "auto_delete_mode": 0,
                "synergy_uuid": str(uuid.uuid4()),
                "apply": 1,
            },
        )
        ok = res["status"] == 200 and (
            '"code":0' in res["body"] or '"code": 0' in res["body"]
        )
        return ok, res

    async def upload_image(self, block_id, obj_token, image_meta, raw):
        name = image_meta.get("name") or f"{block_id}.img"
        prepare = await self.post_json(
            f"{self.upload_host}/space/api/box/upload/prepare/",
            {
                "mount_point": "docx_image",
                "mount_node_token": block_id,
                "name": name,
                "size": len(raw),
                "extra": {"drive_route_token": obj_token},
                "size_checker": False,
            },
        )
        prepare_data = json.loads(prepare["body"])
        pdata = prepare_data["data"]
        upload_id = pdata["upload_id"]
        block_size = pdata["block_size"]
        chunks = [raw[i : i + block_size] for i in range(0, len(raw), block_size)]

        blocks_res = await self.post_json(
            f"{self.upload_host}/space/api/box/upload/blocks/",
            {
                "blocks": [
                    {
                        "hash": base64.b64encode(hashlib.sha256(c).digest()).decode(),
                        "seq": seq,
                        "size": len(c),
                        "checksum": str(zlib.adler32(c) & 0xFFFFFFFF),
                        "isUploaded": True,
                    }
                    for seq, c in enumerate(chunks)
                ],
                "upload_id": upload_id,
                "mount_point": "docx_image",
            },
        )
        blocks_data = json.loads(blocks_res["body"])
        needed = ((blocks_data.get("data") or {}).get("needed_upload_blocks")) or []
        for nb in needed:
            seq = nb["seq"]
            chunk = chunks[seq]
            merge_url = (
                f"{self.stream_host}/space/api/box/stream/upload/merge_block/"
                f"?upload_id={upload_id}&mount_point=docx_image"
            )
            headers = {
                "accept": "application/json, text/plain, */*",
                "content-type": "application/octet-stream",
                "origin": self.tenant_url,
                "referer": f"{self.tenant_url}/",
                "x-csrftoken": self.csrf,
                "x-command": "space.api.box.stream.upload.merge_block",
                "x-block-list-checksum": str(zlib.adler32(chunk) & 0xFFFFFFFF),
                "x-block-origin-size": str(block_size),
                "x-lgw-app-id": "1161",
                "x-lgw-os-type": "1",
                "x-lgw-terminal-type": "2",
                "x-lsc-bizid": "2",
                "x-lsc-terminal": "web",
                "x-lsc-version": "1",
                "x-seq-list": str(seq),
            }
            merge_res = await self.post_bytes(merge_url, chunk, headers)
            if merge_res["status"] != 200:
                raise RuntimeError(
                    f"image merge failed for {block_id} seq={seq}: {merge_res['body'][:500]}"
                )
            mj = json.loads(merge_res["body"])
            if mj.get("code") != 0:
                raise RuntimeError(
                    f"image merge code for {block_id} seq={seq}: {merge_res['body'][:500]}"
                )

        finish = await self.post_json(
            f"{self.upload_host}/space/api/box/upload/finish/",
            {
                "upload_id": upload_id,
                "num_blocks": len(chunks),
                "mount_point": "docx_image",
                "push_open_history_record": 0,
            },
        )
        finish_data = json.loads(finish["body"])
        return finish_data["data"]["file_token"]

    async def download_image(self, token):
        url = (
            f"{self.stream_host}/space/api/box/stream/download/preview/"
            f"{token}?preview_type=16"
        )
        return await self.page.evaluate(
            """async (u) => {
                const res = await fetch(u, { credentials: 'include' });
                if (!res.ok) return {ok: false, status: res.status};
                const bytes = new Uint8Array(await res.arrayBuffer());
                let binary = '';
                const CHUNK = 0x8000;
                for (let i = 0; i < bytes.length; i += CHUNK) {
                    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
                }
                return {ok: true, base64: btoa(binary), size: bytes.byteLength};
            }""",
            url,
        )
