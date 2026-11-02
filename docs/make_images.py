# -*- coding: utf-8 -*-
"""生成文档配图：架构图、流程图、界面示意图（全部程序绘制，不含任何真实数据）。"""
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent / "images"
OUT.mkdir(parents=True, exist_ok=True)

BG = "#0f1420"
PANEL = "#171e2e"
PANEL2 = "#1e2739"
LINE = "#2b3648"
FG = "#e7ecf5"
MUTED = "#93a1b8"
ACCENT = "#5b9dff"
OK = "#3ddc97"
WARN = "#ffb454"
BAD = "#ff8f8f"
PURPLE = "#a98bff"

FONT = r"C:\Windows\Fonts\msyh.ttc"
FONT_B = r"C:\Windows\Fonts\msyhbd.ttc"
S = 2


def font(size, bold=False):
    return ImageFont.truetype(FONT_B if bold else FONT, size * S)


class C:
    def __init__(self, w, h, bg=BG):
        self.w, self.h = w, h
        self.img = Image.new("RGB", (w * S, h * S), bg)
        self.d = ImageDraw.Draw(self.img)

    def rrect(self, x, y, w, h, r=10, fill=None, outline=None, width=1):
        self.d.rounded_rectangle(
            [x * S, y * S, (x + w) * S, (y + h) * S],
            radius=r * S, fill=fill, outline=outline,
            width=max(1, int(width * S)))

    def rect(self, x, y, w, h, fill=None, outline=None, width=1):
        self.d.rectangle([x * S, y * S, (x + w) * S, (y + h) * S],
                         fill=fill, outline=outline, width=max(1, int(width * S)))

    def ellipse(self, box, fill=None, outline=None, width=1):
        self.d.ellipse([v * S for v in box], fill=fill, outline=outline,
                       width=max(1, int(width * S)))

    def text(self, x, y, txt, size=14, fill=FG, bold=False, anchor="la"):
        self.d.text((x * S, y * S), txt, font=font(size, bold), fill=fill, anchor=anchor)

    def wrap(self, x, y, txt, size, fill, width, lh=1.6, bold=False):
        """按像素宽度自动换行（中英混排按字符估算）。"""
        f = font(size, bold)
        lines, cur = [], ""
        for ch in txt:
            if ch == "\n":
                lines.append(cur)
                cur = ""
                continue
            probe = cur + ch
            if self.d.textlength(probe, font=f) > width * S and cur:
                lines.append(cur)
                cur = ch
            else:
                cur = probe
        if cur:
            lines.append(cur)
        for i, line in enumerate(lines):
            self.text(x, y + i * size * lh, line, size, fill, bold)
        return y + len(lines) * size * lh

    def arrow(self, x1, y1, x2, y2, color=LINE, width=2, head=7):
        self.d.line([x1 * S, y1 * S, x2 * S, y2 * S], fill=color, width=max(1, int(width * S)))
        ang = math.atan2(y2 - y1, x2 - x1)
        pts = [(x2, y2),
               (x2 - head * math.cos(ang - 0.42), y2 - head * math.sin(ang - 0.42)),
               (x2 - head * math.cos(ang + 0.42), y2 - head * math.sin(ang + 0.42))]
        self.d.polygon([(p[0] * S, p[1] * S) for p in pts], fill=color)

    def save(self, name):
        path = OUT / name
        self.img.resize((self.w, self.h), Image.LANCZOS).save(path)
        print("  ", path.name, f"{self.w}x{self.h}")


# --------------------------------------------------------------- 1. 架构图
def architecture():
    c = C(1040, 620)
    c.text(40, 30, "宇之语 · 灵犀 · 整体架构", 22, FG, bold=True)
    c.text(40, 66, "全程本地运行，文件和向量都不出这台电脑", 13, MUTED)

    c.rrect(40, 100, 250, 470, 14, fill=PANEL, outline=LINE)
    c.text(58, 116, "① 你的文件夹", 15, ACCENT, bold=True)
    for i, (name, color) in enumerate([("文档 / 代码", MUTED), ("PDF / Office", MUTED),
                                       ("图片", OK), ("音频 / 视频", WARN)]):
        c.rrect(58, 150 + i * 46, 214, 36, 8, fill=PANEL2, outline=LINE)
        c.text(72, 160 + i * 46, name, 13, color)
    c.text(58, 350, "每个文件夹可以有自己的", 12, MUTED)
    c.text(58, 370, "索引模式与向量维度", 12, MUTED)
    c.rrect(58, 400, 214, 70, 10, fill="#122033", outline=ACCENT)
    c.text(72, 414, "安全阀", 12, ACCENT, bold=True)
    c.text(72, 434, "扫描读不到文件时", 11, MUTED)
    c.text(72, 450, "不会误删已有索引", 11, MUTED)

    c.arrow(296, 335, 356, 335, ACCENT)

    c.rrect(360, 100, 250, 470, 14, fill=PANEL, outline=LINE)
    c.text(378, 116, "② 内容抽取", 15, ACCENT, bold=True)
    rows = [("文本 / 代码", "直接读，自动切块"),
            ("PDF / Word / Excel / PPT", "纯 Python 解析，不装 Office"),
            ("图片", "Pillow → ffmpeg 两层解码"),
            ("音频 / 视频", "抽音轨转文本再编码")]
    y = 150
    for title, sub in rows:
        c.rrect(378, y, 214, 62, 8, fill=PANEL2, outline=LINE)
        c.text(390, y + 10, title, 12.5, FG, bold=True)
        c.text(390, y + 32, sub, 11, MUTED)
        y += 74
    c.rrect(378, 452, 214, 100, 8, fill=PANEL2, outline=LINE)
    c.text(390, 464, "失败就单独跳过", 12, WARN, bold=True)
    c.text(390, 486, "一个坏文件不会", 11, MUTED)
    c.text(390, 502, "连累同批其它文件，", 11, MUTED)
    c.text(390, 518, "并写明具体原因", 11, MUTED)

    c.arrow(616, 335, 676, 335, ACCENT)

    c.rrect(680, 100, 320, 470, 14, fill=PANEL, outline=LINE)
    c.text(698, 116, "③ EmbeddingGemma 2 模型", 15, ACCENT, bold=True)
    c.rrect(698, 148, 284, 74, 8, fill=PANEL2, outline=LINE)
    c.text(712, 160, "270M 文本主干", 13, FG, bold=True)
    c.text(712, 184, "768 维，可截断到 512/256/128", 11, MUTED)
    c.text(712, 202, "8192 token 超长上下文", 11, MUTED)
    for i, (name, color) in enumerate([("170M 视觉编码器", OK), ("300M 音频编码器", WARN)]):
        c.rrect(698, 236 + i * 62, 284, 52, 8, fill=PANEL2, outline=LINE)
        c.text(712, 248 + i * 62, name, 12.5, color, bold=True)
        c.text(712, 268 + i * 62, "按需加载，不用就不占显存", 11, MUTED)
    c.rrect(698, 368, 284, 76, 8, fill="#122033", outline=ACCENT)
    c.text(712, 380, "模态自动取并集", 12, ACCENT, bold=True)
    c.text(712, 402, "全局设「仅文本」但某文件夹", 11, MUTED)
    c.text(712, 418, "要图片 → 自动补上视觉编码器", 11, MUTED)
    c.text(698, 462, "bf16 精度 · CUDA / CPU 均可", 12, MUTED)
    c.rrect(698, 492, 284, 60, 8, fill=PANEL2, outline=LINE)
    c.text(712, 504, "索引库 SQLite", 12.5, FG, bold=True)
    c.text(712, 526, "文件夹配置 + 文件清单 + 向量", 11, MUTED)

    c.text(40, 590, "搜索界面在浏览器里，支持预览 / 定位文件 / 下载；"
                    "所有数据只用一个小数据库文件（几 MB）承载。", 12, MUTED)
    c.save("01-架构图.png")


# --------------------------------------------------------------- 2. 部署流程
def deploy_flow():
    c = C(1000, 640)
    c.text(40, 30, "新手部署流程（从零到能搜）", 22, FG, bold=True)
    c.text(40, 66, "全程不改系统设置、不写注册表，装完就是一个独立文件夹", 13, MUTED)

    steps = [
        ("1", "安装 Python 3.12", "官网下载安装包，务必勾选\n“Add python.exe to PATH”", ACCENT, 110),
        ("2", "把项目放到纯英文路径", "例如 D:\\embeddinggemma-2\n中文路径容易踩坑，别用桌面", ACCENT, 190),
        ("3", "双击 setup.bat", "自动创建隔离的 venv 环境、\n装依赖、下载模型（约 5 GB）", OK, 270),
        ("4", "双击 search.bat", "启动本地服务，浏览器自动打开\n首次加载模型约 10-60 秒", OK, 350),
    ]
    y = 110
    for num, title, sub, color, _ in steps:
        c.rrect(60, y, 560, 66, 10, fill=PANEL, outline=LINE)
        c.rrect(72, y + 12, 42, 42, 21, fill=color)
        c.text(93, y + 21, num, 18, "#0f1420", bold=True, anchor="ma")
        c.text(128, y + 12, title, 15, FG, bold=True)
        c.text(128, y + 36, sub.split("\n")[0], 11.5, MUTED)
        if "\n" in sub:
            c.text(128, y + 51, sub.split("\n")[1], 11.5, MUTED)
        y += 78

    c.rrect(60, 422, 560, 66, 10, fill="#122033", outline=ACCENT)
    c.rrect(72, 434, 42, 42, 21, fill=ACCENT)
    c.text(93, 443, "5", 18, "#0f1420", bold=True, anchor="ma")
    c.text(128, 434, "添加文件夹 → 建立索引 → 开始搜索", 15, FG, bold=True)
    c.text(128, 458, "可一次勾选多个文件夹，后台逐个扫描", 11.5, MUTED)

    c.rrect(60, 508, 560, 96, 10, fill=PANEL, outline=LINE)
    c.text(78, 522, "常用的几个脚本", 13, FG, bold=True)
    for i, (name, desc) in enumerate([("start.bat / search.bat", "启动服务"),
                                      ("stop.bat", "停止服务"),
                                      ("status.bat", "看运行状态与索引量"),
                                      ("backup.bat / restore.bat", "备份 / 图形化恢复")]):
        col = i // 2
        row = i % 2
        c.text(78 + col * 280, 550 + row * 22, name, 11.5, ACCENT)
        c.text(78 + col * 280 + 150, 550 + row * 22, desc, 11.5, MUTED)

    c.rrect(660, 110, 300, 200, 12, fill=PANEL, outline=LINE)
    c.text(680, 128, "环境要求", 14, FG, bold=True)
    reqs = ["Windows 10/11 64 位", "Python 3.12（3.10+ 也行）",
            "内存 8 GB 以上", "显存 6 GB 可跑全模态",
            "纯 CPU 也能跑，只是慢一些", "磁盘预留约 10 GB"]
    for i, r in enumerate(reqs):
        c.text(680, 156 + i * 25, "· " + r, 12, MUTED)

    c.rrect(660, 330, 300, 130, 12, fill=PANEL, outline=LINE)
    c.text(680, 348, "装完占多少空间", 14, FG, bold=True)
    for i, (k, v) in enumerate([("模型", "约 1.4 GB"), ("下载缓存", "约 2.2 GB"),
                                ("Python 依赖", "约 3.7 GB"), ("索引库", "几 MB")]):
        c.text(680, 376 + i * 21, k, 12, MUTED)
        c.text(940, 376 + i * 21, v, 12, FG, anchor="ra")

    c.rrect(660, 480, 300, 124, 12, fill="#2a1f12", outline=WARN)
    c.text(680, 498, "装之前注意", 14, WARN, bold=True)
    c.wrap(680, 524, "别放在需要管理员权限才能写的目录，例如系统盘里的程序目录，"
                     "否则模型和索引写不进去。", 11.5, MUTED, 264, 1.5)
    c.save("02-部署流程.png")


# --------------------------------------------------------------- 3. 使用流程
def usage_flow():
    c = C(1000, 520)
    c.text(40, 30, "日常使用流程", 22, FG, bold=True)
    c.text(40, 66, "一次建库，之后随时搜索；新增文件只要再点一次「增量更新」", 13, MUTED)

    boxes = [
        ("添加文件夹", "可多选，一次勾一批", ACCENT, 40),
        ("选索引模式", "全模态 / 文字+图片 /\n仅文字 / 只索引文件名", ACCENT, 230),
        ("后台逐个扫描", "界面显示「● 更新中」和进度\n随时可以停", OK, 420),
        ("自然语言搜索", "“去年的报销发票”\n“蓝色圆形的产品图”", PURPLE, 610),
        ("预览 / 定位 / 下载", "图片音视频直接看\n一键在资源管理器选中", WARN, 800),
    ]
    for title, sub, color, x in boxes:
        c.rrect(x, 120, 172, 150, 12, fill=PANEL, outline=LINE)
        c.rect(x, 120, 172, 4, fill=color)
        c.text(x + 14, 138, title, 14, color, bold=True)
        c.wrap(x + 14, 168, sub, 11.5, MUTED, 146, 1.6)
        if x < 800:
            c.arrow(x + 176, 195, x + 224, 195, LINE, 2, 8)

    c.rrect(40, 300, 920, 90, 12, fill=PANEL, outline=LINE)
    c.text(60, 316, "搜索时是「一起搜」的", 14, FG, bold=True)
    c.wrap(60, 344, "不同文件夹可以有不同的索引模式、不同的向量维度，搜索是统一入口，"
                    "结果混在一起按相似度排序；每条结果都标着它来自哪种模式的文件夹。",
           12, MUTED, 880, 1.6)

    c.rrect(40, 410, 440, 84, 12, fill=PANEL, outline=LINE)
    c.text(60, 426, "增量更新 vs 全部重建", 13, ACCENT, bold=True)
    c.wrap(60, 452, "平时用「增量更新」：只处理新增和改动的文件，很快。\n"
                    "改了向量维度或想彻底重扫时，才用「全部重建」。", 11.5, MUTED, 400, 1.5)

    c.rrect(500, 410, 460, 84, 12, fill=PANEL, outline=LINE)
    c.text(520, 426, "安全阀", 13, OK, bold=True)
    c.wrap(520, 452, "网络共享掉线导致「一个文件都没读到」时，程序会跳过删除并提示，"
                     "绝不会把已有索引误清空。", 11.5, MUTED, 420, 1.5)
    c.save("03-使用流程.png")


# --------------------------------------------------------------- 4. 搜索界面示意
def ui_search():
    c = C(980, 620, "#0b101a")
    c.rrect(0, 0, 980, 56, 0, fill=PANEL)
    c.text(24, 18, "宇之语 · 灵犀", 16, FG, bold=True)
    c.ellipse([252, 22, 266, 36], outline=ACCENT, width=2)
    c.d.line([266 * S, 36 * S, 273 * S, 43 * S], fill=ACCENT, width=4)
    c.rrect(180, 15, 130, 26, 13, fill="#12351f")
    c.text(196, 21, "运行中 · cuda / bf16", 11, OK)
    c.text(920, 22, "API 文档", 11, ACCENT, anchor="ra")

    c.rrect(20, 72, 940, 96, 12, fill=PANEL, outline=LINE)
    c.rrect(36, 88, 700, 40, 8, fill=PANEL2, outline=ACCENT)
    c.text(50, 100, "去年的差旅报销发票", 13, FG)
    c.rrect(748, 88, 92, 40, 8, fill=ACCENT)
    c.text(794, 100, "搜索", 13, "#0b101a", bold=True, anchor="ma")
    c.text(852, 100, "30 条 ▼", 12, MUTED)
    c.text(36, 142, "命中 12 个文件，用时 0.18 秒", 11.5, MUTED)
    c.text(700, 142, "全部类型 ▼      全部文件夹 ▼", 11.5, MUTED)

    results = [
        ("0.823", "差旅报销流程说明.docx", r"资料\财务\差旅报销流程说明.docx",
         "差旅费用报销规定：返回后十个工作日内提交发票，单笔超过三千元需要附审批单……",
         "仅文字"),
        ("0.791", "2024年3月发票汇总.xlsx", r"资料\财务\2024年3月发票汇总.xlsx",
         "差旅费 交通费 住宿费 报销金额 审批人 ……", "仅文字"),
        ("0.744", "蓝色名片设计稿.png", r"素材\设计\蓝色名片设计稿.png",
         "", "文字 + 图片"),
    ]
    y = 176
    for score, name, path, snippet, mode in results:
        c.rrect(20, y, 940, 118, 12, fill=PANEL, outline=LINE)
        c.rrect(36, y + 16, 52, 22, 11, fill="#1d2f4d")
        c.text(62, y + 20, score, 11, ACCENT, anchor="ma")
        c.text(100, y + 18, name, 13.5, FG, bold=True)
        c.rrect(830, y + 16, 112, 22, 11, fill="#1d2f4d")
        c.text(886, y + 20, mode, 10.5, ACCENT, anchor="ma")
        c.text(100, y + 42, path, 11, MUTED)
        if snippet:
            c.rrect(100, y + 62, 700, 34, 6, fill="#0b101a")
            c.text(110, y + 70, snippet[:52], 11, "#c3cee0")
        for i, label in enumerate(["预览", "定位文件", "用默认程序打开", "复制路径"]):
            c.rrect(100 + i * 104, y + 88, 96, 22, 6, fill=PANEL2, outline=LINE)
            c.text(148 + i * 104, y + 92, label, 10.5, FG, anchor="ma")
        y += 132

    c.rrect(20, 578, 940, 30, 8, fill="#122033", outline=ACCENT)
    c.text(34, 585, "点文件名或「预览」直接看内容；点「定位文件」在资源管理器中选中它（窗口自动置前）",
           11.5, ACCENT)
    c.save("04-界面-搜索.png")


# --------------------------------------------------------------- 5. 预览示意
def ui_preview():
    c = C(900, 560, "#0b101a")
    c.rrect(120, 30, 660, 500, 14, fill=PANEL, outline=LINE)
    c.rect(120, 30, 660, 46, fill=PANEL2)
    c.text(140, 44, "蓝色圆形图片.png", 14, FG, bold=True)
    c.text(380, 46, r"素材\设计\蓝色圆形图片.png", 10.5, MUTED)
    c.rrect(720, 42, 44, 24, 6, fill=PANEL, outline=LINE)
    c.text(742, 46, "关闭", 10.5, MUTED, anchor="ma")

    c.rrect(140, 92, 620, 320, 10, fill="#0b101a")
    c.rrect(300, 150, 300, 200, 8, fill="#3a6fb8")
    c.ellipse([390, 210, 510, 330], fill="#f0a04b")
    c.text(450, 268, "图片预览区域", 12, "#0b101a", anchor="ma")

    c.rrect(140, 424, 620, 40, 8, fill=PANEL2, outline=LINE)
    c.text(154, 436, "图片 · 729.5 KB · 显示为 1600×1067", 11, MUTED)
    c.text(154, 436, "", 11, MUTED)

    for i, label in enumerate(["下载到本机", "在资源管理器中定位", "用默认程序打开", "复制路径"]):
        c.rrect(140 + i * 155, 476, 144, 34, 8, fill=PANEL2, outline=LINE)
        c.text(212 + i * 155, 486, label, 11.5, FG, anchor="ma")

    c.text(140, 540, "预览由服务端解码后输出，所以 HEIC / TIFF 也能看；"
                     "确实不是图片的文件会写明原因，而不是留个空白框。", 10.5, MUTED)
    c.save("05-界面-预览.png")


# --------------------------------------------------------------- 6. 每文件夹模式
def per_folder():
    c = C(1000, 470)
    c.text(40, 30, "每个文件夹可以有自己的索引模式", 22, FG, bold=True)
    c.text(40, 66, "搜索是统一入口，不同模式、不同向量维度的结果混在一起排序", 13, MUTED)

    folders = [
        ("资料 / 文档文件夹", "仅文字", "128 维", "最快，用不上图片音频",
         "199 个文件", OK, 40),
        ("产品图 / 截图文件夹", "文字 + 图片", "256 维", "可以用「蓝色圆形」这类描述找图",
         "612 个文件", ACCENT, 350),
        ("安装包 / 备份文件夹", "只索引文件名", "512 维", "不读内容，秒级完成",
         "528 个文件", WARN, 660),
    ]
    for title, mode, dim, note, count, color, x in folders:
        c.rrect(x, 110, 300, 216, 12, fill=PANEL, outline=LINE)
        c.rect(x, 110, 300, 4, fill=color)
        c.text(x + 18, 128, title, 13.5, FG, bold=True)
        c.rrect(x + 18, 156, 130, 28, 14, fill="#1d2f4d")
        c.text(x + 83, 163, mode, 12, color, anchor="ma")
        c.text(x + 158, 163, dim, 12, MUTED)
        c.wrap(x + 18, 198, note, 11.5, MUTED, 264, 1.6)
        c.text(x + 18, 244, count, 11.5, MUTED)
        c.rrect(x + 18, 268, 264, 40, 8, fill=PANEL2, outline=LINE)
        c.text(x + 30, 280, "√ 已建立索引", 11.5, OK)
        c.arrow(x + 150, 330, x + 150, 366, LINE, 2, 7)

    c.rrect(340, 372, 320, 56, 12, fill="#122033", outline=ACCENT)
    c.text(500, 386, "统一搜索，一起出结果", 15, ACCENT, bold=True, anchor="ma")
    c.text(500, 408, "按相似度排序，自动跨模式跨维度", 11, MUTED, anchor="ma")

    c.text(40, 444, "向量维度不同也能混着搜：查询向量按各文件夹的维度切片比对"
                    "（Matryoshka 截断的定义），不需要统一维度、也不用重建索引。", 11, MUTED)
    c.save("06-每文件夹模式.png")


if __name__ == "__main__":
    print("生成配图：")
    c = C(1000, 470)
    architecture()
    deploy_flow()
    usage_flow()
    ui_search()
    ui_preview()
    per_folder()
    print("完成")
