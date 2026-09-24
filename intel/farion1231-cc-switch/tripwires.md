---
repo: farion1231/cc-switch
sha: f8788719a19be6cdef39151b1c0607b4608fa10a
date: 2026-09-24
---

# tripwires — cc-switch

## 2026-09-24 ｜ 是否支持按项目目录自动应用不同的供应商配置（project-scoped profiles）？

**结论：false。**

cc-switch 有"项目 Profile"功能，但它是**用户命名的配置快照、手动一键切换**，与文件系统目录无任何绑定，也没有按 cwd 自动应用机制：

- `profiles` 表结构只有 `id / name / payload / sort_order / created_at / updated_at`，无目录/路径字段（`src-tauri/src/database/dao/profiles.rs:20-28`，建表语句 `database/schema.rs:344-351`）。
- `apply_profile` 命令签名仅接受 `id` 与 `scope`（claude/claude-desktop/codex 三分组），无目录参数，由用户从头部切换器或托盘菜单手动触发（`src-tauri/src/commands/profile.rs:165-171`；前端 `src/components/profiles/ProfileSwitcher.tsx:90` 调用 `applyMutation.mutate({ id, scope })`）。
- 全后端 grep `cwd / working_dir / project_dir / project_path / current_dir` 仅命中 `session_manager`（解析会话 JSONL 里的会话元数据）与打开终端等场景，不存在 cwd→profile 映射逻辑。
- 官方 CHANGELOG 对该功能的描述即"named 'project' + one-click re-apply"，全程手动语义（`CHANGELOG.md:597`）。

最接近的能力是切换离开时**自动回存**当前 scope 的快照（`services/profile.rs:342` autosave），但触发者仍是用户发起的切换动作，且粒度是"命名项目"而非"目录"。
