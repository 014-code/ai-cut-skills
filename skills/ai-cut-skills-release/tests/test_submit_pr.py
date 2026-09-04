import importlib.util
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import call, patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "submit_pr.py"
SPEC = importlib.util.spec_from_file_location("submit_pr", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class SubmitPrHelpersTests(unittest.TestCase):
    def test_branch_name_contains_group_scope_and_date(self) -> None:
        self.assertEqual(
            MODULE.build_branch_name("014-code", "fix", "usergrowth", "20260903"),
            "014-code/fix-usergrowth-20260903",
        )

    def test_rejects_unsafe_scope(self) -> None:
        with self.assertRaises(MODULE.ReleaseError):
            MODULE.build_branch_name("014-code", "fix", "../main", "20260903")

    def test_path_allowlist_is_directory_aware(self) -> None:
        allowed = ["skills/demo", "tests/test_demo.py"]
        self.assertTrue(MODULE.path_is_allowed("skills/demo/scripts/run.py", allowed))
        self.assertTrue(MODULE.path_is_allowed("tests/test_demo.py", allowed))
        self.assertFalse(MODULE.path_is_allowed("skills/demo-extra/run.py", allowed))

    def test_normalizes_relative_paths(self) -> None:
        self.assertEqual(MODULE.normalize_relative_path("tests\\test_demo.py"), "tests/test_demo.py")
        with self.assertRaises(MODULE.ReleaseError):
            MODULE.normalize_relative_path("../outside.txt")

    def test_managed_branch_updates_never_force_push(self) -> None:
        self.assertEqual(
            MODULE.build_push_args("origin", "014-code/fix-demo-20260903", "a" * 40),
            [
                "git",
                "push",
                "--set-upstream",
                "origin",
                "014-code/fix-demo-20260903",
            ],
        )

    def test_refuses_orphan_branch_with_different_tree(self) -> None:
        with patch.object(MODULE, "run_command", side_effect=["head base", "tree-a", "tree-b"]):
            with self.assertRaisesRegex(MODULE.ReleaseError, "不精确匹配"):
                MODULE.verify_orphan_submission(Path("."), "base", "head")

    def test_refuses_orphan_branch_with_different_parent(self) -> None:
        with patch.object(MODULE, "run_command", side_effect=["head other-base", "tree", "tree"]):
            with self.assertRaisesRegex(MODULE.ReleaseError, "不精确匹配"):
                MODULE.verify_orphan_submission(Path("."), "base", "head")

    def test_finds_only_matching_open_pr(self) -> None:
        with patch.object(
            MODULE,
            "run_command",
            return_value='[{"number": 12, "url": "https://example.test/pr/12", "headRefName": "demo", "headRepositoryOwner": {"login": "octocat"}, "baseRefName": "main"}]',
        ) as run:
            result = MODULE.find_open_pr(Path("."), "acme/demo", "demo", "octocat", "main")

        self.assertEqual(result["number"] if result else None, 12)
        self.assertEqual(run.call_args.args[0][run.call_args.args[0].index("--head") + 1], "demo")

    def test_ignores_open_pr_with_incomplete_identity_metadata(self) -> None:
        with patch.object(
            MODULE,
            "run_command",
            return_value='[{"number": 12, "url": "https://example.test/pr/12", "headRefName": "demo", "baseRefName": "main"}]',
        ):
            result = MODULE.find_open_pr(Path("."), "acme/demo", "demo", "octocat", "main")

        self.assertIsNone(result)

    def test_rejects_target_repository_outside_base_remote(self) -> None:
        with patch.object(MODULE, "remote_repository", return_value="liudu2326526/ai-cut-skills"):
            with self.assertRaisesRegex(MODULE.ReleaseError, "跨仓库写入"):
                MODULE.resolve_target_repository(
                    Path("."),
                    "upstream",
                    "other-owner/other-repository",
                )

    def test_rejects_symlink_change_paths(self) -> None:
        with patch.object(MODULE, "path_contains_symlink", return_value=True):
            with self.assertRaisesRegex(MODULE.ReleaseError, "符号链接"):
                MODULE.validate_no_symlink_paths(Path("."), ["skills/demo/link.py"])

    def test_accepts_regular_change_paths(self) -> None:
        with patch.object(MODULE, "path_contains_symlink", return_value=False):
            MODULE.validate_no_symlink_paths(Path("."), ["skills/demo/main.py"])

    def test_accepts_target_repository_matching_base_remote(self) -> None:
        with patch.object(MODULE, "remote_repository", return_value="liudu2326526/ai-cut-skills"):
            self.assertEqual(
                MODULE.resolve_target_repository(
                    Path("."),
                    "upstream",
                    "liudu2326526/ai-cut-skills",
                ),
                "liudu2326526/ai-cut-skills",
            )

    def test_plan_includes_staged_changes_when_repository_has_a_head(self) -> None:
        with patch.object(MODULE, "run_command", side_effect=["staged.py", ""]):
            tracked, untracked = MODULE.list_changed_paths(Path("."), None, ["skills/demo"])

        self.assertEqual(tracked, ["staged.py"])
        self.assertEqual(untracked, [])

    def test_temporary_body_file_closes_descriptor_before_returning(self) -> None:
        path = MODULE.create_temporary_body_file("# PR\n")
        try:
            self.assertEqual(path.read_text(encoding="utf-8"), "# PR\n")
            MODULE.remove_temporary_file(path)
            self.assertFalse(path.exists())
        finally:
            MODULE.remove_temporary_file(path)

    def test_validates_existing_fork_metadata(self) -> None:
        MODULE.validate_fork_metadata(
            {
                "full_name": "014-code/ai-cut-skills",
                "fork": True,
                "parent": {"full_name": "liudu2326526/ai-cut-skills"},
            },
            "liudu2326526/ai-cut-skills",
            "014-code",
            "ai-cut-skills",
        )

    def test_rejects_repository_that_is_not_a_fork(self) -> None:
        with self.assertRaisesRegex(MODULE.ReleaseError, "不是.*fork"):
            MODULE.validate_fork_metadata(
                {
                    "full_name": "014-code/ai-cut-skills",
                    "fork": False,
                    "parent": None,
                },
                "liudu2326526/ai-cut-skills",
                "014-code",
                "ai-cut-skills",
            )

    def test_rejects_fork_with_unexpected_parent(self) -> None:
        with self.assertRaisesRegex(MODULE.ReleaseError, "parent.*不是目标仓库"):
            MODULE.validate_fork_metadata(
                {
                    "full_name": "014-code/ai-cut-skills",
                    "fork": True,
                    "parent": {"full_name": "someone-else/ai-cut-skills"},
                },
                "liudu2326526/ai-cut-skills",
                "014-code",
                "ai-cut-skills",
            )

    def test_adds_missing_push_remote_to_current_account_fork(self) -> None:
        with patch.object(MODULE, "run_command_result", return_value=(2, "", "No such remote")):
            with patch.object(MODULE, "run_command") as run:
                MODULE.ensure_push_remote(
                    Path("."),
                    "origin",
                    "liudu2326526/ai-cut-skills",
                    "014-code",
                    "ai-cut-skills",
                )

        run.assert_called_once_with(
            ["git", "remote", "add", "origin", "https://github.com/014-code/ai-cut-skills.git"],
            Path("."),
        )

    def test_does_not_rewrite_push_remote_pointing_to_another_repository(self) -> None:
        with patch.object(MODULE, "run_command_result", return_value=(0, "git@github.com:someone/other.git", "")):
            with self.assertRaisesRegex(MODULE.ReleaseError, "未自动改写"):
                MODULE.ensure_push_remote(
                    Path("."),
                    "origin",
                    "liudu2326526/ai-cut-skills",
                    "014-code",
                    "ai-cut-skills",
                )

    def test_rejects_push_url_that_differs_from_fetch_url(self) -> None:
        with patch.object(
            MODULE,
            "run_command_result",
            side_effect=[
                (0, "https://github.com/014-code/ai-cut-skills.git", ""),
                (0, "https://github.com/other-account/ai-cut-skills.git", ""),
            ],
        ):
            with self.assertRaisesRegex(MODULE.ReleaseError, "push URL.*不匹配"):
                MODULE.ensure_push_remote(
                    Path("."),
                    "origin",
                    "liudu2326526/ai-cut-skills",
                    "014-code",
                    "ai-cut-skills",
                )

    def test_accepts_matching_explicit_push_url(self) -> None:
        with patch.object(
            MODULE,
            "run_command_result",
            side_effect=[
                (0, "https://github.com/014-code/ai-cut-skills.git", ""),
                (0, "git@github.com:014-code/ai-cut-skills.git", ""),
            ],
        ):
            MODULE.ensure_push_remote(
                Path("."),
                "origin",
                "liudu2326526/ai-cut-skills",
                "014-code",
                "ai-cut-skills",
            )

    def test_no_auto_fork_mode_still_validates_push_destination(self) -> None:
        with patch.object(MODULE, "ensure_push_remote") as ensure_remote:
            MODULE.ensure_release_push_remote(
                Path("."),
                "liudu2326526/ai-cut-skills",
                "014-code",
                "origin",
            )

        ensure_remote.assert_called_once_with(
            Path("."),
            "origin",
            "liudu2326526/ai-cut-skills",
            "014-code",
            "ai-cut-skills",
        )

    def test_python_syntax_check_does_not_create_pycache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = root / "skills" / "demo"
            skill.mkdir(parents=True)
            (skill / "main.py").write_text("value = 1\n", encoding="utf-8")
            config = MODULE.ReleaseConfig(
                repo_root=root,
                skills=("demo",),
                includes=(),
                group="014-code",
                change_type="fix",
                summary="test",
                scope="demo",
                date="20260903",
                base_remote="upstream",
                base_branch="main",
                push_remote="origin",
                github_account=None,
                target_repository=None,
                execute=False,
                skip_tests=True,
                keep_worktree=False,
                auto_fork=True,
            )
            with patch.object(MODULE, "quick_validator_path", return_value=None):
                with patch.object(MODULE, "run_command", return_value=""):
                    checks = MODULE.run_checks(root, config, changed_paths=[])

            syntax = next(row for row in checks["checks"] if row["name"] == "python_syntax")
            self.assertTrue(syntax["ok"])
            self.assertFalse(any(path.name == "__pycache__" for path in root.rglob("__pycache__")))

    def test_creates_fork_after_404_and_waits_until_available(self) -> None:
        api_responses = [
            (1, "", "HTTP 404: Not Found"),
            (1, "", "HTTP 404: Not Found"),
            (
                0,
                '{"full_name":"014-code/ai-cut-skills","fork":true,"parent":{"full_name":"liudu2326526/ai-cut-skills"}}',
                "",
            ),
        ]
        with patch.object(MODULE, "run_command_result", side_effect=api_responses) as result:
            with patch.object(MODULE, "run_command") as run:
                with patch.object(MODULE, "ensure_push_remote") as ensure_remote:
                    with patch.object(MODULE.time, "sleep") as sleep:
                        MODULE.ensure_fork_repository(
                            Path("."),
                            "liudu2326526/ai-cut-skills",
                            "014-code",
                            "ai-cut-skills",
                            "origin",
                        )

        run.assert_has_calls([call(["gh", "repo", "fork", "liudu2326526/ai-cut-skills", "--clone=false"], Path("."))])
        ensure_remote.assert_called_once_with(
            Path("."), "origin", "liudu2326526/ai-cut-skills", "014-code", "ai-cut-skills"
        )
        sleep.assert_called_once_with(1.5)
        self.assertEqual(result.call_count, 3)

    def test_does_not_create_fork_for_non_404_api_failure(self) -> None:
        with patch.object(MODULE, "run_command_result", return_value=(1, "", "HTTP 403: Forbidden")):
            with patch.object(MODULE, "run_command") as run:
                with self.assertRaisesRegex(MODULE.ReleaseError, "无法查询 fork"):
                    MODULE.ensure_fork_repository(
                        Path("."),
                        "liudu2326526/ai-cut-skills",
                        "014-code",
                        "ai-cut-skills",
                        "origin",
                    )

        run.assert_not_called()

    def test_updates_existing_pr_through_rest_api(self) -> None:
        with (
            patch.object(MODULE, "remote_branch_sha", return_value="a" * 40),
            patch.object(MODULE, "find_open_pr", return_value={"number": 15, "url": "https://example.test/pr/15"}),
        ):
            with patch.object(MODULE, "run_command", return_value="") as run:
                result = MODULE.create_or_update_pr(
                    Path("."),
                    "liudu2326526/ai-cut-skills",
                    "014-code/fix-demo-20260903",
                    "014-code",
                    "fix(demo): update",
                    "## 变更\n\n自动 fork\n",
                    "main",
                    push_remote="origin",
                    expected_head_sha="a" * 40,
                )

        self.assertEqual(result, "https://example.test/pr/15")
        args = run.call_args.args[0]
        self.assertEqual(args[:5], ["gh", "api", "--method", "PATCH", "repos/liudu2326526/ai-cut-skills/pulls/15"])
        self.assertIn("--raw-field", args)
        self.assertTrue(any(value.startswith("body=## 变更") for value in args))

    def test_remote_url_credentials_do_not_appear_in_errors(self) -> None:
        secret = "synthetic-password-for-test"
        for url in (
            f"https://user:{secret}@other.test/owner/repo.git",
            f"https://user:{secret}@github.com/owner/repo.git",
            f"https://github.com/owner/repo.git?token={secret}",
            f"https://user:{secret}@github.com/malformed",
        ):
            with self.subTest(url_kind=url.split("@")[-1]):
                with self.assertRaises(MODULE.ReleaseError) as caught:
                    MODULE.repository_from_url(url)
                self.assertNotIn(secret, str(caught.exception))
                self.assertNotIn(secret, str(MODULE.ReleaseError(f"Git command failed: {url}")))

    def test_rejects_changed_remote_head_before_pr_mutation(self) -> None:
        with (
            patch.object(MODULE, "find_open_pr", return_value=None),
            patch.object(MODULE, "remote_branch_sha", return_value="b" * 40),
            patch.object(MODULE, "run_command") as run,
        ):
            with self.assertRaisesRegex(MODULE.ReleaseError, "在校验后变化"):
                MODULE.create_or_update_pr(
                    Path("."), "owner/repo", "branch", "owner", "title", "body", "main",
                    push_remote="origin", expected_head_sha="a" * 40,
                )
        run.assert_not_called()

    def test_auto_fork_is_enabled_by_default_and_can_be_disabled(self) -> None:
        config = MODULE.parse_args(
            [
                "--skill",
                "demo",
                "--group",
                "014-code",
                "--change-type",
                "fix",
                "--summary",
                "test",
            ]
        )
        self.assertTrue(config.auto_fork)
        self.assertFalse(
            MODULE.parse_args(
                [
                    "--skill",
                    "demo",
                    "--group",
                    "014-code",
                    "--change-type",
                    "fix",
                    "--summary",
                    "test",
                    "--no-auto-fork",
                ]
            ).auto_fork
        )


def release_config(root: Path, *extra: str):
    return MODULE.parse_args([
        "--repo-root", str(root), "--skill", "demo", "--group", "test",
        "--change-type", "fix", "--summary", "regression test", *extra,
    ])


class ReleaseBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.git(self.source, "init", "--initial-branch=main")
        self.git(self.source, "config", "user.name", "Release Test")
        self.git(self.source, "config", "user.email", "test@example.invalid")
        skill = self.source / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("demo\n", encoding="utf-8")
        self.git(self.source, "add", ".")
        self.git(self.source, "commit", "-m", "initial")
        self.old_sha = self.git(self.source, "rev-parse", "HEAD")
        self.remote = self.root / "remote.git"
        self.git(self.root, "clone", "--bare", str(self.source), str(self.remote))
        self.local = self.root / "local"
        self.git(self.root, "clone", "--origin", "upstream", str(self.remote), str(self.local))
        (self.source / "upstream.txt").write_text("new upstream content\n", encoding="utf-8")
        (skill / "SKILL.md").write_text("new upstream skill content\n", encoding="utf-8")
        self.git(self.source, "add", ".")
        self.git(self.source, "commit", "-m", "advance upstream")
        self.latest_sha = self.git(self.source, "rev-parse", "HEAD")
        self.git(self.source, "push", str(self.remote), "main")
        self.git(self.local, "config", "remote.upstream.fetch",
                 "+refs/heads/release:refs/remotes/upstream/release")

    @staticmethod
    def git(root: Path, *args: str) -> str:
        return MODULE.run_command(["git", *args], root)

    def test_fetch_refreshes_stale_tracking_ref_and_worktree_baseline(self) -> None:
        # With a narrowed configured refmap, the old command leaves main stale.
        self.git(self.local, "fetch", "upstream", "main")
        self.assertEqual(self.git(self.local, "rev-parse", "FETCH_HEAD"), self.latest_sha)
        self.assertEqual(self.git(self.local, "rev-parse", "upstream/main"), self.old_sha)

        sha = MODULE.fetch_base_commit(self.local, "upstream", "main")
        self.assertEqual(sha, self.latest_sha)
        self.assertEqual(self.git(self.local, "rev-parse", "upstream/main"), self.latest_sha)
        parent, worktree = MODULE.create_check_worktree(self.local, sha, ["not-selected"], [])
        try:
            self.assertEqual(self.git(worktree, "rev-parse", "HEAD"), self.latest_sha)
            self.assertEqual((worktree / "upstream.txt").read_text(), "new upstream content\n")
            self.assertEqual((worktree / "skills/demo/SKILL.md").read_text(), "new upstream skill content\n")
        finally:
            MODULE.remove_worktree(self.local, worktree, parent)

    def test_failed_fetch_does_not_fall_back_to_existing_tracking_ref(self) -> None:
        self.git(self.remote, "update-ref", "-d", "refs/heads/main")
        with self.assertRaises(MODULE.ReleaseError):
            MODULE.fetch_base_commit(self.local, "upstream", "main")
        self.assertEqual(self.git(self.local, "rev-parse", "upstream/main"), self.old_sha)

    def test_execute_uses_same_resolved_sha_for_diff_and_worktree(self) -> None:
        self.git(self.local, "fetch", "upstream", "main")
        self.git(self.local, "merge", "--ff-only", self.latest_sha)
        config = release_config(self.local, "--execute")
        with (
            patch.object(MODULE, "fetch_base_commit", return_value=self.latest_sha),
            patch.object(MODULE, "list_changed_paths", return_value=(["skills/demo/SKILL.md"], [])) as changes,
            patch.object(MODULE, "resolve_target_repository", return_value="owner/repo"),
            patch.object(MODULE, "github_login", return_value="owner"),
            patch.object(MODULE, "ensure_fork_repository"),
            patch.object(MODULE, "find_open_pr", return_value=None),
            patch.object(MODULE, "remote_branch_sha", return_value=None),
            patch.object(MODULE, "create_worktree", side_effect=MODULE.ReleaseError("stop before writes")) as create,
        ):
            with self.assertRaisesRegex(MODULE.ReleaseError, "stop before writes"):
                MODULE.run_release(config)
        self.assertEqual(changes.call_args.args[1], self.latest_sha)
        self.assertEqual(create.call_args.args[2], self.latest_sha)

    def test_stale_checkout_is_rejected_before_any_github_mutation(self) -> None:
        with patch.object(MODULE, "ensure_fork_repository") as fork:
            with self.assertRaisesRegex(MODULE.ReleaseError, "不包含最新基线"):
                MODULE.run_release(release_config(self.local, "--execute"))
        fork.assert_not_called()
        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), self.old_sha)
        self.assertEqual((self.local / "skills/demo/SKILL.md").read_text(), "demo\n")


class ReleaseExecutionTests(unittest.TestCase):
    git = staticmethod(ReleaseBaselineTests.git)

    def setUp(self) -> None:
        ReleaseBaselineTests.setUp(self)
        self.git(self.local, "fetch", "upstream", "main")
        self.git(self.local, "merge", "--ff-only", self.latest_sha)
        self.git(self.local, "config", "user.name", "Release Test")
        self.git(self.local, "config", "user.email", "test@example.invalid")
        self.git(self.local, "remote", "add", "origin", str(self.remote))
        self.config = release_config(self.local, "--execute")
        self.branch = MODULE.build_branch_name(
            self.config.group, self.config.change_type, self.config.scope, self.config.date,
        )
        self.pr = {"number": 15, "url": "https://example.test/pr/15"}

    def stubs(self, existing_pr=None):
        # Keep Git and worktree operations real; never call GitHub from fixtures.
        stack = ExitStack()
        for name, value in (
            ("resolve_target_repository", "owner/repo"),
            ("github_login", "owner"),
            ("ensure_fork_repository", None),
            ("find_open_pr", existing_pr),
            ("run_checks", {"ok": True, "checks": []}),
            ("create_or_update_pr", self.pr["url"]),
        ):
            stack.enter_context(patch.object(MODULE, name, return_value=value))
        return stack

    def publish_existing_pr(self) -> str:
        self.git(self.source, "checkout", "-b", self.branch)
        (self.source / "skills/demo/pr-only.txt").write_text("existing PR work\n")
        self.git(self.source, "add", ".")
        self.git(self.source, "commit", "-m", "existing PR work")
        self.git(self.source, "push", str(self.remote), self.branch)
        return self.git(self.source, "rev-parse", "HEAD")

    def test_missing_existing_pr_work_is_rejected_without_push(self) -> None:
        existing_sha = self.publish_existing_pr()
        (self.local / "skills/demo/local.txt").write_text("local change\n")
        with self.stubs(self.pr):
            with self.assertRaisesRegex(MODULE.ReleaseError, "不包含已有 PR"):
                MODULE.run_release(self.config)
        self.assertEqual(self.git(self.remote, "rev-parse", self.branch), existing_sha)

    def test_managed_update_preserves_existing_commit_and_content(self) -> None:
        existing_sha = self.publish_existing_pr()
        self.git(self.local, "fetch", "origin", self.branch)
        self.git(self.local, "merge", "--ff-only", existing_sha)
        (self.local / "skills/demo/local.txt").write_text("local change\n")
        with self.stubs(self.pr):
            result = MODULE.run_release(self.config)
        new_sha = self.git(self.remote, "rev-parse", self.branch)
        self.assertEqual(new_sha, result["commit"])
        self.assertEqual(self.git(self.remote, "rev-parse", f"{new_sha}^"), existing_sha)
        self.assertEqual(self.git(self.remote, "show", f"{new_sha}:skills/demo/pr-only.txt"), "existing PR work")
        self.assertEqual(self.git(self.remote, "show", f"{new_sha}:skills/demo/local.txt"), "local change")

    def create_orphan(self) -> str:
        (self.local / "skills/demo/local.txt").write_text("local change\n")
        with self.stubs():
            with patch.object(MODULE, "create_or_update_pr", side_effect=MODULE.ReleaseError("PR API timeout")):
                with self.assertRaisesRegex(MODULE.ReleaseError, "PR API timeout"):
                    MODULE.run_release(self.config)
        return self.git(self.remote, "rev-parse", self.branch)

    def test_pr_api_failure_after_push_can_retry_without_another_push(self) -> None:
        orphan_sha = self.create_orphan()
        with self.stubs():
            with patch.object(MODULE, "run_command", wraps=MODULE.run_command) as run:
                result = MODULE.run_release(self.config)
        self.assertEqual(result["commit"], orphan_sha)
        self.assertEqual(self.git(self.remote, "rev-parse", self.branch), orphan_sha)
        self.assertFalse(any(item.args[0][:2] == ["git", "push"] for item in run.call_args_list))

    def test_orphan_retry_with_changed_submission_is_rejected(self) -> None:
        orphan_sha = self.create_orphan()
        (self.local / "skills/demo/local.txt").write_text("different change\n")
        with self.stubs():
            with self.assertRaisesRegex(MODULE.ReleaseError, "不精确匹配"):
                MODULE.run_release(self.config)
        self.assertEqual(self.git(self.remote, "rev-parse", self.branch), orphan_sha)

    def test_remote_change_after_orphan_verification_prevents_pr_creation(self) -> None:
        self.create_orphan()
        original_verify = MODULE.verify_orphan_submission
        original_create = MODULE.create_or_update_pr

        def change_remote_after_verification(worktree, base_commit, remote_sha):
            original_verify(worktree, base_commit, remote_sha)
            self.git(self.remote, "update-ref", f"refs/heads/{self.branch}", self.latest_sha)

        with (
            self.stubs(),
            patch.object(MODULE, "verify_orphan_submission", side_effect=change_remote_after_verification),
            patch.object(MODULE, "create_or_update_pr", new=original_create),
            patch.object(MODULE, "run_command", wraps=MODULE.run_command) as run,
        ):
            with self.assertRaisesRegex(MODULE.ReleaseError, "在校验后变化"):
                MODULE.run_release(self.config)
        self.assertFalse(any(item.args[0][0] == "gh" for item in run.call_args_list))


class ReleaseTestDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        MODULE.run_command(["git", "init"], self.root)
        skill = self.root / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("demo\n", encoding="utf-8")
        validator = self.root / "validate.py"
        validator.write_text("", encoding="utf-8")
        validator_patch = patch.object(MODULE, "quick_validator_path", return_value=validator)
        validator_patch.start()
        self.addCleanup(validator_patch.stop)

    def write_test(self, directory: str, *, passing: bool) -> None:
        tests = self.root / directory
        tests.mkdir(parents=True)
        (tests / "test_contract.py").write_text(
            "import unittest\nclass ContractTest(unittest.TestCase):\n"
            f"    def test_contract(self):\n        self.assertTrue({passing!r})\n",
            encoding="utf-8",
        )

    def check_results(self, *extra: str) -> dict:
        return MODULE.run_checks(self.root, release_config(self.root, *extra), changed_paths=[])

    def test_root_pass_does_not_hide_selected_skill_failure_with_same_module_name(self) -> None:
        self.write_test("tests", passing=True)
        self.write_test("skills/demo/tests", passing=False)
        results = self.check_results()
        checks = {check["name"]: check for check in results["checks"]}
        self.assertTrue(checks["tests"]["ok"])
        self.assertFalse(checks["tests:demo"]["ok"])
        self.assertFalse(results["ok"])

    def test_skill_tests_run_without_repository_tests(self) -> None:
        self.write_test("skills/demo/tests", passing=False)
        results = self.check_results()
        self.assertFalse(results["ok"])
        self.assertTrue(any(check["name"] == "tests:demo" for check in results["checks"]))

    def test_passing_selected_tests_do_not_run_unselected_skill_tests(self) -> None:
        self.write_test("tests", passing=True)
        self.write_test("skills/demo/tests", passing=True)
        self.write_test("skills/unselected/tests", passing=False)
        results = self.check_results()
        self.assertTrue(results["ok"], results)
        self.assertEqual(
            {check["name"] for check in results["checks"] if check["name"].startswith("tests")},
            {"tests", "tests:demo"},
        )

    def test_skip_tests_skips_both_repository_and_selected_skill_suites(self) -> None:
        self.write_test("tests", passing=False)
        self.write_test("skills/demo/tests", passing=False)
        with patch.object(MODULE, "run_command", wraps=MODULE.run_command) as run:
            results = self.check_results("--skip-tests")
        self.assertTrue(results["ok"], results)
        self.assertFalse(any("unittest" in item.args[0] for item in run.call_args_list))


if __name__ == "__main__":
    unittest.main()
