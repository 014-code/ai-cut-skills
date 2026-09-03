from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile


SKILL_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = Path(__file__).resolve().parent / "usergrowth_desktop.py"
APP_NAME = "UserGrowth自动化上传桌面端"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(output_root: Path) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    work_root = output_root / "_pyinstaller_build"
    spec_root = output_root / "_pyinstaller_spec"
    shutil.rmtree(work_root, ignore_errors=True)
    shutil.rmtree(spec_root, ignore_errors=True)
    exe = output_root / f"{APP_NAME}.exe"
    exe.unlink(missing_ok=True)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        APP_NAME,
        "--distpath",
        str(output_root),
        "--workpath",
        str(work_root),
        "--specpath",
        str(spec_root),
        "--paths",
        str(SKILL_ROOT / "scripts"),
        "--collect-all",
        "playwright",
        "--collect-all",
        "ddddocr",
        "--collect-all",
        "onnxruntime",
        "--collect-submodules",
        "usergrowth_automation",
        "--hidden-import",
        "usergrowth_upload",
        "--hidden-import",
        "tomato_music_tagging",
        str(ENTRYPOINT),
    ]
    subprocess.run(command, cwd=SKILL_ROOT, check=True)
    if not exe.is_file():
        raise RuntimeError(f"PyInstaller 未生成目标文件：{exe}")

    digest = sha256(exe)
    (output_root / f"{exe.name}.sha256").write_text(f"{digest}  {exe.name}\n", encoding="utf-8")
    zip_path = output_root / f"{APP_NAME}.zip"
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(exe, exe.name)
        archive.write(output_root / f"{exe.name}.sha256", f"{exe.name}.sha256")
    print(f"EXE: {exe}")
    print(f"SHA256: {digest}")
    print(f"ZIP: {zip_path}")
    return exe


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the UserGrowth automation desktop launcher.")
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    build(args.output_root.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
