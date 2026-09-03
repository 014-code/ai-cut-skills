# Release Policy

## 分支

分支名由命令参数生成：

```text
<group>/<change-type>-<scope>-<YYYYMMDD>
```

`group` 必须是安全的 Git 分支前缀，例如 `014-code`；`scope` 默认由目标 Skill 名称组成，也可以用 `--scope` 覆盖。日期默认使用本地当天日期，测试或补交历史改动时可以用 `--date YYYYMMDD` 指定。

## 变更范围

目标 Skill 通过重复的 `--skill` 指定，额外测试或清单文件必须通过 `--include` 明确指定。脚本会从最新基线重建临时 worktree，再把这些路径的已提交、已暂存和未暂存差异应用到新分支，避免把当前工作区的其他改动带入 PR。

运行时缓存、编译产物和其他未列入允许范围的文件不会被提交。
符号链接路径也不允许进入变更范围；脚本会在收集变更和复制未跟踪文件时检查每一级路径，避免跟随链接读取仓库外内容。

## 校验

提交前依次执行：

- `quick_validate.py` 校验每个目标 Skill 的 frontmatter、目录和占位内容；
- `scripts/sync_skills.py --check` 校验仓库 catalog；
- Python 文件语法检查；
- `git diff --check`；
- 仓库 `tests/` 和本次目标 Skill `skills/<skill>/tests/` 下的 unittest（可用 `--skip-tests` 明确跳过）。

任何失败都会阻止 commit 和 push。提交和本地钩子完成后，脚本还会检查最终 commit 的变更路径；越界时不会 push。校验结果会写入 PR 描述，不写入凭据。

## GitHub Fork 与 PR

仓库当前以 `upstream` 作为 PR 基线，以 `origin` 作为推送远端。脚本从 upstream remote 解析唯一目标仓库，使用 GitHub CLI 当前登录账户作为 head owner；`--github-account` 仅用于账户一致性校验。`--target-repository` 如果提供，必须与 upstream 解析出的仓库完全一致，否则停止，禁止跨仓库写入。

执行模式默认按以下顺序准备 fork：

1. 用 `gh api repos/<当前账户>/<仓库>` 查询 fork。
2. 已存在时必须确认 `fork=true`，且 `parent.full_name` 与 upstream 完全一致；不符合就停止。
3. 不存在（404）时执行 `gh repo fork <upstream> --clone=false`，再轮询 GitHub API 直到 fork 可用；其他 API 错误不会被当作“需要创建 fork”。
4. 推送远端不存在时添加当前账户 fork URL；已有远端若指向其他仓库，停止且不改写。

计划模式是只读的，不会创建或修改 fork；会在临时 detached worktree 中运行可能产生文件的检查，结束后清理。需要跳过 GitHub fork 创建和 parent 校验时可使用 `--no-auto-fork`，但仍必须确认 fetch URL 以及所有有效 push URL 都指向当前账户 fork，并遵守远端分支和 PR 的安全校验。

同一 head 分支已有打开的 PR 时更新标题和正文，不重复创建。只有在 PR 编号、URL、head 分支、head owner 和 base 分支字段全部存在且精确匹配时才允许复用；字段缺失时按不可复用处理。已有远端分支会先刷新并用 `git merge-tree --write-tree` 检查与最新基线的可合并性，再以该远端分支为更新基线，保留已有提交。执行前会先检查远端是否已有同名分支；临时 worktree 结束后会清理脚本创建的本地分支引用：

- 没有远端分支：使用普通 `git push`；
- 有远端分支且存在当前账户指向目标 base 的打开 PR：只用 `--force-with-lease=<expected-sha>` 更新；
- 有远端分支但没有可复用的打开 PR：仅当顶部提交包含本 Skill 写入的受管提交标记时允许安全重试，否则停止，避免覆盖未知改动。

推送前会分别读取 remote 的 fetch URL 和 `remote.<name>.pushurl` 展开的所有有效 push URL。任何 URL 不是当前账户 fork 都会停止；不能只依赖 fetch URL 判断推送目标。

PR 创建和更新都要求 GitHub CLI 已登录并具备目标仓库权限。PR 的 head owner、head branch 和 base branch 必须与本次执行一致。

如果分支已成功推送但 PR API 返回不确定错误，脚本不会自动删除远端分支；提交中的受管标记用于后续重试，避免把可能已经创建成功的 PR 置于无效状态。

合并不属于本 Skill 的自动动作；提交 Skill 只负责生成可审查的 PR。
