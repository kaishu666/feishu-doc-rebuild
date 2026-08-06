---
name: feishu-doc-rebuild
description: Use when a Feishu/Lark（飞书）document is read-only, protected, uneditable, or non-copyable（不能编辑、不能复制）and the user wants all its content organized/rebuilt into a new editable Feishu cloud doc or wiki page（飞书云文档/wiki）with matching formatting. Also use for batch rebuilding or organizing multiple Feishu docs, replacing specified text（替换文案）, remapping internal doc links, moving content into a Feishu wiki/cloud-doc or 教辅项目, preserving images and layout, and cleaning up generated test docs. Not for editing local Word/.docx files.
---

# 飞书文档重建（Feishu Doc Rebuild）

将不可编辑、不可复制的飞书/Lark 文档，在用户已登录的租户内重建为新的可编辑云文档/wiki，保留格式、替换文案并重映射内部链接。

## 工作流

1. 确认 Chrome 已用远程调试端口启动并登录目标飞书租户，浏览器里至少打开一个该租户页面。
2. 根据用户给的源文档链接整理配置 JSON，明确：
   - `docs`：源文档清单，包含 `name` 与 `source_token`。
   - `replacements`：需要替换的文案，例如 `["金旋学科", "教辅项目"]`。
   - `link_remap`：源文档 token 到新文档 `name` 的映射，用于重写文档内链接。
   - `output_dir`：本次任务的工作目录。
3. 运行 `scripts/capture_source_docs.py --config <config>`，抓取每个源文档的 block 数据并下载图片。
4. 运行 `scripts/rebuild_feishu_docs.py --config <config>`，创建新 wiki 文档、插入 block、上传图片、替换文案、重映射文档链接。
5. 核对 `build/rebuild_results.json` 与 `build/verify_*.png`，确认文档可打开、图片完整、旧文案已消失。
6. 仅在用户明确同意清理时，先 `scripts/cleanup_wiki.py --config <cleanup-config>` 查看删除清单，再 `--apply` 执行删除。

## 必读参考

- 接口、payload、批量与错误处理：读 `references/feishu-api.md`。
- 配置示例、执行顺序、验证清单与常见坑：读 `references/workflow.md`。

## 关键规则

- 不要试图绕过源文档的只读权限去复制；改走 API 重建，在原租户生成新的可编辑文档。
- 所有请求都通过已登录浏览器页面内的 `fetch` 发送，带上 `_csrf_token` 与 `credentials: 'include'`。
- 图片必须下载后重新上传，不能直接复用旧 token。
- 按 300 个 block 一批、20 个图片 token 一批提交变更；每批后重新读取 root version 与 child count。
- 用 `rebuild_results.json` 做断点续传；失败批次保存到 `failed_batch.json`。
- 删除 wiki 节点前先拉取实时树，用显式 keep-list 计算删除集合；默认 dry-run，只有 `--apply` 才真正删除。
- 遇到 HTTP 429 时指数退避重试，不要连续轰炸接口。

## 输出要求

- 新文档必须创建在飞书 wiki/云文档中，而不是 Word/DOCX；格式与源文档保持一致。
- 向用户报告每个新文档的 wiki URL、block 数、图片数，以及任何无法自动复制的视频/表格占位内容。
