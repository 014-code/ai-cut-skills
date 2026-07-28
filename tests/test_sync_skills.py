from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "sync_skills.py"
SPEC = importlib.util.spec_from_file_location("sync_skills", MODULE_PATH)
assert SPEC and SPEC.loader
sync_skills = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sync_skills
SPEC.loader.exec_module(sync_skills)


class CatalogTests(unittest.TestCase):
    def test_repository_catalog_matches_skill_directories(self) -> None:
        catalog = sync_skills.load_catalog(REPO_ROOT / "skill-catalog.yaml")
        sync_skills.validate_catalog(catalog, REPO_ROOT / "skills")
        self.assertEqual(len(catalog["categories"]), 6)
        self.assertEqual(len(catalog["skills"]), 11)

    def test_readme_names_every_catalogued_skill(self) -> None:
        catalog = sync_skills.load_catalog(REPO_ROOT / "skill-catalog.yaml")
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        missing = [skill_name for skill_name in catalog["skills"] if f"`{skill_name}`" not in readme]
        self.assertEqual(missing, [])

    def test_selection_uses_category_and_skill_intersection_with_required_dependencies(self) -> None:
        catalog = sync_skills.load_catalog(REPO_ROOT / "skill-catalog.yaml")
        selected = sync_skills.choose_skills(
            catalog,
            ["edit-soda-music-video", "video-motion-effects"],
            ["production"],
        )
        self.assertEqual(
            selected,
            [
                "setup-video-editing-environment",
                "manage-visual-asset-library",
                "edit-soda-music-video",
            ],
        )

    def test_mogong_selection_includes_douyin_toolkit(self) -> None:
        catalog = sync_skills.load_catalog(REPO_ROOT / "skill-catalog.yaml")
        selected = sync_skills.choose_skills(catalog, ["mogong-gid-retrieval"], [])
        self.assertEqual(selected, ["douyin-video-toolkit", "mogong-gid-retrieval"])

    def test_unknown_catalog_dependency_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory) / "skills"
            (skills_dir / "example").mkdir(parents=True)
            (skills_dir / "example" / "SKILL.md").write_text("---\\nname: example\\n---\\n", encoding="utf-8")
            catalog = {
                "schema_version": 1,
                "categories": {"test": {"label": "Test"}},
                "sync": {"exclude_names": [], "exclude_suffixes": []},
                "skills": {
                    "example": {
                        "category": "test",
                        "summary": "Example",
                        "requires": ["missing"],
                        "optional": [],
                        "next_stage": [],
                    }
                },
            }
            with self.assertRaises(sync_skills.CatalogError):
                sync_skills.validate_catalog(catalog, skills_dir)


class SyncTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.excluded_names = {"node_modules", "__pycache__", ".DS_Store"}
        self.excluded_suffixes = (".pyc", ".pyo")

    def test_sync_copies_source_deletes_stale_and_preserves_excluded_runtime_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            source = temporary_root / "source"
            destination = temporary_root / "destination"
            (source / "scripts" / "remotion").mkdir(parents=True)
            (source / "SKILL.md").write_text("new skill", encoding="utf-8")
            (source / "scripts" / "main.py").write_text("print('new')", encoding="utf-8")
            (source / "__pycache__").mkdir()
            (source / "__pycache__" / "ignored.pyc").write_bytes(b"ignored")

            (destination / "scripts" / "remotion" / "node_modules").mkdir(parents=True)
            (destination / "scripts" / "remotion" / "node_modules" / "installed.js").write_text(
                "keep",
                encoding="utf-8",
            )
            (destination / "stale.txt").write_text("delete", encoding="utf-8")

            stats = sync_skills.sync_tree(
                source,
                destination,
                excluded_names=self.excluded_names,
                excluded_suffixes=self.excluded_suffixes,
                delete=True,
                dry_run=False,
            )

            self.assertEqual((destination / "SKILL.md").read_text(encoding="utf-8"), "new skill")
            self.assertTrue((destination / "scripts" / "main.py").is_file())
            self.assertFalse((destination / "stale.txt").exists())
            self.assertFalse((destination / "__pycache__").exists())
            self.assertTrue(
                (destination / "scripts" / "remotion" / "node_modules" / "installed.js").is_file()
            )
            self.assertEqual(stats.copied, 2)
            self.assertGreaterEqual(stats.deleted, 1)

    def test_dry_run_does_not_modify_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            source = temporary_root / "source"
            destination = temporary_root / "destination"
            source.mkdir()
            destination.mkdir()
            (source / "SKILL.md").write_text("new", encoding="utf-8")
            stale = destination / "stale.txt"
            stale.write_text("keep during dry run", encoding="utf-8")

            stats = sync_skills.sync_tree(
                source,
                destination,
                excluded_names=self.excluded_names,
                excluded_suffixes=self.excluded_suffixes,
                delete=True,
                dry_run=True,
            )

            self.assertTrue(stale.is_file())
            self.assertFalse((destination / "SKILL.md").exists())
            self.assertEqual(stats.copied, 1)
            self.assertEqual(stats.deleted, 1)

    def test_catalog_is_json_compatible_yaml(self) -> None:
        content = (REPO_ROOT / "skill-catalog.yaml").read_text(encoding="utf-8")
        parsed = json.loads(content)
        self.assertEqual(parsed["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
