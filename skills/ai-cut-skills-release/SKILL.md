---
name: ai-cut-skills-release
description: "提交 AI Cut Skills 仓库改动并创建或更新 GitHub PR；适用于按组别和日期建分支、执行 Skill 校验、提交变更和推送 PR。"
---

# AI Cut Skills Release

这个 Skill 只处理当前 `ai-cut-skills` 仓库的提交和 GitHub PR 创建。默认只做静态预检，不执行仓库脚本或测试；用户要求运行检查时才加 `--run-tests`，明确要求提交时才使用 `--execute`。

## 工作流

1. 明确目标 Skill、组别、变更类型和摘要。
2. 执行模式显式刷新 `upstream/main` 并锁定 commit SHA；当前 `HEAD` 必须包含该基线，否则停止并要求先同步上游，不能把旧工作区内容当作回退改动提交。
3. 执行模式默认检查当前 GitHub 账户是否已有目标仓库的 fork；没有时自动创建，并等待 fork 可用。
4. 只带入目标 Skill 及显式指定的附加文件，不把其他工作区改动混入提交；符号链接路径会被拒绝，避免把仓库外文件复制进提交。
5. 提交前执行 Skill 结构校验、仓库清单校验、语法检查，以及仓库 `tests/` 和每个所选 Skill 的 `tests/` 下的 unittest。默认静态预检不执行清单脚本和 unittest，结果中明确标为未运行。
6. 生成规范 commit，推送到当前账户 fork，创建或更新对应 GitHub PR。
7. 更新已有 PR 时，PR 分支须包含最新基线，当前 `HEAD` 须包含 PR 全部提交；在 PR 原头提交上追加 commit 并普通快进推送，不重建覆盖历史。若只有已推送分支而无 PR，仅当它的单一父提交和完整文件树精确匹配本次提交时才重试创建 PR，不重复推送。

## 认证

使用本机现有的 GitHub CLI 登录态或 Git Credential Manager/SSH Agent。通过 `--github-account` 可以校验登录账户；不要把 GitHub 密码、Token 或私钥写入参数、配置、任务文件、日志或 PR 内容。
自动 fork 通过 GitHub CLI 完成，仍然只使用登录态，不接受或保存密码。已有的 `origin`（或 `--push-remote` 指定远端）如果缺失会添加为当前账户 fork；如果已指向其他仓库则停止，不自动改写。

## 命令

预检（不创建分支、不提交、不推送）：

```powershell
python skills/ai-cut-skills-release/scripts/submit_pr.py `
  --skill aivideoeditor-usergrowth-automation `
  --group 014-code `
  --change-type fix `
  --summary "restore ARLP platform multi-select" `
  --github-account 014-code
```

如用户明确要求运行仓库检查，可在预检命令中加 `--run-tests`。它会在可清理的 detached worktree 内运行清单脚本和测试，但 worktree **不是安全沙箱**，这些代码仍可访问本机文件、网络和其他进程；不要对不受信任的变更默认执行。

执行提交并创建 PR：

```powershell
python skills/ai-cut-skills-release/scripts/submit_pr.py `
  --skill aivideoeditor-usergrowth-automation `
  --group 014-code `
  --change-type fix `
  --summary "restore ARLP platform multi-select" `
  --github-account 014-code `
  --execute
```

多个 Skill 或测试文件需要显式重复 `--skill` / `--include`；所有包含路径都按字面值处理，不展开 Git 通配符或 pathspec magic。`--target-repository` 只能显式指定与 `upstream` 相同的仓库，不能跨仓库写入。没有目标变更、校验失败、GitHub 账户不匹配、同名远端分支不满足安全复用条件，或 PR 已产生不可安全判断的冲突时停止并报告，不覆盖现有工作。

如需跳过 GitHub fork 的创建和 parent 校验，可显式增加 `--no-auto-fork`。该选项仍会严格校验推送远端的 fetch URL 和所有有效 push URL 必须指向当前账户 fork，不会放宽提交范围、分支和 PR 安全校验。

默认静态预检不创建 worktree；只有 `--run-tests` 才创建检查 worktree，`--execute` 才提交和推送。已有受管 PR 的更新不使用任何 force 选项，并发远端更新导致非快进时停止。这个 Skill 只创建或更新 PR，不自动审批或合并。

详细的分支、提交范围和 PR 规则见 [references/release-policy.md](references/release-policy.md)。
