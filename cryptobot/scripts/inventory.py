"""List every repository file with a line count (handover inventory)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SKIP_PARTS = {"__pycache__", ".repro"}

IGNORED_DIRS = {"reports/exports", "reports/.repro", "reports/.evidence-tmp"}


def main() -> int:
    files = [
        path
        for path in sorted(ROOT.rglob("*"))
        if path.is_file()
        and not (SKIP_PARTS & set(path.parts))
        and not any(str(path.relative_to(ROOT)).replace("\\", "/").startswith(d) for d in IGNORED_DIRS)
    ]
    total_lines = 0
    total_bytes = 0
    for path in files:
        relative = str(path.relative_to(ROOT)).replace("\\", "/")
        raw = path.read_bytes()
        try:
            lines = len(raw.decode("utf-8").splitlines())
        except UnicodeDecodeError:
            lines = 0
        total_lines += lines
        total_bytes += len(raw)
        print("{:<52} {:>7} lines {:>10} bytes".format(relative, lines, len(raw)))
    print()
    print("files: {} | total lines: {} | total size: {:.1f} KiB".format(
        len(files), total_lines, total_bytes / 1024))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
