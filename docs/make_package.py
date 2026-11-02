"""把项目打包成可以直接发给别人的压缩包。

只装「别人没有的那部分」：启动脚本 + 程序源码。

不含本机信息，也不含 docs 里的对外文档（部署笔记、推广文）——
那些是给分享者用的，不随程序包一起发。
不装 venv / models / .cache / data / logs / backups —— 那些几个 GB，
而且 setup.bat 会自动重建，发出去只是浪费带宽。

用法：
    venv\\Scripts\\python.exe docs\\make_package.py
产物：
    dist\\embeddinggemma2-search-v<版本>.zip      （约 1 MB）
"""
from __future__ import annotations

import re
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

# 项目根目录下要带上的文件
ROOT_FILES = [
    "setup.bat", "start.bat", "search.bat", "stop.bat", "restart.bat",
    "status.bat", "backup.bat", "restore.bat", "fix-permissions.bat",
    "selftest.bat", "安装PDF原版式.bat", "config.ini", "requirements.txt", "README.md",
]

# 要完整带上的目录（递归，自动跳过 __pycache__ 等）
TREES = [
    "app",          # 程序本体
    "run/deploy",   # 服务器部署 + 双向同步工具（v2.0 起随包分发）
]

# 某个子目录里额外不要的东西
TREE_SKIP = {
    # keys 里是 SSH 私钥 —— 绝对不能进包；_sync_state 里是本机文件清单（含个人路径）
    "run/deploy": {"keys", "_sync_state", "_synctest_local", "_synctest_remote", "out"},
}
TREE_SKIP_SUFFIX = {
    "run/deploy": {".tar.gz", ".gz", ".tgz"},   # 下载下来的二进制不随包发
}
TREE_SKIP_NAMES = {"known_hosts", "_db_sync_state.json", "_names.json", "askpass.cmd"}
# 以这些前缀开头的也一律不要（deploy 里 _xxx 都是临时/状态文件，可能含个人路径与姓名）
TREE_SKIP_PREFIX = {"run/deploy": ("_",)}

# 任何位置都不要的。
# 注意：这里**不能**放 "run" —— 它会连 run/deploy（服务器部署工具，要随包发）一起拦掉。
# 只扫 TREES 里列出的目录，没列的自然不会被收进来。
SKIP_DIRS = {"__pycache__", ".pytest_cache", "dist", "backups", "logs",
             "venv", "models", "data", ".cache", ".git"}
SKIP_SUFFIX = {".pyc", ".pyo", ".part", ".tmp", ".log", ".db"}

READ_ME = """宇之语 · 灵犀   v{version}
============================================================

用大白话搜自己电脑里的文件。不用记得文件名，说个意思就行，
图片也能用文字描述搜出来。全部在本机运行，不上传云端。


压缩包里有什么
------------------------------------------------------------

  启动脚本
    setup.bat          首次安装：建运行环境、装依赖、下载模型
    search.bat         启动并打开搜索页      ← 平时就用这个
    start.bat          启动服务
    stop.bat           停止服务
    restart.bat        重启（改过 config.ini 之后用）
    status.bat         查看运行状态

  辅助脚本
    selftest.bat       自检，确认各项功能正常
    backup.bat         备份索引
    restore.bat        恢复索引（图形化，点着选文件就行）
    fix-permissions.bat 备用：万一「定位文件」不可用，用它修复
    安装PDF原版式.bat   备用：让 PDF 预览能把某一页渲染成图片看表格版式
                       （setup.bat 会自动装；装完还是看不到再双击它）

  配置与文档
    config.ini         配置文件（端口、设备、模态等）
    requirements.txt   依赖清单（setup.bat 会自动读取）
    README.md          完整功能与接口文档
    先读我-从这里开始.txt  本文件
    打包信息.txt        版本与打包时间

  程序本体
    app\\               源码与网页界面，保持原样即可，不用动
    app\\run_server.bat 服务实际入口（由 search.bat 调用）

  【注意】包里没有模型和运行环境。它们由 setup.bat 首次运行时
          自动下载生成（约 7 GB），所以这个包只有几百 KB。


怎么用
------------------------------------------------------------

  第 1 步  安装 Python 3.12
           下载：https://www.python.org/downloads/windows/
           安装第一屏【务必勾选】"Add python.exe to PATH"

  第 2 步  把整个文件夹解压到【纯英文路径】，例如 D:\\embeddinggemma-2
           然后双击 setup.bat
           自动建环境、装依赖、下模型，约 5 GB，第一次比较久。
           中途断了没关系，再双击一次会接着下。

  第 3 步  双击 search.bat
           浏览器自动打开搜索页 → 点「＋ 添加文件夹（可多选）」
           → 选一个索引模式 → 开始用


几点说明
------------------------------------------------------------

  * 所有数据都留在本机，不上传云端，断网也能搜索
  * 想备份时只需要 data\\index\\ 和 config.ini，其余都能重新生成
  * 不想要了：先双击 stop.bat，再删掉整个文件夹即可，不留系统垃圾
  * 遇到问题先双击 status.bat 看状态


详细部署与使用说明
------------------------------------------------------------

  请参考微信公众号「宇之语」的相关文章。
  （项目根目录的 README.md 里另有完整的功能与接口文档）

============================================================
"""


def version() -> str:
    text = (ROOT / "app" / "server.py").read_text(encoding="utf-8")
    match = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else "0.0.0"


def collect() -> list[tuple[Path, str]]:
    """返回 [(磁盘路径, 压缩包内路径), ...]"""
    items: list[tuple[Path, str]] = []
    for name in ROOT_FILES:
        path = ROOT / name
        if path.exists():
            items.append((path, name))
        else:
            print(f"  [警告] 根目录缺少 {name}")

    for tree in TREES:
        base = ROOT / tree
        if not base.exists():
            continue
        tree_skip = TREE_SKIP.get(tree, set())
        tree_suffix = TREE_SKIP_SUFFIX.get(tree, set())
        tree_prefix = TREE_SKIP_PREFIX.get(tree, ())
        for path in sorted(base.rglob("*")):
            if path.is_dir():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if any(part in tree_skip for part in path.parts):
                continue
            if path.name in TREE_SKIP_NAMES:
                continue
            if tree_prefix and path.name.startswith(tuple(tree_prefix)):
                continue
            if path.suffix.lower() in SKIP_SUFFIX or path.suffix.lower() in tree_suffix:
                continue
            items.append((path, str(path.relative_to(ROOT))))
    return items


def main() -> int:
    ver = version()
    short = ".".join(ver.split(".")[:2])          # 2.0.0 → 2.0，与已发布的 v1.0 命名保持一致
    DIST.mkdir(exist_ok=True)
    out = DIST / f"宇之语-灵犀-v{short}.zip"
    # 已经发布过的包**一律不动**：存在就直接停下，避免手滑覆盖掉存档
    if out.exists():
        print(f"  [停止] 已存在 {out.name} —— 按约定不改动已发布的包。")
        print("         要重新打一份，请先把它改名或移到别处。")
        return 1

    items = collect()
    total = sum(p.stat().st_size for p, _ in items)

    print("=" * 60)
    print(f"  打包 EmbeddingGemma 2 文件搜索 v{ver}")
    print("=" * 60)
    print(f"  源文件 {len(items)} 个，共 {total / 1e6:.2f} MB")
    print()

    # 所有内容都放进一个顶层文件夹，这样不管用什么工具解压，
    # 都只会多出一个文件夹，不会把五十多个文件散落一地
    top = f"宇之语-灵犀-v{short}"

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path, arcname in items:
            zf.write(path, f"{top}/{arcname}")
        # 打包时现生成一份「先读我」，UTF-8 BOM 让记事本也能正确显示
        zf.writestr(f"{top}/先读我-从这里开始.txt",
                    ("\ufeff" + READ_ME.format(version=ver)).encode("utf-8"))
        # 记一下打包时间
        zf.writestr(f"{top}/打包信息.txt",
                    ("\ufeff" + f"版本   : {ver}\n"
                                f"打包于 : {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                                f"文件数 : {len(items) + 2}\n"
                                f"说明   : 不含 venv / models / .cache，"
                                f"运行 setup.bat 会自动生成\n").encode("utf-8"))

    size = out.stat().st_size
    print(f"  已生成 : {out}")
    print(f"  压缩后 : {size / 1e6:.2f} MB（压缩率 {100 - size * 100 / max(total, 1):.0f}%）")
    print()
    print(f"  包内结构：{top}\\")
    with zipfile.ZipFile(out) as zf:
        names = sorted(zf.namelist())
        for name in names:
            short = name[len(top) + 1:]
            if short.count("/") == 0 or short.startswith("docs/"):
                print(f"    {short}")
        print(f"    ... 共 {len(names)} 个文件")
    print()
    print("  解压后只有一个文件夹，放哪都行（纯英文路径最好），双击 setup.bat 开始。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
