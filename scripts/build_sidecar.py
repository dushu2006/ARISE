"""Build script for packaging the ARISE Python backend as a Tauri 2 sidecar executable."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from arise.supervisor import sidecar_binary_name


def build_sidecar(
    *,
    repo_root: Path,
    output_dir: Path | None = None,
    dry_run: bool = False,
) -> Path:
    target_dir = output_dir or (repo_root / "frontend" / "src-tauri" / "binaries")
    target_dir.mkdir(parents=True, exist_ok=True)
    binary_filename = sidecar_binary_name()
    destination = target_dir / binary_filename
    entry_script = repo_root / "src" / "arise" / "server.py"
    if not entry_script.is_file():
        raise FileNotFoundError(f"Backend entrypoint not found: {entry_script}")

    pyinstaller_cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        binary_filename.removesuffix(".exe"),
        "--distpath",
        str(target_dir),
        str(entry_script),
    ]
    if dry_run:
        return destination
    if shutil.which("pyinstaller") is None:
        raise RuntimeError("PyInstaller is required to build the standalone sidecar binary.")
    subprocess.run(pyinstaller_cmd, cwd=str(repo_root), check=True)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build ARISE Python sidecar for Tauri 2.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate paths and return target binary path.",
    )
    args = parser.parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    dest = build_sidecar(repo_root=repo_root, dry_run=args.dry_run)
    print(dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
