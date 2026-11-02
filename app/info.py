"""输出关键配置（key=value 每行一条），供批处理脚本读取。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402


def main() -> int:
    cfg = Config()
    check = cfg.host in ("0.0.0.0", "::", "::0")
    print(f"host={cfg.host}")
    print(f"check_host={'127.0.0.1' if check else cfg.host}")
    print(f"port={cfg.port}")
    print(f"open_browser={'yes' if cfg.open_browser else 'no'}")
    print(f"model_dir={cfg.model_dir}")
    print(f"model_repo={cfg.repo_id}")
    print(f"offline={'yes' if cfg.offline else 'no'}")
    print(f"proxy={cfg.proxy}")
    print(f"mirror={cfg.mirror}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
