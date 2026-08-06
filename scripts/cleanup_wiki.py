#!/usr/bin/env python3
"""Delete unexpected Feishu wiki nodes while preserving an explicit keep-list."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from feishu_client import FeishuClient, open_session


def collect_all_nodes(tree, root_wiki):
    nodes = tree.get("nodes") or {}
    child_map = tree.get("child_map") or {}
    seen = set(nodes)
    stack = list(child_map.get(root_wiki) or [])
    while stack:
        token = stack.pop()
        if token in seen:
            continue
        seen.add(token)
        stack.extend(child_map.get(token) or [])
    return seen


async def main():
    parser = argparse.ArgumentParser(description="Clean up unexpected Feishu wiki nodes.")
    parser.add_argument("--config", required=True, help="Path to config JSON.")
    parser.add_argument("--apply", action="store_true", help="Actually delete nodes.")
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    tenant_url = config["tenant_url"]
    space_id = config["space_id"]
    root_wiki = config["root_wiki"]
    keep = set(config.get("keep_wiki_tokens") or [])
    keep.add(root_wiki)
    out_dir = Path(config.get("output_dir", "work"))
    out_dir.mkdir(parents=True, exist_ok=True)

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
        space_id=space_id,
    )
    try:
        tree = await client.fetch_tree(space_id)
        all_nodes = collect_all_nodes(tree, root_wiki)
        nodes = tree.get("nodes") or {}
        delete_tokens = sorted(t for t in all_nodes if t not in keep)
        print(
            "BEFORE",
            len(all_nodes),
            "nodes",
            len(delete_tokens),
            "to delete" if args.apply else "would delete (dry-run)",
            flush=True,
        )
        for token in delete_tokens:
            print(
                "DEL_LIST",
                token,
                repr((nodes.get(token) or {}).get("title", "")),
                flush=True,
            )

        if args.apply and delete_tokens:
            failed = []
            for i, token in enumerate(delete_tokens, 1):
                ok = False
                res = None
                for attempt in range(6):
                    ok, res = await client.delete_wiki_node(space_id, token)
                    if ok or (res and res["status"] != 429):
                        break
                    print("RETRY_429", token, "attempt", attempt + 1, flush=True)
                    await page.wait_for_timeout(8000 * (attempt + 1))
                print(
                    "DEL",
                    i,
                    token,
                    repr((nodes.get(token) or {}).get("title", "")),
                    "OK" if ok else f"FAIL {res['status'] if res else None} {(res or {}).get('body', '')[:300]}",
                    flush=True,
                )
                if not ok:
                    failed.append(
                        {
                            "token": token,
                            "status": res["status"] if res else None,
                            "body": (res or {}).get("body", "")[:300],
                        }
                    )
            (out_dir / "cleanup_failed.json").write_text(
                json.dumps(failed, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        after = await client.fetch_tree(space_id)
        after_all = collect_all_nodes(after, root_wiki)
        after_root = after.get("root_list") or []
        print("AFTER_ROOT", after_root, flush=True)
        print("AFTER_COUNT", len(after_all), "roots", len(after_root), flush=True)
        unexpected = sorted(t for t in after_all if t not in keep)
        missing = sorted(t for t in keep if t != root_wiki and t not in after_all)
        print("UNEXPECTED", unexpected, flush=True)
        print("MISSING_KEEP", missing, flush=True)
        (out_dir / "tree_after.json").write_text(
            json.dumps(after, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if args.apply and unexpected:
            raise SystemExit(f"cleanup incomplete: {len(unexpected)} unexpected nodes remain")
    finally:
        await pw.stop()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
