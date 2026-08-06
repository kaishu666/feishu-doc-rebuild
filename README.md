# 飞书文档重建（Feishu Doc Rebuild）

把不可编辑、不可复制的飞书/Lark 文档，重建为新的可编辑飞书云文档或 wiki 页面，保留原有格式、图片与布局，并支持替换文案、重映射内部链接、批量处理多份文档。

Rebuild read-only or non-copyable Feishu/Lark docs into new editable Feishu cloud docs or wiki pages. Preserve formatting and images, replace text, remap internal links, and rebuild multiple docs in one batch.

## 用途 / Use cases

- 源文档禁止编辑或复制，需要整理出一份格式一致的新文档。
- 需要批量重建多个飞书文档。
- 需要替换文档中的指定文案，例如把 `金旋学科` 改为 `教辅项目`。
- 需要把内容搬进飞书 wiki 或云文档，并重写文档内链接。
- 需要在用户明确同意后清理生成的测试文档。

## 安装 / Install

```bash
git clone https://github.com/kaishu666/feishu-doc-rebuild.git "$HOME/.codex/skills/feishu-doc-rebuild"
```

然后在 Codex 等支持 Skills 的 Agent 中引用 `feishu-doc-rebuild`。

## 工作流 / Workflow

1. 使用已登录目标飞书租户的浏览器（建议开启远程调试端口），并至少打开一个该租户页面。
2. 根据源文档链接准备配置 JSON：`docs`、`replacements`、`link_remap`、`output_dir`。
3. 运行 `scripts/capture_source_docs.py --config <config>` 抓取 block 数据并下载图片。
4. 运行 `scripts/rebuild_feishu_docs.py --config <config>` 创建新文档、插入 block、上传图片、替换文案、重映射链接。
5. 核对 `build/rebuild_results.json` 与 `build/verify_*.png`。
6. 仅在用户明确同意清理时，通过 `scripts/cleanup_wiki.py --config <cleanup-config>` 查看清单，再 `--apply` 执行删除。

## 目录 / Structure

- `SKILL.md`：技能说明与触发条件。
- `agents/openai.yaml`：Agent 市场元数据。
- `scripts/`：抓取、重建、清理脚本。
- `references/`：飞书 API 与工作流参考文档。

## 说明 / Notes

- 新文档创建在飞书 wiki/云文档中，不是本地 Word/DOCX。
- 不修改源文档，也不绕过源文档的只读权限。
- 图片会下载后重新上传，不会直接复用旧 token。
- 清理操作默认 dry-run，只有 `--apply` 才真正删除。

