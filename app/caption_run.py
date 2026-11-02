"""插图打标执行器 —— 把 caption_queue 里的待办真正跑掉。

项目主 2026-10-19 的用法：
  · 白天索引 → 只**登记**（不调模型、不占显存、不花 API）✓
  · 夜里点「全力执行」→ 由这里把队列跑完 ✓

每条待办的处理链：
  ① 从 PDF 里把这张图抠出来（fitz.extract_image，用 xref 精确定位）✓
  ② 选模型（按文件夹的 caption_model）：
       remote → vlm_client.caption_one      **不进显存锁**（不占本机显存，可并行）✓
       local  → chat_engine 的视觉模型       **进 gpu_slot 锁**（排队，不和 OCR 撞）✓
  ③ 把描述**编码**成向量（用该文件夹的维度 ✓ 同一个文件夹必须同维度）
  ④ 存成 caption 片段，文案带头部「[第 N 页 插图]」
     → 检索命中时能直接告诉用户"第 3 页那张图" ✓
  ⑤ 队列项标 done / failed（失败累加 tries，下次会重排）

⚠ 铁律：
  · 打标失败**不影响**别的东西；一张图失败只记它自己 ✓
  · 到 deadline 就**优雅停下**（剩下的留 pending，下次接着跑）✓
  · 用显存的路径一定走 gpu_slot ✓；调 API 的路径**绝不**进锁（否则白等）✓
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("eg2.captionrun")

# ============================================================ 运行状态（界面可见）
# ★ 2026-10-20 项目主："现在项目在图片打标，我在项目界面也看不到进程啊，这个我怎么监控？
#   后台在干嘛也不知道"
#   以前 run_queue 是"一个函数闷头跑到底"——外面（界面）完全看不到它在第几条、
#   还剩多少、正在处理哪份文件 ✗
#   这里放一份**进程内可见的运行状态**，接口读它就能画进度条 ✓
#   暂停/停止用标志位，循环里自己检查 → 不杀线程、不留半截数据、随时能接着跑 ✓
_RUN: dict = {
    "running": False,
    "paused": False,
    "stopping": False,
    "source": "",          # manual（界面点开始）/ night（「全力执行」）
    "started": 0.0,
    "finished": 0.0,
    "total": 0,            # 这一轮打算跑多少条
    "picked": 0,           # 已处理到第几条
    "done": 0,
    "failed": 0,
    "skipped": 0,
    "current": "",
    "current_page": 0,
    "note": "",
    "last_error": "",
    "model": "",
}
_RUN_LOCK = threading.RLock()


def _run_update(**kw) -> None:                                   # noqa: ANN003
    with _RUN_LOCK:
        _RUN.update(kw)


def run_state() -> dict:
    """给界面看的进度（含速度/预计剩余）✓"""
    with _RUN_LOCK:
        st = dict(_RUN)
    st["queue"] = st.pop("_queue", None)
    started = float(st.get("started") or 0)
    now = time.time()
    if st.get("running") and started:
        spent = max(0.0, now - started)
        st["elapsed"] = round(spent, 1)
        done = int(st.get("done") or 0) + int(st.get("failed") or 0) \
            + int(st.get("skipped") or 0)
        if done > 0 and spent > 3:
            st["per_item"] = round(spent / done, 1)
            left = int(st.get("total") or 0) - done
            st["eta_seconds"] = int(max(0, left) * spent / done) if left > 0 else 0
    elif st.get("finished"):
        st["elapsed"] = round(max(0.0, float(st["finished"]) - started), 1)
    return st


def pause(reason: str = "") -> dict:
    """暂停（当前这条做完就停在原地，随时能继续）✓"""
    with _RUN_LOCK:
        if not _RUN["running"]:
            return {"ok": False, "message": "现在没有打标任务在跑"}
        _RUN["paused"] = True
        _RUN["note"] = reason or "已暂停（点「继续」接着跑）"
    log.info("插图打标被暂停")
    return {"ok": True, "state": run_state()}


def resume() -> dict:
    with _RUN_LOCK:
        if not _RUN["running"]:
            return {"ok": False, "message": "现在没有打标任务在跑"}
        _RUN["paused"] = False
        _RUN["note"] = "继续中…"
    log.info("插图打标继续")
    return {"ok": True, "state": run_state()}


def stop(reason: str = "") -> dict:
    """停下（剩下的留在队列里，下次接着跑）✓"""
    with _RUN_LOCK:
        if not _RUN["running"]:
            return {"ok": False, "message": "现在没有打标任务在跑"}
        _RUN["stopping"] = True
        _RUN["paused"] = False          # 暂停中也能直接停
        _RUN["note"] = reason or "正在停止…"
    log.info("插图打标收到停止指令")
    return {"ok": True, "state": run_state()}


def is_running() -> bool:
    with _RUN_LOCK:
        return bool(_RUN["running"])


def _gate() -> str:
    """循环闸门：暂停就原地等；要停止就返回 'stop'；正常返回 '' ✓"""
    while True:
        with _RUN_LOCK:
            if _RUN["stopping"]:
                return "stop"
            if not _RUN["paused"]:
                return ""
        time.sleep(0.4)


# 插图打标专用提示词
# ⚠ 2026-10-19 实测踩到：写得太像"任务说明"时，模型会把**要求本身**也复述出来 ✗
#   （入库成了「我们需要回答用户。用户要求：这是一份 PDF 文档里的插图…」）
#   → 所以明确禁止复述，并要求"直接给描述"✓
# ⚠ 2026-10-20 又踩到一次（同一份清单式提示词，换 deepseek-v4.1-flash）：
#   入库成了「[第 5 页 插图] 要求：看图，用中文描述它。直接输出描述本身。…」
#   —— 禁止复述的那句话**本身也被复述**了 ✗，而且兜底清洗切不掉 ✗
#   → 提示词改成**一句问句**（问句比"格式指令清单"更不像待复述的文本）+
#     入库前用 _strip_echo() 按提示词特征短语硬切 ✓
PDF_IMG_PROMPT = (
    "这张图里画的是什么？请用一两句话说明它是什么类型的图、主要内容，"
    "并照抄图上出现的文字。"
)
# 旧版（清单式）提示词——**只留作对照/回退**，别再用 ✓
LEGACY_PDF_IMG_PROMPT = (
    "看图，用中文描述它。\n"
    "【直接输出描述本身】不要重复这句话，不要出现"
    "「用户要求」「我们需要回答」「这是一份 PDF」这类过程语言。\n"
    "描述三点：\n"
    "1) 是什么图（示意图 / 界面截图 / 实物照片 / 表格 / 流程图）\n"
    "2) 画了什么内容\n"
    "3) 图上的标注文字（照抄关键词）\n"
    "只写看得见的，不猜。100 字以内。"
)

# ★ 提示词里"最像提示词"的特征短语 —— 用来识别并切掉模型的复述 ✓
#   选长一点的短语，避免误伤正常描述（比如"图上标注文字"不会出现在正常描述里）
# ★ 2026-10-20：还有一类脏东西不是"复述提示词"，而是**整段思维链** ✗
#   （deepseek-v4.1-flash 不关思考时就是这样：1.分析用户请求 → 2.分析图片
#    → 3.构建答案 → 4.优化语言，最后还截断在半截）
#   这种**切不干净**（每个步骤后面都有正文，切掉哪段都丢东西）→
#   识别到就整条作废、标失败重试，**绝不上库** ✓
_THINK_MARKS = ("分析用户请求", "分析用户需求", "分析图片", "分析图像",
                "观察图片", "观察图像", "检查图片", "检查图像", "构建答案",
                "组织回答", "优化语言", "让我重新", "让我仔细", "草稿：")

_PROMPT_MARKS = (
    "用中文描述它", "直接输出描述本身", "不要重复这句话", "不要出现",
    "用户要求", "我们需要回答", "我们需要", "这是一份 PDF", "PDF 文档里的插图",
    "图上标注文字", "照抄关键词", "只写看得见的", "100 字以内",
    "看图，", "描述三点", "是什么图", "画了什么内容",
)


def echoes_prompt(text: str) -> bool:
    """这段文字里**还带着提示词**吗（用来判断清洗干不干净 / 该不该重试）✓"""
    body = str(text or "")
    return any(mark in body for mark in _PROMPT_MARKS)


def is_think_dump(text: str) -> bool:
    """这段文字是**思维链草稿**吗（是 → 整条作废，别切）✓"""
    body = str(text or "")
    return any(mark in body for mark in _THINK_MARKS)


# ★ 推理型模型：不关掉"思考"就会把整段草稿写进正文（2026-10-20 实测）✓
#   deepseek-v4.1-flash：2086 字草稿（1.分析用户请求 → 2.分析图片 → 3.构建答案
#   → 4.优化语言），800 tokens 还没写完就断了，**答案压根没出来** ✗
#   加 `reasoning_effort=none` 后 516 字、13.5 秒，直接给答案 ✓
#   （实测 `enable_thinking=false` 和 `chat_template_kwargs` 都**无效** ✗）
#   只对**认识**的推理模型加，别乱塞到别的网关上（严格校验的网关会 400）✓
_REASONING_MODELS = ("deepseek-v4", "deepseek-r", "o1", "o3", "o4")


def caption_extra_body(model: str) -> dict:
    """按模型名决定要不要在请求体里关掉思考 ✓（见 _REASONING_MODELS 的说明）"""
    name = str(model or "").lower()
    if any(hint in name for hint in _REASONING_MODELS):
        return {"reasoning_effort": "none"}
    return {}


def _strip_echo(raw: str) -> str:
    """把模型复述的提示词整段切掉，只留真正的描述 ✓

    实测形态（2026-10-20，队列 #13）：
      「要求：看图，用中文描述它。直接输出描述本身。不要重复这句话，不要出现
        「用户要求」…只写看得见的，不猜。100 字以内。这张图是 NAS…」
    复述是**连续一整段**，切法是：找到最后一个提示词特征短语，
    从它后面开始取 ✓（比"按句丢开头"稳：按句丢只能丢开头几句，
    中间夹着"不要重复这句话"这类就漏了 ✗）

    返回空串 = 整段都是复述、洗不出描述（调用方应标失败重试，**别入库垃圾**）✗
    """
    body = str(raw or "").strip()
    if not body:
        return ""
    if not echoes_prompt(body):
        return body                       # 干净，原样返回（绝大多数情况走这里）
    # ① 定位最后一个提示词特征短语的**结束位置**
    cut = 0
    for mark in _PROMPT_MARKS:
        pos = body.rfind(mark)
        if pos >= 0:
            cut = max(cut, pos + len(mark))
    tail = body[cut:].lstrip("，,。.：:；;」）)】 \n\t")
    # ② 尾部可能还粘着复述的收尾语（"…以内。" 已经切掉；这里再兜一次句首废话）
    return tail.strip()

# 入库前的兜底清理：万一模型还是复述了过程语言，就把开头的废话切掉 ✓
_JUNK_PREFIX = ("我们需要", "用户要求", "这是一份", "好的，", "根据图片", "这张图是：")


def _extract_image(pdf_path: Path, xref: int):
    """从 PDF 里按 xref 抠出图片，返回 PIL.Image（拿不到就 None）✓"""
    try:
        import fitz
        from PIL import Image
        import io
        doc = fitz.open(str(pdf_path))
        try:
            raw = doc.extract_image(int(xref)) or {}
            data = raw.get("image") or b""
            if not data:
                return None
            im = Image.open(io.BytesIO(data))
            im.load()
            return im.convert("RGB")
        finally:
            doc.close()
    except Exception as exc:                                     # noqa: BLE001
        log.debug("抠图失败（xref=%s）：%s", xref, exc)
        return None


def _caption_text(page: int, desc: str) -> str:
    """入库文案：**头部带页码**（问答引用要能说"第 N 页那张图"）✓

    顺手做两层兜底：
      ① `_strip_echo()` 硬切掉模型复述的提示词（2026-10-20 新增，见它的说明）
      ② 按句丢掉开头的"过程语言"（原有的，保留）
    返回**空串** = 洗不出像样的描述 → 调用方应标失败重试，**别入库垃圾** ✗
    """
    original = str(desc or "")
    if is_think_dump(original):
        # 整段是思考草稿（切哪段都丢东西）→ 整条作废 ✓
        return ""
    raw = _strip_echo(original.replace("\n", " "))
    if not raw:
        return ""
    # 按句子拆开，丢掉开头那些"过程语言"句 ✓
    # 实测踩到：'我们需要回答用户。用户要求：这是一份 PDF 文档里的插图。设备接口示意图…'
    #   → 只切前缀切不干净（切掉"我们需要"后又剩"回答用户。…"）✗
    #   → 所以按句丢：只要开头这句含 我们/用户要求/这是一份/PDF 文档 之类，整句丢掉 ✓
    parts = [x.strip() for x in
             raw.replace("！", "。").replace("？", "。").replace("；", "。").split("。")]
    parts = [x for x in parts if x]
    # ⚠ 2026-10-20 修：这里原来是裸的 "任务" / "我们" ✗ —— 太宽泛，
    #   正常描述一句"…在多个**测试任务**中…"也会被整句丢掉（实测 #47 首句被误删）✗
    #   → 换成**只出现在过程语言里**的完整说法 ✓
    _BAD = ("我们需要回答", "我们需要描述", "用户要求", "这是一份 PDF",
            "PDF 文档", "文档里的插图", "任务说明", "这个任务", "任务是：",
            "需要回答", "需要描述", "请用中文", "简洁描述", "直接给描述",
            "它是什么图", "画了什么", "看图，", "只写看得见的", "照抄")
    while parts and any(k in parts[0] for k in _BAD):
        parts.pop(0)
    d = "。".join(parts).strip() or raw
    # 再收敛一轮纯前缀（"好的，""这张图是："这类）
    changed = True
    while changed:
        changed = False
        for junk in _JUNK_PREFIX:
            if d.startswith(junk):
                d = d[len(junk):].lstrip("，,。.：: ")
                changed = True
    # ★ 切完仍然带提示词 → 说明这段整体是复述，宁可失败重试也不入库 ✗
    if echoes_prompt(d) or is_think_dump(d) or len(d) < 6:
        return ""
    if len(d) > 300:
        d = d[:300] + "…"
    return f"[第 {page} 页 插图] {d}"


def run_queue(cfg, store, engine, chat_engine, *,
              folder_ids: list[int] | None = None,
              file_ids: list[int] | None = None,
              limit: int = 0,
              remote: dict | None = None,
              vl_dir: str | None = None,
              deadline: float | None = None,
              on_progress=None,
              source: str = "manual",
              claim: bool = True) -> dict:
    """把队列里的待办跑掉。返回统计。

    remote: 外部模型配置 {base_url, model, api_key, headers, max_side, max_tokens}
            传了就用外部；没传则本地（由调用方决定，见 folderconf.caption_model）
    vl_dir: 本地视觉模型目录（本地打标时用；None = 沿用当前已加载的模型）
    file_ids: 只打这几份 PDF（项目主 2026-10-20："打标按钮需要指定文件"）✓
    source: 'manual'（界面点开始）/ 'night'（「全力执行」）—— 只用于界面显示来源 ✓
    claim: True = 由本函数自己登记/清空运行状态（夜间任务、脚本直调）✓
           False = 状态由调用方（start_async）管，本函数只更新计数 ✓
    """
    from . import folderconf as FC
    from .ocr import gpu_slot, free_gpu_hint

    started = time.time()
    out = {"ok": True, "done": 0, "failed": 0, "skipped": 0, "texts": 0,
           "stopped": "", "elapsed": 0.0, "queue": store.caption_queue_stats()}

    # ★ 同一时间只允许一个打标任务（否则两个线程抢队列、进度数字也会互相覆盖）
    if claim:
        with _RUN_LOCK:
            if _RUN["running"]:
                out["ok"] = False
                out["stopped"] = "已经有打标任务在跑（先停下或等它结束）"
                return out
            _RUN.update(running=True, paused=False, stopping=False, source=source,
                        started=started, finished=0.0, total=0, picked=0,
                        done=0, failed=0, skipped=0, current="", current_page=0,
                        note="准备中…", last_error="",
                        model=str((remote or {}).get("model") or "本地视觉模型"))
    else:
        _run_update(source=source, started=started,
                    model=str((remote or {}).get("model") or "本地视觉模型"))

    # 取待办（带文件名/路径 ✓）
    rows = store.caption_queue_list(state="pending", folder_ids=folder_ids,
                                    file_ids=file_ids, limit=(limit or 200))
    out["picked"] = len(rows)
    _run_update(total=len(rows), note=f"共 {len(rows)} 条待办")
    if not rows:
        out["stopped"] = "队列里没有待办"
        out["elapsed"] = round(time.time() - started, 1)
        if claim:
            _finish_run(out, started)
        return out

    log.info("开始跑插图打标：%d 条待办（%s）", len(rows), free_gpu_hint() or "显存未知")

    # 文件夹缓存：拿维度、拿 caption_model ✓
    folders: dict[int, dict] = {}
    remote_fn = None
    if remote:
        from .vlm_client import caption_one as _rc
        _u = str(remote.get("base_url") or "")
        _m = str(remote.get("model") or "")
        _k = str(remote.get("api_key") or "")
        _h = remote.get("headers") or None
        _s = int(remote.get("max_side") or 768)
        _tk = int(remote.get("max_tokens") or 220)
        _eb = caption_extra_body(_m)
        if _u and _m:
            # ⚠ 2026-10-20 踩到：这里原来是 timeout=1800（30 分钟）✗
            #   实测有一条请求挂死在网关上 → **整批停摆 20 多分钟**，
            #   界面上看着"进度不动了"，而后面还有 19 条排队 ✗
            #   打标一张图正常只要 5~20 秒，给它 180 秒已经是十倍余量；
            #   超时就把这条标 failed（下次能重试），别拖住整批 ✓
            def remote_fn(im):                                   # noqa: ANN001
                return _rc(_u, _m, im, timeout=180, max_side=_s, max_tokens=_tk,
                           prompt=PDF_IMG_PROMPT, api_key=_k, extra_headers=_h,
                           extra_body=_eb)
            log.info("插图打标使用**外部模型**：%s @ %s%s", _m, _u,
                     f"（请求体附加 {list(_eb)}）" if _eb else "")
    local_ready = False

    # ★ 每份文件该用哪个模型：文件 > 文件夹 > 全局；**隐私文件夹强制本地** ✓
    #   项目主 2026-10-20："pdf打标按钮需要指定文件…针对我某些文件隐私效果需要用到本地"
    #   + "我的隐私文件统统放在里面，这样设置更方便"
    #   排序也一样重要：本地要加载 VL 模型，混着跑会来回切模型（每次几秒）✗
    #   → 走外部 API 的排前面，要用本地的排后面（只切一次）✓
    want_by_file: dict[int, str] = {}
    private_n = 0
    for r in rows:
        fid0 = int(r["file_id"])
        if fid0 in want_by_file:
            continue
        folder0 = int(r["folder_id"])
        frow0 = folders.get(folder0)
        if frow0 is None:
            frow0 = store.get_folder(folder0) or {}
            folders[folder0] = frow0
        if bool(int(frow0.get("privacy") or 0)):
            want_by_file[fid0] = "local"
            private_n += 1
            continue
        fmdl = str(r.get("file_caption_model") or "").strip().lower()
        if fmdl in ("local", "remote"):
            want_by_file[fid0] = fmdl
            continue
        fw = FC.FolderConfig(cfg, frow0.get("config"), engine).caption_model
        want_by_file[fid0] = fw or ("remote" if remote_fn is not None else "local")
    rows.sort(key=lambda r: 0 if want_by_file.get(int(r["file_id"])) == "remote" else 1)
    n_local = sum(1 for v in want_by_file.values() if v == "local")
    if n_local:
        log.info("本批有 %d 份文件走**本地视觉模型**（其中隐私文件夹 %d 份），"
                 "其余走外部 API（若配了）", n_local, private_n)
        if not vl_dir:
            log.warning("要用本地打标，但没找到本地视觉模型（VL）—— "
                        "会沿用当前已加载的对话模型，效果可能很差 ✗")

    for i, r in enumerate(rows, 1):
        if deadline and time.time() >= deadline:
            out["stopped"] = "到时限了（剩下的还留在队列里，下次接着跑）"
            log.info("插图打标到时限，暂停（已处理 %d 条）", i - 1)
            break
        # ★ 暂停 / 停止的闸门（2026-10-20 加）：暂停就在原地等，随时能继续 ✓
        if _gate() == "stop":
            out["stopped"] = "已手动停止（剩下的还留在队列里）"
            log.info("插图打标被手动停止（已处理 %d 条）", i - 1)
            break

        fid = int(r["file_id"])
        folder_id = int(r["folder_id"])
        qid = int(r["id"])
        page = int(r.get("page") or 0)
        xref = int(r.get("xref") or 0)
        pdf_path = Path(str(r.get("file_path") or ""))
        name = str(r.get("file_name") or pdf_path.name)

        # 界面进度：第几条、正在处理哪份文件 ✓
        _run_update(picked=i, current=name, current_page=page,
                    done=out["done"], failed=out["failed"], skipped=out["skipped"],
                    note=f"正在处理 {name}（第 {page} 页）")

        # 这个文件夹要用哪个模型 ✓（优先级：文件 > 文件夹 > 全局；隐私文件夹强制本地）
        frow = folders.get(folder_id)
        if frow is None:
            frow = store.get_folder(folder_id) or {}
            folders[folder_id] = frow
        fcfg = FC.FolderConfig(cfg, frow.get("config"), engine)
        want = want_by_file.get(fid, "")
        if not want:
            want = fcfg.caption_model
            if bool(int(frow.get("privacy") or 0)):
                want = "local"
        use_remote = (want == "remote") or (want == "" and remote_fn is not None)

        if on_progress:
            try:
                on_progress(i, len(rows), name, page)
            except Exception:                                    # noqa: BLE001
                pass

        try:
            if not pdf_path.is_file():
                store.caption_queue_mark(qid, "failed", "PDF 不在了（NAS 没挂载？）")
                out["failed"] += 1
                continue
            im = _extract_image(pdf_path, xref)
            if im is None:
                store.caption_queue_mark(qid, "skip", "这张图抠不出来（可能已损坏）")
                out["skipped"] += 1
                continue

            if use_remote and remote_fn is not None:
                # ★ 调外部 API：**不进显存锁** ✓（不占本机显存，可与别的活并行）
                desc = str(remote_fn(im) or "").strip()
            else:
                # ★ 本地视觉模型：进显存锁 ✓（和 OCR 排队，不撞显存）
                if vl_dir and hasattr(chat_engine, "switch_to"):
                    if not local_ready:
                        chat_engine.switch_to(str(vl_dir))
                        local_ready = True
                with gpu_slot("本地插图打标"):
                    res = chat_engine.chat(
                        [{"role": "user", "content": PDF_IMG_PROMPT}],
                        max_new_tokens=140, temperature=0.2, images=[im])
                desc = str(res or "").strip()

            if not desc:
                store.caption_queue_mark(qid, "failed", "模型没有返回描述")
                out["failed"] += 1
                continue

            text = _caption_text(page, desc)
            if not text:
                # ★ 2026-10-20：模型把提示词本身复述了一遍，洗不出描述 →
                #   宁可标失败留着重试，**绝不把「要求：看图，用中文描述它…」入库** ✗
                store.caption_queue_mark(qid, "failed",
                                         "模型复述了提示词，洗不出描述（可重试）")
                out["failed"] += 1
                continue
            dim = fcfg.index_dim
            # 编码 + 入库（用该文件夹的维度 ✓ 同文件夹必须同维度）
            # ⚠ 编码也要用模型 → 本地路径下同样进显存锁 ✓
            need_gpu = not (use_remote and remote_fn is not None)
            if need_gpu:
                with gpu_slot("插图描述编码"):
                    vec = engine.encode([text], prompt_name="Document", dim=dim)[0]
            else:
                vec = engine.encode([text], prompt_name="Document", dim=dim)[0]
            # add_chunks 是**追加**（不会清掉该 PDF 已有的正文片段 ✓）
            store.add_chunks(fid, [(text, vec, "caption")])
            store.caption_queue_mark(qid, "done", text[:200])
            out["done"] += 1
            out["texts"] += 1
            if out["done"] % 10 == 0:
                log.info("插图打标进度：%d/%d（%s）", i, len(rows), free_gpu_hint() or "")
        except Exception as exc:  # noqa: BLE001
            log.warning("插图打标失败 #%s（%s 第 %s 页）：%s",
                        qid, name, page, exc)
            try:
                store.caption_queue_mark(qid, "failed", f"{type(exc).__name__}: {exc}")
            except Exception:                                    # noqa: BLE001
                pass
            out["failed"] += 1

    out["elapsed"] = round(time.time() - started, 1)
    out["queue"] = store.caption_queue_stats()
    if claim:
        _finish_run(out)
    log.info("插图打标结束：成功 %d / 失败 %d / 跳过 %d，耗时 %.0f 秒",
             out["done"], out["failed"], out["skipped"], out["elapsed"])
    return out


def _finish_run(out: dict) -> None:
    """收尾：清运行状态，但**把结果留在状态里**（界面要看"上次跑了多少"）✓"""
    with _RUN_LOCK:
        _RUN.update(
            running=False, paused=False, stopping=False, finished=time.time(),
            done=int(out.get("done") or 0), failed=int(out.get("failed") or 0),
            skipped=int(out.get("skipped") or 0),
            note=str(out.get("stopped") or "已结束"),
            current="", current_page=0,
        )


def start_async(cfg, store, engine, chat_engine, *,
                folder_ids: list[int] | None = None,
                file_ids: list[int] | None = None,
                limit: int = 0,
                remote: dict | None = None,
                vl_dir: str | None = None,
                deadline: float | None = None,
                source: str = "manual",
                on_done=None) -> dict:
    """在后台线程里跑（界面点「开始打标」走这里）✓

    为什么不直接在接口里调 run_queue：
      一批 200 条要跑十几分钟，同步调用会把 HTTP 请求挂死 ✗
      （浏览器超时、界面看起来"没反应"——正是项目主说的"看不到进程"）
    """
    with _RUN_LOCK:
        if _RUN["running"]:
            return {"ok": False, "message": "已经有打标任务在跑",
                    "state": run_state()}
        _RUN.update(running=True, paused=False, stopping=False, source=source,
                    started=time.time(), finished=0.0, total=0, picked=0,
                    done=0, failed=0, skipped=0, current="", current_page=0,
                    note="启动中…", last_error="",
                    model=str((remote or {}).get("model") or "本地视觉模型"))

    def _worker() -> None:
        try:
            # ★ 一次「开始」就跑到队列空（和夜里「全力执行」一个行为）✓
            #   以前只跑一批 200 条就停，用户得反复点「开始」，很容易以为它坏了 ✗
            batch = int(limit or 200)
            rounds = 0
            while True:
                if deadline and time.time() >= deadline:
                    _run_update(note="到时限了（剩下的还留在队列里）")
                    break
                rounds += 1
                res = run_queue(cfg, store, engine, chat_engine,
                                folder_ids=folder_ids, file_ids=file_ids,
                                limit=batch,
                                remote=remote, vl_dir=vl_dir,
                                deadline=deadline, source=source, claim=False)
                pending = int((res.get("queue") or {}).get("pending") or 0)
                with _RUN_LOCK:
                    stopping = bool(_RUN["stopping"])
                if stopping:
                    break
                if int(res.get("picked") or 0) == 0 or pending <= 0:
                    _run_update(note="队列跑完了")
                    break
                done_n = int(res.get("done") or 0)
                if done_n == 0 and int(res.get("failed") or 0) == 0:
                    break          # 这批一张都没处理 → 别死循环
                log.info("插图打标第 %d 批结束，队列还剩 %d 张", rounds, pending)
        except Exception as exc:                                 # noqa: BLE001
            log.exception("后台插图打标出错")
            _run_update(last_error=f"{type(exc).__name__}: {exc}", note="出错停下了")
        finally:
            with _RUN_LOCK:
                _RUN.update(running=False, paused=False, stopping=False,
                            finished=time.time(), current="", current_page=0)
            if on_done:
                try:
                    on_done()
                except Exception:                                # noqa: BLE001
                    pass

    threading.Thread(target=_worker, name="eg2-captionq", daemon=True).start()
    log.info("插图打标已在后台启动（来源：%s）", source)
    return {"ok": True, "state": run_state()}
