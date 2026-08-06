# Feishu Web API Reference

These endpoints are the browser-side APIs used by Feishu's web app. They require a
logged-in Chrome session; do not use these paths outside the user's browser session.

## Table of Contents

1. Authentication via CDP
2. Doc block read
3. Wiki node create
4. Block mutation
5. Wiki tree read and delete
6. Image download
7. Image upload
8. Batching and errors

## Authentication via CDP

- Start Chrome with a remote debugging port, for example `--remote-debugging-port=9222`.
- Log in to the target Feishu tenant in Chrome and keep the session alive.
- Connect with Playwright: `chromium.connect_over_cdp("http://127.0.0.1:9222")`.
- Pick an existing page whose URL contains the tenant host, or navigate one page to
  `https://<tenant>.feishu.cn/drive/home/`.
- Read the `_csrf_token` cookie from that context and send it as `X-CSRFToken`.
- Always fetch with `credentials: 'include'` so the browser cookie is attached.

Standard JSON request headers:

```text
Content-Type: application/json;charset=UTF-8
X-CSRFToken: <_csrf_token>
x-lsc-terminal: web
x-lsc-version: 1
x-lsc-bizid: 2
x-lgw-terminal-type: 2
x-lgw-app-id: 1161
ccm-scene: web
```

## Doc block read

Read a document's full block tree:

```text
GET /space/api/docx/pages/client_vars?id=<obj_token>&mode=7&limit=239[&cursor=<cursor>]
```

- Response: `data.block_sequence` (ordered block ids), `data.block_map` (block data),
  `data.has_more`, `data.cursor`, `data.meta_map[<token>].title`.
- Use `limit=239` and follow `cursor` pages; merge sequences by de-duplicating ids.
- A single request with `limit=10000` also works for moderate docs, but paging is safer.
- The first id in `block_sequence` is the page root; its `children` are the top-level blocks.

## Wiki node create

Create a new editable wiki document under the space root:

```text
POST /space/api/wiki/v2/tree/create_node/
```

```json
{
  "title": "新文档标题",
  "ua_type": "Web",
  "scene": "wiki_create",
  "node_type": 0,
  "obj_type": 22
}
```

- Success returns `data.wiki_token` and `data.obj_token`.
- The new document URL is `https://<tenant>.feishu.cn/wiki/<wiki_token>`.

## Block mutation

Insert or update blocks with an OT-style change map:

```text
POST /space/api/docx/blocks/user_change
```

```json
{
  "member_id": "<member_id>",
  "uuid": "<random-uuid>",
  "page_id": "<obj_token>",
  "change_map": {
    "<block_id>": {
      "id": "<block_id>",
      "version": 0,
      "payload": {
        "ops": [
          {
            "p": [],
            "action": {
              "oi": { "type": "text", "parent_id": "<parent_id>", "children": [] }
            }
          }
        ]
      }
    }
  }
}
```

- Append top-level children by operating on the root block:
  `{"p": ["children", <index>], "action": {"li": "<child_id>"}}`.
- Update an image token with:
  `{"p": ["image"], "action": {"od": <old_image>, "oi": <new_image>}}`.
- Send each request with a fresh random `uuid`.
- Read `data.block_sequence` and each block's `version` before the next mutation.

## Wiki tree read and delete

Read the full wiki tree:

```text
GET /space/api/wiki/v2/tree/get_info/?space_id=<space_id>&with_space=false&with_perm=false&expand_shortcut=true&need_shared=true&exclude_fields=5
```

- `data.tree.nodes` maps wiki tokens to node metadata.
- `data.tree.child_map` maps parent wiki token to child wiki tokens.
- `data.tree.root_list` lists root nodes.

Delete a node:

```text
POST /space/api/wiki/v2/tree/del_node/
```

```json
{
  "space_id": "<space_id>",
  "wiki_token": "<wiki_token>",
  "auto_delete_mode": 0,
  "synergy_uuid": "<random-uuid>",
  "apply": 1
}
```

## Image download

Download an original image token from the source tenant:

```text
GET https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/preview/<token>?preview_type=16
```

- Requires the same logged-in browser session; use `credentials: 'include'`.
- Store each image as `<token>.<ext>` and keep a manifest with token, mime type,
  dimensions, size, name, and source docs.

## Image upload

Upload a local image into a new document with a four-step flow.

1. Prepare:

```text
POST https://internal-api-space.feishu.cn/space/api/box/upload/prepare/
```

```json
{
  "mount_point": "docx_image",
  "mount_node_token": "<block_id>",
  "name": "image.png",
  "size": 12345,
  "extra": {"drive_route_token": "<obj_token>"},
  "size_checker": false
}
```

2. Register chunks:

```text
POST https://internal-api-space.feishu.cn/space/api/box/upload/blocks/
```

Each chunk needs `hash` (SHA-256 base64), `seq`, `size`, `checksum`
(adler32 decimal), and `isUploaded: true`. The response lists
`needed_upload_blocks` that must actually be streamed.

3. Stream each needed chunk:

```text
POST https://internal-api-drive-stream.feishu.cn/space/api/box/stream/upload/merge_block/?upload_id=<id>&mount_point=docx_image
```

Send the raw bytes as `application/octet-stream` with `x-command`,
`x-block-list-checksum`, `x-block-origin-size`, and `x-seq-list` headers.

4. Finish:

```text
POST https://internal-api-space.feishu.cn/space/api/box/upload/finish/
```

```json
{
  "upload_id": "<id>",
  "num_blocks": 1,
  "mount_point": "docx_image",
  "push_open_history_record": 0
}
```

The finish response contains `data.file_token`, which becomes the new image token.

## Batching and errors

- Batch block insertion at about 300 blocks per request; refresh root version and
  child count after each successful request.
- Batch image token updates at about 20 blocks per request.
- Treat any JSON `code != 0` as failure and persist the payload for resume.
- On HTTP 429, back off with exponential delays (for example 8s, 16s, 24s) and retry.
- Before deleting anything, read the live tree; never delete from a stale snapshot.
