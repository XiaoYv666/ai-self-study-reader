#!/usr/bin/env python
"""孤儿文件清理：扫描 originals/ 与 pdfs/，比对 documents 表，删除无主文件。

手动脚本，不自动跑：
    python cleanup_orphans.py           # 干跑，仅列出
    python cleanup_orphans.py --apply   # 实际删除
"""
import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "app.db"
DIRS = [ROOT / "data" / "originals", ROOT / "data" / "pdfs"]
SKIP_NAMES = {".lo_profile", ".DS_Store"}


def known_paths(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT orig_path, pdf_path FROM documents").fetchall()
    return {p for r in rows for p in r if p}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际删除（默认干跑）")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    known = known_paths(conn)

    orphans: list[Path] = []
    for d in DIRS:
        if not d.exists():
            continue
        for f in d.iterdir():
            if f.name in SKIP_NAMES or f.is_dir():
                continue
            if str(f) not in known:
                orphans.append(f)

    if not orphans:
        print("无孤儿文件")
        return 0

    for f in orphans:
        size_kb = f.stat().st_size / 1024
        print(f"{'DEL' if args.apply else 'DRY'}  {f.relative_to(ROOT)}  ({size_kb:.0f} KB)")
        if args.apply:
            f.unlink()

    print(f"\n共 {len(orphans)} 个孤儿{'已删除' if args.apply else '（干跑未删，--apply 生效）'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
