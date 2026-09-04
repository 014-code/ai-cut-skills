"""Include the release Skill's safety regressions in repository CI discovery."""

import importlib.util
from pathlib import Path


def load_tests(loader, standard_tests, pattern):
    test_file = (
        Path(__file__).resolve().parents[1]
        / "skills" / "ai-cut-skills-release" / "tests" / "test_submit_pr.py"
    )
    spec = importlib.util.spec_from_file_location("release_skill_safety_tests", test_file)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load release Skill tests: {test_file}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return loader.loadTestsFromModule(module)
