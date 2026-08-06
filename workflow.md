# Feishu Doc Rebuild Workflow

## Configuration

Create a JSON config, for example `build/config.json`:

```json
{
  "tenant_url": "https://your-tenant.feishu.cn",
  "cdp_url": "http://127.0.0.1:9222",
  "member_id": "1234567890",
  "tenant_id": "1234567890",
  "space_id": "1234567890123456789",
  "output_dir": "build",
  "docs": [
    {"name": "sop", "source_token": "abc..."},
    {"name": "doc1", "source_token": "def..."}
  ],
  "replacements": [
    ["旧品牌名", "新项目名"]
  ],
  "link_remap": {
    "abc...": "sop",
    "def...": "doc1"
  }
}
```

- `docs` controls creation order and source capture order.
- `replacements` rewrites text anywhere in block data, including titles and mentions.
- `link_remap` points old source doc tokens to new target doc names so internal
  document mentions become links to the rebuilt docs.
- For cleanup, add `root_wiki` and `keep_wiki_tokens` to the same config or a
  separate cleanup config:

```json
{
  "tenant_url": "https://your-tenant.feishu.cn",
  "cdp_url": "http://127.0.0.1:9222",
  "space_id": "1234567890123456789",
  "root_wiki": "wiki-root-token",
  "keep_wiki_tokens": ["wiki-token-to-keep"],
  "output_dir": "build"
}
```

## Run Order

1. Confirm Chrome is running with remote debugging and is logged in to the tenant.
2. Capture source docs and images:

```bash
python scripts/capture_source_docs.py --config build/config.json
```

This writes `build/full_<name>.json` plus `build/images/manifest.json` and the
downloaded image files. Re-run to fill in gaps; existing files are skipped.

3. Rebuild the docs:

```bash
python scripts/rebuild_feishu_docs.py --config build/config.json
```

This creates new wiki docs, inserts all blocks in 300-block batches, uploads images,
remaps document mentions, and verifies block counts, image tokens, and rendered DOM.
Use `--only <name>` for one doc and `--force` to redo a completed doc.

4. Inspect the result URLs and screenshots in `build/verify_<name>.png`.
5. If the user confirms cleanup of probe/test docs, first list candidates:

```bash
python scripts/cleanup_wiki.py --config build/cleanup_config.json
```

Then delete only after reviewing the list:

```bash
python scripts/cleanup_wiki.py --config build/cleanup_config.json --apply
```

## Validation Checklist

- Each target doc has a wiki URL that opens and shows the same structure as the source.
- Every image block has a non-empty token and renders in the DOM.
- Old replacement phrases do not appear in API text or rendered page text.
- Links that pointed to source docs now point to rebuilt docs.
- Embedded videos and sheets are reported as manual rebuild placeholders, never silently dropped.
- After cleanup, `AFTER_COUNT` and `UNEXPECTED []` confirm only intended nodes remain.

## Common Pitfalls

- Do not try to copy images by reusing source tokens across tenants; always download
  then upload into the target doc.
- Keep `rebuild_results.json`; it is the resume checkpoint. If a batch fails, the
  payload is saved to `failed_batch.json` for diagnosis.
- When a text run is replaced, preserve the original attributed-text encoding:
  rebuild `attribs` with the same length base-36 encoding, keeping author and
  link/inline-component attributes.
- Do not run cleanup against a stale tree. Fetch the live tree first and compute the
  delete list from the current nodes.
- Prefer an explicit keep-list for cleanup. Never delete everything below the root
  without user confirmation.
