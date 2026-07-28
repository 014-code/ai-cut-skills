#!/usr/bin/env python3
"""Synchronize catalogued skills into Codex and WorkBuddy runtime directories."""

from __future__ import annotations

import argparse
import filecmp
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = REPO_ROOT / "skill-catalog.yaml"
DEFAULT_SKILLS_DIR = REPO_ROOT / "skills"


class CatalogError(RuntimeError):
    """Raised when the catalog and repository disagree."""


@dataclass
class SyncStats:
    copied: int = 0
    unchanged: int = 0
    deleted: int = 0
    directories_created: int = 0

    def add(self, other: "SyncStats") -> None:
        self.copied += other.copied
        self.unchanged += other.unchanged
        self.deleted += other.deleted
        self.directories_created += other.directories_created

    def as_dict(self) -> dict[str, int]:
        return {
            "copied": self.copied,
            "unchanged": self.unchanged,
            "deleted": self.deleted,
            "directories_created": self.directories_created,
        }


def load_catalog(path: Path) -> dict[str, Any]:
    """Load the zero-dependency JSON-compatible YAML catalog."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CatalogError(f"catalog not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CatalogError(
            f"{path} must remain JSON-compatible YAML so the sync script needs no PyYAML dependency: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise CatalogError("catalog root must be an object")
    return data


def repository_skill_names(skills_dir: Path) -> set[str]:
    if not skills_dir.is_dir():
        raise CatalogError(f"skills directory not found: {skills_dir}")
    return {
        child.name
        for child in skills_dir.iterdir()
        if child.is_dir() and (child / "SKILL.md").is_file()
    }


def require_string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise CatalogError(f"{field} must be a list of non-empty strings")
    return value


def validate_catalog(catalog: dict[str, Any], skills_dir: Path) -> None:
    if catalog.get("schema_version") != 1:
        raise CatalogError("unsupported or missing schema_version; expected 1")

    categories = catalog.get("categories")
    skills = catalog.get("skills")
    sync = catalog.get("sync")
    if not isinstance(categories, dict) or not categories:
        raise CatalogError("categories must be a non-empty object")
    if not isinstance(skills, dict) or not skills:
        raise CatalogError("skills must be a non-empty object")
    if not isinstance(sync, dict):
        raise CatalogError("sync must be an object")

    require_string_list(sync.get("exclude_names"), "sync.exclude_names")
    require_string_list(sync.get("exclude_suffixes"), "sync.exclude_suffixes")

    catalog_names = set(skills)
    disk_names = repository_skill_names(skills_dir)
    if catalog_names != disk_names:
        missing_from_catalog = sorted(disk_names - catalog_names)
        missing_from_disk = sorted(catalog_names - disk_names)
        details = []
        if missing_from_catalog:
            details.append(f"missing from catalog: {', '.join(missing_from_catalog)}")
        if missing_from_disk:
            details.append(f"missing from skills/: {', '.join(missing_from_disk)}")
        raise CatalogError("; ".join(details))

    for category_name, category in categories.items():
        if not isinstance(category_name, str) or not category_name:
            raise CatalogError("category names must be non-empty strings")
        if not isinstance(category, dict):
            raise CatalogError(f"category {category_name} must be an object")
        if not isinstance(category.get("label"), str) or not category["label"]:
            raise CatalogError(f"category {category_name} is missing label")

    for skill_name, metadata in skills.items():
        if not isinstance(metadata, dict):
            raise CatalogError(f"skill {skill_name} metadata must be an object")
        category = metadata.get("category")
        if category not in categories:
            raise CatalogError(f"skill {skill_name} references unknown category: {category}")
        if not isinstance(metadata.get("summary"), str) or not metadata["summary"]:
            raise CatalogError(f"skill {skill_name} is missing summary")
        for field in ("requires", "optional", "next_stage"):
            references = require_string_list(metadata.get(field), f"skills.{skill_name}.{field}")
            unknown = sorted(set(references) - catalog_names)
            if unknown:
                raise CatalogError(
                    f"skill {skill_name} field {field} references unknown skills: {', '.join(unknown)}"
                )
            if skill_name in references:
                raise CatalogError(f"skill {skill_name} cannot reference itself in {field}")


def choose_skills(
    catalog: dict[str, Any],
    requested_skills: Iterable[str],
    requested_categories: Iterable[str],
) -> list[str]:
    skills = catalog["skills"]
    categories = catalog["categories"]
    skill_filter = set(requested_skills)
    category_filter = set(requested_categories)

    unknown_skills = sorted(skill_filter - set(skills))
    if unknown_skills:
        raise CatalogError(f"unknown skills: {', '.join(unknown_skills)}")
    unknown_categories = sorted(category_filter - set(categories))
    if unknown_categories:
        raise CatalogError(f"unknown categories: {', '.join(unknown_categories)}")

    selected = []
    for skill_name, metadata in skills.items():
        if skill_filter and skill_name not in skill_filter:
            continue
        if category_filter and metadata["category"] not in category_filter:
            continue
        selected.append(skill_name)
    if not selected:
        raise CatalogError("filters selected no skills")
    return selected


def default_runtime_skills_dir(environment_name: str, fallback_directory: str) -> Path:
    home = os.environ.get(environment_name)
    base = Path(home).expanduser() if home else Path.home() / fallback_directory
    return base / "skills"


def exclusion_rules(catalog: dict[str, Any]) -> tuple[set[str], tuple[str, ...]]:
    sync = catalog["sync"]
    return set(sync["exclude_names"]), tuple(sync["exclude_suffixes"])


def is_excluded(relative_path: Path, excluded_names: set[str], excluded_suffixes: tuple[str, ...]) -> bool:
    if any(part in excluded_names for part in relative_path.parts):
        return True
    return relative_path.name.endswith(excluded_suffixes)


def files_match(source: Path, destination: Path) -> bool:
    if not destination.is_file() or source.stat().st_size != destination.stat().st_size:
        return False
    return filecmp.cmp(source, destination, shallow=False)


def announce(action: str, path: Path, dry_run: bool) -> None:
    prefix = "DRY-RUN " if dry_run else ""
    print(f"[{prefix}{action}] {path}")


def protected_destination_directories(
    destination: Path,
    excluded_names: set[str],
    excluded_suffixes: tuple[str, ...],
) -> set[Path]:
    protected: set[Path] = set()
    if not destination.exists():
        return protected

    def protect_ancestors(relative_path: Path) -> None:
        current = relative_path
        while current != Path("."):
            protected.add(current)
            current = current.parent

    for root, directory_names, file_names in os.walk(destination):
        root_path = Path(root)
        kept_directories = []
        for name in directory_names:
            relative = (root_path / name).relative_to(destination)
            if is_excluded(relative, excluded_names, excluded_suffixes):
                protect_ancestors(relative)
            else:
                kept_directories.append(name)
        directory_names[:] = kept_directories
        for name in file_names:
            relative = (root_path / name).relative_to(destination)
            if is_excluded(relative, excluded_names, excluded_suffixes):
                protect_ancestors(relative.parent)
    return protected


def sync_tree(
    source: Path,
    destination: Path,
    *,
    excluded_names: set[str],
    excluded_suffixes: tuple[str, ...],
    delete: bool,
    dry_run: bool,
) -> SyncStats:
    if not source.is_dir():
        raise FileNotFoundError(source)

    stats = SyncStats()
    protected_directories = protected_destination_directories(
        destination,
        excluded_names,
        excluded_suffixes,
    )

    if not destination.exists():
        announce("mkdir", destination, dry_run)
        stats.directories_created += 1
        if not dry_run:
            destination.mkdir(parents=True, exist_ok=True)

    for root, directory_names, file_names in os.walk(source):
        root_path = Path(root)
        relative_root = root_path.relative_to(source)
        directory_names[:] = [
            name
            for name in directory_names
            if not is_excluded(relative_root / name, excluded_names, excluded_suffixes)
        ]

        target_root = destination / relative_root
        if relative_root != Path(".") and not target_root.exists():
            announce("mkdir", target_root, dry_run)
            stats.directories_created += 1
            if not dry_run:
                target_root.mkdir(parents=True, exist_ok=True)

        for name in file_names:
            relative = relative_root / name
            if is_excluded(relative, excluded_names, excluded_suffixes):
                continue
            source_file = source / relative
            destination_file = destination / relative
            if destination_file.exists() and files_match(source_file, destination_file):
                stats.unchanged += 1
                continue
            announce("copy", destination_file, dry_run)
            stats.copied += 1
            if not dry_run:
                destination_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, destination_file)

    if delete and destination.exists():
        for root, directory_names, file_names in os.walk(destination, topdown=False):
            root_path = Path(root)
            relative_root = root_path.relative_to(destination)
            for name in file_names:
                relative = relative_root / name
                if is_excluded(relative, excluded_names, excluded_suffixes):
                    continue
                if not (source / relative).exists():
                    stale_file = destination / relative
                    announce("delete", stale_file, dry_run)
                    stats.deleted += 1
                    if not dry_run:
                        stale_file.unlink()
            for name in directory_names:
                relative = relative_root / name
                if (
                    is_excluded(relative, excluded_names, excluded_suffixes)
                    or relative in protected_directories
                    or (source / relative).exists()
                ):
                    continue
                stale_directory = destination / relative
                announce("delete", stale_directory, dry_run)
                stats.deleted += 1
                if not dry_run:
                    shutil.rmtree(stale_directory)
    return stats


def print_catalog(catalog: dict[str, Any]) -> None:
    skills = catalog["skills"]
    for category_name, category in catalog["categories"].items():
        print(f"{category_name}\t{category['label']}")
        for skill_name, metadata in skills.items():
            if metadata["category"] == category_name:
                print(f"  {skill_name}\t{metadata['summary']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synchronize catalogued repository skills into Codex and WorkBuddy runtimes."
    )
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--source-skills-dir", type=Path, default=DEFAULT_SKILLS_DIR)
    parser.add_argument("--runtime", choices=("all", "codex", "workbuddy"), default="all")
    parser.add_argument("--codex-skills-dir", type=Path)
    parser.add_argument("--workbuddy-skills-dir", type=Path)
    parser.add_argument("--category", action="append", default=[], help="Category id; repeatable.")
    parser.add_argument("--skill", action="append", default=[], help="Skill name; repeatable.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-delete", action="store_true")
    parser.add_argument("--check", action="store_true", help="Validate the catalog and exit.")
    parser.add_argument("--list", action="store_true", help="List categories and skills, then exit.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    catalog_path = args.catalog.expanduser().resolve()
    source_skills_dir = args.source_skills_dir.expanduser().resolve()
    catalog = load_catalog(catalog_path)
    validate_catalog(catalog, source_skills_dir)

    if args.check:
        print(
            json.dumps(
                {
                    "ok": True,
                    "catalog": str(catalog_path),
                    "skills_dir": str(source_skills_dir),
                    "skill_count": len(catalog["skills"]),
                    "category_count": len(catalog["categories"]),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.list:
        print_catalog(catalog)
        return 0

    selected = choose_skills(catalog, args.skill, args.category)
    excluded_names, excluded_suffixes = exclusion_rules(catalog)
    codex_skills_dir = (
        args.codex_skills_dir.expanduser().resolve()
        if args.codex_skills_dir
        else default_runtime_skills_dir("CODEX_HOME", ".codex").resolve()
    )
    workbuddy_skills_dir = (
        args.workbuddy_skills_dir.expanduser().resolve()
        if args.workbuddy_skills_dir
        else default_runtime_skills_dir("WORKBUDDY_HOME", ".workbuddy").resolve()
    )
    runtimes = {
        "codex": codex_skills_dir,
        "workbuddy": workbuddy_skills_dir,
    }
    requested_runtimes = tuple(runtimes) if args.runtime == "all" else (args.runtime,)

    total = SyncStats()
    for runtime in requested_runtimes:
        runtime_root = runtimes[runtime]
        for skill_name in selected:
            print(f"\n{runtime}: {skill_name}")
            stats = sync_tree(
                source_skills_dir / skill_name,
                runtime_root / skill_name,
                excluded_names=excluded_names,
                excluded_suffixes=excluded_suffixes,
                delete=not args.no_delete,
                dry_run=args.dry_run,
            )
            total.add(stats)

    print(
        "\n"
        + json.dumps(
            {
                "ok": True,
                "dry_run": args.dry_run,
                "delete": not args.no_delete,
                "runtimes": list(requested_runtimes),
                "skills": selected,
                "stats": total.as_dict(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CatalogError, FileNotFoundError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
