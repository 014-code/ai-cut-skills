import importlib.util
import sys
import tempfile
import unittest
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

    def test_discovers_selected_skill_test_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill_tests = root / "skills" / "demo" / "tests"
            skill_tests.mkdir(parents=True)
            (skill_tests / "test_demo.py").write_text("", encoding="utf-8")

            roots = MODULE.discover_test_roots(root, ("demo",))

        self.assertEqual(roots, [("skill_tests:demo", skill_tests)])

    def test_normalizes_relative_paths(self) -> None:
        self.assertEqual(MODULE.normalize_relative_path("tests\\test_demo.py"), "tests/test_demo.py")
        with self.assertRaises(MODULE.ReleaseError):
            MODULE.normalize_relative_path("../outside.txt")

    def test_builds_force_with_lease_push_for_existing_managed_branch(self) -> None:
        self.assertEqual(
            MODULE.build_push_args("origin", "014-code/fix-demo-20260903", "a" * 40),
            [
                "git",
                "push",
                "--force-with-lease=refs/heads/014-code/fix-demo-20260903:" + "a" * 40,
                "--set-upstream",
                "origin",
                "014-code/fix-demo-20260903",
            ],
        )

    def test_refreshes_base_with_explicit_refspec(self) -> None:
        with patch.object(MODULE, "run_command", side_effect=["", "upstream/main"]) as run:
            result = MODULE.refresh_base_ref(Path("."), "upstream", "main")

        self.assertEqual(result, "upstream/main")
        self.assertEqual(
            run.call_args_list,
            [
                call(
                    [
                        "git",
                        "fetch",
                        "upstream",
                        "+refs/heads/main:refs/remotes/upstream/main",
                    ],
                    Path("."),
                ),
                call(["git", "rev-parse", "--verify", "upstream/main"], Path(".")),
            ],
        )

    def test_preflight_rejects_empty_changes(self) -> None:
        with self.assertRaisesRegex(MODULE.ReleaseError, "没有可提交的变更"):
            MODULE.validate_preflight_result([], {"ok": True, "checks": []})

    def test_preflight_rejects_failed_checks(self) -> None:
        with self.assertRaisesRegex(MODULE.ReleaseError, "提交前校验失败.*tests"):
            MODULE.validate_preflight_result(
                ["skills/demo/SKILL.md"],
                {"ok": False, "checks": [{"name": "tests", "ok": False}]},
            )

    def test_existing_pr_updates_are_based_on_managed_remote_branch(self) -> None:
        self.assertEqual(
            MODULE.select_worktree_ref("upstream/main", "origin", "014-code/fix-demo-20260903", "a" * 40),
            "origin/014-code/fix-demo-20260903",
        )
        self.assertEqual(
            MODULE.select_worktree_ref("upstream/main", "origin", "014-code/fix-demo-20260903", None),
            "upstream/main",
        )

    def test_existing_pr_source_base_uses_only_local_commits_after_remote_branch(self) -> None:
        with patch.object(MODULE, "run_command_result", side_effect=[(0, "", "")]):
            self.assertEqual(
                MODULE.select_source_commit_base(Path("."), "origin/demo"),
                "origin/demo",
            )

        with patch.object(
            MODULE,
            "run_command_result",
            side_effect=[(1, "", "not ancestor"), (0, "", "")],
        ):
            self.assertIsNone(MODULE.select_source_commit_base(Path("."), "origin/demo"))

    def test_rejects_diverged_local_and_existing_pr_branches(self) -> None:
        with patch.object(
            MODULE,
            "run_command_result",
            side_effect=[(1, "", "remote is not ancestor"), (1, "", "head is not ancestor")],
        ):
            with self.assertRaisesRegex(MODULE.ReleaseError, "已分叉"):
                MODULE.select_source_commit_base(Path("."), "origin/demo")

    def test_refuses_existing_remote_branch_without_open_pr(self) -> None:
        with self.assertRaisesRegex(MODULE.ReleaseError, "没有找到.*打开 PR"):
            MODULE.validate_remote_branch_reuse("014-code/fix-demo-20260903", "a" * 40, None)

    def test_allows_existing_remote_branch_only_with_open_pr(self) -> None:
        MODULE.validate_remote_branch_reuse(
            "014-code/fix-demo-20260903",
            "a" * 40,
            {"number": 12, "url": "https://github.com/acme/demo/pull/12"},
        )

    def test_allows_managed_remote_branch_without_open_pr_for_retry(self) -> None:
        MODULE.validate_remote_branch_reuse(
            "014-code/fix-demo-20260903",
            "a" * 40,
            None,
            managed=True,
        )

    def test_rejects_branch_reconciliation_conflict(self) -> None:
        with patch.object(MODULE, "run_command_result", return_value=(1, "CONFLICT", "")):
            with self.assertRaisesRegex(MODULE.ReleaseError, "存在冲突"):
                MODULE.validate_branch_reconcile(Path("."), "upstream/main", "origin/demo")

    def test_accepts_clean_branch_reconciliation(self) -> None:
        with patch.object(MODULE, "run_command_result", return_value=(0, "tree", "")):
            MODULE.validate_branch_reconcile(Path("."), "upstream/main", "origin/demo")

    def test_rejects_out_of_scope_files_in_final_commit(self) -> None:
        with patch.object(MODULE, "run_command", return_value="skills/demo/SKILL.md\nREADME.md"):
            with self.assertRaisesRegex(MODULE.ReleaseError, "允许范围外文件"):
                MODULE.validate_committed_scope(Path("."), ["skills/demo"])

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

    def test_failed_preflight_does_not_create_fork_or_change_push_remote(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "skills" / "demo").mkdir(parents=True)
            (root / "skills" / "demo" / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
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
                execute=True,
                skip_tests=True,
                keep_worktree=False,
                auto_fork=True,
            )
            failed_checks = {"ok": False, "checks": [{"name": "tests", "ok": False}]}
            with patch.object(MODULE, "run_command", return_value=str(root)):
                with patch.object(MODULE, "refresh_base_ref", return_value="upstream/main"):
                    with patch.object(MODULE, "list_changed_paths", return_value=(["skills/demo/SKILL.md"], [])):
                        with patch.object(MODULE, "validate_no_symlink_paths"):
                            with patch.object(
                                MODULE,
                                "resolve_target_repository",
                                return_value="liudu2326526/ai-cut-skills",
                            ):
                                with patch.object(MODULE, "github_login", return_value="014-code"):
                                    with patch.object(MODULE, "find_open_pr", return_value=None):
                                        with patch.object(MODULE, "remote_branch_sha", return_value=None):
                                            with patch.object(
                                                MODULE,
                                                "create_worktree",
                                                return_value=(root, root),
                                            ):
                                                with patch.object(
                                                    MODULE,
                                                    "ensure_staged_scope",
                                                    return_value=["skills/demo/SKILL.md"],
                                                ):
                                                    with patch.object(
                                                        MODULE,
                                                        "run_checks",
                                                        return_value=failed_checks,
                                                    ):
                                                        with patch.object(MODULE, "remove_worktree"):
                                                            with patch.object(
                                                                MODULE,
                                                                "ensure_fork_repository",
                                                            ) as ensure_fork:
                                                                with patch.object(
                                                                    MODULE,
                                                                    "ensure_release_push_remote",
                                                                ) as ensure_remote:
                                                                    with self.assertRaisesRegex(
                                                                        MODULE.ReleaseError,
                                                                        "提交前校验失败",
                                                                    ):
                                                                        MODULE.run_release(config)

            ensure_fork.assert_not_called()
            ensure_remote.assert_not_called()

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
        with patch.object(
            MODULE,
            "find_open_pr",
            return_value={"number": 15, "url": "https://example.test/pr/15"},
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
                )

        self.assertEqual(result, "https://example.test/pr/15")
        args = run.call_args.args[0]
        self.assertEqual(args[:5], ["gh", "api", "--method", "PATCH", "repos/liudu2326526/ai-cut-skills/pulls/15"])
        self.assertIn("--raw-field", args)
        self.assertTrue(any(value.startswith("body=## 变更") for value in args))

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


if __name__ == "__main__":
    unittest.main()
