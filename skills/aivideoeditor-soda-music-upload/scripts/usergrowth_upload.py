"""Delegate Soda Music upload execution to the validated UserGrowth runner.

The online package owns the Soda-only contract and this small entrypoint. The
full browser implementation remains in the maintained UserGrowth automation
Skill so the two packages cannot drift by copying private browser code.
"""

from __future__ import annotations

import os
from pathlib import Path
import runpy
import sys


def _delegate_path() -> Path:
    configured_home = str(os.environ.get("CODEX_HOME") or "").strip()
    codex_home = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
    candidates = [
        codex_home / "skills" / "aivideoeditor-usergrowth-automation" / "scripts" / "usergrowth_upload.py",
        Path.home() / ".codex" / "skills" / "aivideoeditor-usergrowth-automation" / "scripts" / "usergrowth_upload.py",
    ]
    for candidate in candidates:
        if candidate.is_file() and candidate.resolve() != Path(__file__).resolve():
            return candidate
    raise RuntimeError(
        "未找到已安装的 aivideoeditor-usergrowth-automation runner；"
        "请先同步综合 UserGrowth Skill"
    )


if __name__ == "__main__":
    delegate = _delegate_path()
    if str(delegate.parent) not in sys.path:
        sys.path.insert(0, str(delegate.parent))
    runpy.run_path(str(delegate), run_name="__main__")
