# 宇之语 · 灵犀   本地部署 + 文件与知识检索

[![版本](https://img.shields.io/badge/version-v3.5.4-3b82f6)](https://github.com/yqdnsjs-qq/lingxi/releases)
[![平台](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-64748b)](#)
[![数据](https://img.shields.io/badge/data-100%25%20local-22c55e)](#六安全建议)
[![仓库](https://img.shields.io/badge/repo-yqdnsjs--qq%2Flingxi-181717?logo=github)](https://github.com/yqdnsjs-qq/lingxi)

> **灵犀**是装在自己电脑（或自己服务器）上的私有知识库：把散在磁盘里的文档、图片、录音
> 变成可搜、可问、可管的东西——答案带出处，数据一步不出本机。
> 当前版本 **v3.5.4**（2026-11-02）｜仓库：<https://github.com/yqdnsjs-qq/lingxi>

把 Google 的 [google/embeddinggemma-2](https://huggingface.co/google/embeddinggemma-2) 多模态嵌入模型
完整部署在**本文件夹内**，并附带一个**本地文件语义搜索程序**：选一个文件夹建立索引，
以后就能用「大白话」找回文件。

- **模型**：EmbeddingGemma 2（740M = 文本 270M + 视觉 170M + 音频 300M），输出 768 维，
  支持文本 / 图片 / 音频 / 视频以及任意组合
- **文件搜索**：索引本机任意文件夹，支持按内容、按文件名、按图片内容模糊检索
- **界面切换模式**：省显存 / 均衡 / 全功能一键切换，改设备、精度、维度都不用重启程序
- **每个文件夹独立配置**：多选文件夹批量添加，A 文件夹索引图片、B 只索引文字、
  C 只登记文件名，搜索时一起出结果
- **环境**：独立的 Python 虚拟环境（`venv\`），与系统 Python 完全隔离
- **数据**：模型权重、索引数据库、缓存、临时文件**全部写入本文件夹**
- **系统影响**：不写注册表、不改系统 PATH、不装全局包、不注册系统服务
- **运行环境**：Windows 10/11 64 位；有 6 GB 以上显存的独显可跑全模态，纯 CPU 也可运行

> 📖 **详细的部署与使用说明**（图文步骤、备份恢复、故障排查），见微信公众号「**宇之语**」的相关文章。

---

## 一、快速开始

| 文件 | 作用 |
| --- | --- |
| `setup.bat` | **首次使用**：创建环境、安装依赖、下载模型（约 3-4 GB 下载量） |
| `search.bat` | **启动服务**：起服务并直接打开搜索页面（日常就用这个） |
| `start.bat` | 启动服务并打开嵌入测试页 |
| `restart.bat` | 重启服务（先停再起；手工改过 `config.ini` 后用它） |
| `stop.bat` | 停止服务（先优雅退出，必要时强制结束并清理端口） |
| `status.bat` | 查看运行状态、设备、索引量，以及定位文件能不能用 |
| `backup.bat` | 备份索引库（服务开着也能安全备份）/ 列出已有备份 |
| `restore.bat` | **双击选文件即可恢复**（自动停服务、自动留安全副本） |
| `fix-permissions.bat` | 修复「定位文件」不可用（需管理员，会自弹 UAC） |
| `status.bat` | 查看运行状态、进程号、端口 |
| `selftest.bat` | 自检：服务、模型、文本/图片/音频编码、搜索接口是否正常 |

日常三步：**双击 `search.bat` → 添加文件夹 → 以后随时搜索**。用完双击 `stop.bat`。

服务地址：<http://127.0.0.1:8199/search>

---

## 二、宇之语 · 灵犀怎么用

### 1. 建立索引

1. 双击 `search.bat`，浏览器会自动打开搜索页；
2. 点「**＋ 添加文件夹并建立索引**」，在弹出的目录树里逐级进入目标文件夹（磁盘 → 子目录），
   选中后点「索引这个文件夹」；
3. 页面会显示索引进度（已处理/总数、当前文件、新增片段数）。索引**自动保存在本机**
   （`data\index\index.db`），关掉程序、重启电脑都不会丢。

再点「**增量更新全部**」时只会处理**新增和修改过**的文件，删掉的文件会自动从索引里移除。
如果换了向量维度或想彻底重来，用「**全部重建索引**」。

### 2. 搜索

在搜索框里用自然语言描述，回车即可，例如：

| 你想找的东西 | 输入 |
| --- | --- |
| 看过的一篇笔记 | `极光是什么颜色` |
| 一份流程文件 | `差旅发票怎么报销，超过多少钱要审批` |
| 一段代码 | `计算发票总金额的函数` |
| 一张图片 | `蓝色圆形图案的照片` |
| 一份表格 | `各月销售额和订单数` |
| 记不清名字的文件 | `销售数据`（按文件名包含匹配） |

结果按相关度排序，每条显示：文件名、所在目录、**命中的原文片段（带关键词高亮）**、
文件类型/大小/修改时间，以及三个按钮：

- **打开文件** —— 用系统默认程序打开
- **打开所在文件夹** —— 在资源管理器里定位并选中
- **复制路径**

还可以用顶部的下拉框**按文件夹或按文件类型过滤**（PDF / Word / Excel / 图片 / 代码…）。

### 3. 搜索到之后能做什么

每条结果下面有四个按钮：

| 按钮 | 作用 |
| --- | --- |
| **预览** | 不出程序直接看内容（点文件名也是预览） |
| **定位文件** | 让资源管理器**选中**这个文件，并把窗口**提到最前**（走 Shell 接口，不新开进程） |
| **用默认程序打开** | 交给系统默认程序打开（Word / 看图 / 播放器…） |
| **复制路径** | 复制完整路径，方便粘到别处 |
| **下载到本机** | 把文件（或图片预览图）存到「下载」文件夹，桌面功能不可用时的退路 |

**预览支持的类型**（点一下就能看，不用等外部程序）：

| 类型 | 预览方式 |
| --- | --- |
| 图片（jpg/png/bmp/webp/heic/tiff…） | 服务端先解码再显示，所以 HEIC/TIFF 也能看；确实不是图片的文件会写明原因，而不是留个空白框 |
| 音频 / 视频 | 内嵌播放器，可拖动进度 |
| PDF | 三种看法，预览顶部切换：**内置查看器**（原版式、可缩放，带 `#page=N&zoom=page-fit` 直接跳到命中页并整页显示）、**按页文字**（标「第 N / 总页」，写了本页有几处命中，翻到别页还能「跳到命中页」）、**看原版式**（把那一页渲染成图片，并在命中的文字上叠**黄框**）。带查询词打开时默认进「看原版式」：整页缩放看全貌 + 黄框标出命中位置，点图片可在「整页/适宽」间切换。预览在索引里存的只是「各页文字拼起来的一整段」，页码早就丢了、也没有表格样式，所以按页文字、出图、按位置定位都是预览时现读的，不用重建索引。搜索词在页面上没有逐字出现时会明确说「这是语义命中」，不硬指位置 |
| 文本 / 代码 / Markdown | 显示正文，**并高亮命中的关键词**、自动滚到匹配位置 |
| Word / Excel / PPT | 显示抽出的正文文字 |

预览走的是「按索引 ID 取文件」，只允许访问已索引文件夹里的内容，
不会变成任意读文件的缺口。

### 4. 「定位文件」为什么能自动置前

后台进程打开的资源管理器，Windows 默认**只让它在任务栏闪一下**，不允许抢焦点
（前台锁定机制）。所以点完「定位文件」后，程序还会多做一步：找到那个文件夹窗口
并把它激活。按可靠性依次尝试：

1. `SetForegroundWindow` —— 直接申请前台；
2. `AttachThreadInput` 临时接管前台线程的输入队列，再 `SetForegroundWindow`；
3. 模拟一次 ALT 按键，让 Windows 认为「用户刚操作过」，解除前台锁定；
4. 实在抢不到焦点，就用 `SetWindowPos` 把窗口抬到最上层 —— 至少让你能直接看见，
   而不是只在任务栏闪。

窗口是按标题找的。Win11 的标题是「文件夹名 - 文件资源管理器」，
程序会先去掉这个后缀再精确匹配，匹配不上才退化成包含匹配，避免误激活别的窗口。

### 5. 怎么知道索引在不在跑

- 点「增量更新」后**底部立刻弹提示**「已开始更新」；
- 对应文件夹那一行会出现蓝色的 **⏳ 更新中 12/500**（一直在动，表示确实在跑）；
- 每行常年显示 **文件数 · 片段数 · 异常数 · 上次更新时间**；
- 跑完后弹一条汇总：「索引完成：新增 3 · 跳过 800 · 清理 0 · 异常 2」。

如果扫描几秒就结束、且提示里带「已跳过删除以免误清索引」，
说明那次没读到文件（网络共享断了），索引本身是安全的。

### 6. 能索引哪些内容

| 类型 | 索引内容 |
| --- | --- |
| `.txt .md .csv .json .yaml .log` 等文本 | 正文（自动猜 UTF-8 / GBK 编码） |
| `.py .js .ts .java .c .cpp .go .rs .sql` 等 60+ 种源码/配置 | 正文 |
| `.pdf` | 正文（依赖 pypdf，已随 `setup.bat` 安装） |
| `.docx .xlsx .pptx` | 正文（直接解析 OOXML，不装 Office） |
| 旧版 `.doc .xls .ppt` | 只索引文件名（二进制老格式，不解析正文） |
| 图片 `.jpg .jpeg .png .bmp .gif .webp .tif .heic .heif .avif` | **图片内容**（用文字描述就能搜到图） |
| 音频 / 视频 | 默认**关闭**（较慢），需要时改 `config.ini` |
| 其它二进制文件 | 只索引文件名和所在目录 |

默认跳过 `.git node_modules __pycache__ AppData Windows` 等目录，
以及 `.exe .dll .zip .mp4 .tmp .db` 等文件（都在 `config.ini` 里可改）。

### 7. 文件搜索的接口

| 接口 | 说明 |
| --- | --- |
| `GET /search` | 搜索页面 |
| `GET /api/search?q=关键词&top_k=30&folder_ids=&exts=.pdf` | 搜索 |
| `GET /api/folders` | 文件夹列表（含各自模式、可调项定义） |
| `POST /api/folders` | 添加文件夹，支持 `paths` 多选 + `mode` |
| `PUT /api/folders/{id}/config` | 改单个文件夹的配置 |
| `POST /api/folders/config` | 批量给多个文件夹套同一个模式 |
| `POST /api/folders/remove` | 批量移除 |
| `DELETE /api/folders/{id}` | 移除单个文件夹 |
| `GET /api/fs/list?path=` | 目录浏览（用于选择文件夹） |
| `POST /api/index/start` / `POST /api/index/cancel` | 开始 / 取消索引 |
| `GET /api/index/status` | 索引进度与统计 |
| `POST /api/open` | 打开文件或定位到所在文件夹（只允许已索引范围内的文件） |
| `GET /api/settings` / `POST /api/settings` | 读取 / 修改运行模式与各项设置 |
| `POST /api/model/reload` / `GET /api/model/status` | 热重载模型 / 查看重载状态 |

命令行也能用：

```bat
venv\Scripts\python.exe -c "import json,urllib.request;print(json.load(urllib.request.urlopen('http://127.0.0.1:8199/api/search?q=极光'))['results'][:3])"
```

---

### 8. 每个文件夹单独设模式（多选批量添加）

不同文件夹可以用不同的索引策略，搜索时**一起出结果**：

1. 点「＋ 添加文件夹（可多选）」；
2. 在弹窗顶部选好这次要用的 **索引模式**（跟随全局 / 全模态 / 文字+图片 / 文字+音频 / 仅文字 / 只索引文件名）；
3. 在目录树里**勾选多个文件夹**（可以边浏览边勾选，选择会保留），点「添加 N 个文件夹并建立索引」；
4. 后台会**逐个**扫描，进度条会显示「第 2/5 个文件夹」。

已经添加的文件夹随时可以：

- 点它自己的「**配置**」改模式、向量维度、切块长度、是否索引图片/音频/视频等；
- 在列表里**勾选多个**，用「批量设模式 / 批量增量更新 / 批量重建 / 批量移除」统一处理。

**四个要点**

- **模型按需自动加载**：全局设成「省显存（仅文本）」，但某个文件夹要索引图片时，
  程序会自动把视觉编码器也加载上。`/health` 里 `configured_modality` 是你配置的值，
  `modality` 是实际加载的值——不会出现「文件夹配了图片却搜不到」。
- **不同向量维度可以混着搜**：A 文件夹 128 维、B 文件夹 512 维都没问题。
  查询向量按各文件夹的维度切片比对（这正是 Matryoshka 截断的定义），
  不用统一维度、也不用重建索引。
- **「只索引文件名」**：适合安装包、备份、素材堆这种目录，只登记文件名和类型，
  速度最快；里面的文件仍然能按文件名搜到。
- **改动即时生效**：只改索引范围不用重载模型；改了某个文件夹的向量维度，
  界面会提示并帮你**只重建那一个文件夹**，其它文件夹不受影响。

常见组合：

| 文件夹类型 | 建议模式 | 理由 |
| --- | --- | --- |
| 文档 / 资料 / 代码 | 仅文字 | 最快，用不上图片音频 |
| 产品图 / 截图 / 素材 | 文字 + 图片 | 可以用「蓝色圆形」这类描述找图 |
| 录音 / 音频素材 | 文字 + 音频 | 能按声音内容找 |
| 安装包 / 备份 / 压缩包 | 只索引文件名 | 不读内容，秒级完成 |

---

## 三、运行模式与设置（界面直接切换）

搜索页和嵌入页里都有一块「**运行模式与设置**」，不用再手动改 `config.ini` 再重启。

### 1. 三个一键模式

| 模式 | 加载的模态 | 索引内容 | 适合场景 |
| --- | --- | --- | --- |
| **省显存** | 仅文本（270M） | 只索引文字 | 显存小，只找文档 / 代码 |
| **均衡（推荐）** | 全模态（740M） | 文本 + 图片 | 日常找文件 |
| **全功能** | 全模态（740M） | 文本 + 图片 + 音频 + 视频 | 还要搜音视频素材 |

点一下就切好；当前用的模式会打勾，单项改过之后会显示成「自定义」。

### 2. 详细设置（15 项）

点「详细设置 ▾」展开，可以单独调：

- **模型**：加载的模态、运行设备（自动 / 显卡 CUDA / 只用 CPU）、数值精度、嵌入批大小
- **文件索引**：向量维度、是否索引图片/音频/视频、正文读取大小上限、单文件片段上限、
  切块长度、索引批大小、启动时自动增量扫描
- **服务**：默认任务前缀、启动后是否自动开浏览器

### 3. 生效方式（每项都有标签提示）

| 标签 | 含义 |
| --- | --- |
| （无标签）**立即生效** | 保存后下一次编码 / 扫描就用新值 |
| **需重载模型**（橙色） | 换了设备、精度或模态，点「立即重载模型」，约 10-40 秒完成，**不用重启程序** |
| **需重建索引**（红色） | 改向量维度会清空现有索引，界面会二次确认，并要求重建 |

所有改动都会**自动写回 `config.ini`**（原有注释完整保留），下次启动依然是你的选择。
改坏了也不要紧：把 `config.ini` 删掉重跑 `setup.bat` 即可恢复默认。

---

## 四、目录结构

```
embeddinggemma-2\
├─ setup.bat              一键安装
├─ search.bat             启动服务（推荐日常使用）
├─ start.bat              启动服务（嵌入测试页）
├─ backup.bat / restore.bat  备份索引 / 图形化恢复
├─ fix-permissions.bat   修复「定位文件」不可用（需管理员）
├─ 安装PDF原版式.bat     补装 PyMuPDF（让 PDF 能「看原版式」出图；setup.bat 一般已装）
├─ restart.bat           先停再起（带参数时等价于 restart.bat search）
├─ stop.bat / status.bat / selftest.bat
├─ config.ini             全部可调参数
├─ requirements.txt       Python 依赖清单
├─ README.md              本文件
├─ app\
│   ├─ server.py          HTTP 服务（FastAPI + uvicorn）
│   ├─ model.py           模型加载、多模态输入整理、批量编码
│   ├─ extract.py         文本抽取（文本/源码/PDF/Office/图片/音视频）
│   ├─ store.py           索引存储与向量检索（SQLite + numpy）
│   ├─ indexer.py         后台扫描与建立索引
│   ├─ searcher.py        查询编码与结果排序
│   ├─ filesearch.py      文件搜索的 HTTP 接口
│   ├─ folderconf.py      每个文件夹的索引配置与模式
│   ├─ settings.py        界面设置项与运行模式预设
│   ├─ settings_api.py    设置 / 模型热重载接口
│   ├─ close_ui.py        关掉界面的浏览器窗口（stop.bat / start.bat 调用）
│   ├─ config.py / info.py / selftest.py
│   ├─ cli.py             命令行编码工具
│   ├─ download_model.py  模型下载总入口（HuggingFace → ModelScope 自动回退）
│   ├─ fetch_model.py     纯标准库模型下载器（多连接 + 断点续传）
│   ├─ fetch_torch.py     纯标准库 PyTorch 下载器（多连接 + 断点续传）
│   ├─ resumable.py       通用断点续传下载器
│   ├─ run_server.bat     内部脚本（由 start.bat 调用）
│   └─ static\
│       ├─ index.html     嵌入测试界面
│       ├─ search.html    宇之语 · 灵犀界面
│       └─ settings.js    两个页面共用的「运行模式与设置」面板
├─ models\embeddinggemma-2\   模型权重（约 1.5 GB）
├─ venv\                  Python 虚拟环境（约 3.6 GB）
├─ data\
│   ├─ index\index.db     文件索引数据库（你的索引就存在这里）
│   ├─ samples\           示例图片/音频
│   └─ tmp\               处理多模态文件时的临时目录（自动清理）
├─ logs\server.log        运行日志
├─ run\server.pid         进程号
└─ .cache\                pip 缓存、下载缓存、PyTorch 缓存
    └─ wheels\            已下载的 PyTorch wheel（约 2 GB，可安全删除）
```

整个文件夹约 **7.2 GB**（不含你自己索引的文件；索引本身通常只有几十 MB）。

---

## 五、嵌入接口（模型能力）

### 1. OpenAI 兼容接口 `POST /v1/embeddings`

任何支持自定义 `base_url` 的客户端都能直接用（`api_key` 随便填）：

```bash
curl http://127.0.0.1:8199/v1/embeddings ^
  -H "Content-Type: application/json" ^
  -d "{\"input\": [\"第一段文本\", \"第二段文本\"], \"dimensions\": 256}"
```

Python：

```python
import requests

r = requests.post("http://127.0.0.1:8199/v1/embeddings", json={
    "input": ["什么是极光？", "极光由太阳带电粒子与高层大气碰撞产生。"],
    "prompt_name": "SentenceSimilarity",   # 任务前缀，见下文
    "dimensions": 256,                     # 可选 768/512/256/128
})
vecs = [d["embedding"] for d in r.json()["data"]]
```

用 openai 官方 SDK：

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8199/v1", api_key="not-needed")
resp = client.embeddings.create(model="google/embeddinggemma-2", input=["你好", "hello"])
```

| 字段 | 说明 |
| --- | --- |
| `input` | 字符串 / 字符串数组 / 多模态对象数组 |
| `prompt_name` 或 `task` | 任务前缀，如 `SearchQuery`、`Document`、`Classification` |
| `dimensions` 或 `truncate_dim` | 输出维度：768 / 512 / 256 / 128（截断后自动重新归一化） |
| `encoding_format` | `float`（默认）或 `base64` |
| `normalize` | 是否 L2 归一化，默认 `true` |

### 2. 原生接口 `POST /embed`

额外返回维度、耗时、实际使用的前缀，适合调试：`{ "input": "测试文本", "dimensions": 256 }`

### 3. 多模态输入

```json
{
  "input": {
    "text": "商品图 <|image|> 防水跑步鞋",
    "image": "data:image/jpeg;base64,/9j/4AAQ...",
    "video": "D:\\clips\\demo.mp4"
  }
}
```

- `image` / `audio` / `video` 支持三种写法：**base64 data URL**、**http(s) 链接**、**本机绝对路径**
- 多个图片传数组：`"image": ["data:...", "D:\\a.png"]`
- `text` 里的 `<|image|>` / `<|video|>` / `<|audio|>` 是媒体占位符，
  **数量要和媒体数量一致**；没写或写少了会自动补齐。
- 也支持 OpenAI 的 content 数组写法（`{"type":"text"}` + `{"type":"image_url"}`）

### 4. 其它接口

| 接口 | 说明 |
| --- | --- |
| `GET /health` | 健康检查 |
| `GET /v1/models` | OpenAI 风格模型列表 |
| `GET /docs` | 交互式接口文档 |
| `POST /admin/shutdown` | 优雅退出（只允许本机调用） |
| `GET /api/ui-prefs` | 读回界面偏好（折叠状态、上次标签页、视图模式…） |
| `POST /api/ui-prefs` | 保存界面偏好（只收 `eg2-` 开头的键，写进 index.db 的 meta 表） |
| `GET /api/preview/{id}/pdf-pages` | PDF 按页给文字（页码准确），并说明能不能出图 |
| `GET /api/preview/{id}/pdf-page?page=N` | 把 PDF 第 N 页渲染成 PNG（需 PyMuPDF，否则返回 503 和安装提示） |
| `GET /api/preview/{id}/pdf-hits?q=关键词` | 在 PDF 文字层里按字面定位：第几页 + 页内位置（0~1 比例坐标，前端画高亮框用）|
| `GET /dify/info` | 自查：Dify 用的密钥对不对、有哪些知识库、模型就绪没（需 Bearer 密钥）|
| `POST /dify/retrieval` | **Dify「外部知识库」接口**（按 Dify 官方规范）：Dify 来调检索，数据不动 |

接自建 Dify 的用法（Dify 不接受外部向量，所以走"外部知识库"这条路，不重算、不迁移）：
在 `config.ini` 的 `[dify]` 段填一个密钥 → 重启服务 → Dify 侧「设置 → 外部知识库 API」
填地址和同一个密钥 → 创建知识库时选「外部知识库」，External Knowledge ID 填 `all`。
详见 `docs\部署与使用说明.md` 第十三章。

界面偏好为什么要存在服务端：浏览器本来写在 localStorage 里，但 `stop.bat` 用
`taskkill /f` 关窗口，Chrome 来不及落盘就被杀掉，重启后设置会全丢。服务端留一份，
打开页面时回灌，所以折叠状态、上次的标签页这些重启后还在。

### 5. 任务前缀（prompt_name）

检索类用非对称前缀（查询 `SearchQuery`，文档 `Document`）；分类/聚类/相似度这类对称任务
两侧用同一个前缀。**只有文本需要前缀，图片/音频/视频不加。**

| 场景 | prompt_name | 实际前缀 |
| --- | --- | --- |
| 搜索查询 | `SearchQuery` | `task: search result \| query: {内容}` |
| 文档/正文 | `Document` | `title: none \| text: {内容}` |
| 问答 | `QuestionAnswering` | `task: question answering \| query: {问题}` |
| 事实核查 | `FactChecking` | `task: fact checking \| query: {断言}` |
| 代码检索 | `CodeRetrieval` | `task: code retrieval \| query: {代码}` |
| 文本分类 | `Classification` | `task: classification \| query: {内容}` |
| 聚类 | `Clustering` | `task: clustering \| query: {内容}` |
| 相似度 | `SentenceSimilarity` | `task: sentence similarity \| query: {内容}` |
| 不加前缀 | `none` | 直接用原文 |

模型里还有 `BitextMining`、`STS`、`Retrieval`、`Summarization` 等前缀可用；
填了不存在的名字会返回 400，并在错误信息里列出全部可用值。

---

## 六、配置（config.ini）

> 下面这些项目**都可以在界面里直接改**（搜索页的「运行模式与设置」），推荐用界面；
> 本节只说明它们的含义和默认值。直接用文本编辑器改 `config.ini` 的话，需要重启服务生效。

### 服务与模型

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `server.host` | `127.0.0.1` | 改 `0.0.0.0` 可供局域网访问 |
| `server.port` | `8199` | 服务端口 |
| `server.open_browser` | `yes` | 启动后是否自动开浏览器 |
| `model.modality` | `full` | `full` / `text_image` / `text_audio` / `text`（仅文本最快最省） |
| `model.dtype` | `auto` | GPU 用 `bfloat16`，CPU 用 `float32`。**官方禁止 `float16`** |
| `model.device` | `auto` | `auto` / `cuda` / `cpu` |
| `model.batch_size` | `8` | 显存不足时调小到 2 或 1 |
| `encoding.default_prompt` | `none` | 默认任务前缀 |

### 文件搜索

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `search.index_dim` | `256` | 索引向量维度。256 质量几乎无损，存储/搜索快得多 |
| `search.chunk_chars` | `800` | 文本切块长度（字符） |
| `search.chunk_overlap` | `120` | 切块重叠 |
| `search.max_chunks_per_file` | `40` | 单文件最多索引多少块 |
| `search.max_file_mb` | `20` | 超过这个大小只索引文件名 |
| `search.batch_size` | `16` | 一次编码多少块，显存不足调小 |
| `search.index_images` | `yes` | 是否用向量索引图片内容 |
| `search.index_audio` / `index_video` | `no` | 音频/视频索引（较慢） |
| `search.auto_scan_on_start` | `yes` | 启动时自动增量扫描已添加的文件夹 |
| `search.ignore_dirs` | 一长串 | 扫描时跳过的目录名 |
| `search.skip_ext` | 一长串 | 完全不索引的文件后缀 |

### 下载

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `download.source` | `auto` | 模型来源：`auto` / `huggingface` / `modelscope` |
| `download.mirror` | 空 | HuggingFace 镜像，如 `https://hf-mirror.com` |
| `download.workers` | `4` | 下载并发数 |
| `download.proxy` | 空 | 只有必须走代理访问外网时才填 |

---

## 七、命令行用法（可选）

```bat
venv\Scripts\python.exe app\cli.py "今天天气怎么样"
venv\Scripts\python.exe app\cli.py --similar "苹果手机" "iPhone 购买建议"
venv\Scripts\python.exe app\cli.py --prompt SearchQuery --dim 256 "什么是极光"
venv\Scripts\python.exe app\cli.py --image data\samples\sample_image.png --text "一张图片"
```

---

## 八、安装过程说明（网络相关）

`setup.bat` 按这个顺序做五件事，每步都有兜底：

1. **找 Python**（需要 3.10 以上）
2. **建虚拟环境** `venv\`
3. **装 PyTorch**：先让 pip 从官方源装；中断了就自动改用自带的多连接断点续传下载器
   （`app\fetch_torch.py`，每 4 MB 一块、完成即落盘，重跑继续，不会白下）。
   依次尝试 `cu130 → cu128 → cpu`。
4. **装其余依赖**：优先用清华 PyPI 镜像，失败再回退官方源。
5. **下模型**：默认 `source = auto`，先试 HuggingFace，不行自动切到国内 **ModelScope**
   镜像（实测比直连 HuggingFace 快几十倍）。

脚本在**没配置代理时会显式禁用 Windows 系统代理**——这不是小优化：实际使用中，
系统代理会把直连速度拖慢 3-10 倍，而且对境外大文件几乎没有帮助。

---

## 九、常见问题

**Q：搜索找不到我明明有的文件？**
1）确认那个文件夹已经添加并索引完成（页面下方能看到文件数）；
2）换个说法试试，比如「发票怎么报销」↔「差旅报销流程」；
3）如果文件是扫描版 PDF（纯图片），正文抽不出来，只能用文件名搜；
4）大文件（默认 >20MB）只索引文件名，可调 `search.max_file_mb`。

**Q：索引很慢？**
首次索引要读文件 + 算向量。参考速度：纯文本约 **每秒几十到上百个片段**（GPU）。
只想快就把 `search.index_images` 关掉，或把 `modality` 改成 `text`。
大文件夹建议一次加一个、分批索引。

**Q：索引存在哪？换电脑怎么办？**
存在 `data\index\index.db`。把整个文件夹拷走即可；换机器后重新双击 `search.bat` 就能用。
如果改了 `search.index_dim`，服务会自动清空并要求重建索引。

**Q：会把我硬盘里的东西上传吗？**
不会。模型、索引、搜索全部在本机完成，`model.offline = yes` 且启动时设置了
`HF_HUB_OFFLINE=1`，运行期完全不联网。

**Q：索引会占用多少空间？**
每个文本片段约 1KB（含 256 维向量）。1 万个片段大约 10-20 MB，很小。

**Q：页面上出现「⚠ 定位文件不可用」，怎么修？**

先看 `status.bat` 里的这一项：

```
"desktop":{"elevated":false,"integrity":"Low","shell_running":true,"desktop_ok":false}
```

如果 `integrity` 是 **Low**，说明**项目文件夹被打了低完整性标签**，而且
`(OI)(CI)` 继承给了 `venv\Scripts\python.exe` 等所有文件：

```
icacls <项目目录>
    Mandatory Label\Low Mandatory Level:(OI)(CI)(NW)
icacls <项目目录>\venv\Scripts\python.exe
    Mandatory Label\Low Mandatory Level:(I)(NW)
```

Windows 的规则是 **进程完整性 = min(父进程完整性, 可执行文件标签)**，
所以不管你是不是管理员，只要 `python.exe` 带着 Low 标签，服务进程就是 Low，
而低完整性进程**不允许操作资源管理器**（`SHOpenFolderAndSelectItems` 返回
`0x80070005`，`explorer.exe` 直接以 `0xC0000142` 退出）。

**修法**：双击 **`fix-permissions.bat`**（会弹 UAC，点「是」）。

脚本只对**项目根目录**做一次 `icacls /setintegritylevel (OI)(CI)Medium`，
剩下的交给 Windows 的继承机制自动传播给所有子文件——所以是**秒级**完成。
（早期版本对 venv 里 3 万多个文件逐个递归处理，要跑十几分钟；现在不会了。）

脚本是**纯 ASCII** 的：中文 Windows 的系统代码页通常是 936，UTF-8 中文写在批处理里会让
cmd.exe 解析错位、把注释当命令执行。中文说明放在 `app\fix_integrity.py` 里，
脚本最后会调用它打印结果和后续步骤。

跑完后：

1. 双击 `stop.bat` 关掉服务；
2. 双击 `search.bat` 重新启动（关键：必须重启才会用上新的权限）；
3. 双击 `status.bat` 确认，应看到 `桌面交互 : 正常`。

为什么必须重启：进程的完整性级别在创建时就定了。修复前启动的服务仍然是 Low，
只有重新启动才会以 Medium 运行。

**Q：点了「定位文件」没反应 / 弹出 explorer.exe 报错？**

程序优先调用 Windows 的 Shell 接口 `SHOpenFolderAndSelectItems`，
让**已经在运行的**资源管理器去选中文件——不新起 explorer.exe 进程，
所以不会遇到 `explorer.exe 应用程序无法正常启动(0xc0000142)` 那种报错。

如果这个方法也被系统拒绝，页面底部会**明确说明原因和该怎么办**，不会假装成功。
常见原因是服务被后台工具拉起来了、没有桌面权限，解决办法：
双击 `stop.bat` 关掉，再双击 `search.bat` 重新启动。

实在不行还有两条路：点「预览」直接看内容，或点「下载到本机」存下来再打开。

**Q：索引会不会因为网络共享掉线被清空？**
不会。扫描时会对比「这次枚举到多少文件」和「索引里原有几个」：
如果这次几乎什么都没读到（例如共享断线、权限异常），程序**跳过删除**，
并在状态里提示「已跳过删除以免误清索引」。
只有点「全部重建索引」（明确要求以实际扫描为准）才会如实清掉已不存在的记录。

**Q：有些图片索引失败怎么办？**
图片先用 Pillow 解码（jpg/png/bmp/gif/webp/tiff/heic/avif 等），
Pillow 认不出来时再交给 **ffmpeg（PyAV）** 试一次，能覆盖相机 RAW、JPEG2000
等更多格式；仍失败的文件会**单独跳过**并写明原因，不会连累同批其它文件：

- 「无法识别的图片格式」→ 两套解码器都认不出（文件损坏或还没同步完）；
- 「文件是 0 字节，可能还没同步完」→ 云盘/同步盘的占位文件；
- 「没有读取权限」→ 网络共享断开或凭据失效，连上共享后点「增量更新」重试；
- 「文件已被移动或删除」→ 下次扫描会自动从索引里清掉。

失败的文件不会消失，点「增量更新」会重新尝试。

**Q：不同文件夹可以用不同模式/维度吗？**
可以。添加时多选文件夹并选定模式，之后每个文件夹还能单独「配置」。
搜索是统一入口，会把所有文件夹的结果混在一起排序，结果上还标了它属于哪种模式。
向量维度不同也能一起搜，不需要统一。

**Q：在界面上改了设置，需要重启吗？**
绝大多数不用。批大小、索引选项、切块长度这些**保存即生效**；换设备 / 精度 / 模态时，
界面会提示「需重载模型」，点一下按钮等 10-40 秒就好，进程不用重启。
只有「索引向量维度」需要重建索引（界面会二次确认）。

**Q：启动时提示模型加载失败？**
先确认 `models\embeddinggemma-2\model.safetensors` 存在（约 1.5GB）。缺失就重跑 `setup.bat`。
`selftest.bat` 可以帮你确认是哪一步出问题。

**Q：显存不足（CUDA out of memory）？**
把 `model.batch_size` 和 `search.batch_size` 调小；只做文本时把 `modality` 改成 `text`。

**Q：端口被占用？**
改 `config.ini` 的 `server.port`，或先跑一次 `stop.bat` 清理。

**Q：下载太慢 / 连不上 HuggingFace？**
`[download] source` 默认 `auto`，会自动改用 ModelScope 镜像；也可以直接写死 `source = modelscope`。

**Q：启动后那个黑窗口能关吗？**
`start.bat` / `search.bat` 会最小化一个标题为「EmbeddingGemma 2 服务窗口」的窗口，
那是服务本体，**不要关**；要停止请用 `stop.bat`。

**Q：`.cache\wheels` 里的 2GB 文件能删吗？**
能，那是 PyTorch 的安装包备份，删掉不影响使用。

**Q：怎么彻底卸载？**
先 `stop.bat`，然后直接删除整个 `embeddinggemma-2` 文件夹即可，系统里没有留下任何东西。

---

## 十、性能参考

> 下面是参考值（8 GB 显存级别的独显 + 四核 CPU），实际速度随硬件和文件数量不同。

| 场景 | 实测 |
| --- | --- |
| 服务启动（首次加载模型） | 约 36 秒 |
| 界面里热重载模型（换模态/精度/设备） | 8-40 秒 |
| 纯文本嵌入，768 维，2 条短句 | 0.66 秒 |
| 单张图片嵌入 | 0.51 秒 |
| 128 维截断单条文本 | 0.13 秒 |
| 一次文件搜索（几千个片段） | 0.1-0.3 秒 |
| 索引吞吐（文本，GPU） | 约 40-80 片段/秒 |
| 显存占用（全模态 bf16） | 约 2 GB |

---

## 十一、备份与恢复

### 1. 数据都在哪

扫描结果全部存在**一个 SQLite 文件**里：

| 文件 | 大小（你的数据） | 内容 |
| --- | --- | --- |
| `data\index\index.db` | 2.9 MB | 索引主库：文件夹列表、每个文件夹的模式、文件清单、向量片段 |
| `data\index\index.db-wal` | 3.9 MB | 预写日志（WAL） |
| `data\index\index.db-shm` | 0.1 MB | 共享内存索引 |
| `config.ini` | 3.7 KB | 全局设置（服务端口、设备、模态等） |

**要备份的就是这些，加起来不到 10 MB。**

不需要备份的：

| 目录 | 大小 | 为什么不用备份 |
| --- | --- | --- |
| `models\` | 1.4 GB | 模型本体，删了重新运行 `setup.bat` 就能下回来 |
| `.cache\` | 2.2 GB | 下载缓存，同上 |
| `venv\` | 3.7 GB | Python 依赖，`setup.bat` 会重建 |

### 2. ⚠ 千万别直接复制 index.db

数据库跑在 **WAL 模式**：最近的改动可能还留在 `index.db-wal` 里没合并进主库。
只拷 `index.db` 会**丢掉这部分数据** —— 表现为恢复后"最近扫的文件不见了"。
你的机器上 WAL（3.9 MB）比主库（2.9 MB）还大，直接拷主库会丢一半以上。

### 3. 正确做法：用 backup.bat

```
backup.bat                  立刻备份一份（服务开着也能跑）
backup.bat list             列出已有备份
backup.bat restore <文件名> 从备份恢复
```

它用的是 SQLite 官方的**在线备份接口**，会做一致性快照，
把 WAL 里的内容一起合并进去，所以服务运行中备份也是安全的。每份备份包含：

```
backups\index-20260101-120000.db    一致性快照（几 MB）
backups\index-20260101-120000.ini   config.ini 的副本
backups\index-20260101-120000.txt   说明：时间、文件夹列表、文件/片段数量
```

自动只保留最近 **20 份**，不会越攒越多。

### 4. 恢复（双击选文件，不用敲键盘）

**双击 `restore.bat`** 就行，全程点鼠标：

1. 弹出 Windows **文件选择框**，已经定位在 `backups\` 目录，选那个 `.db`；
2. 弹出**确认框**，上面写着这份备份的内容（时间、文件夹列表、文件数、片段数）；
3. 如果服务正在运行，会问「现在自动停止吗」——点是，它就自己停；
4. 恢复完成，提示里告诉你结果；
5. 再问「现在启动服务吗」——点是，它会开一个新窗口把服务拉起来。

恢复时会**先把现有索引另存为** `index-before-restore-<时间>.db`，
并且自动清掉旧的 `-wal` / `-shm`（否则会污染新库），所以不用怕操作失误。

命令行方式（给脚本用，需要服务已停止）：

```
backup.bat restore index-20260101-120000.db
```

对话框都强制置顶，不会跑到后台去。如果系统没有图形界面（tkinter 不可用），
会自动退化成「列出编号、输入序号」的文字菜单。

### 5. 手动备份（不想用脚本时）

停掉服务后，直接整体复制这两个东西即可：

```
data\index\     （整个目录，三个文件一起）
config.ini
```

### 6. 什么时候该备份

- 刚扫完一个大文件夹、结果满意时；
- 要改文件夹模式（改向量维度会触发重建）之前；
- 要把项目搬到别的机器时（只有这几 MB 需要带过去）。
