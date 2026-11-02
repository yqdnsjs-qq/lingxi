#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文本清洗工具（独立单文件，只用 Python 标准库，可以单独拷走用）

干什么
------
把「入库/检索前该洗掉的东西」从文本里清掉：

  · 控制字符（\\x00 之类）、零宽字符（\\u200b、\\ufeff 之类）
  · 连续空行 → 压缩成一个
  · 行尾空白
  · 噪声行：
      - URL 编码串，例如
        fxkjd2%2FkvYy3U%2F%2FPGaMj&exptype=time_recommend&fasttmpl_flag=0
      - 一堆 k=v&k=v 的长参数串（且不含中文）
      - 很长但"可读字符"占比很低的乱码行

不动什么
--------
正文、代码、表格、Markdown 语法一律保留 ✓

用法
----
    # 1) 先看效果（默认只预览，不改任何文件）
    python clean_text.py D:\笔记
    python clean_text.py D:\笔记\某篇.md

    # 2) 写到另一个目录（安全，推荐）
    python clean_text.py D:\笔记 --out D:\笔记-清洗后

    # 3) 原地改（会先把原文件备份成 xxx.bak，再写回）
    python clean_text.py D:\笔记 --inplace

    # 常用参数
    --ext .md,.txt,.csv    只处理这些后缀（默认 .md,.txt）
    --show 3               预览时每个文件展示 3 组"保留/丢弃"样例
    --quiet                只打汇总

注意
----
· 这个工具**不认识**任何特定项目，纯文本进出 ✓
· 幂等：洗过的文件再洗一次结果不变 ✓
· `--inplace` 一定会先建 `.bak` 备份；不放心就先用 `--out`
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

try:                                    # Windows 控制台是 GBK，别因为 ✓ 崩掉
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:                       # noqa: BLE001
    pass

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ZW_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
# 孤立代理字符（U+D800–U+DFFF）：PDF 里的 emoji 被拆散后会剩半个，
# 那不是合法字符，很多程序（含嵌入模型）会直接报错 ✗ → 这里统一清掉 ✓
_SURR_RE = re.compile(r"[\ud800-\udfff]")
# 强力清洗要丢的"危险字符"（v0.2）
_SAFE_DROP_RE = re.compile(
    "[\ud800-\udfff"                  # 代理（半个 emoji）
    "\ue000-\uf8ff"                   # 私有区
    "\ufff0-\uffff"                   # 非字符
    "\x00-\x08\x0b\x0c\x0e-\x1f\x7f"  # 控制字符
    "]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_URLENC_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_KEEP_PUNCT = " ，。、；：！？（）《》-—·.,:;!?()[]{}<>\"'“”‘’"


def _fix_surrogates(text: str) -> str:
    """能配对的 emoji 恢复 ✓，孤立的那半个删掉 ✓"""
    if not _SURR_RE.search(text):
        return text
    try:
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "strict")
    except UnicodeDecodeError:
        fixed = text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
        return fixed.replace("\ufffd", "")


def _readable_ratio(s: str) -> float:
    good = 0
    for ch in s:
        if ch.isalnum() or _CJK_RE.match(ch) or ch in _KEEP_PUNCT:
            good += 1
    return good / max(1, len(s))


def is_noise_line(line: str) -> bool:
    """这一行是不是该丢的噪声（URL 编码串 / 长参数串 / 乱码）"""
    if len(line) <= 40:
        return False
    if line.count("%") >= 3 and _URLENC_RE.search(line):
        return True
    if line.count("&") >= 3 and "=" in line and not _CJK_RE.search(line):
        return True
    return _readable_ratio(line) < 0.30


def hard_clean(text: str) -> str:
    """强力清洗（v0.2 新增）：把一切可能让程序挑剔的字符全丢掉。

    比 clean_text 更狠：代理字符、私有区、非字符、控制字符，以及**所有非 BMP 字符**
    （emoji 之类补充平面的字）。适合喂给"对字符很挑"的程序（嵌入模型、老系统）✓
    用 `--hard` 参数启用 ✓
    """
    if not text:
        return ""
    t = _fix_surrogates(text)
    t = _SAFE_DROP_RE.sub("", t)
    t = "".join(ch for ch in t if ord(ch) <= 0xFFFF)
    return t.strip()


def clean_text(text: str) -> str:
    """核心清洗函数：文本进、干净文本出（幂等）"""
    if not text:
        return ""
    t = _CTRL_RE.sub("", text)
    t = _ZW_RE.sub("", t)
    t = _fix_surrogates(t)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    blank = False
    for raw in t.split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            if not blank and out:
                out.append("")
            blank = True
            continue
        if is_noise_line(stripped):
            # ⚠ 丢噪声行时**不能**重置 blank：噪声行前后常常各有空行，
            #   重置了就会多出一个空行 ✗（实测：`零宽字符\n\n\n正文`）
            continue
        blank = False
        out.append(stripped)
    return "\n".join(out).strip()


def iter_files(root: Path, exts: set[str]):
    if root.is_file():
        if not exts or root.suffix.lower() in exts:
            yield root
        return
    for p in root.rglob("*"):
        try:
            if p.is_file() and p.suffix.lower() in exts:
                yield p
        except OSError:
            continue


def _norm_line(ln: str) -> str:
    """只做"字符级"清洗 + 去空白，**不判噪声** —— 用来对齐"清洗前/后"的行，
    否则"只差一个控制字符"的正文会被误报成"被丢弃"✗（实测踩过）"""
    return _ZW_RE.sub("", _CTRL_RE.sub("", ln)).strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="文本清洗（默认只预览，不改文件）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例：python clean_text.py D:\\笔记 --out D:\\笔记-清洗后",
    )
    ap.add_argument("path", nargs="+",
                    help="要处理的文件或目录（可以给多个：文件、目录混着也行）")
    ap.add_argument("--out", default="", help="输出目录（把清洗后的结果写到这里）")
    ap.add_argument("--inplace", action="store_true",
                    help="原地改（会先备份成 xxx.bak）")
    ap.add_argument("--ext", default=".md,.txt", help="只处理这些后缀（默认 .md,.txt）")
    ap.add_argument("--hard", action="store_true",
                    help="强力清洗：连私有区/非 BMP 字符也丢掉（喂给挑剔的程序时用）")
    ap.add_argument("--show", type=int, default=2, help="预览时每个文件展示几组样例")
    ap.add_argument("--quiet", action="store_true", help="只打汇总")
    args = ap.parse_args(argv)

    roots = [Path(p).expanduser() for p in args.path]
    missing = [r for r in roots if not r.exists()]
    if missing:
        for m in missing:
            print(f"[错误] 找不到：{m}")
        return 2
    exts = {e.strip().lower() if e.strip().startswith(".") else "." + e.strip().lower()
            for e in (args.ext or "").split(",") if e.strip()}

    out_dir = Path(args.out).expanduser() if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    # 多个输入 → 合并成一份文件清单（目录会展开）
    files: list[Path] = []
    for r in roots:
        files.extend(iter_files(r, exts))
    if not files:
        print(f"没有找到要处理的文件（后缀：{','.join(sorted(exts))}）")
        return 0

    print("=" * 72)
    print("  文本清洗")
    for r in roots[:6]:
        print(f"  源：{r}")
    if len(roots) > 6:
        print(f"  …共 {len(roots)} 个输入")
    if out_dir:
        print(f"  输出：{out_dir}")
    elif args.inplace:
        print("  模式：**原地修改**（每个文件先备份成 .bak）")
    else:
        print("  模式：只预览（不改任何文件）")
    print("=" * 72)

    n_files = n_changed = n_noise = 0
    total_lines_dropped = 0

    for p in files:
        n_files += 1
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"  [跳过] {p}（{exc}）")
            continue
        cleaned = hard_clean(raw) if args.hard else clean_text(raw)
        if cleaned == raw.strip():
            if not args.quiet:
                print(f"  · {p.name} — 已经是干净的（无改动）")
            continue
        n_changed += 1
        # ⚠ 对齐前先做"字符级清洗 + strip"，否则只差控制字符/缩进的行会被误报成丢弃 ✗
        before = [_norm_line(ln) for ln in raw.splitlines() if _norm_line(ln)]
        after = [_norm_line(ln) for ln in cleaned.splitlines() if _norm_line(ln)]
        after_set = set(after)
        gone = [ln for ln in before if ln not in after_set]
        if gone:
            n_noise += 1
            total_lines_dropped += len(gone)
        if not args.quiet:
            print(f"\n  · {p}")
            print(f"      字符 {len(raw)} → {len(cleaned)}；有效行 {len(before)} → {len(after)}"
                  + (f"（丢 {len(gone)} 行）" if gone else "（只是空白规整）"))
            if args.show > 0:
                for ln in gone[:args.show]:
                    print(f"      ✗ 丢弃：{ln[:76]}")

        # 写盘
        try:
            if out_dir:
                rel = None
                for r in roots:                     # 尽量保留相对结构 ✓
                    if r.is_dir():
                        try:
                            rel = p.relative_to(r)
                            break
                        except ValueError:
                            continue
                if rel is None:
                    rel = Path(p.name)              # 多目录重名时退回文件名 ✓
                dst = out_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(cleaned, encoding="utf-8")
                if not args.quiet:
                    print(f"      → 已写入 {dst}")
            elif args.inplace:
                bak = p.with_suffix(p.suffix + ".bak")
                if not bak.exists():                 # 别覆盖掉最早的那份备份 ✓
                    shutil.copy2(p, bak)
                p.write_text(cleaned, encoding="utf-8")
                if not args.quiet:
                    print(f"      → 已原地写回（备份：{bak.name}）")
        except OSError as exc:
            print(f"      [写入失败] {exc}")

    print("\n" + "=" * 72)
    print(f"  共 {n_files} 个文件：{n_changed} 个有改动"
          f"（其中 {n_noise} 个真的丢了噪声行、共 {total_lines_dropped} 行）")
    if not out_dir and not args.inplace:
        print("  这是预览，没有写任何文件。要保存请加 --out 目录（推荐）或 --inplace")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
