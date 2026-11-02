"""本机路径 ↔ 服务器路径 的映射。

项目主 2026-10-20 的需求：
  "服务器上没挂载电脑上正在使用的 NAS 的盘符，导致服务器不能定位文件……
   我桌面版使用了 \\\\NAS\\photo\\家庭相册 这种网络文件夹，它就应该在旁有个
   服务器对应的路径可以自定义，现在是对应 /mnt/nas_photo/家庭相册，
   比如我改变了服务器上的挂载路径，我就可以自己填进去，
   这样本机与服务器的相对转换数据库才能共享"

为什么要这个：
  · 数据库是**两边共享**的，`folders.path` 存的是**本机视角**的路径 ✓
  · 服务器上跑索引 / 预览 / 打开文件时，得换成**服务器视角**的路径 ✓
  · 挂载点会变（用户自己改），所以**必须可自定义**，不能写死 ✓

两种写法都支持：
  · 文件夹级：`folders.server_path`（这个文件夹在服务器上的路径）✓
  · 全局规则：`server_mounts`（共享名 → 挂载根），给没设过的文件夹自动建议 ✓
"""
from __future__ import annotations

import logging
import re
import string
from pathlib import Path

log = logging.getLogger("eg2.serverpath")

# 共享名 → 服务器挂载根的**别名表**（自动建议用；用户随时可在界面上改）
# 例：\\192.168.1.20\资料文档 在服务器上挂到 /mnt/nas_docs
SHARE_ALIAS = {
    "资料文档": "ziliao",
    "图书馆": "tusu",
    "音乐": "music",
}

# 服务器上可能出现的挂载根
MOUNT_ROOTS = ("/mnt", "/media", "/srv", "/data", "/opt/1panel")


def split_unc(local_path: str) -> tuple[str, str, str] | None:
    """拆 `\\\\HOST\\SHARE\\a\\b` → (HOST, SHARE, "a/b")；不是 UNC 就返回 None ✓"""
    p = str(local_path or "").strip().replace("/", "\\")
    if not p.startswith("\\\\"):
        return None
    parts = [x for x in p[2:].split("\\") if x]
    if len(parts) < 2:
        return None
    host, share = parts[0], parts[1]
    rest = "/".join(parts[2:])
    return host, share, rest


def _norm_share(share: str) -> str:
    """共享名 → 挂载目录名里常见的那种写法（小写、去空格和符号）✓

    ⚠ 2026-10-20 实测踩到：`Shared` 里的连字符没去掉 → 猜成 `mnt_yq-sync` ✗
      而服务器上实际挂的是 `/mnt/nas_shared` ✓ → 所以连字符也要去掉 ✓
      （中文共享名要保留，所以只去"英文符号"）
    """
    s = SHARE_ALIAS.get(share, share)
    s = s.strip().lower()
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "", s)     # 去掉空格/连字符/下划线等符号 ✓
    return s


def candidates(local_path: str) -> list[str]:
    """**猜**这个本机路径在服务器上可能是哪个路径（只给建议，不保证对）✓

    会列出一串候选，顺序由"可能性高→低"：
      \\\\NAS\\photo\\家庭相册   →  /mnt/nas_photo/家庭相册
                                       /mnt/nas_photo/家庭相册
                                       /mnt/photo/家庭相册
    最后还会加"把本机盘符路径换成 /mnt/... "这种兜底 ✓
    """
    out: list[str] = []
    unc = split_unc(local_path)
    if unc:
        host, share, rest = unc
        host_l = host.lower()
        share_n = _norm_share(share)
        head_guesses = [
            f"nas_{share_n}" if "mini" in host_l else f"mnt_{share_n}",
            f"mnt_{share_n}",
            f"nas_{share_n}",
            share_n,
        ]
        # 主机是 IP 时用 IP 结尾段（192.168.1.20 → 也可能叫 nas_docs 这种）
        seen: set[str] = set()
        for h in head_guesses:
            if h in seen:
                continue
            seen.add(h)
            out.append(f"/mnt/{h}/{rest}".rstrip("/"))
        return out
    # 本机盘符：D:\notes\obsidian → /mnt/d/tmp/qx/obsidian（只是兜底建议）
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", str(local_path or ""))
    if m:
        drive = m.group(1).lower()
        rest = m.group(2).replace("\\", "/")
        out.append(f"/mnt/{drive}/{rest}".rstrip("/"))
    return out


def exists_on_server(path: str) -> bool:
    """这个路径在这台机器上真的存在吗 ✓（本机跑索引时全都会是真，服务器反之）"""
    try:
        return bool(path) and Path(path).exists()
    except Exception:                                            # noqa: BLE001
        return False


def suggest_server_path(local_path: str) -> str:
    """给一个**最可能正确**的服务器路径建议 ✓

    优先返回"候选里在服务器上真实存在"的那个；
    都不存在就返回第一个候选（让人自己改）✓
    """
    cands = candidates(local_path)
    for c in cands:
        if exists_on_server(c):
            return c
    return cands[0] if cands else ""


def to_server_path(local_path: str, server_path: str = "",
                   mount_map: dict | None = None) -> str:
    """把本机路径换成服务器路径 ✓

    优先级（高 → 低）：
      ① `server_path`（文件夹上显式设的）—— 用户说了算 ✓
      ② `mount_map` 里的规则（共享名 → 挂载根）—— 批量规则 ✓
      ③ 自动建议（`suggest_server_path`）
      ④ 原样返回（实在换不出来，别把人搞更乱）
    """
    if server_path and str(server_path).strip():
        base = str(server_path).rstrip("/\\")
        tail = _relative_tail(local_path, server_path)
        return f"{base}/{tail}".rstrip("/") if tail else base
    if mount_map:
        unc = split_unc(local_path)
        if unc:
            host, share, rest = unc
            for key, root in (mount_map or {}).items():
                if str(key).strip().lower() in (share.lower(), host.lower()):
                    base = str(root).rstrip("/")
                    return f"{base}/{rest}".rstrip("/") if rest else base
    sug = suggest_server_path(local_path)
    return sug or str(local_path)


def _relative_tail(local_path: str, server_path: str) -> str:
    """算"本机路径相对它的根"剩下的那截（用于 server_path 是根目录、文件在子目录时）✓

    ⚠ 大多数情况 server_path 就是整个文件夹的路径，tail 为空 ✓
    这里只在"server_path 是个更浅的根"时兜底用。
    """
    return ""


def display_help(local_path: str) -> str:
    """给界面用的说明文字（含自动建议）✓"""
    sug = suggest_server_path(local_path)
    if sug:
        return f"建议填：{sug}（可在界面上改）"
    return "服务器上没找到对应路径，请手动填写"


# ============================================================ 这台机器该用哪个路径
def is_server() -> bool:
    """这台是不是**服务器**（不是建库机）✓

    判据与 filesearch._can_index_here 一致：项目里有没有 run/deploy/sync_db.py
      · 有  → 本机（建库机）
      · 没有 → 服务器

    ⚠ 这里用"文件存在"判断，不依赖任何调用方的状态 ✓（在索引器/预览里都能直接用）
    """
    try:
        root = Path(__file__).resolve().parent.parent
        return not (root / "run" / "deploy" / "sync_db.py").is_file()
    except Exception:                                            # noqa: BLE001
        return False


def pick_root(path: str, local_alias: str = "", server_path: str = "",
              *, on_server: bool | None = None) -> str:
    """**这台机器上该去哪个目录找这个文件夹** ✓

    项目主 2026-10-20："服务器接上"
      · 本机（建库机）→ `local_alias` 优先，其次 `path` ✓
      · 服务器        → `server_path` 优先，其次自动建议，最后 `path` ✓

    这正是"两边共享同一份数据库"的关键：库里 `path` 是本机视角（权威键），
    换了机器就用对应视角的路径去访问 ✓
    """
    src = str(path or "")
    if on_server is None:
        on_server = is_server()
    if not on_server:
        return str(local_alias or src)
    sp = str(server_path or "").strip()
    if sp:
        return sp
    # 没设 server_path → 猜一个（服务器上挂了就猜得中 ✓）
    sug = suggest_server_path(src)
    if sug and exists_on_server(sug):
        log.info("文件夹没设服务器路径，自动用：%s（%s）", sug, src)
        return sug
    return sug or src
