# 放到 GitHub 上（不含数据）+ 以后怎么更新

> 目标：仓库里**只有代码**（约 4.7 MB / 141 个文件），你的索引库、模型、配置、密钥一律不出本机；
> 以后改了代码推上去，别的机器（或服务器）拉下来就能用。
>
> 适用：v3.5.3 及以后。第一次做下面的「一、二」，之后每次只做「三」。

---

## 〇、⚠ 发布方式：导出 + 脱敏，**不改本机**

> 2026-11-02 项目主要求："docs/ 全部文档里也有本机信息，要求也脱敏一下发布，**本机文件不要受影响**"

所以发布走**两个目录**：

| 目录 | 作用 | 会不会被改 |
|---|---|---|
| `D:\lingxi` | 你日常开发的本机仓库（真实 IP、真实路径、真名） | **一个字节都不动** |
| `D:\lingxi-publish` | 公开仓库的副本（已脱敏），真正 push 的是它 | 每次重新生成 |

一条命令搞定（先设 token 环境变量）：

```powershell
$env:GH_TOKEN = "github_pat_xxx"      # 只需 Contents: Read and write
.\venv\Scripts\python.exe run\git_publish.py --push
```

它做四件事：

1. `git ls-files` 列出**已跟踪**文件（数据/模型/配置天然不在里面）
2. 逐文件替换敏感串（内网 IP、域名、NAS 名、真名、账号、私人路径）
3. **自检**：残留敏感串 → 直接中止，不提交；顺便 `py_compile` + `node --check`
4. 在副本里 `git commit` 并推送（token 只用于这一次 push，不写进配置）

> [!warning] token 权限
> 细粒度 token（`github_pat_...`）必须给 **Contents: Read and write**，并选中这个仓库。
> 只读 token 会报 `403 Permission to ... denied`（API 里 `push=true` 不代表 token 有写权限）。

---

## 一、仓库里有什么、没有什么

| 会进仓库（约 4.7 MB / 141 个文件） | 不进仓库 |
|---|---|
| `app/` 全部代码（含前端 html/js/css/图标） | `data/`（索引库、聊天记录、附件，**5.7 GB**） |
| `docs/` 全部文档（手册、更新日志、交接说明） | `models/`（嵌入模型，**25 GB**） |
| `run/` 只挑运行必需的 7 个（重启、快照、发布、部署脚本、OCR 桥） | `venv/`（Python 环境，**6.7 GB**） |
| 根目录的 `.bat`（启动/停止/备份/装 OCR…） | `backups/`（**78 GB** 备份） |
| `requirements.txt`、`README.md`、`AGENTS.md` | `config.ini`（**含 API Key / Dify 密钥 / 同步密码**） |
| `config.example.ini`（脱敏示例） | `run/deploy/keys/`（SSH 私钥）、`askpass.cmd` |
| `.gitignore` / `.gitattributes` | `logs/`、`output/`、`dist/`、`.cache/`、`.browser/`、`face-lab/` |

判断标准写在 `.gitignore` 里；改完可以随时自检：

```powershell
git check-ignore -v data config.ini run/deploy/keys models
# 每行都能打印出"被哪条规则忽略"就对了
```

---

## 二、第一次放上 GitHub（5 分钟）

### 1. 先在 GitHub 网页建一个空仓库

- 名字随意，比如 `lingxi`；**不要**勾「Add a README / .gitignore / license」（勾了会多一个提交，推的时候要合并）
- 公开或私有都行；私有的话后面拉取的机器要配 token

### 2. 本机执行（仓库已经 `git init` 好了，只差 remote）

```powershell
cd D:\lingxi

# 把地址换成你自己的
git remote add origin https://github.com/<你的用户名>/lingxi.git

# 首次推送
git push -u origin main
```

- HTTPS 推会弹浏览器/要 token：GitHub 现在**不收密码**，去
  Settings → Developer settings → Personal access tokens 建一个 `repo` 权限的 token，当密码用
- 想免输：`git config --global credential.helper manager`（Windows 会记住）

> 如果这台机器上 `gh`（GitHub CLI）没装，用上面网页 + token 的方式最省事。

---

## 三、以后怎么"更新"

### 3.1 你在本机改完 → 推到仓库

```powershell
cd D:\lingxi

# 1) 改完先照老规矩：拍快照 + 重启验证（项目约定，别省）
.\venv\Scripts\python.exe run\snapshot.py "这次改了什么"
.\venv\Scripts\python.exe run\rehash_static.py
.\venv\Scripts\python.exe run\restart_server.py --no-ui

# 2) 看看到底改了哪些文件（确认没有 data/、config.ini 混进来）
git status --short

# 3) 提交 + 推
git add -A
git commit -m "fix: 修了 xxx（v3.5.4）"
git push
```

> ⚠ 提交前瞄一眼 `git status`：如果出现 `data/`、`config.ini`、`*.db`、`run/deploy/keys/`，
> **立刻停下**，说明 `.gitignore` 被改坏了（`git check-ignore` 复查）。

### 3.2 别的机器 / 服务器拉最新代码

**Windows（本机另一台）**：双击 `run\git_update.bat`
它会：停服务 → `git pull --ff-only` → 重新生成前端哈希 → 启动。

**Linux 服务器**（装了这套服务的那台）：

```bash
cd /home/lingxi
sudo systemctl stop lingxi
git pull --ff-only            # 首次要先 git clone 或用 rsync 铺一份代码
venv/bin/python run/rehash_static.py
sudo systemctl start lingxi
sleep 30 && curl -s localhost:8199/health | grep -o '"version":"[^"]*"'
```

> 服务器上**不要**有 `run/deploy/sync_db.py`（它是"本机/服务器"的身份标记）。
> 用 git 拉下来时它会被带过去 → 拉完记得 `rm run/deploy/sync_db.py`，
> 或者干脆在服务器上用 `run/deploy/deploy_app.py` 的老办法（只传 app/，不碰标记）。

### 3.3 新电脑第一次从仓库装起来

```powershell
git clone https://github.com/<你>/lingxi.git D:\lingxi
cd D:\lingxi
copy config.example.ini config.ini     # 然后按需填 Key
setup.bat                              # 建 venv、装依赖、下模型
start.bat
```

`data/` 和 `models/` 不会跟着仓库来 —— 索引要自己重建（或从旧机器拷 `data/index/index.db`）。

---

## 三点五、✅ 首次发布记录（2026-11-02）

第一次真正推上去的结果，留个凭据，以后再发可以对照：

| 项 | 值 |
|---|---|
| 仓库 | <https://github.com/yqdnsjs-qq/lingxi>（public） |
| 分支 / 提交 | `main` · **`618d1ef`**「灵犀 v3.5.3」（单提交） |
| 文件数 | **142** 个（约 4.7 MB） |
| 脱敏 | 34 个文件被替换；自检 **残留 0 命中** |
| 代码检查 | Python 全量 `compileall` + 前端 `node --check` 全过 |
| 远端抽查 | 取回 `README.md`、`docs/安装部署手册-v3.5.md`、`docs/交接说明-20261022-v3.0.md`，敏感命中均为 **0** |
| 本机 | 工作区干净，真实 IP / 路径 / 真名**原样保留**（18 个文件仍含真实内网地址，证明脱敏只发生在发布副本里） |

> [!note] 提交信息只写版本号
> 第一版提交信息是「灵犀 v3.5.3（公开版，已脱敏）」，GitHub 文件列表里**每个文件后面都跟着这句**，很碍眼。
> 已把发布副本的历史压成一条、改成 `灵犀 v3.5.3` 后强推（旧的 `4a92560` / `17ead34` 变成游离提交，
> 内容一样是脱敏过的，没有信息泄露）。脚本里也改成从 `app/version.py` 取版本号当提交信息。

> [!note] 当时卡了两回，记下来省得再踩
> ① `.gitignore` 写了行尾注释 → 整行失效，`.cache`/`.browser` 被加进来；
> ② token 只有读权限 → `403 Permission ... denied`。细粒度 token 必须给 **Contents: Read and write**。

---

## 四、几个已经踩过的坑

| 现象 | 原因 | 怎么办 |
|---|---|---|
| 推上去 100 MB+ / 推不动 | `data/`、`models/`、`backups/` 被加进来了 | `.gitignore` 里对应行别删；已经提交过的用 `git rm -r --cached data` 再提交 |
| 服务器上 `lingxi.service` 报 `bad interpreter` / 不启动 | 文件被转成 CRLF | `.gitattributes` 已声明 `*.sh`、`*.service` 用 LF；真出问题 `dos2unix run/deploy/lingxi.service` |
| Windows 下 `.bat` 中文乱码 | 保存成了 UTF-8，但文件里是 `chcp 936` | 保持 GBK 保存；`.gitattributes` 里 `*.bat -text` 就是防这个 |
| `config.ini` 被提交了 | 它被误加进暂存区 | `git rm --cached config.ini`，确认 `.gitignore` 有 `config.ini` |
| 拉下来页面还是老的 | 前端文件名带哈希 | Ctrl+F5；右上角版本徽标变黄就点它 |

---

## 五、想放"发行版"（可选）

`release\*.zip` 不进仓库（约 2 MB×3，且每次都变）。要发版本就在 GitHub 上
**Releases → Draft a new release**，把 `run\build_release.py` 打出来的三个 zip 拖进去，
tag 用 `v3.5.3` 这种，和 `app/version.py` 里的版本号一致即可 ✓
