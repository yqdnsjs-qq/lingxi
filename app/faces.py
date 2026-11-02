"""人脸识别模块（可选功能）。

设计要点 —— 为什么不装进主项目的 venv：
    这个项目曾经因为往 venv 里装人脸依赖，导致 pip 把 torch / numpy /
    pillow 一起降级，把正在跑的服务搞挂了。所以这里改成：

        依赖装在 <项目>\\face-lab\\libs        （pip --target，与 venv 隔离）
        权重放在 <项目>\\face-lab\\models      （TORCH_HOME 指过去）
        本模块运行时把前者加进 sys.path

    好处：venv 一个字节都不动；不想要人脸功能时删掉 face-lab 即可；
    打包分发时也不必带上几百 MB 的模型。

对外接口：
    available()                 人脸功能是否可用（依赖+权重齐全）
    FaceEngine                  延迟加载的模型封装
        .extract(pil_image)     → [Face(...)]
        .embed(pil_image)       → np.ndarray | None（最大那张脸的特征）
"""
from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
FACE_DIR = ROOT / "face-lab"
LIBS_DIR = FACE_DIR / "libs"
MODELS_DIR = FACE_DIR / "models"

# 识别阈值（在 3000+ 张真实人脸上校准过）：
#   同一个人的相似度中位数约 0.88，不同人约 0.32
#   0.55 用于「归到已知某人」，0.50 用于「以图搜人」的宽召回
ASSIGN_THRESHOLD = 0.55
SEARCH_THRESHOLD = 0.45


@dataclass
class Face:
    box: tuple[float, float, float, float]
    prob: float
    vector: np.ndarray          # 已 L2 归一化的 512 维特征
    crop: np.ndarray            # uint8 RGB 图，已按典型方向摆正


def _ensure_paths() -> None:
    libs = str(LIBS_DIR)
    if LIBS_DIR.is_dir() and libs not in sys.path:
        sys.path.insert(0, libs)
    if MODELS_DIR.is_dir():
        # facenet-pytorch 用的是它自己的 get_torch_home()，认 TORCH_HOME
        os.environ.setdefault("TORCH_HOME", str(MODELS_DIR))


def weights_ready() -> bool:
    ckpt = MODELS_DIR / "checkpoints" / "20180402-114759-vggface2.pt"
    return ckpt.exists()


def available() -> tuple[bool, str]:
    """返回 (是否可用, 原因说明)。"""
    if not LIBS_DIR.is_dir():
        return False, f"缺少依赖目录 {LIBS_DIR}（人脸功能未安装）"
    _ensure_paths()
    try:
        import torch  # noqa: F401
        from facenet_pytorch import MTCNN, InceptionResnetV1  # noqa: F401
    except Exception as exc:                     # pragma: no cover
        return False, f"人脸依赖导入失败：{exc}"
    if not weights_ready():
        return False, f"缺少人脸权重 {MODELS_DIR / 'checkpoints'}"
    return True, "ok"


class FaceEngine:
    """延迟加载：第一次用到才载模型，避免拖慢服务启动。"""

    def __init__(self, device: str | None = None):
        self._lock = threading.Lock()
        self._mtcnn = None
        self._resnet = None
        self._device = device
        self._torch = None

    def _load(self) -> None:
        if self._mtcnn is not None:
            return
        with self._lock:
            if self._mtcnn is not None:
                return
            _ensure_paths()
            import torch
            from facenet_pytorch import InceptionResnetV1, MTCNN

            device = self._device
            if device is None:
                device = "cuda" if torch.cuda.is_available() else "cpu"
            self._torch = torch
            self._device = device
            self._mtcnn = MTCNN(keep_all=True, device=device, min_face_size=60,
                                thresholds=[0.6, 0.7, 0.7], post_process=True,
                                factor=0.85)
            self._resnet = self._make_resnet()

    def _make_resnet(self):
        """自己把权重读进来，不用 pretrained='vggface2'。

        为什么：facenet-pytorch 内部用**它自己的** get_torch_home()，会认
        run_server.bat 设的 TORCH_HOME（指向 .cache\\torch）。在那里找不到
        权重时它就去 GitHub 下载 —— 而这台网络到不了 GitHub，结果是
        「重新聚类」「以图搜人」全部 500 报错。

        自己加载还有个好处：完全不去动进程的 TORCH_HOME。
        """
        from facenet_pytorch import InceptionResnetV1

        ckpt = MODELS_DIR / "checkpoints" / "20180402-114759-vggface2.pt"
        if not ckpt.exists():
            raise FileNotFoundError(f"缺少人脸权重：{ckpt}")
        model = InceptionResnetV1(pretrained=None, classify=False,
                                  device=self._device)
        state = self._torch.load(str(ckpt), map_location="cpu", weights_only=False)
        if isinstance(state, dict) and "state_dict" in state:
            state = state["state_dict"]
        try:
            model.load_state_dict(state)
        except RuntimeError:
            model.load_state_dict(state, strict=False)
        return model.eval()

    @property
    def device(self) -> str:
        return self._device or "?"

    # ---------- 核心 ----------
    def extract(self, image, min_prob: float = 0.90,
                min_face: int = 60, max_side: int = 1280) -> list[Face]:
        """从 PIL 图里检出所有人脸并提特征。"""
        self._load()
        from PIL import Image, ImageOps

        img = ImageOps.exif_transpose(image).convert("RGB")   # 按 EXIF 摆正
        w, h = img.size
        longest = max(w, h)
        scale = 1.0
        if longest > max_side:
            scale = max_side / longest
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                             Image.BILINEAR)

        boxes, probs = self._mtcnn.detect(img)
        if boxes is None or len(boxes) == 0:
            return []

        keep, keep_p = [], []
        for box, prob in zip(boxes, probs):
            if prob is None or prob < min_prob:
                continue
            if min(float(box[2] - box[0]), float(box[3] - box[1])) < min_face:
                continue
            keep.append(box)
            keep_p.append(float(prob))
        if not keep:
            return []

        crops = self._mtcnn.extract(img, np.array(keep), save_path=None)
        if crops is None or len(crops) == 0:
            return []
        with self._torch.no_grad():
            vecs = self._resnet(crops.to(self._device)).cpu().numpy()

        out: list[Face] = []
        for k, box in enumerate(keep):
            v = vecs[k].astype(np.float32)
            n = float(np.linalg.norm(v)) + 1e-9
            rgb = (crops[k].permute(1, 2, 0).numpy() * 127.5 + 127.5).clip(0, 255)
            # 框换算回「原图坐标」，这样以后能直接从原文件裁出头像
            if scale != 1.0:
                box = [float(x) / scale for x in box]
            out.append(Face(box=tuple(float(x) for x in box), prob=keep_p[k],
                            vector=v / n, crop=rgb.astype(np.uint8)))
        return out

    def embed(self, image) -> np.ndarray | None:
        """只要一张参考照片里最大那张脸的特征 —— 用于「以图搜人」。"""
        faces = self.extract(image)
        if not faces:
            return None
        faces.sort(key=lambda f: -((f.box[2] - f.box[0]) * (f.box[3] - f.box[1])))
        return faces[0].vector


_ENGINE: FaceEngine | None = None


def engine() -> FaceEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = FaceEngine()
    return _ENGINE
