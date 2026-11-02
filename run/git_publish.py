"""把本机项目"导出成一份脱敏的公开仓库副本"，并推送到 GitHub。

## 为什么要有这个脚本（2026-11-02 项目主要求）

> "docs/ 全部文档内，应该也有本机信息吧，要求也脱敏一下发布，**本机文件不要受影响**"

所以发布走"导出 + 脱敏"两步，**不改本机工作区**：

    本机仓库（真实 IP / 真实路径 / 真名）
        │  git ls-files 列举"已跟踪"的文件（数据/模型/配置本来就没进来）
        ▼
    发布目录 D:\\lingxi-publish（干净副本）
        │  逐文件替换：内网 IP、域名、主机名、账号、私人路径、真名
        │  自检：残留敏感串 → 直接失败，不提交
        ▼
    git commit + push → GitHub

## 脱敏表放哪

**表本身不能进仓库**（表里就写着真实 IP 和真名），所以放在
`run/.pii_map.json`（已在 .gitignore 里排除）。脚本只从那里读：

```json
{"map": [["真实串", "占位符"], ...], "must_be_empty": ["正则", ...]}
```

## 用法

    # 只生成/更新本地那份副本，不推
    python run\\git_publish.py

    # 生成 + 提交 + 推送（token 走环境变量，别写进命令历史）
    $env:GH_TOKEN = "github_pat_xxx"        # 需要 Contents: Read and write
    python run\\git_publish.py --push

## 注意

- 发布目录里**只放已跟踪文件**，所以 `data/ models/ venv/ config.ini` 天然不会出现 ✓
- 以后每次更新：改完本机 → 跑一次这个脚本 → 它会把副本覆盖成最新并提交 ✓
- 本机工作区一个字节都不会被改（脚本只写发布目录）✓
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEST = Path(r"D:\lingxi-publish")
REPO_URL = "https://github.com/admin-qq/lingxi.git"
MAP_FILE = ROOT / "run" / ".pii_map.json"

try:                                    # 控制台是 GBK，输出里带 ✗ ✓ 会炸
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:                       # noqa: BLE001
    pass

TEXT_EXT = {
    ".py", ".js", ".css", ".html", ".md", ".txt", ".ini", ".bat", ".cmd", ".sh",
    ".service", ".json", ".yml", ".yaml", ".ps1", ".gitignore", ".gitattributes",
}


def load_map() -> tuple[list[tuple[str, str]], list[str]]:
    if not MAP_FILE.is_file():
        print(f"✗ 找不到脱敏表：{MAP_FILE}")
        print("  这个文件含真实 IP/真名，**故意不进仓库**；从备份里捞回来再跑。")
        raise SystemExit(4)
    data = json.loads(MAP_FILE.read_text(encoding="utf-8"))
    pairs = [(str(a), str(b)) for a, b in data.get("map", [])]
    must = [str(x) for x in data.get("must_be_empty", [])]
    return pairs, must


def run(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=check)


def tracked_files() -> list[str]:
    out = run(["git", "-c", "core.quotepath=false", "ls-files", "-z"], ROOT).stdout
    return [x for x in out.split("\0") if x]


def copy_tree(dest: Path) -> int:
    files = tracked_files()
    n = 0
    for rel in files:
        src = ROOT / rel
        if not src.is_file():
            continue
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            try:
                os.chmod(dst, 0o666)      # 上次跑可能留下只读文件
            except OSError:
                pass
            try:
                dst.unlink()
            except OSError:
                pass
        shutil.copyfile(src, dst)
        n += 1
    return n


def is_text(p: Path) -> bool:
    return p.suffix.lower() in TEXT_EXT or p.name in (
        ".gitignore", ".gitattributes", "AGENTS.md", "README.md")


def scrub(dest: Path, pairs: list[tuple[str, str]]) -> tuple[int, dict[str, int]]:
    counts: dict[str, int] = {}
    changed = 0
    for p in dest.rglob("*"):
        if not p.is_file() or not is_text(p):
            continue
        try:
            t = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        orig = t
        for old, new in pairs:
            c = t.count(old)
            if c:
                t = t.replace(old, new)
                counts[old] = counts.get(old, 0) + c
        if t != orig:
            p.write_text(t, encoding="utf-8", newline="")
            changed += 1
    return changed, counts


def verify(dest: Path, must: list[str]) -> list[str]:
    bad: list[str] = []
    for p in dest.rglob("*"):
        if not p.is_file() or not is_text(p):
            continue
        try:
            t = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pat in must:
            for m in re.finditer(pat, t):
                line = t[:m.start()].count("\n") + 1
                bad.append(f"{p.relative_to(dest)}:{line}: {m.group(0)}")
    return bad


def syntax_check(dest: Path) -> list[str]:
    """发布副本也要能跑：Python 全量编译 + 前端 node --check。"""
    errs: list[str] = []
    py = ROOT / "venv" / "Scripts" / "python.exe"
    if not py.is_file():
        py = ROOT / "venv" / "bin" / "python"
    if py.is_file():
        r = subprocess.run([str(py), "-m", "compileall", "-q", str(dest / "app")],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        if r.returncode != 0:
            errs.append((r.stdout or "") + (r.stderr or ""))

    node = shutil.which("node")
    if node:
        for js in (dest / "app" / "static").glob("app.*.js"):
            r = subprocess.run([node, "--check", str(js)], capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
            if r.returncode != 0:
                errs.append(f"{js.name}: {r.stderr[:200]}")
    return errs


def git_commit(dest: Path, push: bool, token: str) -> None:
    if not (dest / ".git").is_dir():
        run(["git", "init", "-b", "main"], dest)
        run(["git", "remote", "add", "origin", REPO_URL], dest, check=False)
    run(["git", "add", "-A"], dest)
    st = run(["git", "status", "--porcelain"], dest).stdout.strip()
    if st:
        run(["git", "-c", "user.name=admin",
             "-c", "user.email=admin@users.noreply.github.com",
             "commit", "-q", "-m", "灵犀 v3.5.3（公开版，已脱敏）"], dest)
    run(["git", "remote", "set-url", "origin", REPO_URL], dest, check=False)
    if not push:
        print("   没加 --push，跳过推送")
        return
    if not token:
        print("   ✗ 没有 token：先 $env:GH_TOKEN = 'github_pat_...'")
        return
    url = REPO_URL.replace("https://", f"https://x-access-token:{token}@")
    r = run(["git", "push", url, "main:main"], dest, check=False)
    out = (r.stdout or "") + (r.stderr or "")
    print("   推送输出：", out.strip()[:300])
    if "403" in out or "denied" in out:
        print("   ↑ token 没有写权限：细粒度 token 需要 Contents: Read and write")
    run(["git", "remote", "set-url", "origin", REPO_URL], dest, check=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(DEFAULT_DEST))
    ap.add_argument("--push", action="store_true")
    args = ap.parse_args()

    pairs, must = load_map()
    dest = Path(args.dir)
    print(f"① 导出已跟踪文件 → {dest}")
    if dest.exists():
        for sub in ("app", "docs", "run"):
            shutil.rmtree(dest / sub, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    n = copy_tree(dest)
    print(f"   {n} 个文件")

    print("② 脱敏")
    changed, counts = scrub(dest, pairs)
    print(f"   改动 {changed} 个文件")
    for old, c in sorted(counts.items(), key=lambda x: -x[1])[:10]:
        shown = old if len(old) <= 24 else old[:24] + "…"
        print(f"     {c:4d} 处  {shown!r}")

    print("③ 自检：残留敏感串")
    bad = verify(dest, must)
    if bad:
        print("   ✗ 还有残留，已中止：")
        for line in bad[:12]:
            print("     ", line)
        return 2
    print("   0 命中 ✓")

    print("④ 语法自检（发布副本也要能跑）")
    errs = syntax_check(dest)
    if errs:
        print("   ✗ 失败：", errs[0][:300])
        return 3
    print("   Python / JS 全部通过 ✓")

    print("⑤ 提交" + (" + 推送" if args.push else ""))
    git_commit(dest, args.push, os.environ.get("GH_TOKEN", ""))
    print("\n完成。发布目录：", dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
