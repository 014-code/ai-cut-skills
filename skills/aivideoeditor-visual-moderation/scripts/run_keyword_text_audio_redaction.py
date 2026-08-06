#!/usr/bin/env python3
"""Primary entrypoint for business-keyword subtitle masking and synchronized audio mute planning."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
IMPLEMENTATION = HERE / "simulate_keyword_text_audio_redaction.py"


def main() -> int:
    spec = importlib.util.spec_from_file_location("keyword_text_audio_redaction_impl", IMPLEMENTATION)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load implementation: {IMPLEMENTATION}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return int(module.main())


if __name__ == "__main__":
    raise SystemExit(main())
