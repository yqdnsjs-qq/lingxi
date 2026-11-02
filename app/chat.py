"""本地对话模型（RAG 的「生成」那一半）。

设计原则和 face-lab 一样：**不要往主 venv 里装东西**。
transformers / torch 已经装好了，所以这里只加载一个本地下载好的小模型，
不引入任何新依赖、不联网。

模型放哪：
    models\\Qwen2.5-1.5B-Instruct\\        ← app/fetch_llm.py 下载到这里
    想换模型改 config.ini 的 [chat] model_dir 即可。

显存：默认和嵌入模型共存。3060 Ti 8GB 上，嵌入模型约占 2.3GB，
      1.5B bf16 约 2.9GB，剩下给 KV 缓存够用。
      不够时把嵌入模型切到「省显存」模式，或把这里的 device 设成 cpu。
"""
from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

log = logging.getLogger("eg2.chat")

ROOT = Path(__file__).resolve().parent.parent


class ChatEngine:
    """延迟加载的小型对话模型。"""

    def __init__(self, cfg, model_dir=None):
        self.cfg = cfg
        self._dir_override = Path(model_dir) if model_dir else None
        self._lock = threading.Lock()
        self._model = None
        self._tok = None
        self._torch = None
        self._vram_limit = 0
        self._device = None
        self._note = ""

    # ---------------- 路径 ----------------
    @property
    def model_dir(self) -> Path:
        if self._dir_override is not None:
            return self._dir_override
        p = self.cfg.chat_model_dir
        return p if p.is_absolute() else (ROOT / p)

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def switch_to(self, path) -> None:
        """换模型：先卸下当前的腾显存，再指向新目录（下次提问时加载）。"""
        self.unload()
        self._dir_override = Path(path)

    @property
    def note(self) -> str:
        return self._note

    def available(self) -> tuple[bool, str]:
        d = self.model_dir
        if not d.is_dir():
            return False, f"没有对话模型目录：{d}（用 app\\fetch_llm.py 下载）"
        if not (d / "config.json").exists():
            return False, f"目录里没有 config.json：{d}"
        has_weight = any(d.glob("*.safetensors")) or any(d.glob("*.bin"))
        if not has_weight:
            return False, f"目录里没有权重文件：{d}"
        return True, "ok"

    # ---------------- 加载 ----------------
    def _load(self) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            import json

            import torch

            d = self.model_dir
            want = (self.cfg.chat_device or "auto").lower()
            if want == "cpu":
                device, dtype = "cpu", torch.float32
            elif torch.cuda.is_available():
                device, dtype = "cuda", torch.bfloat16
            else:
                device, dtype = "cpu", torch.float32

            # 判断是不是视觉语言模型（VL）。
            # VL 必须用 Qwen2_5_VLForConditionalGeneration + AutoProcessor，
            # 走 AutoModelForCausalLM 会直接报错（之前选错模型 500 就是这个原因）。
            arch = ""
            try:
                arch = ",".join(json.loads(
                    (d / "config.json").read_text(encoding="utf-8")
                ).get("architectures") or [])
            except (OSError, ValueError):
                pass
            self._is_vl = ("VLForConditional" in arch
                           or "Vision2Seq" in arch
                           or "vl" in d.name.lower())

            self._torch = torch
            self._device = device
            log.info("加载对话模型 %s（device=%s dtype=%s%s）",
                     d.name, device, dtype, " VL" if self._is_vl else "")

            if self._is_vl:
                from transformers import (AutoProcessor,
                                          Qwen2_5_VLForConditionalGeneration)
                self._proc = AutoProcessor.from_pretrained(str(d))
                self._tok = getattr(self._proc, "tokenizer", self._proc)
                kw = dict(dtype=dtype, low_cpu_mem_usage=True)
                if self._vram_limit and device == "cuda":
                    # 用 accelerate 把超出上限的层放 CPU。
                    # max_memory 只对 device_map 生效，所以两者要一起给。
                    kw["device_map"] = "auto"
                    kw["max_memory"] = {0: f"{self._vram_limit}MiB",
                                        "cpu": "48GiB"}
                    log.info("VL 显存上限 %s MB，超出部分走 CPU", self._vram_limit)
                    self._model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
                        str(d), **kw).eval()
                    self._device = "cuda"
                    return
                self._model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
                    str(d), **kw).to(device).eval()
            else:
                from transformers import AutoModelForCausalLM, AutoTokenizer
                self._proc = None
                self._tok = AutoTokenizer.from_pretrained(str(d))
                self._model = AutoModelForCausalLM.from_pretrained(
                    str(d), dtype=dtype, low_cpu_mem_usage=True)
                self._model = self._model.to(device).eval()

    def set_vram_limit(self, mb: int) -> None:
        """给下一次加载设显存上限（MB）。0 = 不限。

        为什么要这个：VL-3B 占 7.5 GB，8 GB 卡上桌面只剩 138 MB →
        电脑卡顿。设个上限（比如 4500）后，超出的层由 accelerate
        自动放到 CPU，显存立刻降下来。**完全可逆** —— 设回 0 就恢复。
        代价：放 CPU 的那部分层慢一些。
        """
        self._vram_limit = max(0, int(mb or 0))
        if self._model is not None:
            self.unload()          # 下次用新上限重新加载

    @property
    def device(self) -> str:
        return self._device or "?"

    # ---------------- 生成 ----------------
    def chat(self, messages: list[dict], max_new_tokens: int = 512,
             temperature: float = 0.2, images: list | None = None) -> dict:
        """跑一次对话。messages 形如 [{"role":"system","content":...}, ...]

        images: 可选，PIL 图片列表。VL 模型会看图作答；
                纯文本模型会忽略它。
        """
        self._load()
        torch = self._torch
        gen = dict(max_new_tokens=max(32, min(int(max_new_tokens), 2048)),
                   do_sample=temperature > 0.05,
                   repetition_penalty=1.05,
                   pad_token_id=self._tok.eos_token_id)
        if temperature > 0.05:
            gen["temperature"] = max(0.05, min(float(temperature), 1.5))

        if self._is_vl:
            # VL 的输入必须由 processor 组装：它负责把图切成视觉 token
            # 并插进文本里的占位符位置。chat 的 messages 是纯文本的，
            # 这里转成多模态格式 —— 图片放在最后一条 user 消息里。
            msgs = [dict(m) for m in messages]
            if images:
                for m in reversed(msgs):
                    if m.get("role") == "user":
                        m["content"] = (
                            [{"type": "image", "image": im} for im in images]
                            + [{"type": "text", "text": str(m.get("content") or "")}])
                        break
            text = self._proc.apply_chat_template(
                msgs, tokenize=False, add_generation_prompt=True)
            kw = {"text": [text], "return_tensors": "pt"}
            if images:
                kw["images"] = list(images)
            inputs = self._proc(**kw).to(self._device)
            n_in = int(inputs["input_ids"].shape[1])
            with torch.no_grad():
                out = self._model.generate(**inputs, **gen)
            answer = self._tok.decode(out[0][n_in:], skip_special_tokens=True).strip()
            return {"answer": answer, "prompt_tokens": n_in,
                    "completion_tokens": int(out.shape[1]) - n_in,
                    "device": self._device, "vl": True}

        text = self._tok.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True)
        inputs = self._tok([text], return_tensors="pt").to(self._device)
        n_in = int(inputs["input_ids"].shape[1])
        with torch.no_grad():
            out = self._model.generate(**inputs, **gen)
        answer = self._tok.decode(out[0][n_in:], skip_special_tokens=True).strip()
        return {"answer": answer, "prompt_tokens": n_in,
                "completion_tokens": int(out.shape[1]) - n_in,
                "device": self._device, "vl": False}

    def unload(self) -> None:
        """腾显存：把模型从显卡上摘下来。"""
        with self._lock:
            if self._model is None:
                return
            try:
                self._model.to("cpu")
            except Exception:                             # noqa: BLE001
                pass
            self._model = None
            self._tok = None
            if self._torch is not None and self._torch.cuda.is_available():
                self._torch.cuda.empty_cache()


# ---------------- 提示词 ----------------
SYSTEM_PROMPT = (
    "你是本地文件知识库助手。请**尽量从用户给出的资料片段里提取信息**来回答问题。\n"
    "要求：\n"
    "1. 用中文，简洁直接，把资料归纳成结论，不要大段照抄原文。\n"
    "2. 每条结论后面用 [数字] 标注它来自哪个片段，例如「部署需要 Python 3.12 [2]」。\n"
    "3. 只有当资料确实与问题完全无关时，才回答「资料里没有提到」。\n"
    "4. 不要编造资料里没有的内容。资料之间矛盾时，把矛盾指出来。\n"
    "\n"
    "注意：资料片段是从长文档里切出来的，可能从句子中间开始、也可能在句子中间结束，\n"
    "这是正常的。请根据片段里的内容作答，不要因为片段不完整就说「没有提到」。\n"
    "\n"
    "**输出格式（很重要）**：\n"
    "- 直接回答问题本身，把资料**综合**成一段或几条要点。\n"
    "- **不要**写成「资料片段1提到…资料片段2提到…」这种逐条转述 ——\n"
    "  那等于没回答，用户要的是结论，不是片段清单。\n"
    "- 引用编号 [数字] 放在对应结论的句子末尾。\n"
    "- 多个片段讲同一件事时合并成一条，编号并列，例如「… [3][7]」。\n"
    "- 同一份文件被切成多片时当成同一份资料，不要重复说。\n"
    "\n"
    "正确示例：\n"
    "  问：Obsidian 怎么安装？\n"
    "  答：Obsidian 官方提供 Windows/macOS/Linux 桌面版，下载安装包直接安装即可 [1]。\n"
    "      要在 NAS 上跑的话，可以用 Docker 一键脚本，支持 13 种主流发行版 [6]。\n"
    "\n"
    "错误示例（千万不要这样）：\n"
    "  资料片段1提到 Obsidian 是什么。资料片段2提到插件使用原则。\n"
    "  资料片段3提到 Canvas Mindmap 插件……\n"
    "\n"
    "**输出纪律（非常重要，违反就等于没答）**：\n"
    "- **只用中文回答**。绝对不能输出英文的思考过程、推理草稿，\n"
    "  例如 The material shows… / Let me think… / Hmm wait… 这类内容。\n"
    "- **直接给结论**，不要复述你是怎么找到的。不要出现\n"
    "  「资料片段1提到…」「根据检索到的内容…」这种过程叙述。\n"
    "- 资料是家谱、名单、表格时，**如实列出相关信息**，\n"
    "  不要编造亲属关系（父亲/姐妹/儿子）—— 名单里没写的就别说。\n"
    "- 资料里确实没有的，一句话说明没有，不要长篇分析。\n"
)

# ★ 上下文预算（2026-10-19 项目主："问答时服务崩了"）
#   实证：一次问答的上下文到了 16411 token，而模型上限是 8192
#   （日志：Token indices sequence length is longer than the specified maximum
#     sequence length for this model (16411 > 8192)）
#   → 回答变得**很慢** → 界面在等回答、心跳被拖断 → 服务被误判成"界面关了"而自杀 ✗
#   中文大约 1 字 ≈ 1 token，上限 8192 → 给资料留预算，其余留给回答和历史 ✓
#   2026-10-19 调整：4000 → 5500（实测 4000 时把手册真正的"配置步骤"片段裁掉了 ✗，
#   而不裁历史/提示词的账要一起算：5500 资料 + 1200 历史 + ~600 提示词 ≈ 7300 token，
#   在 8192 以内还留了余量 ✓）
CONTEXT_BUDGET = 5500        # 资料片段总字数上限
CHUNK_MAX_CHARS = 1500       # 单条片段上限（防止一个巨片吃掉全部预算）
HISTORY_MAX_CHARS = 1200     # 历史对话总字数上限（以前是每条 2000 字、没有总量 ✗）


def trim_chunks(chunks: list[dict], budget: int = CONTEXT_BUDGET) -> list[dict]:
    """按总字数预算裁剪检索结果（**必须在调用方裁** ✓）。

    为什么在调用方裁而不是在 build_messages 里裁：
      「来源」列表（界面上点开看的那份）用的是同一份 chunk_list ✓ ——
      如果只在拼提示词时截断，来源编号就会和实际喂给模型的对不上 ✗
    """
    out: list[dict] = []
    used = 0
    for c in chunks:
        text = (c.get("text") or "").strip()
        if len(text) > CHUNK_MAX_CHARS:
            c = dict(c)
            c["text"] = text[:CHUNK_MAX_CHARS] + "…"
            text = c["text"]
        size = len(text) + 60          # 加上"[n] 来源：xxx"那点开销
        if out and used + size > budget:
            log.info("问答上下文达预算上限：取前 %d 条（共 %d 条），"
                     "约 %d 字（原来可能超过模型上限 8192 token）",
                     len(out), len(chunks), used)
            break
        out.append(c)
        used += size
    return out


def build_messages(question: str, chunks: list[dict],
                   history: list[dict] | None = None) -> list[dict]:
    """把检索到的片段拼成提示词。"""
    parts = []
    for i, c in enumerate(chunks, 1):
        where = c.get("name") or c.get("path") or ""
        parts.append(f"[{i}] 来源：{where}\n{c.get('text', '').strip()}")
    context = "\n\n".join(parts) if parts else "（没有检索到任何资料）"
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    # 历史：以前是「取最近 4 条、每条最多 2000 字」——总量没上限 ✗
    # （4×2000 = 8000 字，光历史就能把上下文撑爆）→ 现在加总量上限 ✓
    hist_chars = 0
    for h in reversed((history or [])[-4:]):
        role = h.get("role")
        if role not in ("user", "assistant") or not h.get("content"):
            continue
        body = str(h["content"])[:600]
        if hist_chars + len(body) > HISTORY_MAX_CHARS:
            break
        hist_chars += len(body)
        msgs.insert(1, {"role": role, "content": body})       # 保持时间顺序 ✓
    # 问题必须放在资料**前面** —— 实测放在后面时，1.5B 模型会直接回
    # 「资料里没有提到」；换到前面就能正常引用片段作答。
    msgs.append({"role": "user",
                 "content": f"问题：{question}\n\n"
                            f"请从下面的资料片段里找答案：\n{context}"})
    return msgs


def cite_numbers(answer: str) -> list[int]:
    """从回答里把 [1] [2] 这种引用编号抠出来。"""
    return sorted({int(x) for x in re.findall(r"\[(\d{1,2})\]", answer or "")})
