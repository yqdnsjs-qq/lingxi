"""把索引扫描过程中发现的损坏文件导出成清单，方便手动清理。"""
import collections
import csv
import sqlite3
from pathlib import Path

DB = r"D:\lingxi\data\index\index.db"
OUT_TXT = Path(r"D:\lingxi\损坏文件清单.txt")
OUT_CSV = Path(r"D:\lingxi\损坏文件清单.csv")

PATTERNS = ["无法识别", "0 字节", "读取图片失败", "broken data", "媒体编码失败"]
WHERE = " OR ".join("note LIKE '%" + p + "%'" for p in PATTERNS)


def reason(note: str) -> str:
    if "0 字节" in note:
        return "文件是 0 字节（同步没完成）"
    if "无法识别" in note:
        return "格式无法识别（文件损坏）"
    if "读取图片失败" in note:
        return "读取失败（数据流损坏）"
    if "broken data" in note:
        return "数据流损坏"
    if "媒体编码失败" in note:
        return "编码失败"
    return note[:40]


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def main() -> int:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        f"SELECT f.path, f.name, f.size, f.note, fo.path AS folder "
        f"FROM files f JOIN folders fo ON fo.id=f.folder_id "
        f"WHERE {WHERE} ORDER BY f.size, f.path").fetchall()

    print("=== 按文件夹统计 ===")
    for k, v in collections.Counter(r["folder"] for r in rows).most_common():
        print(f"  {v:>4} 个   {k}")
    print()
    print("=== 按原因统计 ===")
    for k, v in collections.Counter(reason(r["note"]) for r in rows).most_common():
        print(f"  {v:>4} 个   {k}")
    print()
    zero = sum(1 for r in rows if r["size"] == 0)
    print(f"  0 字节的     : {zero} 个（没占空间）")
    print(f"  有内容但损坏 : {len(rows) - zero} 个（占空间，值得删）")

    with OUT_TXT.open("w", encoding="utf-8-sig") as f:
        f.write("EmbeddingGemma 2 索引扫描发现的损坏文件\n")
        f.write(f"共 {len(rows)} 个 —— 删掉后重新扫描，日志就不会再刷这些警告\n")
        f.write("=" * 72 + "\n")
        cur = None
        for r in rows:
            if r["folder"] != cur:
                cur = r["folder"]
                f.write(f"\n【{cur}】\n")
            f.write(f"  {human(r['size']):>9s}  {reason(r['note']):24s}  {r['path']}\n")

    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["文件夹", "文件名", "完整路径", "字节数", "问题"])
        for r in rows:
            w.writerow([r["folder"], r["name"], r["path"], r["size"], reason(r["note"])])

    print()
    print(f"  已导出: {OUT_TXT}")
    print(f"  已导出: {OUT_CSV}")
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
