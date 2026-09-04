#!/usr/bin/env python3
"""Validate and submit a scoped change from the ai-cut-skills repository."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
CHANGE_TYPES = {"feat", "fix", "docs", "refactor", "test", "chore"}
EXCLUDED_NAMES = {
    ".DS_Store",
    ".idea",
    ".cache",
    ".npm",
    ".ruff_cache",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
}
EXCLUDED_SUFFIXES = (".pyc", ".pyo")


class ReleaseError(RuntimeError):
    """A validation or repository operation failed."""

    def __init__(self, message: str) -> None:
        # Git diagnostics can echo configured URLs containing credentials.
        safe = re.sub(r"(?i)\b(?:https?|ssh)://[^\s]+", "[REDACTED_URL]", str(message))
        safe = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[opusr]_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+)",
                      "[REDACTED_SECRET]", safe)
        super().__init__(safe)


@dataclass(frozen=True)
class ReleaseConfig:
    repo_root: Path
    skills: tuple[str, ...]
    includes: tuple[str, ...]
    group: str
    change_type: str
    summary: str
    scope: str
    date: str
    base_remote: str
    base_branch: str
    push_remote: str
    github_account: str | None
    target_repository: str | None
    execute: bool
    skip_tests: bool
    keep_worktree: bool
    auto_fork: bool
    run_tests: bool = False


def run_command(
    args: list[str],
    cwd: Path,
    *,
    check: bool = True,
    input_text: str | None = None,
) -> str:
    completed = subprocess.run(
        args,
        cwd=str(cwd),
        input=input_text,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise ReleaseError(f"命令失败：{' '.join(args)}\n{detail[-2000:]}")
    return completed.stdout.strip()


def run_bytes(args: list[str], cwd: Path) -> bytes:
    completed = subprocess.run(args, cwd=str(cwd), capture_output=True)
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise ReleaseError(f"命令失败：{' '.join(args)}\n{detail[-2000:]}")
    return completed.stdout


def validate_segment(value: str, field: str) -> str:
    value = value.strip()
    if not value or not SAFE_SEGMENT.fullmatch(value) or value in {".", ".."}:
        raise ReleaseError(f"{field} 不是合法的分支片段：{value!r}")
    return value


def validate_date(value: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise ReleaseError("日期必须是 YYYYMMDD") from exc
    return parsed.strftime("%Y%m%d")


def build_branch_name(group: str, change_type: str, scope: str, date: str) -> str:
    group = validate_segment(group, "组别")
    change_type = validate_segment(change_type, "变更类型")
    scope = validate_segment(scope, "作用域")
    date = validate_date(date)
    return f"{group}/{change_type}-{scope}-{date}"


def normalize_relative_path(value: str) -> str:
    path = Path(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ReleaseError(f"路径必须位于仓库内：{value!r}")
    return path.as_posix()


def selected_pathspecs(config: ReleaseConfig) -> list[str]:
    paths = [f"skills/{skill}" for skill in config.skills]
    paths.extend(config.includes)
    return list(dict.fromkeys(paths))


def path_is_allowed(path: str, pathspecs: list[str]) -> bool:
    candidate = Path(path.replace("\\", "/"))
    return any(candidate == Path(spec) or Path(spec) in candidate.parents for spec in pathspecs)


def validate_skill_paths(repo_root: Path, skills: tuple[str, ...]) -> None:
    for skill in skills:
        if not SAFE_SEGMENT.fullmatch(skill) or skill in {".", ".."}:
            raise ReleaseError(f"Skill 名称不合法：{skill!r}")
        skill_root = repo_root / "skills" / skill
        if not (skill_root / "SKILL.md").is_file():
            raise ReleaseError(f"目标 Skill 不存在或缺少 SKILL.md：{skill_root}")


def list_changed_paths(repo_root: Path, base_ref: str | None, pathspecs: list[str]) -> tuple[list[str], list[str]]:
    diff_args = ["git", "--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv", "--name-only"]
    if base_ref:
        diff_args.append(base_ref)
    else:
        # Include both staged and unstaged changes during a read-only plan.
        diff_args.append("HEAD")
    diff_args.extend(["--", *pathspecs])
    try:
        tracked_output = run_command(diff_args, repo_root)
    except ReleaseError:
        if base_ref:
            raise
        # Repositories without a first commit cannot diff against HEAD yet.
        tracked_output = run_command(["git", "--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv", "--name-only", "--", *pathspecs], repo_root)
    tracked = [line for line in tracked_output.splitlines() if line]
    untracked = [
        line
        for line in run_command(
            ["git", "--literal-pathspecs", "ls-files", "--others", "--exclude-standard", "--", *pathspecs],
            repo_root,
        ).splitlines()
        if line
    ]
    return list(dict.fromkeys(tracked)), list(dict.fromkeys(untracked))


def quick_validator_path() -> Path | None:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    candidate = codex_home / "skills" / ".system" / "skill-creator" / "scripts" / "quick_validate.py"
    return candidate if candidate.is_file() else None


def run_checks(repo_root: Path, config: ReleaseConfig, *, changed_paths: list[str]) -> dict[str, object]:
    checks: list[dict[str, object]] = []

    def check(name: str, args: list[str]) -> None:
        try:
            output = run_command(args, repo_root)
            checks.append({"name": name, "ok": True, "output": output[-2000:]})
        except ReleaseError as exc:
            checks.append({"name": name, "ok": False, "output": str(exc)[-2000:]})

    validator = quick_validator_path()
    for skill in config.skills:
        if validator:
            check(
                f"quick_validate:{skill}",
                [sys.executable, "-X", "utf8", str(validator), str(repo_root / "skills" / skill)],
            )
        else:
            checks.append({"name": f"quick_validate:{skill}", "ok": False, "output": "找不到 quick_validate.py"})

    sync_script = repo_root / "scripts" / "sync_skills.py"
    allow_repo_code = config.execute or config.run_tests
    if sync_script.is_file() and allow_repo_code:
        check("catalog", [sys.executable, "-X", "utf8", str(sync_script), "--check"])
    elif sync_script.is_file():
        checks.append({"name": "catalog", "ok": True, "skipped": True, "output": "repository code requires --run-tests or --execute"})

    python_files = []
    for skill in config.skills:
        python_files.extend((repo_root / "skills" / skill).rglob("*.py"))
    if python_files:
        try:
            for path in python_files:
                source = path.read_text(encoding="utf-8")
                compile(source, str(path), "exec")
            checks.append({"name": "python_syntax", "ok": True, "output": ""})
        except (OSError, SyntaxError, UnicodeError) as exc:
            checks.append({"name": "python_syntax", "ok": False, "output": str(exc)[-2000:]})

    check("diff_check", ["git", "diff", "--no-ext-diff", "--no-textconv", "--check"])
    check("cached_diff_check", ["git", "diff", "--no-ext-diff", "--no-textconv", "--cached", "--check"])

    if config.skip_tests:
        checks.append({"name": "tests", "ok": True, "skipped": True, "output": "skipped by --skip-tests"})
    elif not allow_repo_code:
        checks.append({"name": "tests", "ok": True, "skipped": True, "output": "repository code requires --run-tests or --execute"})
    else:
        test_roots = [("tests", Path("tests"))]
        test_roots.extend(
            (f"tests:{skill}", Path("skills") / skill / "tests")
            for skill in config.skills
        )
        for name, relative in test_roots:
            tests_root = repo_root / relative
            if tests_root.is_dir() and any(tests_root.glob("test_*.py")):
                # Separate interpreters prevent same-named test modules in
                # different Skills from shadowing one another.
                check(name, [
                    sys.executable, "-X", "utf8", "-m", "unittest", "discover",
                    "-s", relative.as_posix(), "-p", "test_*.py", "-v",
                ])
            else:
                checks.append({"name": name, "ok": True, "skipped": True, "output": f"no unittest files in {relative}"})

    checks.append({"name": "changed_paths", "ok": True, "output": "\n".join(changed_paths)})
    return {"ok": all(bool(item.get("ok")) for item in checks), "checks": checks}


def remote_repository(repo_root: Path, remote: str) -> str:
    remote = validate_segment(remote, "远端名称")
    url = run_command(["git", "remote", "get-url", remote], repo_root)
    return repository_from_url(url)


def repository_from_url(url: str) -> str:
    """Parse a GitHub remote URL without consulting the local Git config."""
    url = url.strip()
    if url.startswith("git@github.com:"):
        value = url.split(":", 1)[1]
    else:
        try:
            parsed = urlparse(url)
        except ValueError as exc:
            raise ReleaseError("远端 URL 格式无效") from exc
        if parsed.scheme not in {"https", "ssh"}:
            raise ReleaseError("远端 URL 仅允许 HTTPS 或 SSH 安全传输")
        if parsed.hostname != "github.com":
            raise ReleaseError("远端不是 GitHub 仓库")
        if (parsed.password or parsed.query or parsed.fragment or
                (parsed.username and not (parsed.scheme == "ssh" and parsed.username == "git"))):
            raise ReleaseError("远端 URL 不得包含凭据、查询参数或片段；请使用 Git 登录态或 SSH Agent")
        value = parsed.path.lstrip("/")
    value = value.removesuffix(".git").strip("/")
    parts = value.split("/")
    if len(parts) != 2 or any(not SAFE_SEGMENT.fullmatch(part) or part in {".", ".."} for part in parts):
        raise ReleaseError("无法解析 GitHub 仓库 owner/repo")
    return value


def split_repository(repository: str) -> tuple[str, str]:
    parts = repository.strip().split("/")
    if len(parts) != 2 or any(not SAFE_SEGMENT.fullmatch(part) for part in parts):
        raise ReleaseError(f"GitHub 仓库格式不正确：{repository!r}")
    return parts[0], parts[1]


def path_contains_symlink(repo_root: Path, relative: str) -> bool:
    current = repo_root
    for part in Path(relative.replace("\\", "/")).parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def validate_no_symlink_paths(repo_root: Path, relative_paths: list[str]) -> None:
    for relative in relative_paths:
        if path_contains_symlink(repo_root, relative):
            raise ReleaseError(f"不允许提交符号链接路径：{relative}")


def resolve_target_repository(repo_root: Path, base_remote: str, target_repository: str | None) -> str:
    """Keep PR mutations scoped to the repository represented by the base remote."""
    base_repository = remote_repository(repo_root, base_remote)
    if not target_repository:
        return base_repository
    target = "/".join(split_repository(target_repository))
    if target.casefold() != base_repository.casefold():
        raise ReleaseError(
            f"目标仓库 {target} 与基线远端 {base_repository} 不一致；为避免跨仓库写入，已停止。"
        )
    return target


def github_login(repo_root: Path, expected: str | None) -> str:
    try:
        login = run_command(["gh", "api", "user", "--jq", ".login"], repo_root).strip()
    except ReleaseError as exc:
        raise ReleaseError("GitHub CLI 未登录或不可用，请先完成 gh auth login") from exc
    if not login:
        raise ReleaseError("GitHub CLI 未返回当前账户")
    if expected and login.casefold() != expected.casefold():
        raise ReleaseError(f"GitHub 当前账户为 {login}，与要求账户 {expected} 不一致")
    return login


def run_command_result(args: list[str], cwd: Path) -> tuple[int, str, str]:
    completed = subprocess.run(
        args,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


def validate_fork_metadata(metadata: object, upstream: str, owner: str, repo: str) -> None:
    if not isinstance(metadata, dict):
        raise ReleaseError(f"GitHub 返回的 fork 信息格式不正确：{owner}/{repo}")

    expected_name = f"{owner}/{repo}"
    if metadata.get("full_name") not in {None, expected_name}:
        raise ReleaseError(f"GitHub 返回的仓库与目标 fork 不一致：{metadata.get('full_name')}")

    if metadata.get("fork") is not True or not isinstance(metadata.get("parent"), dict):
        raise ReleaseError(f"仓库 {expected_name} 存在，但不是 {upstream} 的 fork")

    parent = metadata["parent"]
    if parent.get("full_name") != upstream:
        raise ReleaseError(
            f"仓库 {expected_name} 的 parent 是 {parent.get('full_name')!r}，不是目标仓库 {upstream!r}"
        )


def fork_clone_url(owner: str, repo: str) -> str:
    return f"https://github.com/{owner}/{repo}.git"


def ensure_push_remote(repo_root: Path, remote: str, expected_repository: str, owner: str, repo: str) -> None:
    remote = validate_segment(remote, "远端名称")
    expected_full_name = f"{owner}/{repo}"
    status, output, error = run_command_result(["git", "remote", "get-url", "--all", remote], repo_root)
    if status != 0 or not output:
        run_command(["git", "remote", "add", remote, fork_clone_url(owner, repo)], repo_root)
        # Global insteadOf/pushInsteadOf rules also affect newly added remotes.
        status, output, error = run_command_result(["git", "remote", "get-url", "--all", remote], repo_root)
        if status != 0 or not output:
            raise ReleaseError(f"无法复核新增推送远端 {remote} 的有效 URL")

    fetch_urls = [line for line in output.splitlines() if line.strip()]
    if not fetch_urls:
        raise ReleaseError(f"推送远端 {remote} 没有可用的 fetch URL：{error or output}")
    for url in fetch_urls:
        try:
            actual = repository_from_url(url)
        except ReleaseError as exc:
            raise ReleaseError(f"推送远端 {remote} 不是可识别的 GitHub 仓库：{url}") from exc
        if actual.casefold() != expected_full_name.casefold():
            raise ReleaseError(
                f"推送远端 {remote} 当前指向 {actual}，不是当前账户的 fork {expected_repository}；"
                "为避免误推送，未自动改写该远端。"
            )

    push_status, push_output, push_error = run_command_result(
        ["git", "remote", "get-url", "--push", "--all", remote], repo_root
    )
    if push_status != 0 or not push_output:
        raise ReleaseError(f"无法读取推送远端 {remote} 的 push URL：{push_error or push_output}")
    push_urls = [line for line in push_output.splitlines() if line.strip()]
    for url in push_urls:
        try:
            actual = repository_from_url(url)
        except ReleaseError as exc:
            raise ReleaseError(f"推送远端 {remote} 的 push URL 不是可识别的 GitHub 仓库：{url}") from exc
        if actual.casefold() != expected_full_name.casefold():
            raise ReleaseError(
                f"推送远端 {remote} 配置的 push URL 不匹配：{actual}，目标应为当前账户的 fork "
                f"{expected_repository}；为避免误推送，未自动改写该远端。"
            )


def ensure_fork_repository(repo_root: Path, upstream: str, owner: str, repo: str, push_remote: str) -> None:
    endpoint = f"repos/{owner}/{repo}"
    status, output, error = run_command_result(["gh", "api", endpoint], repo_root)
    if status == 0:
        try:
            metadata = json.loads(output)
        except json.JSONDecodeError as exc:
            raise ReleaseError(f"GitHub fork 查询返回的 JSON 无法解析：{owner}/{repo}") from exc
        validate_fork_metadata(metadata, upstream, owner, repo)
        ensure_push_remote(repo_root, push_remote, upstream, owner, repo)
        return

    detail = f"{output}\n{error}"
    if "404" not in detail and "Not Found" not in detail:
        raise ReleaseError(f"无法查询 fork {owner}/{repo}：{(error or output or '未知 GitHub CLI 错误')[-1000:]}")

    print(f"正在为 {upstream} 创建 fork：{owner}/{repo}", file=sys.stderr)
    run_command(["gh", "repo", "fork", upstream, "--clone=false"], repo_root)

    for attempt in range(8):
        status, output, error = run_command_result(["gh", "api", endpoint], repo_root)
        if status == 0:
            try:
                metadata = json.loads(output)
            except json.JSONDecodeError as exc:
                raise ReleaseError(f"GitHub fork 创建后返回的 JSON 无法解析：{owner}/{repo}") from exc
            validate_fork_metadata(metadata, upstream, owner, repo)
            ensure_push_remote(repo_root, push_remote, upstream, owner, repo)
            return

        if attempt < 7:
            time.sleep(1.5 * (attempt + 1))

    detail = error or output or "fork 创建后仍不可见"
    raise ReleaseError(f"Fork {owner}/{repo} 创建后未能在规定时间内可用：{detail[-1000:]}")


def ensure_release_push_remote(repo_root: Path, repository: str, head_owner: str, push_remote: str) -> None:
    """Validate the push destination even when automatic fork checks are disabled."""
    _, repo = split_repository(repository)
    ensure_push_remote(repo_root, push_remote, repository, head_owner, repo)


def find_open_pr(
    repo_root: Path,
    repository: str,
    branch: str,
    head_owner: str,
    base_branch: str,
) -> dict[str, object] | None:
    raw = run_command(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repository,
            "--head",
            # gh CLI accepts the branch filter reliably here; filtering the
            # owner below keeps the check correct for forked PRs as well.
            branch,
            "--base",
            base_branch,
            "--state",
            "open",
            "--json",
            "number,url,headRefName,headRepositoryOwner,baseRefName",
        ],
        repo_root,
    )
    try:
        records = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise ReleaseError("GitHub CLI 返回的 PR 列表不是有效 JSON") from exc

    if not isinstance(records, list):
        raise ReleaseError("GitHub CLI 返回的 PR 列表格式不正确")

    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("headRefName") != branch:
            continue
        owner = record.get("headRepositoryOwner")
        if not isinstance(owner, dict) or owner.get("login") != head_owner:
            continue
        if record.get("baseRefName") != base_branch:
            continue
        if not isinstance(record.get("number"), int) or record.get("number") <= 0:
            continue
        if not isinstance(record.get("url"), str) or not record.get("url"):
            continue
        return record

    return None


def remote_branch_sha(repo_root: Path, remote: str, branch: str) -> str | None:
    remote = validate_segment(remote, "远端名称")
    output = run_command(
        ["git", "ls-remote", "--heads", remote, f"refs/heads/{branch}"],
        repo_root,
    )
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[1] != f"refs/heads/{branch}":
            continue
        sha = parts[0]
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
            raise ReleaseError(f"远端分支 {branch} 返回了无效 commit SHA")
        return sha
    return None


def build_push_args(remote: str, branch: str, remote_sha: str | None) -> list[str]:
    remote = validate_segment(remote, "远端名称")
    # Existing PR updates are descendants of their fetched remote head.
    # A concurrent remote update must be rejected, never overwritten.
    return ["git", "push", "--set-upstream", remote, branch]


def create_temporary_body_file(body: str) -> Path:
    file_descriptor, file_name = tempfile.mkstemp(prefix="ai-cut-skills-pr-", suffix=".md")
    os.close(file_descriptor)
    body_file = Path(file_name)
    body_file.write_text(body, encoding="utf-8")
    return body_file


def remove_temporary_file(path: Path) -> None:
    for attempt in range(4):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt == 3:
                print(f"WARNING: 无法立即清理临时文件：{path}", file=sys.stderr)
                return
            time.sleep(0.1 * (2**attempt))


def apply_source_changes(repo_root: Path, worktree: Path, base_ref: str, pathspecs: list[str], untracked: list[str]) -> None:
    patch = run_bytes(["git", "--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv", "--binary", base_ref, "--", *pathspecs], repo_root)
    if patch:
        patch_path = worktree.parent / "source-changes.patch"
        patch_path.write_bytes(patch)
        try:
            run_command(["git", "apply", "--3way", "--index", str(patch_path)], worktree)
        finally:
            patch_path.unlink(missing_ok=True)

    for relative in untracked:
        source = repo_root / relative
        if path_contains_symlink(repo_root, relative):
            raise ReleaseError(f"不允许复制符号链接路径：{relative}")
        destination = worktree / relative
        if not source.is_file():
            continue
        if any(part in EXCLUDED_NAMES for part in Path(relative).parts) or source.name.endswith(EXCLUDED_SUFFIXES):
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def create_worktree(repo_root: Path, branch: str, base_ref: str, pathspecs: list[str], untracked: list[str]) -> tuple[Path, Path]:
    parent = Path(tempfile.mkdtemp(prefix="ai-cut-skills-release-"))
    worktree = parent / "repo"
    branch_created = False
    try:
        run_command(["git", "worktree", "add", "-b", branch, str(worktree), base_ref], repo_root)
        branch_created = True
        apply_source_changes(repo_root, worktree, base_ref, pathspecs, untracked)
        return parent, worktree
    except Exception:
        if worktree.exists():
            run_command(["git", "worktree", "remove", "--force", str(worktree)], repo_root, check=False)
        if branch_created:
            run_command(["git", "branch", "-D", branch], repo_root, check=False)
        shutil.rmtree(parent, ignore_errors=True)
        raise


def create_check_worktree(repo_root: Path, base_ref: str, pathspecs: list[str], untracked: list[str]) -> tuple[Path, Path]:
    """Create a detached, disposable worktree for read-only plan checks."""
    parent = Path(tempfile.mkdtemp(prefix="ai-cut-skills-check-"))
    worktree = parent / "repo"
    try:
        run_command(["git", "worktree", "add", "--detach", str(worktree), base_ref], repo_root)
        apply_source_changes(repo_root, worktree, base_ref, pathspecs, untracked)
        return parent, worktree
    except Exception:
        if worktree.exists():
            run_command(["git", "worktree", "remove", "--force", str(worktree)], repo_root, check=False)
        shutil.rmtree(parent, ignore_errors=True)
        raise


def remove_worktree(repo_root: Path, worktree: Path, parent: Path, branch: str | None = None) -> None:
    run_command(["git", "worktree", "remove", "--force", str(worktree)], repo_root, check=False)
    if branch:
        run_command(["git", "branch", "-D", branch], repo_root, check=False)
    shutil.rmtree(parent, ignore_errors=True)


def ensure_staged_scope(worktree: Path, pathspecs: list[str]) -> list[str]:
    run_command(["git", "--literal-pathspecs", "add", "--all", "--", *pathspecs], worktree)
    staged = [line for line in run_command(["git", "diff", "--cached", "--name-only"], worktree).splitlines() if line]
    outside = [path for path in staged if not path_is_allowed(path, pathspecs)]
    if outside:
        raise ReleaseError(f"暂存区出现允许范围外文件：{', '.join(outside)}")
    if not staged:
        raise ReleaseError("目标路径没有可提交的变更")
    return staged


def pr_body(config: ReleaseConfig, branch: str, staged: list[str], checks: dict[str, object]) -> str:
    rows = checks.get("checks", [])
    status = "失败" if not checks.get("ok") else (
        "部分未运行" if any(row.get("skipped") for row in rows) else "通过"
    )
    check_lines = "\n".join(
        (
            f"- 未运行：{row.get('name')}（{row.get('output', '')}）"
            if row.get("skipped") else
            f"- {'通过' if row.get('ok') else '失败'}：{row.get('name')}"
        )
        for row in rows
        if row.get("name") not in {"changed_paths"}
    )
    files = "\n".join(f"- `{path}`" for path in staged)
    return (
        f"## 变更\n\n{config.summary}\n\n"
        f"## 分支\n\n`{branch}`\n\n"
        f"## 文件\n\n{files}\n\n"
        f"## 校验（{status}）\n\n{check_lines}\n"
    )


def create_or_update_pr(
    worktree: Path, repository: str, branch: str, head_owner: str,
    title: str, body: str, base_branch: str, *, push_remote: str, expected_head_sha: str,
) -> str:
    body_file = create_temporary_body_file(body)
    try:
        existing = find_open_pr(worktree, repository, branch, head_owner, base_branch)
        if remote_branch_sha(worktree, push_remote, branch) != expected_head_sha:
            raise ReleaseError("远端分支在校验后变化；已停止创建或更新 PR，请重新检查。")
        if existing:
            number = existing.get("number")
            url = existing.get("url")
            if not number or not isinstance(url, str) or not url:
                raise ReleaseError("已有 PR 缺少可用编号或 URL，已停止更新")
            run_command(
                [
                    "gh",
                    "api",
                    "--method",
                    "PATCH",
                    f"repos/{repository}/pulls/{number}",
                    "--raw-field",
                    f"title={title}",
                    "--raw-field",
                    f"body={body_file.read_text(encoding='utf-8')}",
                ],
                worktree,
            )
            return url
        return run_command(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                repository,
                "--base",
                base_branch,
                "--head",
                f"{head_owner}:{branch}",
                "--title",
                title,
                "--body-file",
                str(body_file),
            ],
            worktree,
        ).splitlines()[-1].strip()
    finally:
        remove_temporary_file(body_file)


def parse_args(argv: list[str] | None = None) -> ReleaseConfig:
    parser = argparse.ArgumentParser(description="提交 ai-cut-skills 仓库的限定范围改动并创建 GitHub PR")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--skill", action="append", required=True, help="目标 Skill 名称，可重复")
    parser.add_argument("--include", action="append", default=[], help="额外允许提交的仓库相对路径，可重复")
    parser.add_argument("--group", required=True, help="分支组别，例如 014-code")
    parser.add_argument("--change-type", required=True, choices=sorted(CHANGE_TYPES))
    parser.add_argument("--scope", help="分支作用域，默认使用目标 Skill 名称")
    parser.add_argument("--summary", required=True, help="commit 和 PR 摘要")
    parser.add_argument("--date", default=datetime.now().strftime("%Y%m%d"), help="分支日期 YYYYMMDD")
    parser.add_argument("--base-remote", default="upstream")
    parser.add_argument("--base-branch", default="main")
    parser.add_argument("--push-remote", default="origin")
    parser.add_argument("--github-account", help="期望的 GitHub 登录账户，仅作一致性校验")
    parser.add_argument("--target-repository", help="GitHub owner/repo；默认从 base remote 解析")
    parser.add_argument(
        "--no-auto-fork",
        dest="auto_fork",
        action="store_false",
        default=True,
        help="不自动创建或校验当前账户 fork（保留已有远端工作方式）",
    )
    parser.add_argument("--execute", action="store_true", help="执行创建分支、commit、push 和 PR")
    test_mode = parser.add_mutually_exclusive_group()
    test_mode.add_argument("--skip-tests", action="store_true")
    test_mode.add_argument("--run-tests", action="store_true", help="预检时允许运行仓库脚本和测试；这些代码可产生外部副作用")
    parser.add_argument("--keep-worktree", action="store_true")
    args = parser.parse_args(argv)

    skills = tuple(dict.fromkeys(args.skill))
    scope = args.scope or "-".join(skills)
    includes = tuple(normalize_relative_path(value) for value in args.include)
    return ReleaseConfig(
        repo_root=args.repo_root.resolve(),
        skills=skills,
        includes=includes,
        group=validate_segment(args.group, "组别"),
        change_type=args.change_type,
        summary=args.summary.strip(),
        scope=validate_segment(scope, "作用域"),
        date=validate_date(args.date),
        base_remote=validate_segment(args.base_remote, "基线远端"),
        base_branch=validate_segment(args.base_branch, "基线分支"),
        push_remote=validate_segment(args.push_remote, "推送远端"),
        github_account=args.github_account,
        target_repository=args.target_repository,
        execute=args.execute,
        skip_tests=args.skip_tests,
        keep_worktree=args.keep_worktree,
        auto_fork=args.auto_fork,
        run_tests=args.run_tests,
    )


def fetch_base_commit(repo_root: Path, remote: str, branch: str) -> str:
    """Refresh an explicit tracking ref, then pin this release to its commit."""
    remote = validate_segment(remote, "远端名称")
    tracking_ref = f"refs/remotes/{remote}/{branch}"
    run_command([
        "git", "fetch", "--no-tags", "--refmap=", remote,
        f"+refs/heads/{branch}:{tracking_ref}",
    ], repo_root)
    return run_command(["git", "rev-parse", "--verify", f"{tracking_ref}^{{commit}}"], repo_root)


def require_ancestor(repo_root: Path, ancestor: str, descendant: str, message: str) -> None:
    status, _, _ = run_command_result(["git", "merge-base", "--is-ancestor", ancestor, descendant], repo_root)
    if status != 0:
        raise ReleaseError(message)


def verify_retry_submission(worktree: Path, base_commit: str, remote_sha: str, source_head: str) -> None:
    """Retry only for an identical tree appended to history the caller contains."""
    parents = run_command(["git", "rev-list", "--parents", "-n", "1", remote_sha], worktree).split()
    intended_tree = run_command(["git", "write-tree"], worktree)
    remote_tree = run_command(["git", "rev-parse", f"{remote_sha}^{{tree}}"], worktree)
    message = "远端分支不精确匹配本次提交的历史和文件树；已停止，不覆盖未知改动。"
    if len(parents) != 2 or parents[0] != remote_sha or intended_tree != remote_tree:
        raise ReleaseError(message)
    require_ancestor(worktree, base_commit, parents[1], message)
    require_ancestor(worktree, parents[1], source_head, message)


def run_release(config: ReleaseConfig) -> dict[str, object]:
    validate_segment(config.base_remote, "基线远端")
    validate_segment(config.push_remote, "推送远端")
    repo_root = Path(run_command(["git", "rev-parse", "--show-toplevel"], config.repo_root)).resolve()
    validate_skill_paths(repo_root, config.skills)
    pathspecs = selected_pathspecs(config)
    branch = build_branch_name(config.group, config.change_type, config.scope, config.date)
    base_ref = f"{config.base_remote}/{config.base_branch}"

    if not config.execute:
        tracked, untracked = list_changed_paths(repo_root, None, pathspecs)
        validate_no_symlink_paths(repo_root, tracked + untracked)
        if config.run_tests:
            check_parent, check_worktree = create_check_worktree(repo_root, "HEAD", pathspecs, untracked)
            try:
                checks = run_checks(check_worktree, config, changed_paths=tracked + untracked)
            finally:
                remove_worktree(repo_root, check_worktree, check_parent)
        else:
            # Static-only planning does not execute repository scripts/tests
            # or create a worktree (which may invoke Git checkout hooks).
            checks = run_checks(repo_root, config, changed_paths=tracked + untracked)
        return {
            "status": "planned",
            "branch": branch,
            "base": base_ref,
            "pathspecs": pathspecs,
            "changed_paths": tracked + untracked,
            "checks": checks,
            "execute_command": "加 --execute 执行提交、推送和创建 PR",
        }

    repository = resolve_target_repository(repo_root, config.base_remote, config.target_repository)
    base_commit = fetch_base_commit(repo_root, config.base_remote, config.base_branch)
    source_head = run_command(["git", "rev-parse", "--verify", "HEAD^{commit}"], repo_root)
    require_ancestor(repo_root, base_commit, source_head, "当前 HEAD 不包含最新基线；请先同步上游，避免生成回退 PR。")
    head_owner = github_login(repo_root, config.github_account)
    if config.auto_fork:
        _, upstream_repo = split_repository(repository)
        ensure_fork_repository(
            repo_root,
            repository,
            head_owner,
            upstream_repo,
            config.push_remote,
        )
    else:
        ensure_release_push_remote(repo_root, repository, head_owner, config.push_remote)
    existing_pr = find_open_pr(repo_root, repository, branch, head_owner, config.base_branch)
    existing_remote_sha = remote_branch_sha(repo_root, config.push_remote, branch)
    release_base = base_commit
    retry_submission = False
    if existing_pr and not existing_remote_sha:
        raise ReleaseError("已有 PR 的远端分支不可用；请先检查 PR 状态。")
    if existing_remote_sha:
        fetched_sha = fetch_base_commit(repo_root, config.push_remote, branch)
        if fetched_sha != existing_remote_sha:
            raise ReleaseError("远端分支在检查期间变化；请重新检查后重试。")
        if existing_pr:
            require_ancestor(repo_root, base_commit, existing_remote_sha,
                             "已有 PR 分支不包含最新基线；请先更新 PR 分支并同步当前 HEAD。")
            status, _, _ = run_command_result(
                ["git", "merge-base", "--is-ancestor", existing_remote_sha, source_head], repo_root,
            )
            if status == 0:
                release_base = existing_remote_sha
            elif status == 1:
                retry_submission = True
            else:
                raise ReleaseError("无法验证已有 PR 历史；已停止。")
        else:
            retry_submission = True
    tracked, untracked = list_changed_paths(repo_root, release_base, pathspecs)
    changed = tracked + untracked
    if not changed:
        raise ReleaseError("目标 Skill 相对发布基线没有可提交变更")
    validate_no_symlink_paths(repo_root, changed)
    parent, worktree = create_worktree(repo_root, branch, release_base, pathspecs, untracked)
    try:
        staged = ensure_staged_scope(worktree, pathspecs)
        checks = run_checks(worktree, config, changed_paths=staged)
        if not checks.get("ok"):
            raise ReleaseError("提交前校验失败，请先处理 PR 检查结果")
        title = f"{config.change_type}({config.scope}): {config.summary}"
        body = pr_body(config, branch, staged, checks)
        if retry_submission:
            # The PR API may have failed before or after creating the PR.
            # Matching local content can retry the API, but never rewrite the branch.
            assert existing_remote_sha is not None
            verify_retry_submission(worktree, base_commit, existing_remote_sha, source_head)
            commit = existing_remote_sha
        else:
            run_command(["git", "commit", "-m", title], worktree)
            run_command(build_push_args(config.push_remote, branch, existing_remote_sha), worktree)
            commit = run_command(["git", "rev-parse", "HEAD"], worktree)
        pr_url = create_or_update_pr(
            worktree, repository, branch, head_owner, title, body, config.base_branch,
            push_remote=config.push_remote, expected_head_sha=commit,
        )
        result = {
            "status": "submitted",
            "branch": branch,
            "base_commit": base_commit,
            "commit": commit,
            "repository": repository,
            "pr_url": pr_url,
            "files": staged,
            "checks": checks,
        }
    finally:
        if not config.keep_worktree:
            remove_worktree(repo_root, worktree, parent, branch)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        config = parse_args(argv)
        result = run_release(config)
    except ReleaseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
