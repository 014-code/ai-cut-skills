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
LOW_SIGNAL_TERMS = {
    "skill",
    "ad",
    "video",
    "videos",
    "file",
    "files",
    "task",
    "tasks",
    "需要",
    "只需要",
    "视频",
    "素材",
    "生成",
    "输出",
    "处理",
}


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


@dataclass
class RouteCandidate:
    skill_name: str
    score: float
    match_score: float
    negative_score: float
    quality_score: float
    capability_path: list[str]
    reasons: list[str]
    negative_reasons: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill_name,
            "score": round(self.score, 4),
            "match_score": round(self.match_score, 4),
            "negative_score": round(self.negative_score, 4),
            "quality_score": round(self.quality_score, 4),
            "capability_path": self.capability_path,
            "reasons": self.reasons,
            "negative_reasons": self.negative_reasons,
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


def require_optional_string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    return require_string_list(value, field)


def require_optional_quality(value: Any, field: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise CatalogError(f"{field} must be an object")
    for metric_name, metric_value in value.items():
        if metric_name not in {"confidence", "success_rate"}:
            raise CatalogError(f"{field}.{metric_name} is not supported")
        if not isinstance(metric_value, (int, float)) or not 0 <= metric_value <= 1:
            raise CatalogError(f"{field}.{metric_name} must be a number from 0 to 1")


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
        for field in ("capability_path", "tags", "when_to_use", "when_not_use", "inputs", "outputs"):
            require_optional_string_list(metadata.get(field), f"skills.{skill_name}.{field}")
        require_optional_quality(metadata.get("quality"), f"skills.{skill_name}.quality")
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
    *,
    include_dependencies: bool = True,
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

    initially_selected = []
    for skill_name, metadata in skills.items():
        if skill_filter and skill_name not in skill_filter:
            continue
        if category_filter and metadata["category"] not in category_filter:
            continue
        initially_selected.append(skill_name)
    if not initially_selected:
        raise CatalogError("filters selected no skills")

    selected = set(initially_selected)
    if include_dependencies:
        visiting: set[str] = set()
        visited: set[str] = set()

        def add_dependencies(skill_name: str) -> None:
            if skill_name in visited:
                return
            if skill_name in visiting:
                raise CatalogError(f"cyclic required dependency involving: {skill_name}")
            visiting.add(skill_name)
            for dependency in skills[skill_name]["requires"]:
                selected.add(dependency)
                add_dependencies(dependency)
            visiting.remove(skill_name)
            visited.add(skill_name)

        for skill_name in initially_selected:
            add_dependencies(skill_name)
    return [skill_name for skill_name in skills if skill_name in selected]


def normalize_text(value: str) -> str:
    return " ".join(value.casefold().replace("_", " ").replace("-", " ").split())


def split_terms(value: str) -> list[str]:
    normalized = normalize_text(value)
    separators = " \t\r\n,.;:，。；：、/\\|()[]{}<>\"'`"
    current = []
    terms = []
    for character in normalized:
        if character in separators:
            if current:
                terms.append("".join(current))
                current = []
            continue
        current.append(character)
    if current:
        terms.append("".join(current))
    return [term for term in terms if len(term) >= 2 and term not in LOW_SIGNAL_TERMS]


def add_weighted_matches(
    *,
    query: str,
    phrases: Iterable[str],
    weight: float,
    label: str,
    reasons: list[str],
) -> float:
    score = 0.0
    seen = set(reasons)
    normalized_query = normalize_text(query)
    for phrase in phrases:
        normalized_phrase = normalize_text(phrase)
        if not normalized_phrase:
            continue
        reason = f"{label}: {phrase}"
        if normalized_phrase in normalized_query:
            score += weight
            if reason not in seen:
                reasons.append(reason)
                seen.add(reason)
            continue
        term_hits = [term for term in split_terms(normalized_phrase) if term in normalized_query]
        if term_hits:
            score += weight * min(1.0, 0.35 * len(term_hits))
            term_reason = f"{label}: {'/'.join(term_hits)}"
            if term_reason not in seen:
                reasons.append(term_reason)
                seen.add(term_reason)
    return score


def metadata_quality(metadata: dict[str, Any]) -> float:
    quality = metadata.get("quality") or {}
    if not quality:
        return 1.0
    metrics = [
        float(quality[metric_name])
        for metric_name in ("confidence", "success_rate")
        if metric_name in quality
    ]
    if not metrics:
        return 1.0
    return sum(metrics) / len(metrics)


def route_skills(catalog: dict[str, Any], query: str, *, top: int = 5) -> list[RouteCandidate]:
    if not query.strip():
        raise CatalogError("route query cannot be empty")
    if top <= 0:
        raise CatalogError("top must be greater than 0")

    candidates = []
    categories = catalog["categories"]
    for skill_name, metadata in catalog["skills"].items():
        category = metadata["category"]
        category_text = [category, categories[category]["label"]]
        capability_path = metadata.get("capability_path", [])
        reasons: list[str] = []
        negative_reasons: list[str] = []

        match_score = 0.0
        match_score += add_weighted_matches(
            query=query,
            phrases=[skill_name, metadata["summary"], *category_text],
            weight=1.0,
            label="summary",
            reasons=reasons,
        )
        match_score += add_weighted_matches(
            query=query,
            phrases=capability_path,
            weight=1.4,
            label="capability",
            reasons=reasons,
        )
        match_score += add_weighted_matches(
            query=query,
            phrases=metadata.get("tags", []),
            weight=1.6,
            label="tag",
            reasons=reasons,
        )
        match_score += add_weighted_matches(
            query=query,
            phrases=metadata.get("when_to_use", []),
            weight=2.2,
            label="when_to_use",
            reasons=reasons,
        )
        match_score += add_weighted_matches(
            query=query,
            phrases=metadata.get("inputs", []),
            weight=0.8,
            label="input",
            reasons=reasons,
        )
        match_score += add_weighted_matches(
            query=query,
            phrases=metadata.get("outputs", []),
            weight=0.8,
            label="output",
            reasons=reasons,
        )

        negative_score = add_weighted_matches(
            query=query,
            phrases=metadata.get("when_not_use", []),
            weight=2.5,
            label="when_not_use",
            reasons=negative_reasons,
        )
        quality_score = metadata_quality(metadata)
        score = max(match_score - negative_score, 0.0) * quality_score
        if score > 0:
            candidates.append(
                RouteCandidate(
                    skill_name=skill_name,
                    score=score,
                    match_score=match_score,
                    negative_score=negative_score,
                    quality_score=quality_score,
                    capability_path=capability_path,
                    reasons=reasons[:6],
                    negative_reasons=negative_reasons[:4],
                )
            )

    candidates.sort(key=lambda candidate: (-candidate.score, -candidate.match_score, candidate.skill_name))
    return candidates[:top]


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
    parser.add_argument("--no-dependencies", action="store_true", help="Do not include required Skill dependencies.")
    parser.add_argument("--check", action="store_true", help="Validate the catalog and exit.")
    parser.add_argument("--list", action="store_true", help="List categories and skills, then exit.")
    parser.add_argument("--route", help="Rank candidate skills for a user intent, then exit.")
    parser.add_argument("--top", type=int, default=5, help="Candidate count for --route.")
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
    if args.route:
        candidates = route_skills(catalog, args.route, top=args.top)
        print(
            json.dumps(
                {
                    "ok": True,
                    "query": args.route,
                    "candidates": [candidate.as_dict() for candidate in candidates],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    selected = choose_skills(
        catalog,
        args.skill,
        args.category,
        include_dependencies=not args.no_dependencies,
    )
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
