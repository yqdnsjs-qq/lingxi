// 版本标记：F12 Console 里能看到，方便判断"你打开的是不是最新页面"
// （改完前端一定要 Ctrl+F5 硬刷新，普通刷新有时还是旧 JS）
const EG2_BUILD = '2026-11-02-17:20';
const EG2_VER = '3.5.5';
try { console.log('%c灵犀 v' + EG2_VER + ' build ' + EG2_BUILD, 'color:#5b9dff;font-weight:bold'); } catch (e) {}

/* ---------------- 🔐 界面缓存按账号隔离（2026-10-21）----------------
 * 项目主抓到的 bug："第一次点击编辑 API 后，会弹出管理员填的 API，还是能用"
 * 根因：外部模型的**地址/密钥/预设**存在浏览器 localStorage（eg2-rmapilist 等），
 *       服务端把 /api/ui-prefs 的 eg2-rm* 过滤掉了，但同一浏览器里的旧缓存还在 ✗
 * 做法：页面一打开就同步问一句"你是谁"，跟上次不同就把 eg2-rm* / eg2-vl* 清掉。
 *   只清敏感那两类（折叠状态之类保留，无害）；
 *   用同步 XHR 是为了抢在后面所有 localStorage 读取之前（只发一次，几十毫秒）✓
 *
 * ⚠ 2026-10-22 项目主又抓到一个："管理员填的 Dify，用户端也能看到使用"
 *   根因就是这里清漏了 —— 白名单是 ['eg2-rm','eg2-vl']，**没有 eg2-dify** ✗
 *   于是管理员填的 Dify 库留在同一个浏览器的 localStorage 里，
 *   换个账号登录照样显示、照样能查（数据其实是管理员那个库的）✗
 *   现在把 'eg2-dify' 也加进敏感名单 ✓（个人连接个人的，除非以后专门做共享）
 */
(function guardLocalCreds() {
  try {
    var x = new XMLHttpRequest();
    x.open('GET', '/api/auth/me', false);
    x.send(null);
    var d = JSON.parse(x.responseText || '{}');
    var uid = String((d && d.user && d.user.id) || 0);
    var prev = String(localStorage.getItem('eg2-owner') || '0');
    if (prev !== uid) {
      var sensitive = ['eg2-rm', 'eg2-vl', 'eg2-dify'];
      Object.keys(localStorage).forEach(function (k) {
        if (sensitive.some(function (p2) { return k.indexOf(p2) === 0; })) {
          localStorage.removeItem(k);
        }
      });
      localStorage.setItem('eg2-owner', uid);
    }
  } catch (e) { /* 拿不到就照常跑，别把页面搞挂 */ }
})();

/* 任何未捕获的错误都直接显示在页面上，不用按 F12。
   之前「一直显示连接中」就是脚本某处抛错、而控制台很安静。 */
(function () {
  var seen = [];
  function show(what, msg, extra) {
    var line = what + '：' + msg + (extra ? '\n    ' + extra : '');
    if (seen.indexOf(line) >= 0) return;
    seen.push(line);
    var bar = document.getElementById('eg2-errbar');
    var body = document.getElementById('eg2-errbody');
    if (!bar || !body) return;
    bar.style.display = 'block';
    body.textContent = seen.join('\n\n');
  }
  window.addEventListener('error', function (e) {
    show('脚本错误', (e.message || '未知'),
         (e.filename || '') + (e.lineno ? ':' + e.lineno : ''));
  });
  window.addEventListener('unhandledrejection', function (e) {
    var r = e.reason;
    show('异步错误', (r && (r.message || r)) + '', '');
  });
  window.addEventListener('DOMContentLoaded', function () {
    var c = document.getElementById('eg2-errclose');
    if (c) c.addEventListener('click', function () {
      document.getElementById('eg2-errbar').style.display = 'none';
    });
  });
})();
;
/* ---------------- 界面偏好：本地 + 服务端各存一份 ----------------
   只写 localStorage 是不够的：stop.bat 用 taskkill /f 关浏览器窗口，
   Chrome 来不及把 localStorage 落盘就被杀了 —— 重启后折叠状态、标签页、
   视图模式、外部模型配置全丢，用户得重设一遍。
   所以这里把 eg2-* 的偏好也存到服务端（/api/ui-prefs → 数据库 meta 表）：
   · 打开页面先读 localStorage（快，界面不闪）
   · 再从服务端回灌（防丢），本地有服务端没有的顺手补传
   · 拦截 localStorage.setItem：所有 eg2-* 的写入自动镜像到服务端，
     不用去改几十处调用点
   注意：这段必须在主脚本之前执行，bindCardFolding 也要在这之前定义。 */
(function () {
  const PREFIX = 'eg2-';
  const cache = {};              // 服务端那一份
  const pending = {};            // 等着发给服务端的
  let pushTimer = null;
  let rawSetItem = null;

  /* 折叠状态的 key 不能带数量：卡片标题是「搜索结果（12）」、
     人物页是「已命名（3）」，数量一变 key 就变了，折叠状态就白丢。 */
  function foldKey(text) {
    return 'eg2-fold-' + String(text || '')
      .replace(/[\d０-９（）()\s·、,，.。:：]/g, '').slice(0, 16);
  }

  function flush() {
    pushTimer = null;
    const values = {};
    for (const k in pending) { values[k] = pending[k]; delete pending[k]; }
    if (!Object.keys(values).length) return;
    try {
      fetch('/api/ui-prefs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ values }),
        keepalive: true,
      }).catch(function () { /* 服务端存不上也不影响这次使用 */ });
    } catch (e) { /* 忽略 */ }
  }

  function queue(key, value) {
    pending[key] = value;
    if (pushTimer) return;
    pushTimer = setTimeout(flush, 150);       // 攒一下，连点几次只发一次
  }

  try {
    rawSetItem = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      rawSetItem.call(this, key, value);
      if (this === window.localStorage && String(key).indexOf(PREFIX) === 0) {
        queue(String(key), String(value));
      }
    };
  } catch (e) { /* 忽略 */ }

  // 页面切走/关闭时，尽量把还没发出去的补上
  document.addEventListener('visibilitychange', function () { if (document.hidden) flush(); });
  window.addEventListener('pagehide', flush);

  function localKeys() {
    const out = {};
    try {
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k && k.indexOf(PREFIX) === 0) out[k] = localStorage.getItem(k);
      }
    } catch (e) { /* 忽略 */ }
    return out;
  }

  window.EG2Prefs = {
    foldKey: foldKey,
    get: function (key, dflt) {
      try {
        const v = localStorage.getItem(key);
        if (v !== null) return v;
      } catch (e) { /* 忽略 */ }
      return cache[key] !== undefined ? cache[key] : dflt;
    },
    /* 打开页面就调：拿服务端那份回灌 localStorage，再重新应用一次界面状态。 */
    load: async function () {
      let prefs = {};
      try {
        const r = await fetch('/api/ui-prefs', { cache: 'no-store' });
        const j = await r.json();
        prefs = (j && j.prefs) || {};
      } catch (e) {
        return false;                         // 拿不到就只靠 localStorage
      }
      Object.assign(cache, prefs);
      const push = {};
      for (const k in prefs) {
        // 用原始 setItem 回灌：不要再镜像回服务端，免得来回打架
        try { if (rawSetItem) rawSetItem.call(localStorage, k, prefs[k]); } catch (e) { /* 忽略 */ }
      }
      // 只把「本地有、服务端没有」的补传上去（第一次升级上来时用得到），
      // 服务端已有的不要再发回去，省得每次开页面都白写一遍
      const local = localKeys();
      for (const k in local) if (!(k in prefs)) push[k] = local[k];
      if (Object.keys(push).length) {
        for (const k in push) pending[k] = push[k];
        flush();
      }
      if (typeof window.applyHydratedPrefs === 'function') window.applyHydratedPrefs();
      return true;
    },
  };
})();

/* 折叠功能。
   ① 设置页：每张卡片可折叠（卡片太多，一屏放不下）
   ② 人物页：点「已命名 / 未命名」标题，折叠各自的分区
   状态记在 localStorage，同时由上面那层镜像到服务端。
   注意：这段必须在主脚本之前执行，否则初始化调用时函数还不存在。 */
function bindCardFolding() {
  // ---- 设置页的卡片折叠 ----
  document.querySelectorAll('.card').forEach(function (card) {
    var h2 = card.querySelector(':scope > h2');
    if (!h2) return;
    if (!h2.dataset.foldKey) h2.dataset.foldKey = window.EG2Prefs.foldKey(h2.textContent);
    // 折叠时在标题后面留一行摘要（原来是「一条什么都没有的空横条」，看不出是什么卡）
    if (!h2.dataset.sum) {
      var src = card.getAttribute('data-summary');
      if (!src) {
        var hint = card.querySelector(':scope > .hint');
        src = hint ? hint.textContent : '';
      }
      src = String(src || '').replace(/\s+/g, ' ').trim();
      if (src) h2.dataset.sum = src.length > 56 ? src.slice(0, 56) + '…' : src;
    }
    if (!h2.dataset.foldBound) {
      h2.dataset.foldBound = '1';
      h2.addEventListener('click', function (ev) {
        if (ev.target.closest('button,input,select,a,label')) return;
        var folded = !card.classList.contains('folded');
        card.classList.toggle('folded', folded);
        try { localStorage.setItem(h2.dataset.foldKey, folded ? '1' : '0'); }
        catch (e) { /* 忽略 */ }
      });
    }
    // 每次都对一遍状态：动态渲染出来的卡片能立刻跟上，不用等下一次轮询
    card.classList.toggle('folded', window.EG2Prefs.get(h2.dataset.foldKey) === '1');
  });

  // ---- 人物页的分区折叠（已命名 / 未命名）----
  document.querySelectorAll('.pgroup').forEach(function (g) {
    var grid = g.nextElementSibling;
    if (!grid || !grid.classList.contains('pgrid')) return;
    if (!g.dataset.foldKey) {
      // key 里不能带数量（标题是「已命名（3）」），否则命名一个人就换 key、折叠状态全丢
      var head = (g.textContent || '').trim();
      g.dataset.foldKey = 'eg2-pgfold-' + (/已命名/.test(head) ? 'named'
        : (/未命名/.test(head) ? 'unknown' : head.slice(0, 8)));
    }
    if (!g.dataset.foldBound) {
      g.dataset.foldBound = '1';
      g.style.cursor = 'pointer';
      g.style.userSelect = 'none';
      g.title = '点一下折叠 / 展开这一组';
      var arrow = document.createElement('span');          // 箭头
      arrow.textContent = '\u25BE';
      arrow.style.cssText = 'display:inline-block;width:14px;opacity:.65;'
        + 'transition:transform .15s';
      g.insertBefore(arrow, g.firstChild);
      g.addEventListener('click', function () {
        var folded = g.dataset.folded !== '1';
        try { localStorage.setItem(g.dataset.foldKey, folded ? '1' : '0'); }
        catch (e) { /* 忽略 */ }
        applyGroupFold(g, grid);
      });
    }
    // 列表一重渲染就立刻恢复折叠态（原来要等 2.5 秒，看着像闪了一下）
    applyGroupFold(g, grid);
  });
}

function applyGroupFold(g, grid) {
  var folded = window.EG2Prefs.get(g.dataset.foldKey) === '1';
  grid.style.display = folded ? 'none' : '';
  var arrow = g.querySelector(':scope > span');
  if (arrow) arrow.style.transform = folded ? 'rotate(-90deg)' : '';
  g.dataset.folded = folded ? '1' : '0';
}

/* 页面上卡片是动态渲染的，隔几秒重绑一次（已绑的会跳过，但状态每次都对一遍） */
function bindCardFoldingLazy() {
  bindCardFolding();
  setInterval(bindCardFolding, 2500);
}

/* 服务端偏好回灌完之后再应用一次：折叠态 + 上次的标签页。 */
window.applyHydratedPrefs = function () {
  try { bindCardFolding(); } catch (e) { /* 忽略 */ }
  try {
    // ⚠ 2026-10-22：只恢复**一次**。以前偏好每回灌一次就抢一次标签页，
    //   用户手动点去「搜索」还会被拽回上次那页（表现像"点了没反应"）✗
    var tab = window.__tabRestored ? '' : window.EG2Prefs.get('eg2-tab', '');
    var on = document.querySelector('#tabbar button.on');
    if (tab && (!on || on.dataset.tab !== tab) && typeof window.selectTab === 'function') {
      window.__tabRestored = true;
      window.selectTab(tab);
    } else if (!window.__tabRestored) {
      window.__tabRestored = true;      // 没偏好也算恢复过，后面别再抢
    }
  } catch (e) { /* 忽略 */ }
  // 知识库问答那个下拉是页面初始化时渲染的，那一刻服务端偏好还没回来 ——
  // 不在这里重渲染一次，就看不到「外部 API」那一组（实测踩过）。
  try { if (typeof renderKbEngineOptions === 'function') renderKbEngineOptions(); }
  catch (e) { /* 忽略 */ }
  try { if (typeof renderApiList === 'function') renderApiList(); }
  catch (e) { /* 忽略 */ }
  // ★ 2026-10-22：Dify 卡片也是同样的坑 —— 初始化那一刻 localStorage 还是空的
  //   （服务端那份还没回灌），于是卡片渲染成"还没有接任何 Dify 知识库"，
  //   之后**再也不会重画** → 明明配了库却看着像没配 ✗
  try { if (typeof renderDifyList === 'function') renderDifyList(); }
  catch (e) { /* 忽略 */ }
};

// 立刻去拿服务端那份（放在主脚本之前，尽量赶在初始化之前回来）
window.EG2Prefs.load();
;
const $ = (s) => document.querySelector(s);
// 未登录（401 + need_login）→ 统一跳登录页，别让页面停在一堆报错上
function goLogin() {
  if (window.__goingLogin) return;
  window.__goingLogin = true;
  location.href = '/static/login.html?next=' + encodeURIComponent(location.pathname);
}

const api = (p, opt) => fetch(p, opt).then(async (r) => {
  const t = await r.text();
  let j = null; try { j = JSON.parse(t); } catch (e) { j = { detail: t }; }
  if (!r.ok && r.status === 401 && j && j.need_login) {
    goLogin();                       // 未登录 → 跳登录页（页面不用继续渲染）
    throw new Error('请先登录');
  }
  if (!r.ok) {
    // ⚠ FastAPI 的 422 里 detail 是**数组**，直接 new Error(数组) 会变成
    //   "[object Object]" —— 等于什么信息都没有 ✗（项目主截图就是这个）
    let why = '';
    const d = j && j.detail;
    if (typeof d === 'string') {
      why = d;
    } else if (Array.isArray(d)) {
      why = d.map((x) => (x && (x.msg || x.message)) || JSON.stringify(x)).join('；');
    } else if (d && typeof d === 'object') {
      why = d.msg || d.message || JSON.stringify(d);
    }
    throw new Error(why || t || ('HTTP ' + r.status));
  }
  return j;
});
const post = (p, b, m) => api(p, { method: m || 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(b || {}) });
const CHAN_NAME = { semantic: '语义', literal: '文件名', face: '人脸',
  person: '人物', name_hit: '名称命中' };
function chanBadges(ch) {
  if (!ch) return '';
  const keys = Object.keys(ch);
  if (!keys.length) return '';
  return '<span class="chans">' + keys.map((k) =>
    `<span class="chan c-${k}">${CHAN_NAME[k] || k}</span>`).join('') + '</span>';
}
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtSize = (n) => n >= 1 << 30 ? (n / (1 << 30)).toFixed(2) + ' GB'
  : n >= 1 << 20 ? (n / (1 << 20)).toFixed(1) + ' MB'
  : n >= 1024 ? (n / 1024).toFixed(1) + ' KB' : n + ' B';
const fmtTime = (t) => t ? new Date(t * 1000).toLocaleString('zh-CN', {hour12:false}) : '-';
const fmtWhen = (t) => {
  if (!t) return '尚未扫描';
  const d = new Date(t * 1000), now = new Date();
  const hm = `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
  const sameDay = d.toDateString() === now.toDateString();
  return sameDay ? hm : `${d.getMonth() + 1}-${String(d.getDate()).padStart(2,'0')} ${hm}`;
};
const KIND = {text:'文本', pdf:'PDF', docx:'Word', xlsx:'Excel', pptx:'PPT', image:'图片', audio:'音频', video:'视频', other:'其它'};
const FIELD_LABEL = {
  index_text: '索引正文', index_images: '索引图片内容', index_audio: '索引音频内容',
  index_video: '索引视频内容', index_dim: '向量维度', chunk_chars: '切块长度(字符)',
  chunk_overlap: '切块重叠', max_chunks_per_file: '单文件片段上限',
  max_file_mb: '正文读取上限(MB)', batch_size: '索引批大小',
  use_kb: '作为知识库', exclude: '排除规则',
};
const FIELD_HELP = {
  // 2026-10-18 项目主说"都搞糊涂了" → 这几项按**代码里的真实行为**重写 ✓
  // （行内会显示，鼠标悬停也能看到同样的话 ✓）
  index_text: '读文件的**内容**：PDF / Word / Excel / PPT / txt / md 的正文，'
            + '切块后能用内容里的词搜到。\n'
            + '⚠ 文件名**永远**都会被索引，跟这项无关 —— 就算关掉，也能按文件名搜到 ✓',
  index_images: '一次做两件事：\n'
              + '① 把图片编码成向量 → 能按画面内容搜（比如搜"海边日落"）\n'
              + '② **人脸识别** → 检出的人脸进「人物」页，可命名\n'
              + '需要全局带视觉编码器（现在只要有文件夹需要，就会自动加载 ✓）',
  index_audio: '把音频编码成向量 → 能按声音内容搜（环境音、音乐风格这类）。\n'
             + '注意：**不是**先转成文字再索引（项目里没有语音转文字那一步）',
  index_video: '视频按**抽帧**处理：每一帧都当图片来编码。\n'
             + '所以很慢、很占空间 —— 只对少数需要的文件夹开 ✓',
  index_dim: '128 最省，256 质量几乎无损；不同文件夹可用不同维度',
  use_kb: '打上"问答只从这里找"的标记：\n'
        + '· 勾了 → 「知识库问答」会在这些文件夹里检索\n'
        + '· 没勾 → 不参与问答，但**搜索页照样能搜到** ✓\n'
        + '会自动套用推荐配置：768 维、切块 1000 字、单文件上限 120 片（你没手动改过的话）。'
        + '照片文件夹不要勾',
  exclude: '只对这个文件夹生效。例：.tmp（按扩展名）· node_modules（按名字，任意层级）'
         + ' · 备份/*（按路径）· *草稿*（通配符）',
};

let FOLDER_SCHEMA = null, FOLDER_MODES = [], selected = new Set();
let mergeSource = null;      // 正在「选择合并目标」的源人物 id
let picked = new Set(), pickPath = '', cfgTargets = [];

/* ---------------- 索引模式 ↔ 开关 联动 ----------------
   项目主 2026-10-18："选了索引模式，下面相应开关应该联动，这样更加直观"
   以前：模式显示「文字 + 图片」，下面两个开关却是关的 ✗ → 看着自相矛盾。
   现在：选了具体预设就自动同步开关 ✓；「跟随全局设置」「自定义」则**不动**（那是让你自己勾的）✓ */
async function loadModeSwitches() {
  if (window.__modeSwitches) return window.__modeSwitches;
  try {
    const d = await api('/api/folder-categories');
    window.__modeSwitches = d.mode_switches || {};
  } catch (e) {
    window.__modeSwitches = {};        // 拉不到就不联动，别报错 ✓
  }
  return window.__modeSwitches;
}

function bindModeLinkage() {
  const body = $('#cfg-body');
  if (!body) return;
  // ⚠ 真实页面里「索引模式」下拉的 id 是 **cfg-mode** ✗（不是 data-fkey="mode" ✗）——
  //   我第一版按 data-fkey 找 → 拿到 null → 直接 return → **完全没联动** ✗
  //   （jsdom 测试里我自己造了带 data-fkey 的下拉，结构跟真实的不一样，所以没测出来 ✗）
  const modeSels = body.querySelectorAll('#cfg-mode');
  if (!modeSels.length) return;
  const apply = () => {
    modeSels.forEach((sel) => {
      const sw = (window.__modeSwitches || {})[sel.value];
      if (!sw) return;                 // 跟随全局 / 自定义 / （不变）→ 不动 ✓
      ['index_text', 'index_images', 'index_audio', 'index_video'].forEach((k) => {
        const box = body.querySelector(`[data-fkey="${k}"]`);
        if (box && typeof sw[k] === 'boolean') box.checked = sw[k];
      });
    });
  };
  modeSels.forEach((sel) => { sel.onchange = apply; });
  apply();                             // 已有缓存先对齐 ✓
  // 联动表拿到后再对齐一次；再补一个延后调用，覆盖"先绑定、后回填 mode 值"的顺序 ✓
  loadModeSwitches().then(() => { if (modeSels[0].isConnected) apply(); });
  setTimeout(() => { if (modeSels[0].isConnected) apply(); }, 60);
}

async function refreshHealth() {
  try {
    const h = await api('/health');
    $('#status').textContent = '运行中 · ' + h.device + ' / ' + h.dtype;
    $('#status').className = 'badge ok';

    // 桌面定位能不能用，直接写在页面上，不用等点了才发现
    const d = h.desktop || {};
    window.__desktopOk = d.desktop_ok !== false;
    window.__desktopWhy = d.desktop_ok === false
      ? `当前服务进程的完整性级别是 ${d.integrity}，Windows 不允许它操作资源管理器，` +
        '所以「定位文件 / 用默认程序打开」用不了。改用「预览」「复制路径」「下载到本机」。'
      : '';
    const warn = $('#desktop-warn');
    if (d.desktop_ok === false) {
      warn.hidden = false;
      warn.textContent = '⚠ 定位文件不可用';
      warn.title =
        `当前服务进程的完整性级别是 ${d.integrity}，资源管理器` +
        `${d.shell_running ? '在运行' : '未运行'}。\n` +
        'Windows 不允许受限进程操作桌面，所以「定位文件 / 用默认程序打开」会失败。\n' +
        '请双击 stop.bat 关掉服务，再在桌面上双击 search.bat 重新启动。\n' +
        '在此之前可以用「预览」「复制路径」「下载到本机」。';
    } else {
      warn.hidden = true;
    }
  } catch (e) {
    $('#status').textContent = '服务未就绪';
    $('#status').className = 'badge err';
  }
}

/* 把文件夹按父目录分组显示。
   为什么要分组：NAS 上一个目录下挂十几个相册，平铺出来一屏全是路径，
   既看不全也点不准。按父目录收成一组，组内只显示叶子名，清爽很多。
   注意：这只是**界面分组**，数据上每个文件夹仍然独立
   （各有自己的模式、排除规则、知识库标记），互不影响。 */
function groupFolders(list) {
  const groups = new Map();
  for (const f of list) {
    const p = String(f.path || '').replace(/\//g, '\\');
    const cut = p.lastIndexOf('\\');
    const parent = cut > 1 ? p.slice(0, cut) : '';
    if (!groups.has(parent)) groups.set(parent, []);
    groups.get(parent).push(f);
  }
  return groups;
}

function folderRowHtml(f, showFull) {
  const leaf = showFull ? f.path
    : String(f.path || '').split(/[\\/]/).filter(Boolean).pop() || f.path;
  // ★ 「🔗 路径」只在本机版（建库机）显示 ✓
  //   项目主 2026-10-20："这个文件夹自定义映射服务器地址功能只需添加到本机版即可，
  //   服务器版上不用" ✓（服务器版这一行别多出个按钮让人误点）
  const showPathMap = window.__isBuilder !== false;
  return `
    <div class="folder ${f.exists ? '' : 'missing'} ${selected.has(f.id) ? 'sel' : ''}"
         data-folder-id="${f.id}" data-folder-path="${esc(f.path)}">
      <input type="checkbox" data-pick="${f.id}" ${selected.has(f.id) ? 'checked' : ''}>
      <span class="p" title="${esc(f.path)}">${esc(leaf)}${f.exists ? '' : (f.exists_reason === 'windows_path'
        ? ' <span class="hint" title="这是建库机上的 Windows 路径，服务器上没有这个盘（不是故障）">· 本机路径</span>'
        : ' ⚠ 目录不存在')}</span>
      ${f.category
        ? `<span class="badge" data-cat-tag="${f.id}" title="用途：${esc(f.category_label || '')}（点「配置」可以重新分类）">${esc(f.category_icon || '')} ${esc(f.category_label || '')}</span>`
        : '<span class="badge" data-cat-tag="' + f.id + '" style="opacity:.5" title="还没归类（点「配置」可以选一个用途）">未分类</span>'}
      <span class="badge mode">${esc((f.summary && f.summary.label) || '')}</span>
      ${(f.config && f.config.use_kb) ? '<span class="badge kbtag">知识库</span>' : ''}
      <span class="badge run" data-run="${f.id}" hidden></span>
      <span class="meta"><span style="opacity:.75">${esc(f.path)}</span><br>${f.files} 文件 · ${f.chunks} 片段${
        f.errors ? ` · <b style="color:var(--warn)" data-err-folder="${f.id}" title="${esc(errTip(f.id))}">${f.errors} 异常</b>` : ''}${
        f.summary ? ' · ' + f.summary.index_dim + ' 维' : ''} · ${
        f.last_scan ? '更新 ' + fmtWhen(f.last_scan) : '尚未扫描'}</span>
      <button class="ghost tiny" data-act="scan" data-id="${f.id}">增量更新</button>
      <button class="ghost tiny" data-act="start" data-id="${f.id}"
              title="开始（或继续）这个文件夹的索引：已建好的部分会跳过">▶ 开始</button>
      <button class="ghost tiny" data-act="pause" data-id="${f.id}"
              title="暂停当前索引（全局）：进度会保留，之后点「开始」接着跑">⏸ 暂停</button>
      <button class="ghost tiny" data-act="config" data-id="${f.id}">配置</button>
      ${showPathMap ? `<button class="ghost tiny" data-act="paths" data-id="${f.id}"
              title="🔗 路径映射：这里可以指定
· 本机实际目录（本机把同一份资料挂到了别处时用）
· 服务器实际目录（如 /mnt/nas_photo/家庭相册）
数据库本机与服务器共享，靠它俩对上号 ✓">🔗 路径</button>` : ''}
      <button class="ghost tiny" data-act="remove" data-id="${f.id}">移除</button>
    </div>`;
}

// 分组外壳（「按路径」和「按归类」两种分组共用 ✓）—— 可折叠 ✓
function fgroupHtml(key, title, items, showFull, expanded) {
  const files = items.reduce((a, f) => a + (f.files || 0), 0);
  const chunks = items.reduce((a, f) => a + (f.chunks || 0), 0);
  const errs = items.reduce((a, f) => a + (f.errors || 0), 0);
  const open = expanded.has(key);
  return `<div class="fgroup${open ? '' : ' off'}" data-group="${esc(key)}">
    <div class="fghead">
      <button class="fgtog" data-tog="${esc(key)}"
              title="${open ? '收起' : '展开'}">${open ? '▾' : '▸'}</button>
      <span class="fgpath" title="${esc(title)}">${esc(title)}</span>
      <span class="fgmeta">${items.length} 个文件夹 · ${files} 文件 · ${chunks} 片段${
        errs ? ` · <b style="color:var(--warn)" data-err-group="1" title="${esc(errTip(null, items))}">${errs} 异常</b>` : ''}</span>
      <button class="ghost tiny fgall" data-all="${esc(key)}">${
        open ? '全部收起' : '全部展开'}</button>
    </div>
    <div class="fgbodd">${items.map((f) => folderRowHtml(f, showFull)).join('')}</div>
  </div>`;
}

function foldersHtml(list) {
  // 记录「**展开**过」的组 —— 这样默认状态就是收起的，界面一开始就清爽
  const expanded = window.__expandedGroups || (window.__expandedGroups = new Set());

  // ---- 按「用途归类」分组（项目主 2026-10-18 要的"可折叠的归类组"）----
  // 组内一律显示**完整路径**：同一个归类下的文件夹通常来自不同目录，
  // 只给叶子名会分不清谁是谁 ✗
  if (window.__groupBy === 'category') {
    const buckets = new Map();
    for (const f of list) {
      const cid = f.category || '';
      if (!buckets.has(cid)) buckets.set(cid, []);
      buckets.get(cid).push(f);
    }
    const known = catList().map((c) => c.id);
    const keys = known.filter((cid) => buckets.has(cid))
      .concat([...buckets.keys()].filter((cid) => !known.includes(cid)));
    let out = '';
    for (const cid of keys) {
      const items = buckets.get(cid);
      const meta = catList().find((c) => c.id === cid);
      // 标签优先用类别表；类别表还没加载/被删掉时，退回文件夹自己带的标签 ✓
      // （否则会显示成 "find" 这种内部 id ✗，看起来像"没归类"✗）
      const anyF = items.find((f) => f.category_label) || {};
      const label = cid
        ? `${(meta && meta.icon) || anyF.category_icon || ''} `
          + `${(meta && meta.label) || anyF.category_label || cid}`
        : '未分类';
      out += fgroupHtml('cat:' + (cid || '-'), label, items, true, expanded);
    }
    return out || '<div class="empty">还没有文件夹。</div>';
  }

  // ---- 按路径分组（原来的行为，不变 ✓）----
  let html = '';
  for (const [parent, items] of groupFolders(list)) {
    if (items.length < 2) {          // 单独的文件夹不分组，直接显示
      html += folderRowHtml(items[0], true);
      continue;
    }
    html += fgroupHtml(parent, parent, items, false, expanded);
  }
  return html;
}

/* ---------------- 服务停止检测 ---------------- */
let deadFails = 0;          // 连续失败次数
let deadShown = false;

function showDead() {
  if (deadShown) return;
  deadShown = true;
  const ov = $('#dead-ov');
  if (!ov) return;
  ov.style.display = 'flex';
  // ⚠ 2026-10-18 第三次投诉的真正原因：
  //   这里原来会 `window.close()` —— 服务重启（我部署时要 30~60 秒）导致轮询失败 3 次（12 秒）
  //   → 弹这个"服务已停止"遮罩 + **尝试关掉窗口** ✗
  //   → 而且 `pingAlive` 看到 deadShown 就 return，服务回来了也**永远不消失** ✗
  //   所以项目主看到的是"界面被关了 / 盖住了，但服务黑框还在"✗
  //   现在：**绝不自动关窗** ✓，只提示"在等它回来" ✓，并且会**自动恢复** ✓。
  const t = $('#dead-title');
  if (t) t.textContent = '服务暂时连不上（正在重启？）';
  const d = $('#dead-desc');
  if (d) {
    d.innerHTML = '会自动重试，服务回来这个提示会自己消失 ✓<br>'
      + '<span style="opacity:.7">如果一直连不上，再双击 start.bat</span>';
  }
  const b = $('#dead-close');
  if (b) {
    b.textContent = '立即重试';
    b.onclick = () => { pingAlive(true); };
  }
}

function hideDead() {
  const ov = $('#dead-ov');
  if (ov) ov.style.display = 'none';
  deadShown = false;
  deadFails = 0;
}

async function pingAlive(force) {                             // noqa: F841
  // ⚠ 不再因为"已经显示过遮罩"就停止检测 —— 否则服务回来了也恢复不了 ✗
  try {
    const r = await fetch('/health', { cache: 'no-store' });
    if (r.ok) { hideDead(); return; }                          // 服务回来了 → 提示自动消失 ✓
    deadFails += 1;
  } catch (e) {
    deadFails += 1;
  }
  // 连续 5 次失败（约 20 秒）才提示（重启往往要 30~60 秒，提示会自动消失）
  if (deadFails >= 5) showDead();
}

/* ---------------- 服务器连接（本机改同步目标）---------------- */
const VIEW_MODE_NAME = { personal: '个人版（本机，建库全开）', admin: '管理员版', member: '成员（只读他人数据）' };
const ROLE_NAME = { admin: '管理员', user: '普通用户' };

/* 本机这一侧的身份：是不是建库机 + 当前视图档位 */
function paintSyncIdent(d, serverUser) {
  const box = $('#sync-ident');
  if (!box) return;
  const parts = [];
  if (d) {
    parts.push((d.is_builder
      ? '🖥 本机 = <b>建库机</b>（能建库 / 索引 / 打标）'
      : '☁ 本机 = <b>服务器</b>（不建库，只检索）')
      + ' · 视图：<b>' + esc(VIEW_MODE_NAME[d.view_mode] || d.view_mode || '?') + '</b>');
    if (d.me && d.me.name) {
      parts.push('本机登录：<b>' + esc(d.me.name) + '</b>（'
        + esc(ROLE_NAME[d.me.role] || d.me.role || '?') + '）');
    }
    parts.push(d.has_password || d.has_token
      ? '服务器凭据：<b style="color:var(--ok)">已保存</b>'
      : '服务器凭据：<b style="color:var(--warn)">还没保存</b>');
  }
  if (serverUser && serverUser.name) {
    parts.push('🌐 服务器上你是：<b>' + esc(serverUser.name) + '</b>（'
      + esc(ROLE_NAME[serverUser.role] || serverUser.role || '?') + '）');
  }
  box.innerHTML = parts.join('<br>');
}

async function loadSyncServer() {
  const s = $('#sync-server');
  if (!s) return;
  try {
    const d = await api('/api/settings/sync');
    s.value = d.server || '';
    const u = $('#sync-user');
    if (u) u.value = d.user || '';
    paintSyncCandidates(d);
    paintSyncIdent(d, null);
    // 已经存过密码 → 输入框给个提示，别让人以为没设置
    const p = $('#sync-pass');
    if (p) p.placeholder = d.has_password
      ? '密码已保存（留空 = 不改）' : '服务器密码（必填）';
  } catch (e) { /* 服务器上没有这个接口，忽略 */ }
}

// 显示「现在用哪个 / 每个候选通不通」（自适应结果）
function paintSyncCandidates(d) {
  const box = $('#sync-current');
  if (!box) return;
  const cands = (d && d.candidates) || [];
  const cur = (d && (d.current || d.probe && d.probe.chosen)) || '';
  if (!cands.length) {
    box.innerHTML = cur ? ('当前使用：<b>' + esc(cur) + '</b>') : '';
    return;
  }
  box.innerHTML = '当前使用：<b>' + esc(cur || '（未探到）') + '</b>　候选：'
    + cands.map((x) => (x.ok ? '✅ ' : '⛔ ') + esc(x.server)).join('　');
}

function bindSyncServer() {
  const btn = $('#sync-save');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    const server = ($('#sync-server') || {}).value || '';
    const user = ($('#sync-user') || {}).value || '';
    const pwd = ($('#sync-pass') || {}).value || '';
    const msg = $('#sync-msg');
    const say = (html) => { if (msg) msg.innerHTML = html; };
    if (!server.trim()) { say('<span style="color:var(--bad)">先填服务器地址</span>'); return; }
    if (!user.trim()) { say('<span style="color:var(--bad)">必须填服务器账号</span>'); return; }
    btn.disabled = true;
    say('正在登录服务器验证…');
    try {
      // ★ 不再"只保存不验证"：这一步真的拿账号密码去服务器换会话，
      //   成功了才算连上，并顺手把换到的 token 存进 config.ini（推库直接可用）✓
      const d = await post('/api/settings/sync/verify',
        { server, user, password: pwd, save: true });
      const who = (d.user && d.user.name) || user;
      const role = ROLE_NAME[(d.user || {}).role] || (d.user || {}).role || '?';
      say('<span style="color:var(--ok)">✅ 已连上 ' + esc(d.server || server)
        + '，服务器上你是 ' + esc(who) + '（' + esc(role) + '）</span>');
      if ($('#sync-pass')) { $('#sync-pass').value = ''; }
      if ($('#sync-server')) $('#sync-server').value = d.server || server;
      if ($('#sync-user')) $('#sync-user').value = user;
      paintSyncIdent({ is_builder: true, view_mode: d.local_view_mode,
        me: null, has_password: true, has_token: true }, d.user);
      // 顺带刷新一下候选连通性
      try {
        const st = await api('/api/settings/sync');
        paintSyncCandidates(st);
      } catch (e) { /* 忽略 */ }
    } catch (e) {
      say('<span style="color:var(--bad)">❌ ' + esc(e.message) + '</span>');
    } finally {
      btn.disabled = false;
    }
  });
  if (tabActive('settings')) loadSyncServer();   // 1.4 秒的接口，不放在首屏抢带宽
}

/* ---------------- 显存控制 / 外部模型 ---------------- */
async function loadGpu() {
  const b = $('#gpu-state');
  if (!b) return;
  try {
    const g = await api('/api/gpu');
    b.textContent = '空闲 ' + g.free_mb + ' MB';
    $('#gpu-note').textContent = '嵌入模型在 ' + String(g.emb_device).toUpperCase()
      + ' · 共 ' + g.total_mb + ' MB，已用 ' + g.used_mb + ' MB';
  } catch (e) { b.textContent = '读取失败'; }
}

function bindGpu() {
  const toCpu = $('#emb-cpu');
  const toGpu = $('#emb-cuda');
  if (!toCpu) return;
  const go = async (dev, btn) => {
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = '切换中…';
    try {
      const r = await post('/api/gpu/embedding', { device: dev });
      await loadGpu();
      alert(r.note || '完成');
    } catch (e) { alert('失败：' + e.message); }
    btn.disabled = false; btn.textContent = old;
  };
  toCpu.addEventListener('click', () => go('cpu', toCpu));
  toGpu.addEventListener('click', () => go('cuda', toGpu));
  // 显卡状态一次要 8 秒左右（调 nvidia-smi），以前**不分页面**每 15 秒拉一次，
  // 把单线程服务堵得死死的。现在只在「设置」页可见时才拉，且 60 秒一次 ✓
  if (tabActive('settings')) loadGpu();
  setInterval(() => { if (tabActive('settings') && $('#gpu-state')) loadGpu(); }, 60000);
}

/* ---------------- 默认排除目录（设置里自己改）----------------
   项目主 2026-10-18：以前只能改 config.ini ✗ → 现在做成设置项 ✓
   保存走 PUT /api/settings/ignore-dirs（服务端写 config.ini 后立刻 reload ✓ 不用重启）*/
async function loadIgnoreDirs() {
  const box = $('#ig-list');
  if (!box) return;
  try {
    const d = await api('/api/settings/ignore-dirs');
    box.value = (d.dirs || []).join('\n');
    $('#ig-count').textContent = `（共 ${(d.dirs || []).length} 项）`;
    $('#ig-where').textContent = '配置文件：' + (d.path || '');
    window.__igPresets = d.presets || [];
    $('#ig-msg').textContent = '';
  } catch (e) {
    $('#ig-msg').textContent = '读取失败：' + e.message;
  }
}

function bindIgnoreDirs() {
  const box = $('#ig-list');
  if (!box) return;
  const msg = (t, bad) => {
    const m = $('#ig-msg');
    if (m) { m.textContent = t; m.style.color = bad ? 'var(--bad)' : 'var(--ok)'; }
  };
  $('#ig-save').onclick = async () => {
    const dirs = box.value.split('\n').map((x) => x.trim()).filter(Boolean);
    if (!dirs.length) { msg('至少要留一条，否则会把所有目录都索引进去', true); return; }
    if (!confirm(`保存这 ${dirs.length} 条排除目录吗？\n\n保存后立刻生效。\n`
                 + '已经索引过的文件夹，下次重建/增量更新才会按新规则清理。')) return;
    const b = $('#ig-save');
    b.disabled = true; b.textContent = '保存中…';
    try {
      const r = await post('/api/settings/ignore-dirs', { dirs }, 'PUT');
      msg(`已保存 ${r.count} 项 ✓ 立刻生效`);
      await loadIgnoreDirs();
    } catch (e) {
      msg('保存失败：' + e.message, true);
    } finally {
      b.disabled = false; b.textContent = '保存';
    }
  };
  $('#ig-preset').onclick = () => {
    const p = window.__igPresets || [];
    if (!p.length) { msg('没拿到建议清单，先点「重新读取」', true); return; }
    if (!confirm(`用建议清单覆盖当前内容吗？（${p.length} 项，还没保存）`)) return;
    box.value = p.join('\n');
    msg('已填入建议清单，点「保存」才生效');
  };
  $('#ig-reload').onclick = () => loadIgnoreDirs();
  loadIgnoreDirs();
}

function manualModel() {
  const sel = $('#rm-model');
  if (!sel || $('#rm-manual')) return;
  const inp = document.createElement('input');
  inp.type = 'text';
  inp.id = 'rm-manual';
  inp.placeholder = '手填模型名，例如 gpt-4o / claude-sonnet-4 / qwen-vl-max';
  inp.style.cssText = 'flex:1;min-width:260px;padding:6px 10px;border-radius:8px;'
    + 'border:1px solid var(--line);background:var(--panel2);color:var(--fg)';
  sel.parentNode.insertBefore(inp, sel);
  sel.style.display = 'none';
  inp.addEventListener('change', saveModel);
  try {
    const w = localStorage.getItem('eg2-rmmodel');
    if (w && !inp.value) inp.value = w;
  } catch (e) { /* 忽略 */ }
}

function modelValue() {
  const m = $('#rm-manual');
  if (m && m.value.trim()) return m.value.trim();
  const sel = ($('#rm-model') || {}).value || '';
  if (sel) return sel;
  // 界面上还没填（比如刚打开页面、还没探测），回退到上次记住的模型名
  try { return localStorage.getItem('eg2-rmmodel') || ''; } catch (e) { return ''; }
}

/* 记住选中的模型名 —— 不然每次刷新/重启都要重选一遍 */
function saveModel() {
  try {
    const v = ($('#rm-manual') && $('#rm-manual').value.trim())
      ? $('#rm-manual').value.trim()
      : (($('#rm-model') || {}).value || '');
    if (v) localStorage.setItem('eg2-rmmodel', v);
  } catch (e) { /* 忽略 */ }
}

/* 把上次记住的模型名恢复到下拉/手填框里 */
function restoreModel() {
  let want = '';
  try { want = localStorage.getItem('eg2-rmmodel') || ''; } catch (e) { /* 忽略 */ }
  if (!want) return;
  const sel = $('#rm-model');
  const man = $('#rm-manual');
  if (man) { man.value = want; return; }
  if (!sel) return;
  // 下拉里如果有这个选项就选上，没有就临时插一个（探测后会被真实列表替换）
  let hit = false;
  for (const op of sel.options) { if (op.value === want) { hit = true; break; } }
  if (!hit) {
    const op = document.createElement('option');
    op.value = want;
    op.textContent = want + '（上次用的）';
    sel.insertBefore(op, sel.firstChild);
  }
  sel.value = want;
}

/* 从推理模型的长思考里尽量抠出结论。
   这些模型的 reasoning 往往以「所以我会直接描述：」「最终答案：」这类
   句子收尾，后面的才是真正要说的话。 */
function pickConclusion(text) {
  if (!text) return '';
  const marks = ['我会直接描述：', '我会直接描述:', '最终答案：', '最终答案:',
                 '描述如下：', '描述如下:', '答案是：', '答案是:',
                 '所以描述：', '所以描述:', '描述为：', '描述为:'];
  let best = -1;
  let bestLen = 0;
  for (const mk of marks) {
    const i = text.lastIndexOf(mk);
    if (i > best) { best = i; bestLen = mk.length; }
  }
  if (best >= 0) {
    const tail = text.slice(best + bestLen).trim();
    if (tail.length >= 8) return tail;
  }
  return text;
}

function genSessionHeader() {
  // OpenCode 的网关要求带一个 x-opencode-session，只是个路由标记，
  // 任意唯一字符串都可以（示例里的 session_12345 是占位符，别照抄）。
  const uuid = (window.crypto && crypto.randomUUID)
    ? crypto.randomUUID()
    : (Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 12));
  const el = $('#rm-headers');
  if (!el) return;
  let obj = {};
  try { obj = el.value.trim() ? JSON.parse(el.value) : {}; } catch (e) { obj = {}; }
  obj['x-opencode-session'] = uuid;
  el.value = JSON.stringify(obj, null, 2);
  try { localStorage.setItem('eg2-rmhdr', el.value); } catch (e) { /* 忽略 */ }
}

/* 把「外部模型」卡里的配置打包出来，给打标和问答共用 */
function remoteConfig() {
  const el = $('#rm-headers');
  let hdr = null;
  if (el && el.value.trim()) {
    try { hdr = JSON.parse(el.value); } catch (e) { hdr = null; }
  }
  return {
    base_url: (($('#rm-url') || {}).value || '').trim(),
    api_key: (($('#rm-key') || {}).value || '').trim(),
    model: (typeof modelValue === 'function' ? modelValue() : ''),
    headers: hdr,
    max_side: Number(($('#rm-side') || {}).value) || 448,
    max_tokens: Number(($('#rm-tok') || {}).value) || 800,
  };
}

function customHeaders() {
  const el = $('#rm-headers');
  if (!el || !el.value.trim()) return null;
  try { return JSON.parse(el.value); }
  catch (e) { throw new Error('自定义请求头不是合法 JSON：' + e.message); }
}

/* ============ 外部 API：多套配置并存、可折叠并列显示 ============
   以前只有一套，在「本地 llama.cpp」和「云端」之间来回切就得重填地址/密钥。
   现在可以存任意多套（最多 12），设置里并列折叠显示，点一下就切；
   知识库问答那个下拉里能直接选「用哪一套回答」。
   存放（eg2- 前缀会被偏好层同步到服务端，PC 和服务器共用同一份）：
     eg2-rmapilist  JSON 数组：[{name, base_url, model, api_key, headers}, ...]
     eg2-rmactive   当前使用第几套（"0" 起；打标 + 默认问答走它）
     eg2-rmurl/model/key/hdr   当前那套的镜像（后端自动回退、打标流程读这几个） */
function rmApiList() {
  try {
    const j = JSON.parse(localStorage.getItem('eg2-rmapilist') || 'null');
    if (Array.isArray(j)) return j;
  } catch (e) { /* 忽略 */ }
  return [];
}
function rmSaveApiList(list) {
  try { localStorage.setItem('eg2-rmapilist', JSON.stringify(list.slice(0, 12))); }
  catch (e) { /* 忽略 */ }
}
function rmApi(i) { return rmApiList()[i] || null; }
/* 这套 API 共享给了哪些人（存的是用户名；老记录只存了 id 数组，显示成 #id） */
function shareNames(s) {
  if (!s) return [];
  const a = s.share_to_names;
  if (Array.isArray(a) && a.length) return a.map(String);
  if (typeof a === 'string' && a.trim()) return [a.trim()];
  const ids = s.share_to;
  if (Array.isArray(ids) && ids.length) return ids.map((x) => '#' + x);
  return [];
}

/* 改「谁能用」（由 HTML 的 onchange 直接调用，2026-10-22） */
function rmShareMode(i, mode) {
  const l2 = rmApiList();
  if (!l2[i]) return;
  const inp = document.getElementById('rm-share-names-' + i);
  if (mode === 'to') {
    if (inp) { inp.style.display = ''; inp.focus(); }
    delete l2[i].share;
  } else if (mode === 'all') {
    l2[i].share = true;
    delete l2[i].share_to_names;
    delete l2[i].share_to;
    if (inp) inp.style.display = 'none';
  } else {
    delete l2[i].share;
    delete l2[i].share_to_names;
    delete l2[i].share_to;
    if (inp) inp.style.display = 'none';
  }
  rmSaveApiList(l2);
}

/* 改「指定的人」名单（用户名，逗号分隔；写错会当场提示） */
function rmShareNames(i, raw) {
  const l2 = rmApiList();
  if (!l2[i]) return;
  const names = String(raw || '').replace(/，/g, ',').split(',')
    .map((x) => x.trim()).filter(Boolean);
  if (names.length) { l2[i].share_to_names = names; delete l2[i].share; }
  else { delete l2[i].share_to_names; }
  rmSaveApiList(l2);
  const tip = document.getElementById('rm-share-tip-' + i);
  if (!names.length) { if (tip) tip.textContent = ''; return; }
  if (tip) tip.textContent = '正在核对…';
  post('/api/dify/validate', { names }, 'POST').then((r) => {
    if (!tip) return;
    tip.innerHTML = (r.unknown || []).length
      ? '<span style="color:var(--bad)">查无此人：' + esc((r.unknown || []).join('、')) + '</span>'
      : '<span style="color:var(--ok)">已共享给：' + esc((r.valid || []).join('、')) + '</span>';
  }).catch(() => { if (tip) tip.textContent = ''; });
}
function rmActiveIdx() {
  try {
    const n = Number(localStorage.getItem('eg2-rmactive') || '0');
    const list = rmApiList();
    return (n >= 0 && n < list.length) ? n : 0;
  } catch (e) { return 0; }
}
/* 当前使用的那套（打标、默认问答都走它）。
   不能用编辑框：用户可能正在编辑另一套。 */
function activeRemote() {
  const s = rmApi(rmActiveIdx());
  if (!s || !s.base_url || !s.model) return null;
  let hdr = null;
  if (s.headers) { try { hdr = JSON.parse(s.headers); } catch (e) { hdr = null; } }
  return { base_url: s.base_url, model: s.model, api_key: s.api_key || '',
           headers: hdr, max_side: 448, max_tokens: 800 };
}
function rmLoadIntoForm(s) {
  if ($('#rm-url')) $('#rm-url').value = (s && s.base_url) || '';
  if ($('#rm-key')) $('#rm-key').value = (s && s.api_key) || '';
  if ($('#rm-headers')) $('#rm-headers').value = (s && s.headers) || '';
  try { if (s && s.model) localStorage.setItem('eg2-rmmodel', s.model); } catch (e) { /* 忽略 */ }
  if (typeof restoreModel === 'function') restoreModel();
}
/* 把"当前使用"那套镜像到旧键上 —— 后端 /api/chat 的自动回退读它们 */
function rmMirrorActive(s) {
  if (!s) return;
  try {
    localStorage.setItem('eg2-rmurl', s.base_url || '');
    localStorage.setItem('eg2-rmmodel', s.model || '');
    localStorage.setItem('eg2-rmhdr', s.headers || '');
    if (s.api_key) localStorage.setItem('eg2-rmkey', s.api_key);
  } catch (e) { /* 忽略 */ }
}
/* 正在编辑第几套。
   注意不能用 `rmEditIdx()` —— 索引 0 是假值，
   编辑第一套时会被当成"没设置"而落到当前使用的那套上（实测踩过）。 */
function rmEditIdx() {
  const v = window.__rmEditSlot;
  return (typeof v === 'number' && v >= 0) ? v : rmActiveIdx();
}
/* 把编辑框里的内容存回第 i 套 */
function rmSaveFromForm(i) {
  const list = rmApiList();
  if (!list[i]) return null;
  const rm = remoteConfig();
  list[i] = { name: list[i].name, base_url: rm.base_url, model: rm.model,
              api_key: rm.api_key, headers: (($('#rm-headers') || {}).value || '') };
  rmSaveApiList(list);
  if (i === rmActiveIdx()) rmMirrorActive(list[i]);
  renderApiList();
  renderKbEngineOptions();
  return list[i];
}
function rmSetActive(i, quiet) {
  try { localStorage.setItem('eg2-rmactive', String(i)); } catch (e) { /* 忽略 */ }
  rmMirrorActive(rmApi(i));
  renderApiList();
  renderKbEngineOptions();
  if (!quiet && $('#rm-out')) {
    $('#rm-out').innerHTML = '<span style="color:var(--ok)">已设为当前使用：'
      + esc((rmApi(i) || {}).name || ('配置' + (i + 1))) + '</span>';
  }
}
function rmAddApi() {
  const list = rmApiList();
  list.push({ name: '配置 ' + (list.length + 1), base_url: '', model: '',
              api_key: '', headers: '' });
  rmSaveApiList(list);
  window.__rmEditSlot = list.length - 1;
  rmLoadIntoForm(list[list.length - 1]);
  renderApiList();
  return list.length - 1;
}
function rmDelApi(i) {
  const list = rmApiList();
  if (!list[i]) return;
  if (!confirm('删除配置「' + (list[i].name || ('配置' + (i + 1))) + '」？')) return;
  list.splice(i, 1);
  rmSaveApiList(list);
  const act = rmActiveIdx();
  try { localStorage.setItem('eg2-rmactive', String(act >= list.length ? Math.max(0, list.length - 1) : act)); } catch (e) { /* 忽略 */ }
  rmMirrorActive(rmApi(rmActiveIdx()));
  window.__rmEditSlot = 0;
  rmLoadIntoForm(rmApi(0));
  renderApiList();
  renderKbEngineOptions();
}
/* 设置里：每套一张可折叠卡片，并列显示 */
/* ★ 2026-10-22：把"服务端认为你能用的 API"缓存下来（自己的 + 公共/共享的）。
   普通用户没法在本地配，但又必须能选到服务器那台 llama.cpp —— 靠这个列表 ✓ */
async function loadSrvApiModels() {
  try {
    const r = await api('/api/chat2/models');
    window.__srvApis = (r.models || []).filter((m) => m.kind === 'api');
  } catch (e) {
    window.__srvApis = window.__srvApis || [];
  }
  try { if (typeof renderKbEngineOptions === 'function') renderKbEngineOptions(); }
  catch (e) { /* 忽略 */ }
  return window.__srvApis;
}

/* 设置页里给普通用户看的只读清单：公共 / 共享给我的模型（不含密钥） */
function paintSrvApiHint() {
  const box = $('#rm-srvlist');
  if (!box) return;
  const list = (window.__srvApis || []).filter((m) => !m.label.startsWith('本机'));
  if (!list.length) { box.innerHTML = ''; return; }
  box.innerHTML = '<div class="hint" style="margin:8px 0;padding:8px 10px;'
    + 'border:1px solid var(--line);border-radius:8px;background:rgba(91,157,255,.06)">'
    + '<b>服务器上现成能用的模型</b>（不需要你配，直接在下拉里选）：<br>'
    + list.map((m) => '☁ ' + esc(m.label)).join('<br>')
    + '<br><span style="opacity:.75">密钥在对方/服务端，你只拿到模型名 ✓</span></div>';
}

function renderApiList() {
  const box = $('#rm-slotbar');
  if (!box) return;
  paintSrvApiHint();  const list = rmApiList();
  const act = rmActiveIdx();
  const edit = window.__rmEditSlot === undefined ? act : window.__rmEditSlot;
  let html = '';
  if (!list.length) {
    html = '<div class="hint">还没有外部 API 配置 —— 填好下面的地址、探测一个模型，'
      + '再点「存为第 1 套」。</div>';
  }
  list.forEach((s, i) => {
    const on = (i === act) ? ' <span style="color:var(--ok)">✓ 当前使用</span>' : '';
    const mark = (i === edit) ? '（正在下面编辑）' : '';
    const mode = s.share ? 'all' : (shareNames(s).length ? 'to' : 'private');
    html += '<details class="rm-api"' + (i === edit || list.length <= 2 ? ' open' : '') + '>'
      + '<summary><b>' + (i + 1) + '）' + esc(s.name || ('配置' + (i + 1))) + '</b>'
      + ' <span class="hint">' + esc(s.model || '（没填模型）') + '</span>'
      + ' <span class="hint">' + esc(s.base_url || '（没填地址）') + '</span>'
      + on + ' <span class="hint">' + mark + '</span></summary>'
      + '<div style="display:flex;gap:6px;flex-wrap:wrap;margin:6px 0 2px">'
      + '<button class="ghost" data-edit="' + i + '">编辑这套</button>'
      + '<button class="ghost" data-use="' + i + '">设为当前使用</button>'
      + '<button class="ghost" data-del="' + i + '">删除</button>'
      + '<span class="hint">密钥：' + (s.api_key ? '已保存' : '（无）') + '</span>'
      + '</div>'
      // ★ 2026-10-22：共享改成"卡片里一行"，和 Dify 那边一致。
      //   原来的弹窗要先异步取用户列表，在这页上一直拿不到结果（按钮点了没反应）✗
      + '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;'
      + 'padding:6px 0 2px;border-top:1px dashed rgba(255,255,255,.08)">'
      + '<span class="hint">谁能用</span>'
      + '<select onchange="rmShareMode(' + i + ',this.value)"'
      + ' style="padding:3px 6px;border-radius:6px;'
      + 'border:1px solid var(--line);background:var(--panel);color:var(--fg)">'
      + '<option value="private"' + (mode === 'private' ? ' selected' : '') + '>只有我</option>'
      + '<option value="all"' + (mode === 'all' ? ' selected' : '') + '>所有登录用户</option>'
      + '<option value="to"' + (mode === 'to' ? ' selected' : '') + '>指定的人</option>'
      + '</select>'
      + '<input type="text" id="rm-share-names-' + i + '"'
      + ' onchange="rmShareNames(' + i + ',this.value)"'
      + ' value="' + esc(shareNames(s).join(', ')) + '" placeholder="填用户名，多个用逗号隔开"'
      + ' title="只在「指定的人」时生效；填对方的登录用户名"'
      + ' style="width:240px;padding:3px 6px;border-radius:6px;border:1px solid var(--line);'
      + 'background:var(--panel);color:var(--fg);' + (mode === 'to' ? '' : 'display:none') + '">'
      + '<span class="hint" id="rm-share-tip-' + i + '"></span>'
      + '<span class="hint" style="opacity:.75">共享的是"能用它回答"，'
      + '对方看不到你的密钥 ✓</span>'
      + '</div></details>';
  });
  html += '<div class="hint">每套都能单独填地址/模型/密钥/请求头，互不影响；'
    + '「设为当前使用」决定打标和默认问答走哪套。知识库问答那里的下拉'
    + '可以直接选任意一套来回答。</div>'
    + '<div style="margin:6px 0 10px"><button class="ghost" id="rm-api-add">＋ 新增一套 API</button>'
    + ' <span class="hint">（最多 12 套，都存在偏好里，PC 和服务器共用同一份）</span></div>';
  box.innerHTML = html;
  box.querySelectorAll('button[data-edit]').forEach((b) => {
    b.addEventListener('click', () => {
      if (window.__rmEditSlot !== undefined) rmSaveFromForm(window.__rmEditSlot);
      window.__rmEditSlot = Number(b.dataset.edit);
      rmLoadIntoForm(rmApi(window.__rmEditSlot));
      renderApiList();
    });
  });
  box.querySelectorAll('button[data-use]').forEach((b) => {
    b.addEventListener('click', () => rmSetActive(Number(b.dataset.use)));
  });
  box.querySelectorAll('button[data-del]').forEach((b) => {
    b.addEventListener('click', () => rmDelApi(Number(b.dataset.del)));
  });
  // ★ 2026-10-22 项目主："管理员共享模型按钮也无法点击" ——
  //   改成卡片里一行下拉 + 用户名。保存逻辑写在全局函数 rmShareMode / rmShareNames 里，
  //   由 HTML 的 onchange 直接调 —— 卡片重画多少次都不受影响 ✓
  const add = $('#rm-api-add');
  if (add) {
    add.addEventListener('click', () => {
      if (window.__rmEditSlot !== undefined) rmSaveFromForm(window.__rmEditSlot);
      rmAddApi();
    });
  }
}
/* 知识库问答那个下拉里追加「外部 API」一组（每套一条） */
function renderKbEngineOptions() {
  const sel = $('#kb-model');
  if (!sel) return;
  // 老版本只有一套配置（eg2-rmurl / rmmodel / rmkey / rmhdr）。列表还空着时先补一条 ——
  // 否则老用户打开界面会以为"用外部模型回答"这个功能被删了（实际是并进这个下拉了）。
  if (!rmApiList().length) {
    let u = '';
    let m = '';
    let k = '';
    let h = '';
    try {
      u = localStorage.getItem('eg2-rmurl') || '';
      m = localStorage.getItem('eg2-rmmodel') || '';
      k = localStorage.getItem('eg2-rmkey') || '';
      h = localStorage.getItem('eg2-rmhdr') || '';
    } catch (e) { /* 忽略 */ }
    if (u) {
      rmSaveApiList([{ name: m ? ('外部：' + m) : '外部模型', base_url: u, model: m,
                       api_key: k, headers: h }]);
    }
  }
  const old = sel.querySelector('optgroup[data-api]');
  if (old) old.remove();
  let list = rmApiList();
  let fromServer = false;
  // ★ 2026-10-22：**服务器上部署的 llama.cpp** 属于"公共资源"（无密钥），
  //   它在管理员的偏好里；普通用户的 localStorage 里根本没有这一条 ✗
  //   所以优先用服务端算好的"你这次能用的那几套"（自己的 + 公共/共享的）✓
  if (Array.isArray(window.__srvApis) && window.__srvApis.length) {
    list = window.__srvApis.map((x) => ({
      name: x.label, base_url: 'srv', model: '', _id: x.id,
    }));
    fromServer = true;
  }
  const g = document.createElement('optgroup');
  g.setAttribute('data-api', '1');
  g.label = fromServer ? '外部 API（含服务器公用/共享）' : '外部 API';
  list.forEach((s, i) => {
    if (!fromServer && (!s.base_url || !s.model)) return;
    const op = document.createElement('option');
    op.value = fromServer ? s._id : ('api:' + i);
    op.textContent = fromServer
      ? s.name
      : ((s.name || ('配置' + (i + 1))) + ' · ' + s.model);
    if (fromServer) op.dataset.srv = '1';
    g.appendChild(op);
  });
  if (g.children.length) {
    sel.appendChild(g);
    try {
      const want = localStorage.getItem('eg2-kbengine')
        || (localStorage.getItem('eg2-kbremote') === '1' ? ('api:' + rmActiveIdx()) : '');
      if (want && sel.querySelector('option[value="' + want + '"]')) sel.value = want;
    } catch (e) { /* 忽略 */ }
  }
}
function bindSlots() {
  // 第一次跑：把老的单套配置 / 两槽位 / 之前的预设都迁进配置列表
  if (!localStorage.getItem('eg2-rmapilist')) {
    const list = [];
    try {
      const ps = JSON.parse(localStorage.getItem('eg2-rmpresets') || '[]');
      if (Array.isArray(ps)) {
        ps.forEach((p) => list.push({ name: p.name || ('配置' + (list.length + 1)),
          base_url: p.base_url || '', model: p.model || '',
          api_key: p.api_key || '', headers: p.headers || '' }));
      }
    } catch (e) { /* 忽略 */ }
    [1, 2].forEach((i) => {
      try {
        const j = JSON.parse(localStorage.getItem('eg2-rmp' + i) || 'null');
        if (j && j.base_url && !list.some((x) => x.base_url === j.base_url)) {
          list.push({ name: j.name || ('配置' + (list.length + 1)),
            base_url: j.base_url, model: j.model || '',
            api_key: j.api_key || '', headers: j.headers || '' });
        }
      } catch (e) { /* 忽略 */ }
    });
    if (!list.length) {
      const u = localStorage.getItem('eg2-rmurl') || '';
      if (u) {
        list.push({ name: '配置 1', base_url: u,
          model: localStorage.getItem('eg2-rmmodel') || '',
          api_key: localStorage.getItem('eg2-rmkey') || '',
          headers: localStorage.getItem('eg2-rmhdr') || '' });
      }
    }
    if (list.length) {
      rmSaveApiList(list);
      try {
        const old = Number(localStorage.getItem('eg2-rmactive') || '1');
        localStorage.setItem('eg2-rmactive', String(Math.max(0, Math.min(old - 1, list.length - 1))));
      } catch (e) { /* 忽略 */ }
    }
  }
  window.__rmEditSlot = rmActiveIdx();
  rmLoadIntoForm(rmApi(window.__rmEditSlot));
  renderApiList();
  renderKbEngineOptions();
  // 编辑框一改就存回"正在编辑的那套"
  ['#rm-url', '#rm-key', '#rm-headers', '#rm-model', '#rm-manual', '#rm-side', '#rm-tok']
    .forEach((sel) => {
      const el = $(sel);
      if (!el || el.dataset.slotBound) return;
      el.dataset.slotBound = '1';
      el.addEventListener('change', () => rmSaveFromForm(rmEditIdx()));
    });
  // 「探测模型」是异步的：探测完（列表被替换）再把模型名存进正在编辑的那套
  const pb = document.getElementById('rm-probe');
  if (pb && !pb.dataset.slotBound) {
    pb.dataset.slotBound = '1';
    pb.addEventListener('click', () => {
      setTimeout(() => rmSaveFromForm(rmEditIdx()), 2500);
    });
  }
}

function bindRemote() {
  bindSlots();
  const pb = $('#rm-probe');
  if (!pb) return;
  try {
    const u = localStorage.getItem('eg2-rmurl');
    if (u) $('#rm-url').value = u;
    const k = localStorage.getItem('eg2-rmkey');
    if (k && $('#rm-key')) { $('#rm-key').value = k; $('#rm-remember').checked = true; }
    const hd = localStorage.getItem('eg2-rmhdr');
    if (hd && $('#rm-headers')) $('#rm-headers').value = hd;
    restoreModel();
  } catch (e) { /* 忽略 */ }
  pb.addEventListener('click', async () => {
    const url = ($('#rm-url').value || '').trim();
    if (!url) { alert('先填地址'); return; }
    pb.disabled = true; pb.textContent = '探测中…';
    try {
      localStorage.setItem('eg2-rmurl', url);
      const key = ($('#rm-key') || {}).value || '';
      if ($('#rm-remember') && $('#rm-remember').checked) {
        try { localStorage.setItem('eg2-rmkey', key); } catch (e) {}
      }
      try { localStorage.setItem('eg2-rmhdr', ($('#rm-headers') || {}).value || ''); } catch (e) {}
      let hdr = null;
      try { hdr = customHeaders(); }
      catch (e) { $('#rm-out').innerHTML = '<span style="color:var(--bad)">'
        + esc(e.message) + '</span>'; return; }
      const r = await post('/api/remote/probe',
        { base_url: url, api_key: key, headers: hdr });
      if (!r.ok) {
        $('#rm-out').innerHTML = '<span style="color:var(--bad)">探测失败：'
          + esc(r.error || '') + '</span>';
        if (r.can_manual) {
          $('#rm-model').innerHTML =
            '<option value="">（探测不行，直接在下面手填模型名）</option>';
          $('#rm-out').innerHTML += '<br><b>不用卡在这步</b> —— '
            + '在下面的框里<b>手写模型名</b>，直接点「拿真图试一张」也行。';
          manualModel();
        }
        return;
      }
      const list = (r.models || []).map((m) => (typeof m === 'string'
        ? { name: m } : m));
      $('#rm-model').innerHTML = list.length
        ? list.map((m) => '<option value="' + esc(m.name) + '">'
            + esc(m.name) + (m.gb ? '（' + m.gb + ' GB）' : '')
            + (m.family ? ' · ' + esc(m.family) : '') + '</option>').join('')
        : '<option value="">（这个地址上没有模型）</option>';
      $('#rm-out').textContent = '找到 ' + list.length + ' 个模型（协议：' + r.kind + '）';
      restoreModel();          // 探测出真实列表后，把上次选的模型重新选上
    } catch (e) {
      $('#rm-out').innerHTML = '<span style="color:var(--bad)">' + esc(e.message) + '</span>';
    }
    pb.disabled = false; pb.textContent = '探测模型';
  });
  try {
    const a = localStorage.getItem('eg2-rmside');
    const b = localStorage.getItem('eg2-rmtok');
    if (a && $('#rm-side')) $('#rm-side').value = a;
    // 推理模型（glm-5.x / deepseek-v4 等）会把 token 全用在思考上，
    // 给少于 256 的话最终答案是空的。这里自动纠正旧的小值。
    const tokEl = $('#rm-tok');
    if (tokEl) tokEl.value = (b && Number(b) >= 256) ? b : '800';
  } catch (e) { /* 忽略 */ }
  const gs = $('#rm-gen-session');
  if (gs) gs.addEventListener('click', genSessionHeader);
  const sel2 = $('#rm-model');
  if (sel2) sel2.addEventListener('change', saveModel);
  const man2 = $('#rm-manual');
  if (man2) man2.addEventListener('change', saveModel);
  const tb = $('#rm-test');
  if (tb) tb.addEventListener('click', async () => {
    const url = ($('#rm-url').value || '').trim();
    const model = modelValue();
    if (!url || !model) { alert('先探测并选一个模型'); return; }
    tb.disabled = true; tb.textContent = '测试中…（大模型可能要等几分钟）';
    try {
      const side = Number(($('#rm-side') || {}).value) || 448;
      const tok = Number(($('#rm-tok') || {}).value) || 800;
      try {
        localStorage.setItem('eg2-rmside', String(side));
        localStorage.setItem('eg2-rmtok', String(tok));
      } catch (e) { /* 忽略 */ }
      const r = await post('/api/remote/test',
        { base_url: url, model: model, max_side: side, max_tokens: tok,
          api_key: ($('#rm-key') || {}).value || '', headers: customHeaders() });
      $('#rm-out').innerHTML = '用时 ' + r.elapsed + ' 秒（边长 ' + (r.max_side || '?')
        + 'px，上限 ' + (r.max_tokens || '?') + ' tokens）· 测试图：'
        + esc(String(r.file).slice(0, 30)) + '<br><b>描述：</b>' + esc(r.caption);
    } catch (e) {
      $('#rm-out').innerHTML = '<span style="color:var(--bad)">失败：'
        + esc(e.message) + '</span>';
    }
    tb.disabled = false; tb.textContent = '拿真图试一张';
  });
}

/* ---------------- 上传图片问答 ---------------- */
let kbImages = [];          // 待发送的图片（base64 data URI）

function renderKbImgs() {
  const box = $('#kb-imgs');
  if (!box) return;
  box.innerHTML = kbImages.map((d, i) =>
    `<span style="position:relative;display:inline-block">
       <img src="${d}" style="width:72px;height:72px;object-fit:cover;
            border-radius:8px;border:1px solid var(--line)">
       <b data-dropimg="${i}" title="去掉"
          style="position:absolute;top:-6px;right:-6px;background:var(--bad);color:#000;
                 border-radius:50%;width:18px;height:18px;line-height:17px;
                 text-align:center;font-size:12px;cursor:pointer">×</b>
     </span>`).join('');
  box.querySelectorAll('[data-dropimg]').forEach((el) => {
    el.addEventListener('click', () => {
      kbImages.splice(Number(el.dataset.dropimg), 1);
      renderKbImgs();
    });
  });
}

function updateKbGuide() {
  const g = $('#kb-guide');
  if (!g) return;
  const cur = (window.__models || []).find((m) => m.current);
  const isVl = !!(cur && cur.vl);
  const parts = [];
  if (kbImages.length) {
    parts.push(isVl
      ? `📎 已附 ${kbImages.length} 张图，当前模型<b>能看图</b>，可以直接问。`
      : `📎 已附 ${kbImages.length} 张图，但当前模型是<b>纯文本</b>，`
        + `看不到图。请在上面的下拉里选带 👁 视觉 的模型。`);
  } else {
    parts.push(isVl
      ? '当前是<b>视觉模型</b>：可以「📎 附图」后问图里的内容，也能问资料库。'
      : '当前是<b>文本模型</b>：擅长读资料库答问题（快、省显存）。'
        + '要问图片内容，选带 👁 视觉 的模型。');
  }
  g.innerHTML = parts.join('<br>');
}

function bindKbImages() {
  const inp = $('#kb-img');
  if (inp) inp.addEventListener('change', async (ev) => {
    const files = [...(ev.target.files || [])].slice(0, 4 - kbImages.length);
    ev.target.value = '';
    for (const f of files) {
      try {
        const b64 = await new Promise((res, rej) => {
          const fr = new FileReader();
          fr.onload = () => res(String(fr.result).split(',')[1]);
          fr.onerror = rej;
          fr.readAsDataURL(f);
        });
        const r = await post('/api/upload-image', { data: b64 });
        kbImages.push(r.data);
      } catch (e) { alert('图片处理失败：' + e.message); }
    }
    renderKbImgs();
    updateKbGuide();
  });
  renderKbImgs();
  updateKbGuide();
}

/* ---------------- 图片打标（VL） ---------------- */
function vlHours(save) {
  const e1 = $('#vl-h1');
  const e2 = $('#vl-h2');
  if (!e1 || !e2) return null;
  const a = e1.value;
  const b = e2.value;
  // 填了就存起来，刷新页面不会丢
  if (save) {
    try {
      localStorage.setItem('eg2-vlh1', a);
      localStorage.setItem('eg2-vlh2', b);
    } catch (e) { /* 忽略 */ }
  }
  if (a === '' || b === '' || a === undefined || b === undefined) return null;
  return [Number(a), Number(b)];
}

function restoreVlHours() {
  try {
    const a = localStorage.getItem('eg2-vlh1');
    const b = localStorage.getItem('eg2-vlh2');
    if (a !== null && $('#vl-h1')) $('#vl-h1').value = a;
    if (b !== null && $('#vl-h2')) $('#vl-h2').value = b;
  } catch (e) { /* 忽略 */ }
}

function renderVl(s) {
  const st = $('#vl-state');
  const pr = $('#vl-progress');
  if (!st) return;
  st.textContent = s.running ? (s.paused ? '已暂停（不在时段）' : '运行中') : '空闲';
  st.style.background = s.running ? (s.paused ? 'var(--warn)' : 'var(--ok)') : '';
  st.style.color = s.running && !s.paused ? '#000' : '';
  const pct = s.total ? Math.round(s.done * 100 / s.total) : 0;
  let t = s.total
    ? '进度 ' + s.done + '/' + s.total + '（' + pct + '%） · 失败 ' + s.errors
    : (s.message || '');
  if (s.elapsed) t += ' · 已用 ' + Math.round(s.elapsed / 60) + ' 分';
  if (s.current) t += ' · 当前：' + s.current;
  if (s.window) t += ' · 时段 ' + s.window[0] + ':00 ~ ' + s.window[1] + ':00';
  if (s.model) t += ' · 模型 ' + s.model + (s.model_kind === 'remote' ? '（外部）' : '');
  if (s.vram_limit) t += '（显存上限 ' + s.vram_limit + 'MB）';
  pr.textContent = t;
}

async function loadVl() {
  try {
    const s = await api('/api/vl/status');
    renderVl(s);
    const sel = $('#vl-folder');
    if (sel && sel.options.length <= 1 && window.__eg2Folders) {
      sel.innerHTML = '<option value="">全部文件夹</option>'
        + window.__eg2Folders.map((f) => '<option value="' + f.id + '">'
            + esc(String(f.path).split(/[\\/]/).filter(Boolean).pop())
            + '</option>').join('');
    }
  } catch (e) { /* 忽略 */ }
}

/* 查看/清空「坏图跳过名单」。
   名单里只有**解码就失败**的图（文件本身读不了，换模型也没用）。
   网络错误、模型错误从来不记，那些下次会自动重试。
   所以清空这个名单是安全的。 */
async function showSkipList() {
  try {
    const r = await api('/api/vl/skip');
    if (!r.count) {
      alert('没有被跳过的坏图。\n\n只有「文件本身读不了」的图会进这个名单；'
        + '网络错误、模型错误不会记，那些下次会自动重试。');
      return;
    }
    const lines = (r.items || []).slice(0, 30)
      .map((x) => '  ' + (x.name || ('#' + x.file_id)) + '  —  ' + (x.reason || ''));
    const msg = '被跳过的坏图：' + r.count + ' 张\n\n'
      + lines.join('\n')
      + (r.count > 30 ? '\n  …（还有 ' + (r.count - 30) + ' 张）' : '')
      + '\n\n这些都是文件本身读不了的图，换模型也读不了。'
      + '\n网络错误和模型错误不会被记在这里，那些下次会自动重试。'
      + '\n\n点「确定」清空名单，让它们下次重新尝试？';
    if (confirm(msg)) {
      const c = await post('/api/vl/skip/clear', {});
      alert('已清空 ' + c.cleared + ' 条。下次开始打标会重新尝试这些图。');
    }
  } catch (e) {
    alert('读取失败：' + e.message);
  }
}

function restoreChecks() {
  // 「用外部模型打标 / 回答」这两个勾选也记住，省得每次都点
  [['vl-use-remote', 'eg2-vlremote']]
    .forEach(([id, key]) => {
      const el = document.getElementById(id);
      if (!el) return;
      try {
        const v = localStorage.getItem(key);
        if (v !== null) el.checked = (v === '1');
      } catch (e) { /* 忽略 */ }
      el.addEventListener('change', () => {
        try { localStorage.setItem(key, el.checked ? '1' : '0'); } catch (e) {}
      });
    });
}

/* ---- 打标模型下拉（项目主 2026-10-18：任意选，可以和对话模型不一样）----
   选项 = 本机那个 VL 模型 + 「外部模型」卡里每一套端点（各带它的模型名）✓
   选「本机」= 走本机 VL（省显存以外都最稳）；选外部 = 直接把那套配置传给后端 ✓
   和对话模型**完全独立** ✓（对话模型在下面的「外部模型」卡里另外选） */
function vlModelChoices() {
  const out = [{ id: 'local', label: '本机视觉模型（推荐）', remote: null }];
  const list = (function () {
    try { return JSON.parse(localStorage.getItem('eg2-rmapilist') || '[]'); } catch (e) { return []; }
  })();
  list.forEach((s, i) => {
    if (!s || !s.base_url || !s.model) return;
    let hdr = null;
    if (s.headers) { try { hdr = JSON.parse(s.headers); } catch (e) { hdr = null; } }
    out.push({
      id: 'remote:' + i,
      label: `外部 · ${s.name || s.base_url} · ${s.model}（需能看图）`,
      remote: { base_url: s.base_url, model: s.model, api_key: s.api_key || '',
                headers: hdr, max_side: 448, max_tokens: 800 },
    });
  });
  return out;
}

function vlChosen() {
  const sel = $('#vl-model');
  if (!sel) return null;
  const hit = vlModelChoices().find((c) => c.id === sel.value);
  const base = hit || vlModelChoices()[0];
  if (!base.remote) return base;
  // 模型名以输入框为准 ✓（同一个接口可以填不同的模型）
  const typed = (($('#vl-model-name') || {}).value || '').trim();
  return { ...base, remote: { ...base.remote, model: typed || base.remote.model } };
}

function fillVlModel() {
  const sel = $('#vl-model');
  if (!sel) return;
  const choices = vlModelChoices();
  const keep = (function () {
    try { return localStorage.getItem('eg2-vlmodel') || ''; } catch (e) { return ''; }
  })();
  // ★ 2026-10-21 项目主："服务器用户图片打标用API可行，服务器本地模型也变灰"
  //   服务器上没有本地视觉模型 → 「本机」选项直接 disabled，默认改成第一个外部接口 ✓
  sel.innerHTML = choices.map((c) => {
    const off = (!LOCAL_VL_OK && c.id === 'local');
    return `<option value="${esc(c.id)}"${off ? ' disabled' : ''}>`
      + esc(c.label) + (off ? '（服务器没有本地模型，请选外部 API）' : '') + '</option>';
  }).join('');
  const keepOk = choices.some((c) => c.id === keep && (LOCAL_VL_OK || c.id !== 'local'));
  if (keepOk) {
    sel.value = keep;
  } else if (LOCAL_VL_OK) {
    sel.value = 'local';
  } else {
    const alt = choices.find((c) => c.id !== 'local');
    sel.value = alt ? alt.id : (choices[0] ? choices[0].id : '');
  }

  // 模型名候选：所有外部接口里出现过的模型名（去重）✓
  const names = [];
  (function () {
    try {
      JSON.parse(localStorage.getItem('eg2-rmapilist') || '[]').forEach((s) => {
        if (s && s.model && !names.includes(s.model)) names.push(s.model);
      });
    } catch (e) { /* 忽略 */ }
  })();
  const dl = $('#vl-model-names');
  if (dl) dl.innerHTML = names.map((n) => `<option value="${esc(n)}"></option>`).join('');

  const applyName = () => {
    const c = choices.find((x) => x.id === sel.value);
    const input = $('#vl-model-name');
    if (!input) return;
    if (!c || !c.remote) {                 // 本机：不需要模型名
      input.disabled = true;
      input.value = '';
      input.placeholder = '（本机模型，不用填）';
      return;
    }
    input.disabled = false;
    const saved = (function () {
      try { return localStorage.getItem('eg2-vlmodelname') || ''; } catch (e) { return ''; }
    })();
    input.value = saved || c.remote.model;
    input.placeholder = c.remote.model || '模型名';
  };
  applyName();

  sel.onchange = () => {
    try { localStorage.setItem('eg2-vlmodel', sel.value); } catch (e) { /* 忽略 */ }
    try { localStorage.removeItem('eg2-vlmodelname'); } catch (e) { /* 忽略 */ }
    applyName();
    syncProbeBtn();                 // ★ 切回本机时要把「探测」按钮禁用 ✓
    refreshVlWho();
    // 切到外部接口时**自动探测一次**（省得再点一下；失败了也能手填 ✓）
    const picked = choices.find((x) => x.id === sel.value);
    if (picked && picked.remote) setTimeout(() => vlProbe(), 0);
  };
  const nameInput = $('#vl-model-name');
  if (nameInput) {
    nameInput.onchange = () => {
      try { localStorage.setItem('eg2-vlmodelname', nameInput.value.trim()); } catch (e) { /* 忽略 */ }
      refreshVlWho();
    };
  }
  const probeBtn = $('#vl-probe');
  if (probeBtn) {
    probeBtn.onclick = () => vlProbe();
  }
  syncProbeBtn();
}

/* 探测当前选中接口的模型列表（项目主 2026-10-18：外部接口有多个模型，要能选）*/
function syncProbeBtn() {
  const btn = $('#vl-probe');
  const input = $('#vl-model-name');
  const c = vlChosen();
  const isRemote = !!(c && c.remote);
  if (btn) btn.disabled = !isRemote;
  if (input) input.disabled = !isRemote;
}

async function vlProbe() {
  const c = vlChosen();
  const msg = $('#vl-probe-msg');
  const say = (text, bad) => {
    if (msg) {
      msg.textContent = text;
      msg.style.color = bad ? 'var(--bad)' : 'var(--muted)';
    }
  };
  if (!c || !c.remote) {
    say('先在上面选一个「外部 · …」接口，才能探测', true);
    return;
  }
  const btn = $('#vl-probe');
  const back = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '探测中…'; }
  say('正在探测 ' + c.remote.base_url + ' …（外网请求，可能要几秒）');
  try {
    const r = await post('/api/remote/probe', {
      base_url: c.remote.base_url,
      api_key: c.remote.api_key || '',
      headers: c.remote.headers || null,
    });
    if (!r || !r.ok) {
      const why = (r && (r.error || r.detail)) || '接口没返回模型';
      say('✗ 探测失败：' + why, true);
      toast('探测失败：' + why, true);
      return;
    }
    const list = (r.models || []).map((m) => (typeof m === 'string' ? { name: m } : m));
    const dl = $('#vl-model-names');
    if (dl) {
      dl.innerHTML = list.map((m) => `<option value="${esc(m.name)}">${
        esc(m.name)}${m.gb ? '（' + esc(m.gb) + ' GB）' : ''}</option>`).join('');
    }
    // ★ 真正能看见、能点的模型列表（datalist 不直观 ✗）
    const pick = $('#vl-model-pick');
    if (pick) {
      pick.innerHTML = list.map((m) => `<option value="${esc(m.name)}">${
        esc(m.name)}${m.gb ? '（' + esc(m.gb) + ' GB）' : ''}</option>`).join('');
      pick.style.display = list.length ? '' : 'none';
      pick.onchange = () => {
        const input = $('#vl-model-name');
        if (input) input.value = pick.value;
        try { localStorage.setItem('eg2-vlmodelname', pick.value); } catch (e) { /* 忽略 */ }
        refreshVlWho();
      };
      if (list.length) {
        const cur = ($('#vl-model-name') || {}).value || '';
        pick.value = list.some((m) => m.name === cur) ? cur : list[0].name;
        pick.onchange();
      }
    }
    say(`✓ 探测到 ${list.length} 个模型 — 用右边下拉选`, false);
    const input = $('#vl-model-name');
    if (input && list.length) {
      const cur = input.value.trim();
      if (!cur || !list.some((m) => m.name === cur)) {
        input.value = list[0].name;
        try { localStorage.setItem('eg2-vlmodelname', input.value); } catch (e) { /* 忽略 */ }
      }
    }
    toast(`探测到 ${list.length} 个模型 ✓`);
    refreshVlWho();
  } catch (e) {
    // post() 抛错时通常带着后端 detail（比如 403 需要管理员）✓
    say('✗ 探测失败：' + (e.message || e), true);
    toast('探测失败：' + (e.message || e), true);
  } finally {
    if (btn) { btn.textContent = back || '探测'; btn.disabled = false; }
    syncProbeBtn();
  }
}

/* 显示"打标当前会用哪个模型"（项目主问"怎么确认"→ 直接在界面上写清楚 ✓） */
async function refreshVlWho() {
  const box = $('#vl-who');
  if (!box) return;
  const chosen = vlChosen() || { id: 'local', remote: null };
  if (chosen.remote) {
    box.innerHTML = `当前打标模型：<b>外部 · ${esc(chosen.remote.model)}</b>`
      + `（${esc(chosen.remote.base_url)}）— 接口在<b>设置页「外部模型」卡</b>里增删；`
      + '模型名可以就在左边输入框里改（<b>和对话模型互不影响</b> ✓）。'
      + '<span style="color:var(--warn)">必须是能看图的模型，否则打标会失败</span> ✓';
    return;
  }
  let localName = '';
  try {
    const d = await api('/api/vl/who');
    localName = (d.local_vl && d.local_vl.name) || '';
  } catch (e) { /* 读不到就只显示占位 */ }
  box.innerHTML = `当前打标模型：<b>本机 · ${esc(localName || '(没找到 VL 模型)')}</b>`
    + ' — 想换成外部模型就在左边下拉里选 ✓（和对话模型互不影响）';
}

/* ---------------- 🧭 这台机器是什么形态（个人版 / 管理员版 / 成员版） ---------------- */
// 项目主 2026-10-20："本地版是希望个人专用（应该有管理员版可以统管全局，与个人版区别）"
// 判据由后端给（本机=建库机→personal；服务器上按角色分 admin/member）
let VIEW_MODE = 'personal';
let LOCAL_VL_OK = true;   // 服务器上没有本地视觉模型 → false（下拉里「本机」会变灰）

function applyViewMode(mode) {
  VIEW_MODE = mode || 'personal';
  // 带 data-view 的卡片：列出的形态里没我 → 藏起来
  document.querySelectorAll('[data-view]').forEach((el) => {
    const allow = String(el.getAttribute('data-view') || '').split(/\s+/);
    el.style.display = allow.includes(VIEW_MODE) ? '' : 'none';
  });
  // 管理员入口（用户管理 / 共享设置）只在**管理员版**出现；
  // 个人版虽然登录的也是管理员，但这台机器就我一个人用，不需要那些 ✓
  document.querySelectorAll('[data-admin-entry]').forEach((el) => {
    el.style.display = (VIEW_MODE === 'admin') ? '' : 'none';
  });
  // ★ 2026-10-21 项目主："服务器的用户不能在服务器上建库，这里应该变灰"
  //   后端本来就拦（普通用户在服务器上跑索引会 403），但按钮亮着、点了才报错，
  //   体验很差 ✗ → 直接在**成员形态**下灰掉，并把原因写在悬停提示里 ✓
  // 类别设置（cat-settings）也是**全局**口径：它决定各类文件夹默认怎么扫，
  // 服务器上的普通用户改它会波及全站 ✗ → 一并灰掉
  // ★ 2026-10-21 项目主："服务器上，这些（人物聚类那一排）用户也变灰吧，
  //   这些是用户本机版功能。个人都在自己本机上建好、共享上传服务器才显示人像。"
  // ⚠ 2026-10-21 修正：这里原来写的是 people-sort，但它是**排序下拉**（不该禁）✗
  //   「合并相似组」真正的 id 是 people-merge-similar ✓
  const MEMBER_OFF = ['add-folder', 'btn-plan', 'btn-night', 'btn-ocr-cfg',
                      'cat-settings', 'ig-save', 'ig-preset', 'ig-reload',
                      'people-rebuild', 'people-assign', 'people-dedup',
                      'people-merge-similar', 'cluster-thr',
                      // 补提人脸（含"范围"下拉）也是本机版功能：服务器上没 GPU、
                      // 也不该让普通用户往共享库里写人脸 ✗
                      'backfill-folder', 'face-backfill'];
  MEMBER_OFF.forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (VIEW_MODE === 'member') {
      el.disabled = true;
      el.title = '这是本机版的功能（建库 / 索引 / 人物聚类）：'
        + '请在你自己的电脑上操作，建好再共享上传到这里 ✓';
      el.style.opacity = '0.45';
      el.style.cursor = 'not-allowed';
    } else if (!el.dataset.origTitle) {
      el.style.opacity = '';
      el.style.cursor = '';
    }
  });
  // 普通用户在服务器上：这里是他自己的配置，要说清楚"管理员的不共享给你" ✓
  const rmHint = document.getElementById('rm-mine-hint');
  if (rmHint) rmHint.style.display = (VIEW_MODE === 'member') ? 'block' : 'none';

  // 顶部小牌上标一下形态，省得搞不清自己在哪个版本
  const badge = document.getElementById('view-mode-badge');
  if (badge) {
    badge.textContent = ({ personal: '个人版', admin: '管理员版', member: '成员' })[VIEW_MODE] || '';
    badge.style.display = VIEW_MODE === 'admin' ? 'none' : '';
  }
}

/* ---------------- 🤝 共享给我的 ---------------- */
async function loadGifts() {
  const box = $('#gift-list');
  if (!box) return;
  try {
    const d = await api('/api/shared-with-me?_=' + Date.now());
    const items = d.items || [];
    $('#gift-badge').textContent = items.length + ' 个';
    if (!items.length) {
      box.innerHTML = '<div class="hint" style="padding:8px">还没有人共享文件夹给你。</div>';
      return;
    }
    box.innerHTML = items.map((x) =>
      '<div class="folder" style="align-items:center;gap:8px">'
      + '<span class="badge mode">' + esc(x.owner) + '</span>'
      + '<span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;'
      + 'white-space:nowrap" title="' + esc(x.path) + '">' + esc(x.path) + '</span>'
      + '<span class="hint" style="flex:none">' + x.n_files + ' 文件 / '
      + x.n_chunks + ' 片段</span></div>').join('');
  } catch (e) {
    const m = String(e && e.message || '');
    if (m.indexOf('404') >= 0 || m.indexOf('Not Found') >= 0) {
      const c = $('#gift-card');
      if (c) c.remove();
      return;
    }
    box.innerHTML = '<div class="hint" style="padding:8px">读不到：' + esc(m) + '</div>';
  }
}

function bindGifts() {
  if (!$('#gift-list')) return;
  // 这几张「设置页」的卡片以前不分页面常驻轮询（10 秒一次），
  // 现在只在设置页可见时跑；切到设置页时会立刻补拉一次（见 refreshTabData）✓
  if (tabActive('settings')) loadGifts();
  setInterval(() => { if (tabActive('settings')) loadGifts(); }, 15000);
}

/* ---------------- 📦 我的数据（自己的能撤，别人的不能） ---------------- */
function mineMsg(text, bad) {
  const el = $('#mine-msg');
  if (!el) return;
  el.innerHTML = text || '';
  el.style.color = bad ? 'var(--bad)' : '';
}

async function loadMine() {
  const box = $('#mine-list');
  if (!box) return;
  try {
    const d = await api('/api/my/data?_=' + Date.now());
    $('#mine-badge').textContent = d.count + ' 个 · ' + d.total_mb + ' MB';
    if (!d.items.length) {
      box.innerHTML = '<div class="hint" style="padding:8px">这里还没有你的数据。</div>';
      return;
    }
    box.innerHTML = d.items.map((x) => {
      // 共享入口：只有自己的能改（别人的连按钮都不给）
      const shareBtn = x.can_withdraw
        ? '<a class="ghost tiny" href="/static/shared.html#f' + x.id
          + '" style="text-decoration:none;padding:2px 8px;border-radius:6px;'
          + 'border:1px solid var(--line);color:var(--fg)">共享…</a>'
        : '';
      const own = x.can_withdraw
        ? '<button class="ghost tiny" data-mw="' + x.id + '">撤下</button>'
        : '<span class="hint" title="不是你的，撤不了">别人上传</span>';
      return '<div class="folder" style="align-items:center;gap:8px">'
        + '<span class="badge mode">' + (x.visibility === 'shared' ? '共享' : '私有') + '</span>'
        + (x.privacy ? '<span class="badge mode">🔒</span>' : '')
        + '<span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;'
        + 'white-space:nowrap" title="' + esc(x.path) + '">' + esc(x.path) + '</span>'
        + '<span class="hint" style="flex:none">' + x.n_files + ' 文件 / '
        + x.n_chunks + ' 片段 / ' + x.mb + ' MB</span>'
        + shareBtn + own + '</div>';
    }).join('');
    box.querySelectorAll('[data-mw]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const fid = Number(btn.getAttribute('data-mw'));
        const item = d.items.find((i) => i.id === fid);
        if (!confirm('把「' + (item ? item.path : fid) + '」从这台服务器撤下？\n\n'
          + '· 只删这边的数据\n· 你本机上的文件不受影响\n\n确认撤下？')) return;
        mineMsg('正在撤下…');
        try {
          const r = await post('/api/my/withdraw', { folder_id: fid, confirm: true });
          mineMsg(r.message || '已撤下');
          await loadMine();
        } catch (e) {
          mineMsg('撤下失败：' + e.message, true);
        }
      });
    });
  } catch (e) {
    const m = String(e && e.message || '');
    if (m.indexOf('404') >= 0 || m.indexOf('Not Found') >= 0) {
      const c = $('#mine-card');
      if (c) c.remove();
      return;
    }
    box.innerHTML = '<div class="hint" style="padding:8px">读不到：' + esc(m) + '</div>';
  }
}

function bindMine() {
  if (!$('#mine-list')) return;
  if (tabActive('settings')) loadMine();
  setInterval(() => { if (tabActive('settings')) loadMine(); }, 15000);
}

/* ---------------- ⬆ 上传到服务器（本机 → 服务器） ---------------- */
let pushPicked = [];

function pushMsg(text, bad) {
  const el = $('#push-log');
  if (!el) return;
  el.innerHTML = text || '';
  el.style.color = bad ? 'var(--bad)' : '';
}

async function loadPushState() {
  const box = $('#push-folders');
  if (!box) return;
  try {
    const s = await api('/api/sync/push/status?_=' + Date.now());
    if (!s.can_push) {                       // 服务器上不显示这个功能
      const c = $('#push-card');
      if (c) c.remove();
      return;
    }
    const acc = s.account || {};
    const ver = s.verified || {};
    // ★ 2026-10-22 项目主："不能在这张卡里随便填账号改变身份" ——
    //   身份只认「服务器连接」里验证通过的那一组（后端 verify 时记下来的）
    const idBox = $('#push-ident');
    if (idBox) {
      const who = ver.name || acc.user || '';
      const roleName = ({ admin: '管理员', user: '普通用户' })[ver.role] || '';
      if (!acc.has_token && !(acc.user && acc.has_password)) {
        idBox.innerHTML = '⚠ <b style="color:var(--warn)">还没验证服务器身份</b>：'
          + '去「服务器连接」填账号 + 密码点「保存并验证」，通过后才能上传 ✓';
      } else if (!ver.name) {
        // 有令牌但没验证过：身份其实是令牌主人的，本机不知道是谁 —— 如实说
        idBox.innerHTML = '🌐 目标服务器：<code>' + esc(acc.server || '—') + '</code><br>'
          + '👤 上传身份：<b style="color:var(--warn)">以已保存的令牌身份（本机未验证过）</b><br>'
          + '📦 上传后数据归属：令牌对应的那个账号　'
          + '<span style="opacity:.75">（想知道是谁 / 想换人，去「服务器连接」点「保存并验证」）</span>';
      } else {
        idBox.innerHTML = '🌐 目标服务器：<code>' + esc(acc.server || '—') + '</code><br>'
          + '👤 上传身份：<b>' + esc(who) + '</b>'
          + (roleName ? '（服务器 ' + roleName + '）' : '')
          + (ver.at ? '　·　上次验证：' + esc(ver.at) : '') + '<br>'
          + '📦 上传后数据归属：<b>' + esc(who) + '</b>'
          + '　<span style="opacity:.75">（要换身份就去「服务器连接」重新验证）</span>';
      }
    }
    $('#push-badge').textContent = s.running ? '进行中…' : '就绪';

    const rows = [];
    if (s.running) {
      rows.push('<span style="color:var(--accent)">' + esc(s.phase)
        + '：' + esc(s.note) + '</span>');
    }
    if (s.error) rows.push('<span style="color:var(--bad)">出错：' + esc(s.error) + '</span>');
    if (s.result) {
      const r = s.result;
      rows.push((r.dry_run ? '🔍 <b>预演结果</b>' : '✓ <b>上传结果</b>')
        + '：文件夹 新建 ' + (r.new_folders || 0) + ' / 命中 ' + (r.hit_folders || 0)
        + '，文件 新建 ' + (r.new_files || 0) + ' / 命中 ' + (r.hit_files || 0)
        + '，片段 新建 ' + (r.new_chunks || 0) + ' / 命中 ' + (r.hit_chunks || 0));
      // ★ 维度检查（项目主 2026-10-21）：不一致就是"传了也用不了"，必须说清楚
      const sdim = r.server_dim || r.dim;
      const skipped = r.skipped_dim || 0;
      if (r.dim_ok === false) {
        // ★ 维度不同**不是错误**：系统按维度分组检索，混合维度能一起搜 ✓
        //   只是会多占空间、跨维度比较略慢 → 提示"建议统一"，不拦上传
        rows.push('<span style="color:var(--warn)">💡 <b>维度不同</b>：'
          + '你这边 ' + (r.dim || '?') + ' 维，服务器 ' + (sdim || '?') + ' 维。'
          + '<b>能用</b>（检索会按维度分组，一样能搜到），'
          + '但统一成 ' + (sdim || '?') + ' 维更省空间、更快。<br>'
          + '想统一：把本机这个文件夹的维度改成 ' + (sdim || '?')
          + ' 维后<b>重建索引</b>，再上传 ✓</span>');
      } else if (sdim) {
        rows.push('<span style="color:var(--ok)">维度一致（' + sdim + ' 维）✓</span>');
      }
      if (skipped && r.dim_ok !== false) {
        rows.push('<span style="color:var(--warn)">（有 ' + skipped
          + ' 条片段因维度不符被跳过）</span>');
      }
    }
    $('#push-state').innerHTML = rows.join('<br>');

    // 文件夹多选（只在第一次或列表变化时重建，避免把用户的勾选冲掉）
    if (!box.dataset.built) {
      const f = await api('/api/folders?_=' + Date.now());
      const list = (f.folders || []);
      if (!list.length) {
        box.innerHTML = '<div class="hint">本机还没有文件夹。</div>';
      } else {
        box.innerHTML = list.map((x) => {
          const n = (x.stats && x.stats.files) || x.n_files || '';
          return '<label class="hint" style="display:flex;align-items:center;gap:8px;padding:3px 0">'
            + '<input type="checkbox" data-pf="' + x.id + '">'
            + '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'
            + esc(x.path) + '</span>'
            + (n !== '' ? '<span>' + n + ' 个文件</span>' : '') + '</label>';
        }).join('');
        box.dataset.built = '1';
        box.querySelectorAll('[data-pf]').forEach((el) => {
          el.addEventListener('change', () => {
            pushPicked = Array.from(box.querySelectorAll('[data-pf]:checked'))
              .map((e) => Number(e.getAttribute('data-pf')));
            pushMsg('已选 ' + pushPicked.length + ' 个文件夹');
          });
        });
      }
    }

    const running = !!s.running;
    $('#push-go').disabled = running;
    $('#push-dry').disabled = running;
    const wrap = $('#push-bar-wrap');
    if (wrap) wrap.style.display = running ? 'block' : 'none';
    if (running && s.pack_mb) {
      const m = String(s.note || '').match(/(\d+)%/);
      if (m) $('#push-bar').style.width = m[1] + '%';
    } else if (!running) {
      $('#push-bar').style.width = s.result ? '100%' : '0';
    }
  } catch (e) {
    const m = String(e && e.message || '');
    if (m.indexOf('404') >= 0 || m.indexOf('Not Found') >= 0) {
      const c = $('#push-card');
      if (c) c.remove();
      return;
    }
    box.innerHTML = '<div class="hint">读不到状态：' + esc(m) + '</div>';
  }
}

function bindPush() {
  if (!$('#push-state')) return;
  // 这张卡不再提供"服务器地址 / 账号 / 密码"输入（2026-10-22 项目主）：
  // 身份只由「服务器连接」验证决定，避免在这里临时换成别人再传文件 ✗
  $('#push-select-all').onclick = () => {
    document.querySelectorAll('#push-folders [data-pf]').forEach((el) => {
      el.checked = true;
    });
    pushPicked = Array.from(document.querySelectorAll('#push-folders [data-pf]'))
      .map((e) => Number(e.getAttribute('data-pf')));
    pushMsg('已选 ' + pushPicked.length + ' 个文件夹');
  };
  $('#push-select-none').onclick = () => {
    document.querySelectorAll('#push-folders [data-pf]').forEach((el) => {
      el.checked = false;
    });
    pushPicked = [];
    pushMsg('已清空选择');
  };
  const go = async (dry) => {
    if (!pushPicked.length) { pushMsg('先勾选要上传的文件夹', true); return; }
    if (!dry && !confirm('真的把这 ' + pushPicked.length
      + ' 个文件夹上传到服务器？\n（服务器按路径合并，只增不删）')) return;
    pushMsg(dry ? '开始预演…' : '开始上传…');
    try {
      const r = await post('/api/sync/push', { folder_ids: pushPicked, dry_run: dry });
      if (!r.ok) pushMsg(r.message || '没启动', true);
      await loadPushState();
    } catch (e) { pushMsg('启动失败：' + e.message, true); }
  };
  $('#push-dry').onclick = () => go(true);
  $('#push-go').onclick = () => go(false);
  // 首次仍然拉一次（它还会带回 view_mode，全站的可见性靠它 ✓，接口很快）；
  // 但 3 秒一次的轮询改成「只在设置页可见时、8 秒一次」✓
  loadPushState();
  setInterval(() => { if (tabActive('settings')) loadPushState(); }, 8000);
}

/* ---------------- 📦 快照与异地副本 ---------------- */
function snapMsg(text, bad) {
  const el = $('#snap-msg');
  if (!el) return;
  el.textContent = text || '';
  el.style.color = bad ? 'var(--bad)' : 'var(--ok)';
}

async function loadSnapshots() {
  const box = $('#snap-list');
  if (!box) return;
  try {
    const s = await api('/api/snapshot/status?_=' + Date.now());
    const usedTxt = (s.used_gb === null || s.used_gb === undefined)
      ? '统计中…' : (s.used_gb + ' GB');
    $('#snap-badge').textContent = s.count + ' 份 · ' + usedTxt;
    $('#snap-target').value = s.target || '';
    const tgt = s.target
      ? (s.target_ok ? '✓ 可用' : '✗ 不存在或没挂载')
      : '（还没设置）';
    const rows = [];
    rows.push('快照总数：<b>' + s.count + '</b> 份，占 <b>' + usedTxt + '</b>');
    rows.push('同步目标：<code>' + esc(s.target || '—') + '</code> ' + tgt
      + (s.sync_last ? ' · 上次同步 ' + esc(s.sync_last) : ''));
    if (s.running) rows.push('<span style="color:var(--accent)">📸 ' + esc(s.note || '正在拍…') + '</span>');
    if (s.syncing) rows.push('<span style="color:var(--accent)">⬆ ' + esc(s.note || '正在同步…') + '</span>');
    if (!s.running && !s.syncing && s.note) rows.push('上次：' + esc(s.note));
    if (s.error) rows.push('<span style="color:var(--bad)">出错：' + esc(s.error) + '</span>');
    if (s.pending) {
      rows.push('⏳ <b>待还原：' + esc(s.pending.name) + '</b>（代码='
        + (s.pending.code ? '是' : '否') + '，库=' + (s.pending.db ? '是' : '否')
        + '）—— 重启服务时自动执行');
    }
    $('#snap-state').innerHTML = rows.join('<br>');

    if (!s.snapshots.length) {
      box.innerHTML = '<div class="empty">还没有快照。</div>';
    } else {
      box.innerHTML = s.snapshots.map((x) => {
        const t = x.name.replace('snapshot-', '');
        const when = t.slice(0, 4) + '-' + t.slice(4, 6) + '-' + t.slice(6, 8)
          + ' ' + t.slice(9, 11) + ':' + t.slice(11, 13);
        return '<div class="folder" style="align-items:center">'
          + '<span class="badge mode">' + esc(when) + '</span>'
          + '<span class="hint" style="flex:1;min-width:0;overflow:hidden;'
          + 'text-overflow:ellipsis;white-space:nowrap">'
          + esc(x.note || '（没写备注）') + '</span>'
          + '<span class="hint" style="flex:none">'
          + (x.has_db ? x.db_mb + ' MB 库' : '无库') + '</span></div>';
      }).join('');
    }

    $('#snap-run').disabled = !!s.running;
    $('#snap-sync').disabled = !!s.syncing || !s.target_ok;
  } catch (e) {
    const m = String(e && e.message || '');
    if (m.indexOf('404') >= 0 || m.indexOf('Not Found') >= 0) {
      const c = $('#snap-card');
      if (c) c.remove();
      return;
    }
    box.innerHTML = '<div class="empty">读不到快照状态：' + esc(m) + '</div>';
  }
}

function bindSnapshot() {
  if (!$('#snap-state')) return;
  $('#snap-run').onclick = async () => {
    const note = ($('#snap-note').value || '').trim();
    snapMsg('已开始拍（约 1~2 分钟，可以去做别的）');
    try {
      const r = await post('/api/snapshot/run', { note });
      if (!r.ok) snapMsg(r.message || '没启动', true);
      await loadSnapshots();
    } catch (e) { snapMsg('启动失败：' + e.message, true); }
  };
  $('#snap-save').onclick = async () => {
    const path = ($('#snap-target').value || '').trim();
    try {
      await post('/api/snapshot/target', { path }, 'PUT');
      snapMsg(path ? '已保存目标路径' : '已清空目标路径');
      await loadSnapshots();
    } catch (e) { snapMsg('保存失败：' + e.message, true); }
  };
  $('#snap-sync').onclick = async () => {
    snapMsg('正在同步（大的是库，慢一点）…');
    try {
      const r = await post('/api/snapshot/sync', {});
      if (!r.ok) snapMsg(r.message || '没开始', true);
      await loadSnapshots();
    } catch (e) { snapMsg('同步失败：' + e.message, true); }
  };
  // ---- 还原（2026-10-20 项目主："在按钮旁也应有一个还原按钮吧？"）----
  const openRestore = async () => {
    const wrap = $('#restore-picker');
    if (wrap) wrap.style.display = 'flex';
    $('#restore-msg').textContent = '';
    try {
      const s = await api('/api/snapshot/status?_=' + Date.now());
      const sel = $('#restore-name');
      sel.innerHTML = (s.snapshots || []).map((x) => {
        const t0 = x.name.replace('snapshot-', '');
        const when = t0.slice(0, 4) + '-' + t0.slice(4, 6) + '-' + t0.slice(6, 8)
          + ' ' + t0.slice(9, 11) + ':' + t0.slice(11, 13);
        return '<option value="' + esc(x.name) + '">' + when + ' · '
          + (x.has_db ? (x.db_mb + 'MB 库') : '无库') + ' · '
          + esc(x.note || '（无备注）') + '</option>';
      }).join('');
    } catch (e) {
      $('#restore-msg').textContent = '读不到快照：' + e.message;
    }
  };
  if ($('#snap-restore')) $('#snap-restore').onclick = openRestore;
  if ($('#restore-close')) {
    $('#restore-close').onclick = () => {
      $('#restore-picker').style.display = 'none';
    };
  }
  if ($('#restore-go')) {
    $('#restore-go').onclick = async () => {
      const name = $('#restore-name').value;
      const code = $('#restore-code').checked;
      const db = $('#restore-db').checked;
      if (!name) { $('#restore-msg').textContent = '没有可选的快照'; return; }
      if (!code && !db) {
        $('#restore-msg').textContent = '至少选一样（代码 / 索引库）';
        return;
      }
      if (db && !confirm('还原索引库会把现在的数据整个换掉。\n'
        + '（会先把现有库改名成 index-before-restore-*.db 留底）\n\n确认？')) {
        return;
      }
      try {
        const r = await post('/api/snapshot/restore', { name, code, db });
        $('#restore-msg').textContent = r.message || '已登记';
        await loadSnapshots();
      } catch (e) {
        $('#restore-msg').textContent = '登记失败：' + e.message;
      }
    };
  }

  // ---- 非管理员：这些是"改整站"的操作，一律禁用（服务器版尤其重要）----
  api('/api/auth/me').then((d) => {
    const admin = !d || !d.user || d.user.role === 'admin';
    if (admin) return;
    ['snap-run', 'snap-save', 'snap-sync', 'snap-restore'].forEach((id) => {
      const el = $('#' + id);
      if (el) { el.disabled = true; el.title = '需要管理员权限'; }
    });
  });

  if (tabActive('settings')) loadSnapshots();
  setInterval(() => { if (tabActive('settings')) loadSnapshots(); }, 10000);
}

/* ---------------- 🔒 隐私文件夹：普通 / 隐私 两个分区，一键来回切 ----------------
   2026-10-20 项目主："干脆文件夹做成两个大分类吧，一类普通一类隐私，但可以切换" ✓ */
async function loadPrivacy() {
  const box = $('#priv-list');
  if (!box) return;
  try {
    const r = await api('/api/folders?_=' + Date.now());
  if (r.view_mode) applyViewMode(r.view_mode);
  if (typeof r.local_vl_ok === 'boolean') LOCAL_VL_OK = r.local_vl_ok;
    const list = (r.folders || []);
    const normal = list.filter((f) => !f.privacy);
    const priv = list.filter((f) => f.privacy);
    $('#priv-badge').textContent =
      priv.length + ' 个隐私 / ' + normal.length + ' 个普通';
    const row = (f, isPriv) => {
      const name = f.name || f.path || ('#' + f.id);
      return '<div style="display:flex;align-items:center;gap:8px;padding:5px 8px;'
        + 'border-bottom:1px dashed rgba(255,255,255,.06)">'
        + '<span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;'
        + 'white-space:nowrap" title="' + esc(f.path || '') + '">'
        + (isPriv ? '🔒 ' : '') + esc(name) + '</span>'
        + '<button class="ghost tiny" data-priv-id="' + f.id + '" data-priv-on="'
        + (isPriv ? '0' : '1') + '">' + (isPriv ? '← 改为普通' : '设为隐私 →')
        + '</button></div>';
    };
    const col = (title, items, isPriv) =>
      '<div style="flex:1;min-width:280px">'
      + '<div class="hint" style="margin-bottom:6px"><b>' + title
      + '</b>（' + items.length + '）</div>'
      + (items.length
        ? items.map((f) => row(f, isPriv)).join('')
        : '<div class="hint" style="opacity:.6">（没有）</div>')
      + '</div>';
    box.innerHTML = '<div style="display:flex;gap:18px;flex-wrap:wrap">'
      + col('普通文件夹', normal, false)
      + col('🔒 隐私文件夹（内容不出机器）', priv, true)
      + '</div>';
    box.querySelectorAll('[data-priv-id]').forEach((el) => {
      el.addEventListener('click', async () => {
        const on = el.dataset.privOn === '1';
        el.disabled = true;
        try {
          await post('/api/folders/' + el.dataset.privId + '/privacy',
            { privacy: on }, 'PUT');
          await loadPrivacy();
        } catch (e) {
          alert('切换失败：' + e.message);
          el.disabled = false;
        }
      });
    });
  } catch (e) {
    const m = String(e && e.message || '');
    if (m.indexOf('404') >= 0 || m.indexOf('Not Found') >= 0) {
      const c = $('#priv-card');
      if (c) c.remove();
      return;
    }
    box.innerHTML = '读不到文件夹列表：' + esc(m);
  }
}

/* ---------------- 📄 只打某几份 PDF ---------------- */
let cpqPicked = [];      // 选中的 file_id（空 = 全部待标）
let cpqFileList = [];    // 上一次拉到的文件清单

function cpqScopeNote() {
  const el = $('#cpq-scope-note');
  if (!el) return;
  if (!cpqPicked.length) {
    el.innerHTML = '范围：<b>全部待标</b>';
    return;
  }
  el.innerHTML = '范围：<b>选中的 ' + cpqPicked.length + ' 份文件</b>'
    + ' <a href="#" id="cpq-clear-scope" style="color:var(--accent)">清空</a>';
  const a = $('#cpq-clear-scope');
  if (a) {
    a.onclick = (ev) => {
      ev.preventDefault();
      cpqPicked = [];
      cpqScopeNote();
    };
  }
}

async function cpqStart(fileIds) {
  const n = Number($('#cpq-limit').value) || 200;
  const payload = { limit: n };
  if (fileIds && fileIds.length) payload.file_ids = fileIds;
  cpqMsg('正在启动…');
  try {
    const r = await post('/api/caption/queue/start', payload);
    cpqMsg(r.ok
      ? (fileIds && fileIds.length
        ? ('已开始（只打选中的 ' + fileIds.length + ' 份）') : '已开始（全部待标）')
      : (r.message || '没启动'), !r.ok);
    await loadCaptionQueue();
  } catch (e) { cpqMsg('启动失败：' + e.message, true); }
}

async function openCpPicker() {
  const wrap = $('#cpq-picker');
  const box = $('#cpq-files');
  if (!wrap || !box) return;
  wrap.style.display = 'flex';
  box.innerHTML = '正在读取…';
  $('#cpq-pick-msg').textContent = '';
  try {
    const r = await api('/api/caption/queue/files?_=' + Date.now());
    cpqFileList = r.files || [];
    if (!cpqFileList.length) {
      box.innerHTML = '<div class="hint">队列里没有待标的文件。</div>';
      return;
    }
    box.innerHTML = cpqFileList.map((f) => {
      const own = String(f.file_caption_model || '');
      return '<div style="display:flex;align-items:center;gap:8px;padding:5px 4px;'
        + 'border-bottom:1px dashed rgba(255,255,255,.06)">'
        + '<label style="display:flex;align-items:center;gap:6px;flex:1;min-width:0">'
        + '<input type="checkbox" data-cf="' + f.file_id + '"'
        + (cpqPicked.indexOf(f.file_id) >= 0 ? ' checked' : '') + '>'
        + '<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap" '
        + 'title="' + esc(f.name || '') + '">' + (f.privacy ? '🔒 ' : '')
        + esc(f.name || ('#' + f.file_id)) + '</span>'
        + '<span class="hint" style="flex:none">剩 ' + f.pending + ' 张</span>'
        + '</label>'
        + '<select data-cfm="' + f.file_id + '" title="这份文件用哪个模型打标" '
        + 'style="padding:3px 6px;border-radius:6px;border:1px solid var(--line);'
        + 'background:var(--panel2);color:var(--fg)">'
        + '<option value=""' + (own ? '' : ' selected') + '>跟随文件夹</option>'
        + '<option value="local"' + (own === 'local' ? ' selected' : '') + '>本地（不出机器）</option>'
        + '<option value="remote"' + (own === 'remote' ? ' selected' : '') + '>外部 API</option>'
        + '</select></div>';
    }).join('');
    box.querySelectorAll('[data-cf]').forEach((el) => {
      el.addEventListener('change', () => {
        const fid = Number(el.dataset.cf);
        if (el.checked) { if (cpqPicked.indexOf(fid) < 0) cpqPicked.push(fid); }
        else cpqPicked = cpqPicked.filter((x) => x !== fid);
        $('#cpq-pick-msg').textContent = '已选 ' + cpqPicked.length + ' 份';
      });
    });
    box.querySelectorAll('[data-cfm]').forEach((el) => {
      el.addEventListener('change', async () => {
        el.disabled = true;
        try {
          await post('/api/caption/queue/file-model',
            { file_ids: [Number(el.dataset.cfm)], model: el.value });
          $('#cpq-pick-msg').textContent = '模型已保存 ✓';
        } catch (e) {
          $('#cpq-pick-msg').textContent = '保存失败：' + e.message;
        }
        el.disabled = false;
      });
    });
    $('#cpq-pick-msg').textContent = '已选 ' + cpqPicked.length + ' 份';
  } catch (e) {
    box.innerHTML = '<div class="hint">读不到文件清单：' + esc(e.message) + '</div>';
  }
}


function cpqMsg(text, bad) {
  const el = $('#cpq-msg');
  if (!el) return;
  el.textContent = text || '';
  el.style.color = bad ? 'var(--bad)' : 'var(--ok)';
}

function fmtDur(sec) {
  sec = Math.max(0, Math.round(Number(sec) || 0));
  if (sec < 60) return sec + ' 秒';
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  if (m < 60) return m + ' 分' + (s ? s + ' 秒' : '');
  return Math.floor(m / 60) + ' 小时 ' + (m % 60) + ' 分';
}

async function loadCaptionQueue() {
  const box = $('#cpq-state');
  if (!box) return;
  try {
    const s = await api('/api/caption/queue/status');
    const q = s.queue || {};
    const pend = q.pending || 0;
    const doneAll = q.done || 0;
    const failAll = q.failed || 0;
    const running = !!s.running;
    const paused = !!s.paused;
    const total = Number(s.total) || 0;
    const fin = (s.done || 0) + (s.failed || 0) + (s.skipped || 0);
    const pct = total ? Math.min(100, Math.round(fin * 100 / total)) : 0;
    $('#cpq-bar').style.width = pct + '%';
    const badge = $('#cpq-badge');
    badge.textContent = running ? (paused ? '⏸ 已暂停' : '运行中 ' + pct + '%')
      : (pend ? ('待标 ' + pend + ' 张') : '空闲');
    badge.style.color = running ? (paused ? 'var(--warn)' : 'var(--ok)') : '';

    const rows = [];
    rows.push('队列：<b>待标 ' + pend + ' 张</b>'
      + ' · 已完成 ' + doneAll + ' 张'
      + (failAll ? ' · <span style="color:var(--warn)">失败 ' + failAll + ' 张</span>' : ''));
    if (running || total) {
      const per = Number(s.per_item) || 0;
      rows.push('本批：第 <b>' + (s.picked || 0) + '</b> / ' + total + ' 条'
        + '（成功 ' + (s.done || 0) + '，失败 ' + (s.failed || 0) + '）'
        + (per ? ' · 每张约 ' + per + ' 秒' : '')
        + (per && pend ? ' · <b>全部跑完约 ' + fmtDur(per * pend) + '</b>' : ''));
    }
    if (s.current) {
      rows.push('正在处理：<b>' + esc(s.current) + '</b>'
        + (s.current_page ? '（第 ' + s.current_page + ' 页）' : ''));
    }
    rows.push('模型：' + esc(s.model || '—')
      + (s.source === 'night' ? ' · 来源：🚀 全力执行' : (s.source === 'manual' ? ' · 来源：手动开始' : ''))
      + (s.started && (running || s.finished)
        ? ' · ' + (running ? '已跑 ' : '用时 ') + fmtDur(s.elapsed) : ''));
    if (s.last_error) rows.push('<span style="color:var(--bad)">上次出错：' + esc(s.last_error) + '</span>');
    if (!running && !total && s.note) rows.push('上次：' + esc(s.note));
    box.innerHTML = rows.join('<br>');

    $('#cpq-start').disabled = running;
    $('#cpq-pause').disabled = !running || paused;
    $('#cpq-resume').disabled = !running || !paused;
    $('#cpq-stop').disabled = !running;
  } catch (e) {
    const msg = String(e && e.message || '');
    if (msg.indexOf('404') >= 0 || msg.indexOf('Not Found') >= 0) {
      const card = $('#cpq-card');
      if (card) card.remove();
      return;
    }
    box.innerHTML = '读不到打标状态：' + esc(msg);
  }
}

function bindCaptionQueue() {
  if (!$('#cpq-state')) return;
  $('#cpq-start').onclick = () => cpqStart(cpqPicked);
  $('#cpq-choose').onclick = () => openCpPicker();
  const closePick = () => {
    const w = $('#cpq-picker');
    if (w) w.style.display = 'none';
  };
  if ($('#cpq-close')) $('#cpq-close').onclick = closePick;
  if ($('#cpq-all')) {
    $('#cpq-all').onclick = () => {
      cpqPicked = cpqFileList.map((f) => f.file_id);
      $('#cpq-files').querySelectorAll('[data-cf]').forEach((el) => { el.checked = true; });
      $('#cpq-pick-msg').textContent = '已选 ' + cpqPicked.length + ' 份';
    };
  }
  if ($('#cpq-none')) {
    $('#cpq-none').onclick = () => {
      cpqPicked = [];
      $('#cpq-files').querySelectorAll('[data-cf]').forEach((el) => { el.checked = false; });
      $('#cpq-pick-msg').textContent = '已选 0 份';
    };
  }
  if ($('#cpq-ok')) {
    $('#cpq-ok').onclick = async () => {
      closePick();
      cpqScopeNote();
      if (!cpqPicked.length) {
        cpqMsg('没选文件（范围已恢复成全部待标）', true);
        return;
      }
      await cpqStart(cpqPicked);
    };
  }
  cpqScopeNote();
  $('#cpq-pause').onclick = async () => {
    try {
      await post('/api/caption/queue/pause', {});
      cpqMsg('已暂停（当前这条做完就停）');
      await loadCaptionQueue();
    } catch (e) { cpqMsg('暂停失败：' + e.message, true); }
  };
  $('#cpq-resume').onclick = async () => {
    try {
      await post('/api/caption/queue/resume', {});
      cpqMsg('继续中…');
      await loadCaptionQueue();
    } catch (e) { cpqMsg('继续失败：' + e.message, true); }
  };
  $('#cpq-stop').onclick = async () => {
    try {
      await post('/api/caption/queue/stop', {});
      cpqMsg('正在停止（剩下的留在队列里，下次接着跑）');
      await loadCaptionQueue();
    } catch (e) { cpqMsg('停止失败：' + e.message, true); }
  };
  if (tabActive('settings')) loadCaptionQueue();
  // 实时进度：只在设置页可见时跑，5 秒一次（以前不分页面 2 秒一次，
  // 在搜索页也一直打接口，白占服务）✓
  setInterval(() => { if (tabActive('settings')) loadCaptionQueue(); }, 5000);
}

function bindVl() {
  restoreVlHours();
  restoreChecks();
  fillVlModel();          // 打标模型下拉：本机 + 各外部端点 ✓
  refreshVlWho();
  // ⚠ 这里**直接绑一次**探测按钮：不依赖 fillVlModel 里的绑定
  //   （2026-10-18：按钮当时在 <label> 里，点了没反应 → 现在移出来了，并且在这里兜底绑 ✓）
  const _pb = $('#vl-probe');
  if (_pb && !_pb.onclick) _pb.onclick = () => vlProbe();
  syncProbeBtn();
  // 探测/打标都要**管理员**（服务端会 403）。普通用户点了只会看到"需要管理员"✗
  // → 界面上提前说清楚，别让人白点 ✓
  api('/api/auth/me').then((d) => {
    if (!d || !d.user || d.user.role === 'admin') return;
    const btn = $('#vl-probe');
    if (btn) { btn.disabled = true; btn.title = '探测需要管理员权限'; }
    const st = $('#vl-start');
    if (st) { st.disabled = true; st.title = '打标需要管理员权限'; }
    const msg = $('#vl-probe-msg');
    if (msg) msg.textContent = '当前账号是普通用户：探测和打标需要管理员';
  }).catch(() => { /* 拿不到就算了 */ });
  const sk = $('#vl-skip');
  if (sk) sk.addEventListener('click', showSkipList);
  ['vl-h1', 'vl-h2'].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', () => vlHours(true));
  });
  const b = $('#vl-start');
  if (!b) return;
  b.addEventListener('click', async () => {
    b.disabled = true;
    try {
      const body = {};
      const w = vlHours(true);
      if (w) body.window = w;
      const fid = ($('#vl-folder') || {}).value;
      if (fid) body.folder_ids = [Number(fid)];
      // 打标模型：下拉里选的那个（本机 = 不传 remote ✓；外部 = 把整套配置传过去 ✓）
      const chosen = vlChosen() || { remote: null };
      if (chosen.remote) {
        if (!chosen.remote.base_url || !chosen.remote.model) {
          alert('选的外部模型没配好地址/模型，请到设置页「外部模型」卡里补上。');
          return;
        }
        body.remote = chosen.remote;
      }
      const r = await post('/api/vl/caption', body);
      if (!r.started) alert(r.reason || '没能启动');
      loadVl();
    } catch (e) { alert('启动失败：' + e.message); }
    b.disabled = false;
  });
  const st = $('#vl-stop');
  if (st) st.addEventListener('click', async () => {
    await post('/api/vl/stop', {});
    setTimeout(loadVl, 800);
  });
  if (tabActive('settings')) loadVl();
  setInterval(() => { if (tabActive('settings') && $('#vl-state')) loadVl(); }, 30000);
}

/* ---------------- 💾 定时增量备份（2026-10-20 项目主要求）----------------
   后端：app/dbbackup.py（页级差异），接口 /api/backup/*
   要保证的三件事：增量省空间 / 保留份数可自定义 / 固定份永不被轮换删掉 ✓ */
function bkMsg(text, bad) {
  const el = $('#bk-msg');
  if (!el) return;
  el.textContent = text || '';
  el.style.color = bad ? 'var(--bad)' : 'var(--ok)';
}

async function loadBackupStatus() {
  const list = $('#bk-list');
  if (!list) return;
  try {
    const s = await api('/api/backup/status');
    $('#bk-enabled').checked = !!s.enabled;
    $('#bk-keep').value = s.keep;
    $('#bk-interval').value = s.interval_hours;
    $('#bk-badge').textContent = s.count + ' 份 · ' + (Math.round(s.used_mb * 10) / 10) + ' MB';
    const last = s.last || {};
    const when = last.last_check
      ? new Date(last.last_check * 1000).toLocaleString('zh-CN') : '还没跑过';
    $('#bk-state').innerHTML = '目录 <code>' + esc(s.dir) + '</code>'
      + ' · 上次检查 ' + esc(when) + '（' + esc(last.last_result || '—') + '）'
      + ' · 若每份都存全量要 ' + s.would_be_full_mb + ' MB'
      + '<br>' + (s.should_run ? '✓ 该做了：' : '· 还没到点：') + esc(s.why || '');
    if (!s.sets.length) {
      list.innerHTML = '<div class="empty">还没有增量备份。点「立刻备份」。</div>';
      return;
    }
    list.innerHTML = s.sets.slice().reverse().map((b) => {
      const t = b.created ? new Date(b.created * 1000).toLocaleString('zh-CN', {
        month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '';
      return '<div class="folder" style="align-items:center">'
        + '<span class="badge mode">' + (b.kind === 'full' ? '全量' : '增量') + '</span>'
        + '<span class="p">' + esc(b.stamp) + '</span>'
        + '<span class="hint">' + esc(t) + ' · 存 ' + fmtBytes(b.stored_bytes)
        + (b.kind === 'incr' ? '（变动 ' + b.changed_pages + ' 页）' : '')
        + ' · 整库 ' + fmtBytes(b.file_size) + '</span>'
        + (b.pinned ? '<span class="badge">📌 固定</span>' : '')
        + '<button class="ghost tiny" data-bk-pin="' + esc(b.stamp)
        + '" data-bk-on="' + (b.pinned ? '0' : '1') + '">'
        + (b.pinned ? '取消固定' : '📌 固定') + '</button>'
        + '<button class="ghost tiny" data-bk-verify="' + esc(b.stamp) + '">校验能恢复</button>'
        + '</div>';
    }).join('');
    list.querySelectorAll('[data-bk-pin]').forEach((el) => {
      el.addEventListener('click', async () => {
        el.disabled = true;
        try {
          await post('/api/backup/pin',
            { stamp: el.dataset.bkPin, pinned: el.dataset.bkOn === '1' });
          await loadBackupStatus();
        } catch (e) { bkMsg('固定失败：' + e.message, true); }
        el.disabled = false;
      });
    });
    list.querySelectorAll('[data-bk-verify]').forEach((el) => {
      el.addEventListener('click', async () => {
        el.disabled = true;
        bkMsg('正在重建这份备份做校验（大库要几秒）…');
        try {
          const r = await post('/api/backup/verify', { stamp: el.dataset.bkVerify });
          bkMsg(r.ok ? '✓ 校验通过：这份能完整恢复' : '✗ 校验不一致，别依赖这份', !r.ok);
        } catch (e) { bkMsg('校验失败：' + e.message, true); }
        el.disabled = false;
      });
    });
  } catch (e) {
    // 服务器版可能还没同步这组接口（那边是 crontab 在跑备份）→
    // 拿不到就直接把这张卡收起来，别留一个"读不到"的坏卡片让人以为坏了 ✓
    const msg = String(e && e.message || '');
    if (msg.indexOf('404') >= 0 || msg.indexOf('Not Found') >= 0) {
      const card = $('#bk-card');
      if (card) card.remove();
      return;
    }
    list.innerHTML = '<div class="empty">读不到备份状态：' + esc(msg) + '</div>';
  }
}

function bindBackup() {
  if (!$('#bk-list')) return;
  $('#bk-refresh').onclick = () => loadBackupStatus();
  $('#bk-run').onclick = async () => {
    const b = $('#bk-run');
    b.disabled = true; b.textContent = '备份中…';
    bkMsg('正在做一致性快照（大库要几秒）…');
    try {
      const r = await post('/api/backup/run', {});
      if (r.changed) {
        bkMsg('✓ ' + (r.message || '已存') + '，这次落盘 ' + fmtBytes(r.stored_bytes));
      } else {
        bkMsg('· ' + (r.message || '没有变化'));
      }
      await loadBackupStatus();
    } catch (e) { bkMsg('备份失败：' + e.message, true); }
    b.disabled = false; b.textContent = '立刻备份';
  };
  $('#bk-prune').onclick = async () => {
    try {
      const r = await post('/api/backup/prune',
        { keep: Number($('#bk-keep').value) || 0 });
      bkMsg('轮换完成：删了 ' + r.removed.length + ' 份，现在 ' + r.kept + ' 份');
      await loadBackupStatus();
    } catch (e) { bkMsg('轮换失败：' + e.message, true); }
  };
  $('#bk-save').onclick = async () => {
    try {
      const r = await post('/api/backup/settings', {
        enabled: $('#bk-enabled').checked,
        keep: Number($('#bk-keep').value),
        interval_hours: Number($('#bk-interval').value),
      }, 'PUT');
      const bad = !!(r.warnings && r.warnings.length);
      bkMsg(bad ? r.warnings.join('；') : '已保存，立刻生效', bad);
      await loadBackupStatus();
    } catch (e) { bkMsg('保存失败：' + e.message, true); }
  };
  loadBackupStatus();
}

/* ---------------- 备份版本管理 ---------------- */
function fmtBytes(n) {
  if (!n) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i += 1; }
  return n.toFixed(i > 1 ? 1 : 0) + ' ' + u[i];
}

async function loadBackups() {
  const box = $('#bak-list');
  if (!box) return;
  try {
    const r = await api('/api/backups');
    const pend = $('#bak-pending');
    if (r.pending) {
      pend.hidden = false;
      pend.innerHTML = '⚠ 已标记还原到 <b>' + esc(r.pending) + '</b> —— '
        + '请用 stop.bat 停服务，再双击 search.bat 启动，启动时会自动替换。'
        + ' <button class="ghost tiny" id="bak-cancel">取消还原</button>';
      const c = $('#bak-cancel');
      if (c) c.addEventListener('click', async () => {
        await post('/api/backups/cancel-restore', {});
        loadBackups();
      });
    } else {
      pend.hidden = true;
    }
    if (!r.backups.length) {
      box.innerHTML = '<div class="empty">还没有备份。点右上角「立即备份」。</div>';
      return;
    }
    box.innerHTML = r.backups.map((b) => {
      const when = new Date(b.mtime * 1000).toLocaleString(
        'zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
      const stat = b.ok
        ? (b.people + ' 人物 · 已归属 ' + b.assigned + '/' + b.faces + ' 张')
        : '<span style="color:var(--bad)">读取失败</span>';
      return '<div class="folder" style="align-items:center">'
        + '<span class="p" title="' + esc(b.name) + '">' + esc(b.name) + '</span>'
        + '<span class="badge mode">' + when + '</span>'
        + '<span class="hint">' + fmtBytes(b.size) + ' · ' + stat + '</span>'
        + '<button class="ghost tiny" data-bak-restore="' + esc(b.name) + '">还原到这个</button>'
        + '<button class="ghost tiny" data-bak-del="' + esc(b.name) + '">删除</button>'
        + '</div>';
    }).join('');
    $('#bak-total').textContent =
      '共 ' + r.backups.length + ' 份 · 合计 ' + fmtBytes(r.total_bytes);

    box.querySelectorAll('[data-bak-restore]').forEach((el) => {
      el.addEventListener('click', async () => {
        const nm = el.dataset.bakRestore;
        if (!confirm('确定还原到「' + nm + '」吗？\n\n'
          + '· 点确定后只是记下标记，不会立刻改动\n'
          + '· 然后用 stop.bat 停服务、search.bat 启动\n'
          + '· 启动时才会真正替换数据库\n\n'
          + '当前库会先自动备份一份，能再还原回来。')) return;
        await post('/api/backups/restore', { name: nm });
        loadBackups();
      });
    });
    box.querySelectorAll('[data-bak-del]').forEach((el) => {
      el.addEventListener('click', async () => {
        const nm = el.dataset.bakDel;
        if (!confirm('删除备份「' + nm + '」？\n删了不能恢复。')) return;
        await api('/api/backups/' + encodeURIComponent(nm), { method: 'DELETE' });
        loadBackups();
      });
    });
  } catch (e) {
    box.innerHTML = '<div class="empty" style="color:var(--bad)">' + esc(e.message) + '</div>';
  }
}

/* 异常原因提示（项目主 2026-10-18：给个鼠标放上去的说明，知道什么原因）。
   数据来自 /api/folders/errors/summary（按 note 归类 + 数量 + 样例文件名）✓ */
async function loadErrorReasons() {
  try {
    const d = await api('/api/folders/errors/summary');
    window.__errReasons = d.by_folder || {};
    window.__errHint = d.hint || '';
  } catch (e) {
    window.__errReasons = {};
    window.__errHint = '';
  }
}

function errTip(folderId, items) {
  const map = window.__errReasons || {};
  const parts = [];
  const fids = folderId != null ? [folderId] : (items || []).map((f) => f.id);
  for (const fid of fids) {
    const reasons = map[fid];
    if (!reasons || !reasons.length) continue;
    for (const r of reasons) {
      parts.push(`${r.n} 个：${r.note}` + (r.sample ? `（例：${r.sample}）` : ''));
    }
  }
  const head = '异常 = 这个文件在读内容/解码时失败了，不影响其它文件。\n同一条原因会出现同名文件很多次。\n'
    + '重建一次索引会按最新规则重新判定（格式不支持的不再算异常）。';
  if (!parts.length) return head;
  return head + '\n\n' + parts.slice(0, 8).join('\n');
}

/* 点「27 异常」→ 列出**具体是哪些文件**（项目主 2026-10-18：
   "最好提示说明哪个文件出问题，我好针对它做出修改"）✓
   弹窗里给完整路径 + 原因 + 一键复制全部路径 ✓ */
async function openErrorList(fid) {
  let d;
  try {
    d = await api(`/api/folders/${fid}/errors?limit=500`);
  } catch (e) {
    toast('读异常清单失败：' + e.message, true);
    return;
  }
  const items = d.items || [];
  const rows = items.map((it, i) => `
    <tr>
      <td style="padding:4px 8px;color:var(--muted)">${i + 1}</td>
      <td style="padding:4px 8px;word-break:break-all">${esc(it.name)}</td>
      <td style="padding:4px 8px;color:var(--warn)">${esc(it.note)}</td>
      <td style="padding:4px 8px"><button class="ghost tiny" data-copy-path="${esc(it.path)}">复制路径</button></td>
    </tr>`).join('');
  const ov = document.createElement('div');
  ov.className = 'modal show';
  ov.innerHTML = `<div class="box" style="max-width:1000px">
    <div class="head"><b>异常文件（${d.total} 个${d.returned < d.total ? `，显示前 ${d.returned} 个` : ''}）</b>
      <span style="flex:1"></span>
      <button class="ghost tiny" id="err-close">关闭</button></div>
    <div class="body">
      <div class="hint" style="margin-bottom:8px">${esc(d.hint || '')}</div>
      <div class="hint" style="margin-bottom:8px">文件夹：<code>${esc(d.root || '')}</code></div>
      <div style="max-height:52vh;overflow:auto;border:1px solid var(--line);border-radius:8px">
        <table style="width:100%;border-collapse:collapse;font-size:12.5px">
          <tr style="position:sticky;top:0;background:var(--panel2)">
            <th style="padding:6px 8px;text-align:left">#</th>
            <th style="padding:6px 8px;text-align:left">文件名</th>
            <th style="padding:6px 8px;text-align:left">原因</th>
            <th style="padding:6px 8px"></th></tr>
          ${rows || '<tr><td colspan="4" style="padding:10px">没有异常 ✓</td></tr>'}
        </table>
      </div>
    </div>
    <div class="foot"><span class="hint" id="err-msg"></span><span style="flex:1"></span>
      <button class="ghost" id="err-copy-all">复制全部路径</button></div></div>`;
  document.body.appendChild(ov);
  const close = () => ov.remove();
  ov.onclick = (ev) => { if (ev.target === ov) close(); };
  ov.querySelector('#err-close').onclick = close;
  ov.querySelectorAll('[data-copy-path]').forEach((b) => {
    b.onclick = async () => {
      try {
        await navigator.clipboard.writeText(b.dataset.copyPath || '');
        b.textContent = '已复制 ✓';
      } catch (e) { toast('复制失败，请手动选中', true); }
    };
  });
  ov.querySelector('#err-copy-all').onclick = async () => {
    const all = items.map((it) => it.path).join('\r\n');
    try {
      await navigator.clipboard.writeText(all);
      ov.querySelector('#err-msg').textContent = `已复制 ${items.length} 条路径 ✓`;
    } catch (e) {
      // 剪贴板 API 在 http 下可能不可用 → 退回一个可选中的文本框 ✓
      const ta = document.createElement('textarea');
      ta.value = all; ta.style.cssText = 'width:100%;height:120px;margin-top:8px';
      ov.querySelector('.body').appendChild(ta);
      ta.select();
      ov.querySelector('#err-msg').textContent = '已选中，按 Ctrl+C 复制';
    }
  };
}

/* ---------------- 📋 计划增量更新 / 🚀 全力执行 ---------------- */
function fmtWhen2(ts) {
  if (!ts) return '从未扫描';
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function planUpdate() {
  const btn = $('#btn-plan');
  const old = btn ? btn.textContent : '';
  try {
    if (btn) { btn.textContent = '📋 正在算…'; btn.disabled = true; }
    const d = await post('/api/index/plan', {});
    const t = d.totals || {};
    const rows = (d.folders || []).map((f) => `
      <tr>
        <td style="padding:4px 8px">#${f.id}</td>
        <td style="padding:4px 8px;word-break:break-all">${esc(f.path)}</td>
        <td style="padding:4px 8px">${esc(f.caption_mode || 'off')}</td>
        <td style="padding:4px 8px;text-align:right">${f.caption_pending || 0}</td>
        <td style="padding:4px 8px;color:var(--muted)">${fmtWhen2(f.last_scan)}</td>
      </tr>`).join('');
    const ov = document.createElement('div');
    ov.className = 'modal show';
    ov.innerHTML = `<div class="box" style="max-width:960px">
      <div class="head"><b>📋 计划增量更新</b><span style="flex:1"></span>
        <button class="ghost tiny" id="plan-close">关闭</button></div>
      <div class="body">
        <div class="hint" style="margin-bottom:8px">${esc(d.hint || '')}</div>
        <div style="display:flex;gap:18px;flex-wrap:wrap;margin:6px 0 12px">
          <div><b>${t.folders || 0}</b> 个文件夹</div>
          <div><b>${t.caption_pending || 0}</b> 张插图待打标（约 ${t.est_caption_minutes || 0} 分钟）</div>
          <div>OCR：${d.ocr && d.ocr.enabled ? '开' : '关'}／引擎 ${esc((d.ocr || {}).engine || '')}／上限 ${(d.ocr || {}).max_pages} 页</div>
          <div>队列：待办 ${(d.queue || {}).pending || 0} · 失败 ${(d.queue || {}).failed || 0}</div>
        </div>
        <div style="max-height:50vh;overflow:auto;border:1px solid var(--line);border-radius:8px">
          <table style="width:100%;border-collapse:collapse;font-size:12.5px">
            <tr style="position:sticky;top:0;background:var(--panel2)">
              <th style="padding:6px 8px;text-align:left">id</th>
              <th style="padding:6px 8px;text-align:left">文件夹</th>
              <th style="padding:6px 8px;text-align:left">打标档位</th>
              <th style="padding:6px 8px;text-align:right">待打标</th>
              <th style="padding:6px 8px;text-align:left">上次扫描</th></tr>
            ${rows || '<tr><td colspan="5" style="padding:10px">没有文件夹</td></tr>'}
          </table>
        </div>
      </div>
      <div class="foot"><span class="hint">点「🚀 全力执行」就按这个计划跑</span>
        <span style="flex:1"></span>
        <button class="ghost" id="plan-run">🚀 全力执行</button></div></div>`;
    document.body.appendChild(ov);
    const close = () => ov.remove();
    ov.onclick = (ev) => { if (ev.target === ov) close(); };
    ov.querySelector('#plan-close').onclick = close;
    ov.querySelector('#plan-run').onclick = () => { close(); runNight(); };
  } catch (e) {
    alert('算计划失败：' + e.message);
  } finally {
    if (btn) { btn.textContent = old; btn.disabled = false; }
  }
}

async function runNight() {
  const hours = prompt(
    '🚀 全力执行\n\n会按顺序做：\n'
    + '① 腾显存（嵌入模型挪到 CPU）\n'
    + '② 索引（选中的文件夹；没选就全部）\n'
    + '③ 插图打标\n'
    + '④ 收尾（停 MinerU、嵌入模型挪回显卡）\n\n'
    + '最多跑多少小时？（留空或 0 = 不限时；到点会优雅暂停，进度保留）', '6');
  if (hours === null) return;
  const h = Number(hours) || 0;
  const ids = [...selected];
  try {
    const r = await post('/api/index/run-plan',
                         { max_hours: h, folder_ids: ids.length ? ids : undefined });
    if (!r.started) { alert('没能启动：' + (r.message || '未知原因')); return; }
    toast(h ? `已开始全力执行（最多 ${h} 小时，到点自动暂停）` : '已开始全力执行（不限时）');
    clearSelection();
    setTimeout(checkOnce, 800);
  } catch (e) {
    alert('启动失败：' + e.message);
  }
}

async function refreshNightBar() {
  // 显存占用 + 全力执行进度（轻量轮询，不要每次都重绘整个列表）
  try {
    const s = await api('/api/gpu/status').catch(() => null);
    // ⚠ 原来两处都写 #gpu-note（设置页的显存卡 + 这里的工具栏），
    //   id 重名 → 只有第一个被更新，另一个永远空着。现在工具栏那个叫 #gpu-note-folder ✓
    const el = $('#gpu-note-folder') || $('#gpu-note');
    if (el && s) {
      el.textContent = s.holder
        ? `🎮 显卡占用：${s.holder}${s.waiting ? `（排队 ${s.waiting}）` : ''}`
        : '🎮 显卡空闲';
    }
  } catch (e) { /* 忽略 */ }
}

$('#btn-plan').onclick = planUpdate;
$('#btn-night').onclick = runNight;
// 夜间进度条属于「人物 · 文件夹」页，只在那一页可见时刷新 ✓
// ⚠ 首屏也不能无条件拉：/api/gpu/status 要 8 秒（调 nvidia-smi），
//   排在首个请求里会把后面的接口全堵住，页面 14 秒才显示「运行中」✗
if (tabActive('folders')) refreshNightBar();
setInterval(() => { if (tabActive('folders')) refreshNightBar(); }, 20000);

/* ---------------- 🔗 本机 / 服务器 路径映射 ---------------- */
async function openPathMap(folderId, folderPath) {
  let d;
  try {
    d = await api(`/api/folders/${folderId}/paths`);
  } catch (e) {
    alert('读不到路径信息：' + e.message);
    return;
  }
  const ov = document.createElement('div');
  ov.className = 'modal show';
  ov.innerHTML = `<div class="box" style="max-width:680px">
    <div class="head"><b>🔗 路径映射</b><span style="flex:1"></span>
      <button class="ghost tiny" id="pm-x">关闭</button></div>
    <div class="body" style="display:grid;gap:14px">
      <div class="hint">数据库两边共享时，靠这三个路径对上号 ✓</div>
      <div>
        <b>索引里的路径</b>（权威键，不建议改）
        <div style="margin-top:4px;padding:6px 8px;border-radius:6px;background:var(--panel2);
                    word-break:break-all;font-size:12.5px">${esc(d.path)}</div>
      </div>
      <div>
        <b>本机实际目录</b>
        <input type="text" id="pm-local" value="${esc(d.local || '')}"
               style="width:100%;margin-top:4px" placeholder="留空 = 就用上面的路径">
        <div class="hint" style="margin-top:3px">
          这台机器上**实际去哪个目录找**。例如 NAS 改名、盘符换了，或你本机把同一份资料
          挂到了别处（D:\\data\\家庭相册）→ 填这里 ✓ 扫描和预览都会用它。
        </div>
      </div>
      <div>
        <b>服务器实际目录</b>
        <input type="text" id="pm-server" value="${esc(d.server || '')}"
               style="width:100%;margin-top:4px"
               placeholder="${esc(d.suggest_server || '/mnt/...')}">
        <div class="hint" style="margin-top:3px">
          服务器上这个文件夹在哪。建议：<b>${esc(d.suggest_server || '（没猜到，手动填）')}</b><br>
          服务器上的挂载点你改了，就在这里改 ✓ 留空 = 按自动建议。
        </div>
      </div>
    </div>
    <div class="foot"><span class="hint" id="pm-msg"></span><span style="flex:1"></span>
      <button class="ghost" id="pm-save">保存</button></div></div>`;
  document.body.appendChild(ov);
  const close = () => ov.remove();
  ov.onclick = (ev) => { if (ev.target === ov) close(); };
  ov.querySelector('#pm-x').onclick = close;
  ov.querySelector('#pm-save').onclick = async () => {
    const msg = ov.querySelector('#pm-msg');
    msg.textContent = '保存中…';
    try {
      const r = await api(`/api/folders/${folderId}/paths`, 'PUT', {
        local: ov.querySelector('#pm-local').value,
        server: ov.querySelector('#pm-server').value,
      });
      msg.innerHTML = `<b style="color:#4caf50">${esc(r.message || '已保存 ✓')}</b>`;
      setTimeout(close, 1500);
    } catch (e) {
      msg.textContent = '保存失败：' + e.message;
    }
  };
}

/* ---------------- ⚙ OCR 设置 ---------------- */
async function openOcrSettings() {
  let s;
  try {
    s = await api('/api/settings/ocr');
  } catch (e) {
    alert('读不到 OCR 设置：' + e.message);
    return;
  }
  const engs = (s.engines || []).map((e) => {
    const ok = e.available === false ? '（本机不可用）' : '';
    return `<option value="${esc(e.id)}" ${e.id === s.engine ? 'selected' : ''}>${esc(e.name || e.id)}${ok}</option>`;
  }).join('');
  const ov = document.createElement('div');
  ov.className = 'modal show';
  ov.innerHTML = `<div class="box" style="max-width:640px">
    <div class="head"><b>⚙ OCR / 插图打标 设置</b><span style="flex:1"></span>
      <button class="ghost tiny" id="ocr-x">关闭</button></div>
    <div class="body" style="display:grid;gap:12px">
      <div style="display:flex;align-items:center;gap:10px">
        <label class="sw"><input type="checkbox" id="ocr-on" ${s.enabled ? 'checked' : ''}><span class="sl"></span></label>
        <div><b>扫描版 OCR 总开关</b>
          <div class="hint" style="margin-top:2px">关掉后，任何文件夹都不做 OCR（文件夹上的勾也不生效）</div></div>
      </div>
      <div>
        <b>OCR 引擎</b>
        <select id="ocr-engine" style="margin-left:8px">${engs}</select>
        <div class="hint" style="margin-top:4px">
          auto = 扫描版优先 MinerU，它没运行就退回 PP-OCRv6 ✓<br>
          MinerU API：${s.mineru_api ? '<b style="color:#4caf50">运行中</b>' : '未运行'} ·
          OXR API：${s.oxr_api ? '<b style="color:#4caf50">运行中</b>' : '未运行'} ·
          显存：${esc(s.free_hint || '未知')} · 当前自动批量：${s.auto_batch} 页/批
        </div>
      </div>
      <div>
        <b>一个 PDF 最多 OCR 多少页</b>
        <input type="number" id="ocr-pages" min="0" value="${s.max_pages}"
               style="width:110px;margin-left:8px">
        <div class="hint" style="margin-top:4px">
          <b>0 = 全部做</b>（默认，按实际页数跑完）✓<br>
          想做一部分就填具体数字，比如 50；之后改回 0 再扫，会<b>从断点接着做</b>，不重做已完成的页 ✓
        </div>
      </div>
      <div>
        <b>一个 PDF 最多给多少张插图打标</b>
        <input type="number" id="ocr-images" min="0" value="${s.max_images}"
               style="width:110px;margin-left:8px">
        <div class="hint" style="margin-top:4px">
          <b>0 = 全部标</b>（默认，筛出来的真插图全打）✓ 想省时间就填具体数字<br>
          只标**筛选后的真插图**（去掉图标/装饰/重复）✓
        </div>
      </div>
      <div>
        <b>OCR 页码范围</b>
        <input type="number" id="ocr-from" min="1" value="${s.page_from}"
               style="width:80px;margin-left:8px" title="从第几页开始">
        <span style="margin:0 4px">~</span>
        <input type="number" id="ocr-to" min="0" value="${s.page_to}"
               style="width:80px" title="到第几页（0 = 最后一页）">
        <div class="hint" style="margin-top:4px">
          只想做其中一段就填，比如 <b>20 ~ 50</b>（不用从第一页开始算）✓<br>
          结束页填 <b>0</b> = 到最后一页；⚠ 已做过的页仍不会重做（从断点续）✓
        </div>
      </div>
      <details id="ocr-paths" style="border-top:1px solid var(--line);padding-top:10px">
        <summary style="cursor:pointer;font-size:13.5px">
          <b>引擎路径</b>
          <span class="hint">（装在哪就填哪；留空 = 用默认值）</span>
        </summary>
        <div style="display:grid;gap:9px;margin-top:10px">
          <div class="hint">
            只有「本机不可用」的引擎才需要填。填的是<b>那个环境里</b>的
            python.exe 和桥接脚本：<br>
            · <b>PP-OCRv6</b>（要 GPU）　· <b>RapidOCR</b>（纯 CPU，最轻）　· <b>MinerU</b>（最强，要先跑它的 API）　· <b>OXR</b>（版面/表格最像原文，要先跑 OXR）
          </div>
          <label style="display:grid;gap:3px">
            <span class="hint">PP-OCRv6 · python</span>
            <input id="ocr-p-ppocr-py" value="${esc((s.paths || {}).ppocr_python || '')}"
                   placeholder="D:\\GJ\\PP-OCRv6\\.venv\\Scripts\\python.exe"
                   style="font:12.5px Consolas,monospace">
          </label>
          <label style="display:grid;gap:3px">
            <span class="hint">PP-OCRv6 · 桥接脚本</span>
            <input id="ocr-p-ppocr-br" value="${esc((s.paths || {}).ppocr_bridge || '')}"
                   placeholder="D:\\GJ\\PP-OCRv6\\_lingxi_bridge.py"
                   style="font:12.5px Consolas,monospace">
          </label>
          <label style="display:grid;gap:3px">
            <span class="hint">RapidOCR · python</span>
            <input id="ocr-p-rapid-py" value="${esc((s.paths || {}).rapid_python || '')}"
                   placeholder="D:\\lingxi\\ocr-rapid\\.venv\\Scripts\\python.exe"
                   style="font:12.5px Consolas,monospace">
          </label>
          <label style="display:grid;gap:3px">
            <span class="hint">RapidOCR · 桥接脚本</span>
            <input id="ocr-p-rapid-br" value="${esc((s.paths || {}).rapid_bridge || '')}"
                   placeholder="D:\\lingxi\\ocr-rapid\\bridge.py"
                   style="font:12.5px Consolas,monospace">
          </label>
          <label style="display:grid;gap:3px">
            <span class="hint">MinerU · python</span>
            <input id="ocr-p-mineru-py" value="${esc((s.paths || {}).mineru_python || '')}"
                   placeholder="...\\MinerU\\runtime\\python\\python.exe"
                   style="font:12.5px Consolas,monospace">
          </label>
          <label style="display:grid;gap:3px">
            <span class="hint">MinerU · 桥接脚本</span>
            <input id="ocr-p-mineru-br" value="${esc((s.paths || {}).mineru_bridge || '')}"
                   placeholder="...\\MinerU\\tools\\submit_mineru_task.py"
                   style="font:12.5px Consolas,monospace">
          </label>
          <label style="display:grid;gap:3px">
            <span class="hint">OXR · 解析服务地址（HTTP，不用填 python）</span>
            <input id="ocr-p-oxr-api" value="${esc((s.paths || {}).oxr_api || '')}"
                   placeholder="http://127.0.0.1:8000"
                   style="font:12.5px Consolas,monospace">
          </label>
          <label style="display:grid;gap:3px">
            <span class="hint">OXR · 桥接脚本</span>
            <input id="ocr-p-oxr-br" value="${esc((s.paths || {}).oxr_bridge || '')}"
                   placeholder="...\run\ocr_bridges\oxr_bridge.py"
                   style="font:12.5px Consolas,monospace">
          </label>
          <div class="hint">
            保存后会写进 <code>config.ini</code> 的 <code>[ocr]</code>，
            <b>立即生效</b>（不用重启）✓ 然后点「重新检测」看引擎是否可用。
          </div>
          <div>
            <button class="ghost tiny" id="ocr-recheck">重新检测引擎</button>
            <span class="hint" id="ocr-recheck-msg"></span>
          </div>
        </div>
      </details>
      <div class="hint">
        判断"要不要 OCR"看的是<b>文字密度</b>：每页少于 ${s.min_chars_per_page} 字
        就当扫描版（有文字层的 PDF 直接提取，不 OCR）✓
      </div>
    </div>
    <div class="foot"><span class="hint" id="ocr-msg"></span><span style="flex:1"></span>
      <button class="ghost" id="ocr-save">保存</button></div></div>`;
  document.body.appendChild(ov);
  const close = () => ov.remove();
  ov.onclick = (ev) => { if (ev.target === ov) close(); };
  ov.querySelector('#ocr-x').onclick = close;
  const rk = ov.querySelector('#ocr-recheck');
  if (rk) {
    rk.onclick = async () => {
      const msg = ov.querySelector('#ocr-recheck-msg');
      if (msg) msg.textContent = '检测中…';
      try {
        const d = await api('/api/settings/ocr');
        const bad = (d.engines || []).filter((e) => e.available === false);
        if (msg) {
          msg.innerHTML = bad.length
            ? ('<span style="color:var(--bad)">仍不可用：'
               + bad.map((e) => esc(e.name || e.id)).join('、') + '</span>')
            : '<span style="color:var(--ok)">全部可用 ✓</span>';
        }
      } catch (e) {
        if (msg) msg.textContent = '失败：' + e.message;
      }
    };
  }
  ov.querySelector('#ocr-save').onclick = async () => {
    const msg = ov.querySelector('#ocr-msg');
    msg.textContent = '保存中…';
    try {
      const r = await api('/api/settings/ocr', 'PUT', {
        enabled: ov.querySelector('#ocr-on').checked,
        engine: ov.querySelector('#ocr-engine').value,
        max_pages: Number(ov.querySelector('#ocr-pages').value),
        max_images: Number(ov.querySelector('#ocr-images').value),
        page_from: Number(ov.querySelector('#ocr-from').value),
        page_to: Number(ov.querySelector('#ocr-to').value),
        paths: {
          ppocr_python: ov.querySelector('#ocr-p-ppocr-py').value.trim(),
          ppocr_bridge: ov.querySelector('#ocr-p-ppocr-br').value.trim(),
          rapid_python: ov.querySelector('#ocr-p-rapid-py').value.trim(),
          rapid_bridge: ov.querySelector('#ocr-p-rapid-br').value.trim(),
          mineru_python: ov.querySelector('#ocr-p-mineru-py').value.trim(),
          mineru_bridge: ov.querySelector('#ocr-p-mineru-br').value.trim(),
        },
      });
      if (!r.ok) {
        msg.textContent = '没保存：' + (r.message || (r.warnings || []).join('；'));
        return;
      }
      const pg = r.now.page_to === 0
        ? `${r.now.page_from} ~ 最后一页`
        : `${r.now.page_from} ~ ${r.now.page_to}`;
      msg.innerHTML = `<b style="color:#4caf50">已保存 ✓</b> 页数上限 = ${
        r.now.max_pages === 0 ? '全部' : r.now.max_pages}，插图上限 = ${
        r.now.max_images === 0 ? '全部' : r.now.max_images}，页码 ${pg}，引擎 = ${
        esc(r.now.engine)}`
        + (r.warnings && r.warnings.length ? `（${r.warnings.join('；')}）` : '');
      setTimeout(close, 1200);
    } catch (e) {
      msg.textContent = '保存失败：' + e.message;
    }
  };
}

$('#btn-ocr-cfg').onclick = openOcrSettings;

async function refreshFolders() {


  await loadErrorReasons();          // 异常原因（悬停提示要用，先拿到再渲染）✓
  const data = await api('/api/folders');
  FOLDER_SCHEMA = data.schema;
  FOLDER_MODES = data.modes;
  window.__folders = data.folders;
  window.__foldersResp = data;             // 类别清单 / 默认类别（添加弹窗要用）
  // ★ 是不是本机版（建库机）：决定要不要显示「🔗 路径」等只在本机有用的按钮 ✓
  window.__isBuilder = data.is_builder !== false && data.can_index_here !== false;

  const sel = $('#folder-filter');
  const keep = sel.value;
  sel.innerHTML = '<option value="">全部文件夹</option>' +
    data.folders.map((f) => `<option value="${f.id}">${esc(f.path)}</option>`).join('');
  sel.value = keep;
  sel.style.display = data.folders.length > 1 ? '' : 'none';

  for (const id of [...selected]) {
    if (!data.folders.some((f) => f.id === id)) selected.delete(id);
  }

  $('#folders').innerHTML = data.folders.length
    ? foldersHtml(data.folders)
    : '<div class="empty">还没有添加文件夹。点上面的「＋ 添加文件夹」可以一次选多个。</div>';

  window.__eg2Folders = data.folders;      // 给「补提范围」下拉用
  fillBackfillFolders();

  // 分组的展开 / 收起
  // 组的展开 / 收起
  function setGroupOpen(key, open) {
    const set = window.__expandedGroups || (window.__expandedGroups = new Set());
    if (open) set.add(key); else set.delete(key);
    const box = document.querySelector(`.fgroup[data-group="${CSS.escape(key)}"]`);
    if (!box) return;
    box.classList.toggle('off', !open);
    const tog = box.querySelector('.fgtog');
    if (tog) { tog.textContent = open ? '\u25be' : '\u25b8'; tog.title = open ? '收起' : '展开'; }
    const all = box.querySelector('.fgall');
    if (all) all.textContent = open ? '全部收起' : '全部展开';
  }
  $('#folders').querySelectorAll('.fgtog, .fgall').forEach((btn) => {
    btn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const key = btn.dataset.tog || btn.dataset.all;
      const set = window.__expandedGroups || (window.__expandedGroups = new Set());
      setGroupOpen(key, !set.has(key));
    });
  });
  // 点组头空白处也切换
  $('#folders').querySelectorAll('.fghead').forEach((h) => {
    h.addEventListener('click', (ev) => {
      if (ev.target.closest('button')) return;
      const key = h.parentElement.dataset.group;
      const set = window.__expandedGroups || (window.__expandedGroups = new Set());
      setGroupOpen(key, !set.has(key));
    });
  });

  const n = selected.size;
  $('#bulk-toolbar').style.display = n ? '' : 'none';
  $('#bulk-count').textContent = `已选中 ${n} 个文件夹`;

  const s = data.stats;
  $('#stats').innerHTML = `
    <div class="stat"><b>${s.files}</b><span>已索引文件</span></div>
    <div class="stat"><b>${s.chunks}</b><span>文本片段</span></div>
    <div class="stat"><b>${fmtSize(s.bytes)}</b><span>文件总大小</span></div>
    <div class="stat"><b>${data.folders.length}</b><span>文件夹</span></div>
    <div class="stat"><b>${s.errors}</b><span>索引异常</span></div>`;
  // 普通用户看到的是「他能用的」统计，标出口径免得误会成整站 ✓
  $('#stat-badge').textContent = (s.scoped ? '（我的）' : '') + `${s.files} 个文件已索引`;
  return data;
}

let polling = null;
let lastFinishedAt = null;

function markRunning(status) {
  document.querySelectorAll('[data-run]').forEach((el) => {
    const folder = (window.__folders || []).find((f) => f.id === +el.dataset.run);
    const active = !!(status.running && folder && status.folder === folder.path);
    el.hidden = !active;
    el.textContent = active
      ? `⏳ 更新中 ${status.folder_done}/${status.folder_files}`
      : '';
    el.closest('.folder').classList.toggle('sel', active);
  });
}

async function pollStatus() {
  const data = await api('/api/index/status');
  const st = data.status;
  if (st.running) {
    $('#progress-card').style.display = '';
    const done = st.folder_done || 0, total = st.folder_files || 0;
    $('#progress-bar').style.width = (total ? Math.min(100, done / total * 100) : 0).toFixed(1) + '%';
    const which = st.folder_total > 1 ? `（第 ${st.folder_index}/${st.folder_total} 个文件夹）` : '';
    $('#progress-text').textContent = (st.message || '正在索引…') + which;
    $('#progress-detail').textContent =
      `${st.phase} · 本文件夹 ${done}/${total} · 新建 ${st.indexed} · 跳过 ${st.skipped} · ` +
      `片段 ${st.chunks} · 出错 ${st.errors} · 已用 ${st.elapsed}s` +
      (st.current ? ` · 当前：${st.current}` : '');
    markRunning(st);
    setIndexButtons(true);
    if (!polling) polling = setInterval(checkOnce, 1200);
  } else {
    const wasPolling = !!polling;
    if (polling) { clearInterval(polling); polling = null; }
    markRunning(st);
    setIndexButtons(false);
    if (wasPolling && st.finished_at && st.finished_at !== lastFinishedAt) {
      lastFinishedAt = st.finished_at;
      const bad = st.errors > 0;
      toast(`${st.message || '索引完成'}：新增 ${st.indexed} · 跳过 ${st.skipped}` +
            ` · 清理 ${st.removed}${bad ? ` · 异常 ${st.errors}` : ''}`, bad);
    }
    if ($('#progress-card').style.display !== 'none') {
      $('#progress-bar').style.width = '100%';
      $('#progress-text').textContent = st.message || '索引完成';
      $('#progress-detail').textContent =
        `本次：新建 ${st.indexed} · 跳过 ${st.skipped} · 删除 ${st.removed} · ` +
        `新增片段 ${st.chunks} · 出错 ${st.errors}`;
      refreshFolders();
      // ⚠ 2026-10-18 项目主："开始与暂停按钮刷新不出来" ——
      //   以前这里 6 秒后把整张卡隐藏 ✗，结果想再点「开始」时找不到按钮 ✗。
      //   现在这张卡**常显**，不再自动隐藏 ✓（按钮也就永远在 ✓）
    }
  }
  return st;
}
const checkOnce = () => pollStatus().catch(() => {});

/* ---------------- 人物 / 人脸 ---------------- */
const faceState = { people: [], picked: new Set(), ready: false };

async function loadFaceStatus() {
  try {
    const st = await api('/api/faces/status');
    faceState.ready = !!st.available;
    $('#people-info').textContent = st.available
      ? (st.enabled ? '已开启' : '模型可用，但配置里 face.enabled = no')
      : '不可用（' + st.reason + '）';
    // ⚠ 2026-10-21 项目主："人脸数量现在是统计的全站数量吗？"
    //   —— 是。这里原来是 `st.faces`（全站）。现在不在这里写，
    //   改由 renderPeople() 用「他已经过滤过的人物」汇总 ✓
    void st;
    return st;
  } catch (e) {
    $('#people-info').textContent = '读取失败：' + e.message;
    return null;
  }
}

async function loadPeople() {
  try {
    const data = await api('/api/people');
    faceState.people = data.persons || [];
    renderPeople();
  } catch (e) { /* 静默 */ }
}

function fillBackfillFolders() {
  const sel = $('#backfill-folder');
  if (!sel) return;
  const opts = ['<option value="">全部文件夹</option>'];
  document.querySelectorAll('#folders [data-folder-id]').forEach((el) => {
    const id = el.getAttribute('data-folder-id');
    const path = el.getAttribute('data-folder-path') || '';
    opts.push(`<option value="${id}">${esc(path)}</option>`);
  });
  // 也兜底用 /api/folders 的数据
  if (opts.length === 1 && window.__eg2Folders) {
    for (const f of window.__eg2Folders) {
      opts.push(`<option value="${f.id}">${esc(f.path)}</option>`);
    }
  }
  sel.innerHTML = opts.join('');
}

/* 按用户选的规则排序人物卡片 */
function sortPeople(arr) {
  const mode = window.__peopleSort || (window.__peopleSort = 'faces-desc');
  const n = (p) => (p.n_faces != null ? p.n_faces : (p.n_files || 0));
  const nm = (p) => (p.name || '').trim();
  const out = arr.slice();
  if (mode === 'faces-asc') out.sort((a, b) => n(a) - n(b));
  else if (mode === 'name-asc') out.sort((a, b) => nm(a).localeCompare(nm(b), 'zh'));
  else if (mode === 'name-desc') out.sort((a, b) => nm(b).localeCompare(nm(a), 'zh'));
  else out.sort((a, b) => n(b) - n(a));            // faces-desc（默认）
  return out;
}

function renderPeople() {
  // 搜索页的人物下拉跟着人物数据一起刷新
  if (typeof fillQuickPeople === 'function') fillQuickPeople();
  if (typeof fillPeopleFolderFilter === 'function') fillPeopleFolderFilter();
  const box = $('#people-grid');
  const _cnt = {};
  for (const x of faceState.people) {
    const nm = (x.name || '').trim();
    if (nm) _cnt[nm] = (_cnt[nm] || 0) + 1;
  }
  for (const x of faceState.people) {
    const nm = (x.name || '').trim();
    x.dup = nm && _cnt[nm] > 1;
  }
  // ★ 服务器上（member）：「筛选文件夹」真的筛人物；本地版保持原样（不筛）✓
  const _fsel = $('#people-folder-filter');
  const _plbl = $('#pff-label');
  const _bsel = $('#backfill-folder');
  const _blbl = $('#bf-label');
  const isMember = (VIEW_MODE === 'member');
  // ★ 2026-10-21 项目主（截图）：改的是「补提范围」那个下拉 → 当然不变 ✗
  //   根因是我让两个下拉并排显示（筛选器 + 补提范围），极易改错。
  //   → 两个形态都只留「筛选文件夹」；补提范围隐藏，
  //     「只补提人脸」固定处理全部文件夹（要单文件夹补提，去文件夹页单独配）
  if (_fsel) _fsel.style.display = '';
  if (_plbl) _plbl.style.display = '';
  if (_bsel) _bsel.style.display = 'none';
  if (_blbl) _blbl.style.display = 'none';
  const _fid = (_fsel && _fsel.value) ? Number(_fsel.value) : 0;
  const _scope = _fid
    ? faceState.people.filter((p) => (p.folders || []).includes(_fid))
    : faceState.people;
  const list = sortPeople(_scope.filter((p) => p.name.trim()));
  const unknown = sortPeople(_scope.filter((p) => !p.name.trim()));
  if (!list.length && !unknown.length) {
    if (_fid) {
      box.innerHTML = '<div class="empty">这个文件夹里还没有出现过人脸人物。'
        + '换个文件夹，或选「全部（我名下）」看看 ✓</div>';
      return;
    }
    // ★ 2026-10-21：只显示**你自己**的人物（共享文件夹里的人物不给你看）。
    //   所以不同形态的空列表提示也要不一样，别让普通用户去点他点不了的按钮 ✓
    box.innerHTML = (VIEW_MODE === 'member')
      ? '<div class="empty">你名下还没有带人脸的照片。'
        + '（别人共享给你的照片里的人物<b>不会</b>出现在这里 —— '
        + '那是别人建的人物标签，对你没什么用。）<br>'
        + '你自己上传照片并在本机建库之后，这些人物会出现在这里 ✓</div>'
      : '<div class="empty">还没有人物数据。'
        + '先把图片文件夹加入索引（并在 config.ini 里把 face.enabled 设为 yes），'
        + '索引完成后点「重新聚类」。</div>';
    return;
  }
  // 卡片上的"N 张"：选了文件夹就显示**那个文件夹里的**张数
  // （项目主 2026-10-21：三张截图里 97/38/91 在变，但卡片张数一直 1640 不变 ✗）
  const _numOf = (p) => {
    // 卡片上的是**照片数**（不是脸数）：一个文件里同一个人的多张脸只算 1 张 ✓
    if (_fid) {
      const v = (p.files_by_folder || {})[String(_fid)];
      return v != null ? v : 0;            // 这个文件夹里没有他
    }
    // 没筛选：用后端算好的（普通用户=只算他自己的；管理员=全库）
    return p.n_files || 0;
  };
  const card = (p) => {
    const on = faceState.picked.has(p.id) ? ' on' : '';
    // ★ 2026-10-21 项目主："服务器用户应该不能改共享的人脸和重命名"
    //   后端 `/api/people` 现在给了 writable（整组的脸都在我能写的文件夹里才 true）。
    //   改不了的：两个按钮灰掉 + 悬停说明，别等点了才 403 ✓
    const canEdit = p.writable !== false;
    const why = canEdit ? '' : '这个人物在共享文件夹里，只有管理员能改';
    const cover = p.cover_face
      // v=封面脸编号：管理员换头像 → URL 变 → 立刻重新取 ✓
      // （缩略图带 24 小时缓存，不带版本号的话用户端会一直显示旧头像 ✗）
      ? `<img src="/api/faces/${p.cover_face}/thumb?w=112&v=${p.cover_face}" loading="lazy" alt="">`
      : '<div class="ph"></div>';
    const kind = p.kind === 'object' ? ' <span class="kindtag">物件</span>' : '';
    return `<div class="pcard${on}" data-id="${p.id}"${canEdit ? '' : ' data-ro="1"'}
      title="${canEdit ? '未命名：点击命名；已命名：点击筛选 / 再点取消' : why}">
      <div class="pav">${cover}
        <button class="pcov" data-menu="${p.id}"${canEdit ? '' : ' disabled'}
                title="${canEdit ? '整组处理 / 命名' : why}"
                style="left:4px;right:auto;${canEdit ? '' : 'opacity:.45;cursor:not-allowed'}">⋯</button>
        ${(p.name || '').trim()
          ? `<button class="pcov" data-cover="${p.id}"${canEdit ? '' : ' disabled'}
                   title="${canEdit ? '换一张做头像' : why}"
                   style="${canEdit ? '' : 'opacity:.45;cursor:not-allowed'}">换</button>`
          : ''}</div>
      <div class="pname">${esc(p.name || '（未命名）')}${kind}${
        p.dup ? ' <span class="kindtag" style="background:var(--warn);color:#000">重名</span>' : ''}</div>
      <div class="pnum">${_numOf(p)} 张</div>
    </div>`;
  };
  let html = '';
  if (mergeSource !== null) {
    const src = faceState.people.find((x) => x.id === mergeSource);
    html += `<div class="hint" style="margin:0 0 10px;padding:10px 14px;`
      + `background:var(--panel2);border-radius:10px;border:1px solid var(--accent)">`
      + `<b style="color:var(--accent)">合并模式</b> —— 正在把 `
      + `「${esc((src && src.name) || '未命名')}」并进别人。<br>`
      + `请在下面<b>点一下目标人物</b>的卡片；点自己或点这里取消。`
      + ` <button class="ghost tiny" id="merge-cancel">取消合并</button></div>`;
  }
  if (list.length) {
    html += `<div class="pgroup">已命名（${list.length}）</div><div class="pgrid">`
      + list.map(card).join('') + '</div>';
  }
  if (unknown.length) {
    // 按脸数从多到少排，只显示值得命名的。
    // 之前是 slice(0, 60) 直接取前 60 个，顺序还是随机的 ——
    // 结果一屏全是 1-2 张脸的碎片，真正的大组反而看不到。
    const MIN_FACES = 5;
    // unknown 已经按用户选的规则排过序了，这里直接用
    const worth = unknown.filter((x) => (x.n_faces || 0) >= MIN_FACES);
    const shown = (worth.length ? worth : unknown).slice(0, 150);
    const hidden = unknown.length - shown.length;
    html += `<div class="pgroup">未命名（共 ${unknown.length} 组，`
      + `显示脸数最多的 ${shown.length} 个）—— 点一下可以命名</div>`
      + `<div class="pgrid">${shown.map(card).join('')}</div>`;
    if (hidden > 0) {
      html += `<div class="hint" style="margin-top:8px">`
        + `另有 ${hidden} 个小组（脸数不足 ${MIN_FACES}）已隐藏 —— `
        + `这些多是误检或只出现一两次的路人，不值得花时间。</div>`;
    }
  }
  box.innerHTML = html;
  const mc = box.querySelector('#merge-cancel');
  if (mc) mc.addEventListener('click', (ev) => {
    ev.stopPropagation(); mergeSource = null; renderPeople();
  });
  box.querySelectorAll('[data-menu]').forEach((b) => {
    b.addEventListener('click', (ev) => {
      ev.stopPropagation();
      personMenu(Number(b.dataset.menu));
    });
  });
  box.querySelectorAll('[data-cover]').forEach((b) => {
    b.addEventListener('click', (ev) => {
      ev.stopPropagation();
      openCoverPicker(Number(b.dataset.cover));
    });
  });
  box.querySelectorAll('.pcard').forEach((el) => {
    el.addEventListener('click', async () => {
      const id = Number(el.dataset.id);
      // 未命名的组：点一下**打开人脸墙**（不是盲猜命名）。
      // 先看脸、再命名 —— 只看一张封面缩略图没法判断 1000 多张脸是谁。
      const me = faceState.people.find((x) => x.id === id);
      if (me && !(me.name || '').trim()) {
        openCoverPicker(id);
        return;
      }
      if (mergeSource !== null) {
        if (id === mergeSource) { mergeSource = null; renderPeople(); return; }
        const src = faceState.people.find((x) => x.id === mergeSource);
        const tgt = faceState.people.find((x) => x.id === id);
        if (!confirm(`把「${(src && src.name) || '未命名'}」整组并进 `
                     + `「${(tgt && tgt.name) || '未命名'}」？`)) return;
        try {
          await post('/api/people/people-merge',
                     { target: id, sources: [mergeSource] });
          mergeSource = null;
          await loadPeople();
          alert('合并完成');
        } catch (e) { alert('合并失败：' + e.message); }
        return;
      }
      pickPerson(id);
    });;
    el.addEventListener('dblclick', async (ev) => {
      ev.preventDefault();
      const id = Number(el.dataset.id);
      const p = faceState.people.find((x) => x.id === id);
      const name = prompt('给这个人起个名字', p ? p.name : '');
      if (name === null) return;
      try {
        await post('/api/people/' + id, { name: name.trim() }, 'PUT');
        await loadPeople();
      } catch (e) { alert('命名失败：' + e.message); }
    });
  });
  // 刚重渲染完，立刻把「已命名 / 未命名」的折叠状态接上。
  // 以前只靠 2.5 秒的定时重绑，重渲染后会有一次展开→折叠的闪动。
  if (typeof bindCardFolding === 'function') bindCardFolding();
  // ★ 人脸数字按"他能看到的那些人物"汇总（管理员看到的是全部，天然一致 ✓）
  //   `n_faces` 后端已经按他可写的文件夹重算过，所以这里是"他自己的张数"
  try {
    const tot = (_scope || []).reduce((a, p) => a + _numOf(p), 0);
    const named = (_scope || [])
      .filter((p) => (p.name || '').trim()).length;
    const el = $('#face-info');
    if (el) {
      // ★ 把"当前筛的是哪个文件夹"写进去：能一眼区分
      //   "筛选没生效" / "页面没刷新" / "生效了但数字恰好一样"
      let _sc = '';
      if (_fid) {
        const _f = (window.__folders || []).find((x) => Number(x.id) === Number(_fid));
        const _nm = _f
          ? String(_f.path || '').replace(/^.*[\\/]/, '')
          : ('#' + _fid);
        _sc = '【筛选中：' + _nm + '】';
      }
      el.textContent = _sc + (VIEW_MODE === 'member' ? '你的' : '索引里')
        + `共 ${tot} 张脸 / ${named} 个已命名人物`
        + (VIEW_MODE === 'member' ? '（只算你自己文件夹里的）' : '');
    }
  } catch (e) { /* 忽略 */ }

}

/* 整组处理：改名 / 合并 / 清空名字 / 整组解散 */
async function personMenu(pid) {
  const me = faceState.people.find((x) => x.id === pid);
  if (!me) return;
  const n = me.n_files || me.n || 0;
  const tip = `【${me.name || '未命名'}】${n} 张\n\n`
    + '  1 = 改名\n'
    + '  2 = 合并到另一个人（整组都该是别人时用这个）\n'
    + '  3 = 清空名字（回到未命名，脸留着）\n'
    + '  4 = 整组解散（所有脸变未归属，名字也删掉）\n\n'
    + '输入序号，直接回车取消：';
  const act = (prompt(tip, '1') || '').trim();
  if (!act) return;

  try {
    if (act === '1') {
      const nm = prompt('新的名字：', me.name || '');
      if (nm === null) return;
      await post(`/api/people/${pid}`, { name: nm.trim() }, 'PUT');

    } else if (act === '2') {
      // 不用文本列表：人物一多，prompt 弹窗根本显示不全（44 个只看到 23 个）。
      // 改成进入「点选模式」—— 直接在下面的卡片里点目标人物。
      mergeSource = pid;
      renderPeople();
      return;

    } else if (act === '3') {
      await post(`/api/people/${pid}`, { name: '' }, 'PUT');

    } else if (act === '4') {
      if (!confirm(`确定解散「${me.name || '未命名'}」吗？\n\n`
        + `名下 ${n} 张脸会变成「未归属」，名字也会删掉。\n`
        + '之后可以点「重新聚类」重新分组。')) return;
      const r = await post(`/api/people/${pid}/detach-all`, {});
      alert(`已解散，${r.detached} 张脸回到未归属`);

    } else {
      return;
    }
    await loadPeople();
  } catch (e) {
    alert('操作失败：' + e.message);
  }
}

async function openCoverPicker(pid, offset) {
  window.__pickPid = pid;
  let ov = document.getElementById('cover-ov');
  if (!ov) {
    ov = document.createElement('div');
    ov.id = 'cover-ov';
    ov.className = 'coverov';
    ov.innerHTML = '<div class="coverbox"><div class="coverhead">'
      + '<b id="cover-title">选一张做头像</b>'
      + '<input type="text" id="cover-name" list="people-names" '
      + 'autocomplete="off" placeholder="给这组起个名字（点下拉选已有）…" '
      + 'style="width:230px;padding:5px 10px;border-radius:8px;'
      + 'border:1px solid var(--line);background:var(--panel2);color:var(--fg)">'
      + '<datalist id="people-names"></datalist>'
      + '<button class="ghost tiny" id="cover-name-save">命名这组</button>'
      + '<button class="ghost tiny" id="cover-more">换一批</button>'
      + '<span class="hint" id="cover-tip">按清晰度排序，点一下就换上</span>'
      + '<span style="flex:1"></span><button class="ghost tiny" id="cover-close">关闭</button>'
      + '</div><div class="covergrid" id="cover-grid"></div></div>';
    ov.querySelector('#cover-name-save').addEventListener('click', async () => {
      const nm = (ov.querySelector('#cover-name').value || '').trim();
      const pid2 = window.__pickPid;
      if (!pid2) return;
      try {
        // 名字已经存在 → 提示合并，避免出现两组同名
        // （同名会让"按人物筛选"分裂成两半，很难发现）
        const dup = (faceState.people || []).find(
          (x) => x.id !== pid2 && (x.name || '').trim() === nm && nm);
        if (dup) {
          const me = (faceState.people || []).find((x) => x.id === pid2);
          if (confirm(`已经有「${nm}」这一组了（${dup.n_faces || 0} 张）。\n\n`
            + `把这组（${(me && me.n_faces) || 0} 张）并进去吗？\n\n`
            + '确定 = 合并成一组\n取消 = 换个名字')) {
            await post('/api/people/people-merge',
                       { target: dup.id, sources: [pid2] });
            ov.classList.remove('show');
            await loadPeople();
            return;
          }
          return;                       // 用户选"换个名字"，留在弹窗里改
        }
        await post(`/api/people/${pid2}`, { name: nm }, 'PUT');
        ov.classList.remove('show');
        await loadPeople();
      } catch (e) { alert('命名失败：' + e.message); }
    });
    ov.querySelector('#cover-name').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') ov.querySelector('#cover-name-save').click();
    });
    document.body.appendChild(ov);
    ov.addEventListener('click', (ev) => { if (ev.target === ov) ov.classList.remove('show'); });
    ov.querySelector('#cover-close').addEventListener('click', () => ov.classList.remove('show'));
    const _more = ov.querySelector('#cover-more');
    if (_more) {
      _more.addEventListener('click', () => {
        if (_more.disabled) return;
        openCoverPicker(pid, (window.__coverOff || 0) + 90);
      });
    }
  }
  ov.classList.add('show');
  const g = ov.querySelector('#cover-grid');
  const tip = ov.querySelector('#cover-tip');
  g.innerHTML = '<div class="hint">正在读取…</div>';
  // ★ 2026-10-21：分页看候选脸（原来只能看排最前的 90 张，没法换 ✗）
  const PAGE = 90;
  const OFF = Math.max(0, Number(offset) || 0);
  window.__coverPid = pid;
  window.__coverOff = OFF;
  try {
    const r = await api(`/api/people/${pid}/faces?limit=${PAGE}&offset=${OFF}`);
    if (!r.faces.length) {
      g.innerHTML = OFF
        ? '<div class="hint">没有更多了（已经到最后一页）</div>'
        : '<div class="hint">这个人物还没有脸</div>';
      const mb0 = ov.querySelector('#cover-more');
      if (mb0) { mb0.disabled = true; mb0.textContent = '没有更多了'; }
      return;
    }
    const _tot = Number(r.total || r.count || 0);
    const _to = OFF + r.faces.length;
    if (tip) {
      tip.textContent = `按清晰度排序，点一下就换上（第 ${OFF + 1}-${_to} 张 / 共 ${_tot} 张）`;
    }
    const _mb = ov.querySelector('#cover-more');
    if (_mb) {
      _mb.disabled = _tot <= _to;
      _mb.textContent = _tot <= _to ? '没有更多了' : '换一批';
    }
    // 未命名的组：标题改成"看看这是谁"，输入框自动聚焦，
    // 让用户先看脸再命名（不用盲猜）
    const me = (faceState.people || []).find((x) => x.id === pid);
    const named = ((me && me.name) || '').trim();
    ov.querySelector('#cover-title').textContent = named
      ? `选一张做头像（${named}）` : '这组是谁？看完命名';
    const nameInput = ov.querySelector('#cover-name');
    nameInput.value = named;
    nameInput.style.display = '';
    ov.querySelector('#cover-name-save').style.display = '';
    // 把所有已有的名字填进候选 —— 点一下就能选，不用手打
    const dl = ov.querySelector('#people-names');
    if (dl) {
      const names = (faceState.people || [])
        .filter((x) => x.id !== pid && (x.name || '').trim())
        .map((x) => x.name.trim())
        .sort((a, b) => a.localeCompare(b, 'zh'));
      dl.innerHTML = names.map(
        (n) => `<option value="${esc(n)}"></option>`).join('');
    }
    if (!named) setTimeout(() => nameInput.focus(), 60);
    tip.textContent = named
      ? `共 ${r.count} 张脸 · 点图=设头像，「移除」=不是他，「非脸」=不是人脸`
      : `共 ${r.count} 张脸 · 看到认识的人就输入名字，认不出就关掉`;
    // 图全挂了就直说原因（多数是浏览器插件拦了 /api/faces/ 请求）
    setTimeout(() => {
      const imgs = g.querySelectorAll('[data-face] img');
      const bad = [...imgs].filter((i) => !i.getAttribute('src')).length;
      if (imgs.length && bad === imgs.length) {
        tip.innerHTML = '<span style="color:var(--warn)">图片加载失败 —— '
          + '可能是浏览器插件（如 Adblock）拦了 /api/faces/ 请求，'
          + '请对本站关闭拦截插件后刷新</span>';
      } else if (bad) {
        tip.textContent += ` （其中 ${bad} 张加载失败）`;
      }
    }, 1500);
    // 不用 loading="lazy"：弹窗是弹出后才可见的，延迟加载在这里反而可能不触发
    // 尺寸全部写死在 style 里：不依赖任何 CSS 类，
    // 免得某条规则没生效就变成一排只有文字的细条（查了很久的坑）
    g.style.cssText = 'display:flex;flex-wrap:wrap;gap:10px;padding:14px 18px;'
      + 'overflow:auto;align-content:flex-start';
    g.innerHTML = r.faces.map((f) => `
      <div data-face="${f.id}" title="${esc(f.file || '')}"
           style="width:124px;cursor:pointer;border-radius:10px;overflow:hidden;
                  border:2px solid transparent;background:var(--panel2)">
        <div style="position:relative">
          <img src="${f.thumb || ('/api/faces/' + f.id + '/thumb?w=200')}" alt=""
               style="display:block;width:120px;height:120px;object-fit:cover"
               onerror="this.style.background='#333';this.removeAttribute('src')">
          <button data-rm="${f.id}" title="这张脸不是这个人 —— 摘掉它"
                  style="position:absolute;top:3px;right:3px;background:rgba(0,0,0,.72);
                         color:#fff;border:none;border-radius:6px;padding:2px 7px;
                         font-size:11px;cursor:pointer">移除</button>
          <button data-drop="${f.id}" title="这根本不是人脸（栏杆/花纹/后视镜等误检）—— 彻底删掉"
                  style="position:absolute;top:3px;left:3px;background:rgba(180,40,40,.82);
                         color:#fff;border:none;border-radius:6px;padding:2px 7px;
                         font-size:11px;cursor:pointer">非脸</button>
        </div>
        <div style="font-size:10.5px;color:var(--muted);text-align:center;padding:3px 4px">
          ${(f.prob * 100).toFixed(0)}% · ${f.w}px
        </div>
      </div>`).join('');
    g.querySelectorAll('[data-drop]').forEach((btn) => {
      btn.addEventListener('click', async (ev) => {
        ev.stopPropagation();
        const fid = Number(btn.dataset.drop);
        btn.textContent = '…';
        btn.disabled = true;
        try {
          await api(`/api/faces/${fid}/drop`, { method: 'DELETE' });
          const cell = btn.closest('[data-face]');
          if (cell) cell.remove();
          const left = g.querySelectorAll('[data-face]').length;
          tip.textContent = `已删除这条误检，这组还剩 ${left} 张。`
            + '误检删掉后不会再出现在聚类结果里。';
          await loadPeople();
        } catch (e) {
          btn.textContent = '非脸';
          btn.disabled = false;
          tip.textContent = '删除失败：' + e.message;
        }
      });
    });
    g.querySelectorAll('[data-rm]').forEach((btn) => {
      btn.addEventListener('click', async (ev) => {
        ev.stopPropagation();
        const fid = Number(btn.dataset.rm);
        btn.textContent = '…';
        btn.disabled = true;
        try {
          await api(`/api/faces/${fid}/person`, { method: 'DELETE' });
          const cell = btn.closest('[data-face]');
          if (cell) cell.remove();
          const left = g.querySelectorAll('[data-face]').length;
          tip.textContent = `已摘掉这张脸，还剩 ${left} 张。`
            + '摘掉的脸可以在「重新聚类」后重新命名。';
          await loadPeople();
        } catch (e) {
          btn.textContent = '移除';
          btn.disabled = false;
          tip.textContent = '移除失败：' + e.message;
        }
      });
    });
    g.querySelectorAll('[data-face]').forEach((el) => {
      el.addEventListener('click', async () => {
        const fid = Number(el.dataset.face);
        el.style.opacity = '0.4';
        try {
          await post(`/api/people/${pid}/cover`, { face_id: fid }, 'PUT');
          await loadPeople();
          ov.classList.remove('show');
        } catch (e) {
          el.style.opacity = '';
          tip.textContent = '设置失败：' + e.message;
        }
      });
    });
  } catch (e) {
    g.innerHTML = `<div class="hint" style="color:var(--bad)">读取失败：${esc(e.message)}</div>`;
  }
}

function pickPerson(id) {
  if (faceState.picked.has(id)) faceState.picked.delete(id);
  else faceState.picked.add(id);
  renderPeople();
  // 已选人物时，即使没有关键词也给出结果
  doSearch();
}

function clearPeople() {
  faceState.picked.clear();
  renderPeople();
  $('#face-guess').innerHTML = '';
  doSearch();
}

async function searchByFace(file) {
  if (!file) return;
  $('#results').innerHTML = '<div class="card"><div class="empty">正在比对这张脸…</div></div>';
  $('#face-guess').innerHTML = '';
  try {
    const buf = await file.arrayBuffer();
    const r = await fetch('/api/faces/search?top_k=60', {
      method: 'POST',
      headers: { 'Content-Type': file.type || 'application/octet-stream' },
      body: buf,
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
    if (data.guess) {
      const g = data.guess;
      $('#face-guess').innerHTML = `<div class="guessbox">最像：<b>${esc(g.name || '未命名')}</b>
        <span class="hint">相似度 ${(g.similarity * 100).toFixed(1)}%</span>
        <button class="ghost tiny" id="guess-pick" data-id="${g.person_id}">按这个人筛选</button></div>`;
      const btn = $('#guess-pick');
      if (btn) btn.addEventListener('click', () => {
        faceState.picked.add(g.person_id);
        renderPeople();
        doSearch();
      });
    } else {
      $('#face-guess').innerHTML = '<div class="hint">这张脸和已知人物都不太像，下面是直接按人脸相似度找出的照片。</div>';
    }
    render({ query: '', count: data.count, results: data.results, elapsed: 0 });
  } catch (e) {
    $('#results').innerHTML = `<div class="card"><div class="empty" style="color:var(--bad)">${esc(e.message)}</div></div>`;
  }
}

async function runBackfill() {
  const btn = $('#face-backfill');
  const tip = $('#backfill-progress');
  if (btn.disabled) return;
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = '补提中…';
  let total = 0, batch = 0;
  try {
    for (;;) {
      const fid = $('#backfill-folder') ? $('#backfill-folder').value : '';
      const body = { limit: 20 };
      if (fid) body.folder_id = Number(fid);
      const r = await post('/api/faces/backfill', body);
      total += r.faces;
      batch += r.processed;
      const scope = fid ? '本文件夹' : '全部文件夹';
      tip.textContent = `[${scope}] 已补 ${batch} 张图 / 累计 ${total} 张脸，剩 ${r.remaining} 张`;
      if (r.processed === 0 || r.remaining === 0) break;
      // 让出主线程，界面不至于卡死
      await new Promise((res) => setTimeout(res, 0));
    }
    await loadFaceStatus(); await loadPeople();
    tip.textContent = `补提完成：处理 ${batch} 张图，新增 ${total} 张脸。`
      + '现在可以点「重新聚类」。';
  } catch (e) {
    tip.textContent = '';
    alert('补提失败：' + e.message);
  }
  btn.disabled = false;
  btn.textContent = old;
}

/* ---- 搜索页里的人物筛选（不用切到「人物」标签页）---- */
function renderPickedChips() {
  const box = $('#picked-chips');
  if (!box) return;
  const names = [...faceState.picked].map((id) => {
    const p = faceState.people.find((x) => x.id === id);
    return { id, name: (p && p.name) || `#${id}` };
  });
  box.innerHTML = names.map((n) => `
    <span class="chip" data-unpick="${n.id}" title="点 × 取消">
      ${esc(n.name)} <b>×</b></span>`).join('');
  box.querySelectorAll('[data-unpick]').forEach((el) => {
    el.addEventListener('click', () => {
      faceState.picked.delete(Number(el.dataset.unpick));
      renderPickedChips();
      renderPeople();
      doSearch();
    });
  });
}

function fillQuickPeople() {
  const sel = $('#quick-person');
  if (!sel) return;
  const keep = sel.value;
  const named = faceState.people.filter((p) => (p.name || '').trim());
  sel.innerHTML = '<option value="">＋ 按人物筛选</option>'
    + named.map((p) => `<option value="${p.id}">${esc(p.name)}（${p.n_files || p.n || 0} 张）</option>`).join('');
  sel.value = keep;
  renderPickedChips();
}

function fillPeopleFolderFilter() {
  // 选项 = 我自己的人物里出现过的文件夹（后端 people[].folders 只给可写的）
  const sel = $('#people-folder-filter');
  if (!sel) return;
  const keep = sel.value;
  const ids = new Set();
  for (const pp of (faceState.people || [])) {
    for (const f of (pp.folders || [])) ids.add(f);
  }
  const opts = [`<option value="">${VIEW_MODE === 'member' ? '全部（我名下）' : '全部'}</option>`];
  for (const f of (window.__folders || [])) {
    if (!ids.has(f.id)) continue;
    const short = String(f.path || '').replace(/^.*[\\/]/, '') || f.path;
    opts.push(`<option value="${f.id}">${esc(short)}</option>`);
  }
  sel.innerHTML = opts.join('');
  sel.value = [...sel.options].some((o) => o.value === keep) ? keep : '';
}


/* ---------------- 💬 聊天（会话保存 + 联网搜索）---------------- */
// 剥掉模型吐出来的思考过程（<think>…</think>）——流式显示时也要滤，
// 否则用户会看到"让我想想…"这种无关内容 ✗
function stripThink(s) {
  let x = String(s == null ? '' : s);
  x = x.replace(/<think[\s\S]*?<\/think\s*>/gi, '');
  x = x.replace(/^\s*<think[\s\S]*$/i, '');
  return x.trim();
}

const chat2 = {
  convs: [], cur: 0, msgs: [], busy: false,
  atts: [],        // 已上传的附件：{id, name, kind}
};

// 上传一个文件，返回后端给的信息
async function uploadChat2File(f) {
  const fd = new FormData();
  fd.append('file', f);
  const r = await fetch('/api/chat2/upload', {
    method: 'POST', credentials: 'same-origin', body: fd,
  });
  const txt = await r.text();
  let j = null;
  try { j = JSON.parse(txt); } catch (e) { j = { detail: txt }; }
  if (!r.ok) {
    let why = '';
    const d = j && j.detail;
    if (typeof d === 'string') why = d;
    else if (Array.isArray(d)) why = d.map((x) => (x && x.msg) || '').join('；');
    throw new Error(why || ('HTTP ' + r.status));
  }
  return j;
}

function paintChat2Atts() {
  const box = $('#chat2-atts');
  if (!box) return;
  if (!chat2.atts.length) { box.style.display = 'none'; box.innerHTML = ''; return; }
  box.style.display = 'flex';
  box.innerHTML = chat2.atts.map((a, i) => `
    <span class="kindtag" style="display:inline-flex;gap:6px;align-items:center;
          padding:4px 8px;font-size:12px">
      ${a.kind === 'image' ? '🖼' : '📄'} ${esc((a.name || '').slice(0, 24))}
      <button class="ghost tiny" data-rmatt="${i}" style="padding:0 4px">✕</button>
    </span>`).join('');
  box.querySelectorAll('[data-rmatt]').forEach((b) => {
    b.addEventListener('click', () => {
      chat2.atts.splice(Number(b.dataset.rmatt), 1);
      paintChat2Atts();
    });
  });
}

// 当前选的模型能不能看图？拿不准就返回 null（不拦）
function chat2ModelVL() {
  const sel = $('#chat2-model');
  const v = sel ? (sel.value || '') : '';
  if (!v) return null;                          // 默认那套，不知道，不拦
  const hit = (window.__chat2Models || []).find((x) => x.id === v);
  if (!hit || typeof hit.vl !== 'boolean') return null;
  return hit.vl;
}

async function pickChat2Files(files) {
  const tip = $('#chat2-tip');
  // ★ 项目主 2026-10-22："做个提示" —— 纯文本模型收图会答"我看不到图片"
  const hasImg = files.some((f) => /^image\//.test(f.type || ''));
  const vl = chat2ModelVL();
  if (hasImg && vl === false) {
    const sel = $('#chat2-model');
    const nm = sel && sel.selectedIndex >= 0
      ? sel.options[sel.selectedIndex].textContent : '当前模型';
    const ok = confirm('「' + nm + '」是纯文本模型，看不到图片 ✗\n\n'
      + '传上去它也只会说"我看不到图片"。\n\n'
      + '要点：确定 = 照传（只当文件名参考）\n取消 = 先换个能看图的模型');
    if (!ok) return;
  }
  for (const f of files) {
    try {
      if (tip) tip.textContent = '正在上传 ' + f.name + '…';
      const j = await uploadChat2File(f);
      chat2.atts.push({ id: j.id, name: j.name, kind: j.kind });
      paintChat2Atts();
    } catch (e) {
      alert('上传失败：' + e.message);
    }
  }
  if (tip) tip.textContent = chat2.atts.length ? ('已附 ' + chat2.atts.length + ' 个') : '';
}

async function loadChat2Convs() {
  try {
    const r = await api('/api/chat2/conversations');
    chat2.convs = r.conversations || [];
    paintChat2Convs();
  } catch (e) {
    const box = $('#chat2-convs');
    if (box) box.innerHTML = `<div class="hint" style="color:var(--bad)">${esc(e.message)}</div>`;
  }
}

function paintChat2Convs() {
  const box = $('#chat2-convs');
  if (!box) return;
  if (!chat2.convs.length) {
    box.innerHTML = '<div class="hint">还没有对话，点上面「＋ 新对话」开始</div>';
    return;
  }
  box.innerHTML = chat2.convs.map((c) => `
    <div class="conv${c.id === chat2.cur ? ' on' : ''}" data-conv="${c.id}">
      <span class="del" data-del="${c.id}" title="删除这个对话">✕</span>
      <span class="t">${esc(c.title || '新对话')}</span>
      <span class="hint" style="font-size:11px">${c.n || 0} 条</span>
    </div>`).join('');
  box.querySelectorAll('[data-conv]').forEach((el) => {
    el.addEventListener('click', (ev) => {
      if (ev.target.dataset.del) return;
      openChat2Conv(Number(el.dataset.conv));
    });
  });
  box.querySelectorAll('[data-del]').forEach((el) => {
    el.addEventListener('click', async (ev) => {
      ev.stopPropagation();
      const id = Number(el.dataset.del);
      if (!confirm('删掉这个对话？（消息也会一起删）')) return;
      try {
        await api(`/api/chat2/conversations/${id}`, { method: 'DELETE' });
  box.querySelectorAll('[data-share]').forEach((b) => {
    b.addEventListener('click', (ev) => {
      ev.preventDefault(); ev.stopPropagation();
      openApiShare(Number(b.dataset.share));
    });
  });
        if (chat2.cur === id) { chat2.cur = 0; paintChat2Msgs(); }
        await loadChat2Convs();
      } catch (e) { alert('删除失败：' + e.message); }
    });
  });
}

function paintChat2Msgs() {
  const box = $('#chat2-msgs');
  if (!box) return;
  if (!chat2.cur) {
    box.innerHTML = '<div class="empty">左边选一个对话，或者点「＋ 新对话」</div>';
    return;
  }
  if (!chat2.msgs.length) {
    box.innerHTML = '<div class="empty">这个对话还是空的，在下面输入开始聊 ✓</div>';
    return;
  }
  box.innerHTML = chat2.msgs.map((m) => {
    let src = '';
    const sw = (m.sources && m.sources.web) || [];
    if (sw.length) {
      src += '<div class="src">🌐 参考了网上资料：'
        + sw.map((x, i) => `<a href="${esc(x.url)}" target="_blank" `
          + `rel="noreferrer">[W${i + 1}] ${esc((x.title || '').slice(0, 40))}</a>`).join(' · ')
        + '</div>';
    }
    const sd = (m.sources && m.sources.dify) || [];
    if (sd.length) {
      src += '<div class="src">☁ Dify 知识库：'
        + sd.map((x, i) => `[D${i + 1}] ${esc((x.title || '').slice(0, 40))}`).join(' · ')
        + '</div>';
    }
    const sk = (m.sources && m.sources.kb) || [];
    if (sk.length) {
      src += '<div class="src">📚 参考了自己的资料：'
        + sk.map((x, i) => `[K${i + 1}] ${esc((x.file || '').slice(0, 46))}`).join(' · ')
        + '</div>';
    } else if (m.sources && m.sources.kb_note) {
      // 勾了知识库但一条没查到 —— 也要说出来，不然看起来像"没生效"（2026-10-23）
      src += '<div class="src" style="opacity:.7">📚 ' + esc(m.sources.kb_note) + '</div>';
    }
    const _body = (m.role === 'user') ? m.content : stripThink(m.content);
    return `<div class="msg ${m.role === 'user' ? 'user' : 'assistant'}">`
      + esc(_body) + src + '</div>';
  }).join('');
  box.scrollTop = box.scrollHeight;
}

async function openChat2Conv(id) {
  chat2.cur = id;
  paintChat2Convs();
  try {
    const r = await api(`/api/chat2/conversations/${id}`);
    chat2.msgs = r.messages || [];
    paintChat2Msgs();
  } catch (e) {
    $('#chat2-msgs').innerHTML =
      `<div class="empty" style="color:var(--bad)">${esc(e.message)}</div>`;
  }
}

async function newChat2Conv() {
  try {
    // ⚠ 必须带 Content-Type，否则 FastAPI 当 text/plain 收 → 422 ✗
    const r = await api('/api/chat2/conversations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: '新对话' }) });
    chat2.cur = r.id;
    chat2.msgs = [];
    await loadChat2Convs();
    paintChat2Msgs();
    const inp = $('#chat2-input');
    if (inp) inp.focus();
  } catch (e) { alert('新建失败：' + e.message); }
}

async function sendChat2() {
  if (chat2.busy) return;
  const inp = $('#chat2-input');
  const btn = $('#chat2-send');
  const tip = $('#chat2-tip');
  const text = (inp.value || '').trim();
  if (!text) return;
  if (!chat2.cur) { await newChat2Conv(); if (!chat2.cur) return; }
  const web = !!($('#chat2-web') || {}).checked;
  const kb = !!($('#chat2-kb') || {}).checked;
  const dfy = !!($('#chat2-dify') || {}).checked;
  const pick = (($('#chat2-model') || {}).value || '');
  const atts = chat2.atts.map((a) => a.id);
  chat2.busy = true;
  btn.disabled = true;
  inp.value = '';
  chat2.attNames = chat2.atts.map((a) => a.name);   // 记下来给气泡显示
  chat2.atts = [];          // 附件跟着这条消息走了
  paintChat2Atts();
  chat2.msgs.push({ role: 'user', content: text });
  paintChat2Msgs();
  const wait = document.createElement('div');
  wait.className = 'msg assistant';
  const _parts = [];
  if (web) _parts.push('联网搜索');
  if (dfy) _parts.push('Dify 知识库');
  if (kb) _parts.push('我的知识库');
  wait.textContent = _parts.length
    ? ('正在' + _parts.join(' + ') + '，然后思考…') : '正在思考…';
  wait.style.opacity = '.65';
  $('#chat2-msgs').appendChild(wait);
  $('#chat2-msgs').scrollTop = 1e9;
  if (tip) tip.textContent = web ? '联网搜索中（可能要十几秒）' : '';
  // ★ 2026-10-22：改成**流式**接收（逐字显示，不用干等整段）✓
  let acc = '';
  let srcWeb = [];
  let srcKb = [];
  let srcDify = [];
  let kbNote = '';
  let errMsg = '';
  try {
    const res = await fetch(`/api/chat2/conversations/${chat2.cur}/stream`, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, web, kb, dify: dfy, pick, attachments: atts }),
    });
    if (!res.ok) {
      let why = 'HTTP ' + res.status;
      try { const j = await res.json(); why = j.detail || why; } catch (e) { /* 忽略 */ }
      throw new Error(why);
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    wait.textContent = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx = buf.indexOf('\n\n');
      while (idx >= 0) {
        const chunk = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 2);
        idx = buf.indexOf('\n\n');
        if (!chunk.startsWith('data:')) continue;
        let obj = null;
        try { obj = JSON.parse(chunk.slice(5).trim()); } catch (e) { continue; }
        if (obj.delta) {
          acc += obj.delta;
          wait.textContent = stripThink(acc);   // 逐字更新（也剥思考）
          $('#chat2-msgs').scrollTop = 1e9;
        } else if (obj.error) {
          errMsg = obj.error;
        } else if (obj.done) {
          srcWeb = ((obj.web || {}).results) || [];
          // ★ 2026-10-23：知识库 / Dify 的命中也要接住，否则界面上没有任何标记 ✗
          srcKb = obj.kb || [];
          srcDify = obj.dify || [];
          kbNote = obj.kb_note || '';
          if (obj.web && obj.web.error) errMsg = obj.web.error;
        }
      }
    }
    if (wait.parentNode) wait.remove();
    chat2.msgs.push({ role: 'assistant', content: acc || ('（没收到内容' + (errMsg ? '：' + errMsg : '') + '）'),
                      sources: { web: srcWeb, kb: srcKb, dify: srcDify, kb_note: kbNote } });
    paintChat2Msgs();
    if (tip) tip.textContent = errMsg ? ('⚠ ' + errMsg) : '';
    // 检索不到时明确说一句，别让人以为"勾了没生效"（2026-10-23）
    if (kb && !srcKb.length && kbNote && tip) tip.textContent = '📚 ' + kbNote;
    await loadChat2Convs();
  } catch (e) {
    if (wait.parentNode) wait.remove();
    chat2.msgs.push({ role: 'assistant', content: '（出错了：' + e.message + '）' });
    paintChat2Msgs();
    if (tip) tip.textContent = '发送失败：' + e.message;
  } finally {
    chat2.busy = false;
    btn.disabled = false;
    inp.focus();
  }
}

async function loadChat2Models() {
  const sel = $('#chat2-model');
  if (!sel) return;
  const keep = sel.value;
  const items = [];
  // ① 本机已部署的
  try {
    const r = await api('/api/chat/models');
    for (const m of (r.models || [])) {
      items.push({ id: 'local:' + (m.path || m.name || ''),
                   label: '本机 · ' + (m.name || '?'),
                   vl: !!m.vl });           // 后端按模型架构判断的
    }
  } catch (e) { /* 本机没模型就算了 */ }
  // ② 用户配的 API
  try {
    const r2 = await api('/api/chat2/models');
    for (const m of (r2.models || [])) items.push(m);
  } catch (e) { /* 忽略 */ }
  window.__chat2Models = items;    // 记下每个模型的 vl 标记（传图时要用）
  const opts = ['<option value="">默认（设置里的那套）</option>']
    .concat(items.map((x) => `<option value="${esc(x.id)}">${esc(x.label)}`
      + (x.vl ? '　👁 可看图' : '') + '</option>'));
  sel.innerHTML = opts.join('');
  sel.value = [...sel.options].some((o) => o.value === keep) ? keep : '';
}

function bindChat2() {
  const nb = $('#chat2-new');
  if (nb) nb.addEventListener('click', newChat2Conv);
  const sb = $('#chat2-send');
  if (sb) sb.addEventListener('click', sendChat2);
  const inp = $('#chat2-input');
  if (inp) {
    inp.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); sendChat2(); }
    });
  }
  const ex = $('#chat2-export');
  if (ex) {
    ex.addEventListener('click', () => {
      if (!chat2.cur) { alert('先选一个对话'); return; }
      const fmt = confirm('导出 Markdown？\n\n确定 = Markdown（可读）\n取消 = JSON（可导入）')
        ? 'md' : 'json';
      window.open(`/api/chat2/conversations/${chat2.cur}/export?fmt=${fmt}`, '_blank');
    });
  }
  const ab = $('#chat2-att');
  const af = $('#chat2-file');
  if (ab && af) {
    ab.addEventListener('click', () => af.click());
    af.addEventListener('change', async (ev) => {
      const fs = [...(ev.target.files || [])];
      ev.target.value = '';
      if (fs.length) await pickChat2Files(fs);
    });
  }
  loadChat2Convs();
  loadChat2Models();
}

/* 「这套 API 谁能用」：打勾选人（项目主 2026-10-22） */
async function openApiShare(idx) {
  const list = rmApiList();
  const s = list[idx];
  if (!s) return;
  let users = [];
  try {
    // ⚠ 2026-10-22：原来请求的是 **/api/admin/users**（这个接口不存在 → 404）
    //   → 弹窗永远出不来，表现就是"共享按钮点不动" ✗
    //   真实的用户列表在 /api/auth/users（管理员可读）
    const r = await api('/api/auth/users');
    users = r.users || [];
  } catch (e) {
    alert('拿不到用户列表（这个操作要管理员）：' + e.message);
    return;
  }
  const cur = new Set((s.share_to || []).map(Number));
  let ov = document.getElementById('share-ov');
  if (ov) ov.remove();
  ov = document.createElement('div');
  ov.id = 'share-ov';
  ov.className = 'coverov';
  ov.innerHTML = '<div class="coverbox" style="max-width:420px">'
    + '<div class="coverhead"><b>「' + esc(s.name || ('配置' + (idx + 1)))
    + '」谁能用？</b><span style="flex:1"></span>'
    + '<button class="ghost tiny" id="share-close">关闭</button></div>'
    + '<div style="padding:6px 2px">'
    + '<label style="display:flex;gap:8px;align-items:center;padding:7px 4px;'
    + 'border-bottom:1px solid var(--line)">'
    + '<input type="checkbox" id="share-all"' + (s.share ? ' checked' : '') + '>'
    + '<b>所有人</b><span class="hint">（勾上就不必再挑人）</span></label>'
    + users.map((u) => '<label style="display:flex;gap:8px;align-items:center;'
        + 'padding:7px 4px"><input type="checkbox" data-uid="' + u.id + '"'
        + (cur.has(Number(u.id)) ? ' checked' : '') + '>'
        + esc(u.name || ('#' + u.id))
        + '<span class="hint">' + esc(u.role || '') + '</span></label>').join('')
    + '</div>'
    + '<div class="hint" style="margin-top:8px">密钥只在服务器上用，'
    + '对方只看到模型名，拿不到你的 key ✓</div>'
    + '<div style="display:flex;gap:8px;margin-top:10px">'
    + '<button id="share-save">保存</button></div></div>';
  document.body.appendChild(ov);
  ov.classList.add('show');
  ov.querySelector('#share-close').addEventListener('click', () => ov.remove());
  ov.addEventListener('click', (ev) => { if (ev.target === ov) ov.remove(); });
  ov.querySelector('#share-save').addEventListener('click', () => {
    const all = ov.querySelector('#share-all').checked;
    const ids = [...ov.querySelectorAll('[data-uid]')]
      .filter((x) => x.checked).map((x) => Number(x.dataset.uid));
    const l2 = rmApiList();
    if (!l2[idx]) return;
    if (all) { l2[idx].share = true; delete l2[idx].share_to; }
    else { delete l2[idx].share; if (ids.length) l2[idx].share_to = ids; else delete l2[idx].share_to; }
    rmSaveApiList(l2);
    renderApiList();
    ov.remove();
  });
}

/* ---------------- ☁ Dify 知识库（每个用户自己的多个库）---------------- */
function difyList() {
  try { return JSON.parse(localStorage.getItem('eg2-difylist') || '[]'); }
  catch (e) { return []; }
}
function difySave(list) {
  try { localStorage.setItem('eg2-difylist', JSON.stringify(list)); }
  catch (e) { alert('存不下：' + e.message); }
}

/* 共享名单在 localStorage 里存成字符串（"张三, 张三"），渲染时转一下 ✓ */
function shareToText(d) {
  const v = (d && d.share_to) || [];
  if (Array.isArray(v)) return v.join(', ');
  return String(v || '');
}

/* 别人共享给我的库（含谁共享的）；没有就不显示这一块 */
async function loadSharedDify() {
  const box = document.getElementById('dify-shared');
  if (!box) return;
  try {
    const r = await api('/api/dify/shared');
    const items = (r && r.items) || [];
    if (!items.length) { box.innerHTML = ''; return; }
    box.innerHTML = '<div class="hint" style="margin-top:10px">'
      + '🤝 <b>别人共享给我的库</b>（你只省了配置，密钥在对方手里）：</div>'
      + items.map((x) => `<div class="hint" style="padding:4px 8px;margin-top:4px;
          border:1px solid var(--line);border-radius:8px;background:var(--panel2)">
          ☁ <b>${esc(x.name || '(没起名)')}</b>
          <span style="opacity:.75">· 来自 ${esc(x.owner || '?')}</span></div>`).join('');
  } catch (e) { box.innerHTML = ''; }
}

function renderDifyList() {
  const box = $('#dify-list');
  if (!box) return;
  const list = difyList();
  if (!list.length) {
    box.innerHTML = '<div class="hint">还没有接任何 Dify 知识库。</div>';
    loadSharedDify();
    return;
  }
  box.innerHTML = list.map((d, i) => {
    const sh = (d.share || 'private');
    return `
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;
                padding:8px 10px;border:1px solid var(--line);border-radius:8px;
                background:var(--panel2)">
      <input type="checkbox" data-dify-en="${i}" ${d.enabled === false ? '' : 'checked'}
             title="取消勾选就临时不用这个库">
      <input type="text" data-dify-f="name" data-i="${i}" value="${esc(d.name || '')}"
             placeholder="起个名（比如「产品文档」）"
             style="width:130px;padding:3px 6px;border-radius:6px;
                    border:1px solid var(--line);background:var(--panel);color:var(--fg)">
      <input type="text" data-dify-f="base_url" data-i="${i}" value="${esc(d.base_url || '')}"
             placeholder="http://192.168.1.30:8080（别带 /v1）"
             title="只填到端口：http://地址:端口。/v1 会被自动去掉，带不带都能用。"
             style="width:250px;padding:3px 6px;border-radius:6px;
                    border:1px solid var(--line);background:var(--panel);color:var(--fg)">
      <input type="text" data-dify-f="dataset_id" data-i="${i}" value="${esc(d.dataset_id || '')}"
             placeholder="知识库 ID（36 位 UUID）"
             title="Dify 知识库 → 左侧「API」里那串 8-4-4-4-12 的 UUID，必须复制完整"
             style="width:290px;padding:3px 6px;border-radius:6px;
                    border:1px solid var(--line);background:var(--panel);color:var(--fg)">
      <input type="password" data-dify-f="dataset_key" data-i="${i}"
             value="${esc(d.dataset_key || '')}" placeholder="dataset-xxxx"
             style="width:170px;padding:3px 6px;border-radius:6px;
                    border:1px solid var(--line);background:var(--panel);color:var(--fg)">
      <button class="ghost tiny" data-dify-del="${i}">删除</button>
      <!-- ★ 2026-10-22 项目主："做共享" —— 每个库单独决定给谁用 -->
      <div style="flex-basis:100%;display:flex;gap:8px;align-items:center;flex-wrap:wrap;
                  padding-top:6px;border-top:1px dashed rgba(255,255,255,.08)">
        <span class="hint">谁能用</span>
        <select data-dify-f="share" data-i="${i}"
                style="padding:3px 6px;border-radius:6px;border:1px solid var(--line);
                       background:var(--panel);color:var(--fg)">
          <option value="private" ${sh === 'private' ? 'selected' : ''}>只有我</option>
          <option value="all" ${sh === 'all' ? 'selected' : ''}>所有登录用户</option>
          <option value="to" ${sh === 'to' ? 'selected' : ''}>指定的人</option>
        </select>
        <input type="text" data-dify-f="share_to" data-i="${i}"
               value="${esc(shareToText(d))}" placeholder="填用户名，多个用逗号隔开"
               title="只在「指定的人」时生效；填的是对方的登录用户名"
               style="width:260px;padding:3px 6px;border-radius:6px;
                      border:1px solid var(--line);background:var(--panel);color:var(--fg);
                      ${sh === 'to' ? '' : 'display:none'}">
        <span class="hint" data-share-note="${i}"></span>
        <span class="hint" style="opacity:.75">共享出去的是「能用它检索」，
          对方界面上看不到你的密钥 ✓</span>
      </div>
    </div>`;
  }).join('');

  box.querySelectorAll('[data-dify-f]').forEach((el) => {
    el.addEventListener('change', () => {
      const l2 = difyList();
      const i = Number(el.dataset.i);
      if (!l2[i]) return;
      l2[i][el.dataset.difyF] = el.value.trim();
      difySave(l2);
      const msg = $('#dify-msg');
      if (msg) { msg.textContent = '已保存'; setTimeout(() => { msg.textContent = ''; }, 1500); }
      const f = el.dataset.difyF;
      if (f === 'share') {
        // 切成「指定的人」才显示名单框；切回别的就把它藏起来 ✓
        const inp = box.querySelector('[data-dify-f="share_to"][data-i="' + i + '"]');
        if (inp) inp.style.display = (el.value === 'to') ? '' : 'none';
        loadSharedDify();
      }
      if (f === 'share_to') {
        // 名字写错等于没共享 → 立刻校验，查无此人当场提示 ✓
        const note = box.querySelector('[data-share-note="' + i + '"]');
        const names = el.value.replace(/，/g, ',').split(',').map((x) => x.trim())
          .filter(Boolean);
        if (!names.length) { if (note) note.textContent = ''; return; }
        post('/api/dify/validate', { names }, 'POST').then((r) => {
          if (!note) return;
          if ((r.unknown || []).length) {
            note.innerHTML = '<span style="color:var(--bad)">查无此人：'
              + esc((r.unknown || []).join('、')) + '</span>';
          } else {
            note.innerHTML = '<span style="color:var(--ok)">已共享给：'
              + esc((r.valid || []).join('、')) + '</span>';
          }
        }).catch(() => { /* 校验失败不影响保存 */ });
      }
    });
  });
  box.querySelectorAll('[data-dify-en]').forEach((el) => {
    el.addEventListener('change', () => {
      const l2 = difyList();
      const i = Number(el.dataset.difyEn);
      if (!l2[i]) return;
      l2[i].enabled = el.checked;
      difySave(l2);
    });
  });
  box.querySelectorAll('[data-dify-del]').forEach((el) => {
    el.addEventListener('click', () => {
      const i = Number(el.dataset.difyDel);
      const l2 = difyList();
      if (!confirm('删掉「' + (l2[i] && l2[i].name || ('#' + (i + 1))) + '」？')) return;
      l2.splice(i, 1);
      difySave(l2);
      renderDifyList();
    });
  });
}

function bindDify() {
  const add = $('#dify-add');
  if (add) {
    add.addEventListener('click', () => {
      const l2 = difyList();
      l2.push({ name: '知识库' + (l2.length + 1), base_url: '', dataset_id: '',
                dataset_key: '', enabled: true, share: 'private' });
      difySave(l2);
      renderDifyList();
    });
  }
  renderDifyList();
  loadSharedDify();
}

function bindPeople() {
  const f = $('#face-file');
  if (f) f.addEventListener('change', (ev) => {
    const file = ev.target.files && ev.target.files[0];
    ev.target.value = '';
    searchByFace(file);
  });
  const c = $('#people-clear'); if (c) c.addEventListener('click', clearPeople);
  const ct = $('#cluster-thr');
  if (ct) {
    try {
      const saved = localStorage.getItem('eg2-cthr');
      if (saved) ct.value = saved;
    } catch (e) { /* 忽略 */ }
  }
  // 人物筛选器：换文件夹就重排（纯前端过滤，不重新请求 ✓）
  const pff = $('#people-folder-filter');
  if (pff) pff.addEventListener('change', () => renderPeople());
  bindDify();
  bindChat2();
  bindVl();
  bindCaptionQueue();        // 🗂 PDF 插图打标（队列）：开始/暂停/继续/停止 + 实时进度 ✓
  loadPrivacy();             // 🔒 隐私文件夹：两栏（普通/隐私）一键切换 ✓
  bindSyncServer();
  bindGpu();
bindIgnoreDirs();          // 默认排除目录（设置里自己改）✓
  bindBackup();              // 定时增量备份（设置页「💾 定时增量备份」卡）✓
  bindSnapshot();            // 📦 快照与异地副本（手动拍 + 同步到 NAS）✓
  bindPush();                // ⬆ 上传到服务器（本机 → 服务器）✓
  bindMine();                // 📦 我的数据（能撤自己的，不能删别人的）✓
  bindGifts();               // 🤝 共享给我的（别人指定共享给我）✓
  bindRemote();
  bindKbImages();
  const bn = $('#bak-now');
  if (bn) bn.addEventListener('click', async () => {
    bn.disabled = true; bn.textContent = '备份中…';
    try {
      const r = await post('/api/backup', {});
      await loadBackups();
      alert(r.message || '备份完成');
    } catch (e) { alert('备份失败：' + e.message); }
    bn.disabled = false; bn.textContent = '立即备份';
  });
  loadBackups().catch(() => {});
  const dd = $('#people-dedup');
  if (dd) dd.addEventListener('click', async () => {
    dd.disabled = true; dd.textContent = '清理中…';
    try {
      const r = await post('/api/people/dedup', {});
      await loadPeople();
      alert(r.names ? `清理了 ${r.names} 个重名，合并 ${r.merged} 个碎片组。`
                    : '没有重名，很干净 ✓');
    } catch (e) { alert('失败：' + e.message); }
    dd.disabled = false; dd.textContent = '同名去重';
  });
  const ms = $('#people-merge-similar');
  if (ms) ms.addEventListener('click', async () => {
    ms.disabled = true; ms.textContent = '分析中…';
    try {
      const pre = await post('/api/people/merge-similar', { threshold: 0.68, dry: true });
      if (!pre.candidates) { alert('没有找到可以整组合并的组。'); return; }
      const lines = (pre.preview || []).slice(0, 12)
        .map((x) => `  ${x.faces} 张 → ${x.name}（相似度 ${x.sim}）`).join('\n');
      if (!confirm(`找到 ${pre.candidates} 个未命名组可以整组并入已有的人物，\n`
        + `共 ${pre.faces} 张脸。\n\n前几个：\n${lines}\n\n`
        + `确定执行吗？（已命名的组不会被改）`)) return;
      ms.textContent = '合并中…';
      const r = await post('/api/people/merge-similar', { threshold: 0.68, dry: false });
      await loadPeople();
      alert(`已把 ${r.merged} 个组并入对应人物，共 ${r.faces} 张脸。`);
    } catch (e) { alert('失败：' + e.message); }
    ms.disabled = false; ms.textContent = '合并相似组';
  });
  const ps = $('#people-sort');
  if (ps) {
    try {
      const saved = localStorage.getItem('eg2-psort');
      if (saved) { window.__peopleSort = saved; ps.value = saved; }
    } catch (e) { /* 忽略 */ }
    ps.addEventListener('change', () => {
      window.__peopleSort = ps.value;
      try { localStorage.setItem('eg2-psort', ps.value); } catch (e) {}
      renderPeople();
    });
  }
  const rb = $('#people-rebuild');
  if (rb) rb.addEventListener('click', async () => {
    if (!confirm('把**还没命名**的人脸重新聚类成新组？\n\n· 你已经命名过的脸会原样保留，不会丢\n· 只会动那些还没归属到任何人的脸\n\n继续？')) return;
    rb.disabled = true; rb.textContent = '聚类中…';
    try {
      const thr = parseFloat(($('#cluster-thr') || {}).value) || 0.75;
      const r = await post('/api/people/rebuild', { threshold: thr });
      await loadPeople(); await loadFaceStatus();
      try { localStorage.setItem('eg2-cthr', String(thr)); } catch (e) {}
      alert(`完成（阈值 ${thr}）：${r.groups} 组，${r.faces} 张脸`
        + (r.restored ? `，保回命名 ${r.restored} 张` : ''));
    } catch (e) { alert('失败：' + e.message); }
    rb.disabled = false; rb.textContent = '重新聚类';
  });
  const bf = $('#face-backfill');
  if (bf) bf.addEventListener('click', () => { fillBackfillFolders(); runBackfill(); });
  fillBackfillFolders();
  const as = $('#people-assign');
  if (as) as.addEventListener('click', async () => {
    as.disabled = true; as.textContent = '归类中…';
    try {
      const r = await post('/api/people/assign', {});
      await loadPeople();
      alert(`归到某人：${r.assigned} 张；仍未知：${r.unknown} 张`);
    } catch (e) { alert('失败：' + e.message); }
    as.disabled = false; as.textContent = '归类补全';
  });
}


/* ---------------- 知识库问答 ---------------- */
let kbHistory = [];

async function loadKbStatus() {
  // 每次刷新状态时顺便把「外部 API」那一组补回问答下拉（幂等），
  // 免得初始化时序不同导致它没出现。
  try { if (typeof renderKbEngineOptions === 'function') renderKbEngineOptions(); }
  catch (e) { /* 忽略 */ }
  try {
    const s = await api('/api/chat/status');
    const el = $('#kb-status');
    if (!s.available) {
      // 服务器上没显卡、不装本机对话模型是正常的：只要配了外部模型就照样能问答
      if (s.remote_configured) {
        el.innerHTML = '<span style="color:var(--ok)">本机没有对话模型，将用外部模型回答</span>'
          + ` —— ${esc(s.remote_model)} @ ${esc(s.remote_url)}`;
        return s;
      }
      el.innerHTML = `<span style="color:var(--warn)">对话模型没就绪</span> —— ${esc(s.reason)}`
        + '<br>也可以去「设置 → 外部模型」填一个（服务器上没显卡时就用它）';
      return s;
    }
    if (!s.kb_folders) {
      el.innerHTML = '<span style="color:var(--warn)">还没有文件夹被标为「知识库」</span>'
        + ' —— 在下面文件夹那行点「配置」，把「作为知识库」打开，然后重建索引';
      return s;
    }
    el.textContent = `就绪 · ${s.kb_folders} 个知识库文件夹 · 设备 ${s.device || '未加载'}`;
    return s;
  } catch (e) {
    $('#kb-status').textContent = '状态读取失败：' + e.message;
    return null;
  }
}

function renderKbAnswer(data) {
  const box = $('#kb-answer');
  // 服务端如果"自动改用外部模型"（没勾选但本机没有对话模型），会带回一句说明 ——
  // 显示出来，免得用户以为勾选框坏了
  if (data && data.note) {
    const tip = document.createElement('div');
    tip.className = 'hint';
    tip.style.marginBottom = '6px';
    tip.textContent = 'ⓘ ' + data.note;
    box.appendChild(tip);
  }
  // 图片类来源也给出缩略图 —— 只用文件名和路径判断，不必额外请求
  const IMG_EXT = /\.(jpe?g|png|gif|bmp|webp|heic|heif|avif|tiff?)$/i;
  const src = (data.sources || []).map((s) => {
    const isImg = IMG_EXT.test(s.name || '');
    const thumb = isImg
      ? `<img class="kbsrcimg" loading="lazy" alt=""
             src="/api/preview/${s.file_id}/image?w=240"
             data-preview="${s.file_id}" title="点击预览">`
      : '';
    return `
    <div class="kbsrc${s.cited ? ' cited' : ''}${isImg ? ' withimg' : ''}">
      ${thumb}
      <div class="kbsecbody">
        <div class="top"><b>[${s.n}]</b>
          <span class="name" data-preview="${s.file_id}" title="点击预览">${esc(s.name)}</span>
          <span class="hint">${esc(s.dir || '')} · 匹配度 ${(s.score || 0).toFixed(1)}${
            isImg ? ' · 图片' : ''}</span>
        </div>
        <div class="snip">${esc((s.text || '').slice(0, 220))}</div>
      </div>
    </div>`;
  }).join('');
  // ☁ Dify 来源（2026-10-22）：勾了「Dify 一起查」才有。
  // 引用编号是 [D1] [D2]，和本项目的 [1] [2] 分开，免得对错来源。
  const dfy = (data && data.dify) || null;
  let dtxt = '';
  if (dfy) {
    const dres = (dfy.results || []).map((d, i) => `
      <div class="kbsrc cited">
        <div class="top"><b>[D${i + 1}]</b>
          <span class="name">${esc(d.title || 'Dify 片段')}</span>
          <span class="hint">${esc(d.source || '')}${d.score ? ` · 匹配度 ${d.score}` : ''}</span>
        </div>
        <div class="snip">${esc((d.snippet || '').slice(0, 220))}</div>
      </div>`).join('');
    const derr = (dfy.errors || []).length
      ? `<div class="hint" style="color:var(--warn)">Dify 提示：${esc((dfy.errors || []).join('；'))}</div>`
      : '';
    dtxt = (dres ? `<div class="kbhead">☁ Dify 知识库来源</div>${dres}` : '') + derr;
  }
  const meta = `<div class="hint" style="margin-top:8px">
      用了 ${data.chunks || 0} 个片段 · 引用 ${(data.cited || []).length} 处
      · 耗时 ${data.elapsed || 0}s${data.tokens ? ` · 生成 ${data.tokens} tokens` : ''}</div>`;
  box.innerHTML = `<div class="kbans">${esc(data.answer || '（没生成内容）')
      .replace(/\n/g, '<br>').replace(/\[(\d{1,2})\]/g, '<b class="cite">[$1]</b>')
      .replace(/\[D(\d{1,2})\]/g, '<b class="cite">[D$1]</b>')}</div>`
    + (src ? `<div class="kbhead">来源</div>${src}` : '') + dtxt + meta;
  // 记住这次回答的来源，点预览时按 file_id 找回来。
  // openPreview() 要的是**对象**（id/name/path/kind/size/mtime），
  // 之前直接传了数字，于是弹出 "[object Object] / 缺少 path"。
  window.__kbSources = data.sources || [];
  const IMG_RE = /\.(jpe?g|png|gif|bmp|webp|heic|heif|avif|tiff?)$/i;
  box.querySelectorAll('[data-preview]').forEach((el) => {
    el.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const id = Number(el.dataset.preview);
      const src = (window.__kbSources || []).find((x) => Number(x.file_id) === id);
      if (!src) { location.hash = 'pv-' + id; return; }
      // 定位词用"这条来源里真的出现过的关键词"（后端算好的），
      // 没有才回退到输入框里的整句 ✓ —— 这样知识库来源点开也能跳到命中页 ✓
      previewTermOverride = (src.hit_terms && src.hit_terms.length)
        ? src.hit_terms.join(' ') : '';
      openPreview({
        id: src.file_id,
        name: src.name,
        path: src.path || src.name,
        kind: IMG_RE.test(src.name || '') ? 'image' : 'other',
        size: src.size || 0,
        mtime: src.mtime || 0,
      });
    });
  });
}

async function loadChatModels() {
  const qp = $('#quick-person');
  if (qp) qp.addEventListener('change', () => {
    const id = Number(qp.value);
    if (!id) return;
    faceState.picked.add(id);
    qp.value = '';
    renderPickedChips();
    renderPeople();
    doSearch();
  });
  const sel = $('#kb-model');
  if (!sel) return;
  try {
    const r = await api('/api/chat/models');
    if (!r.models.length) {
      sel.innerHTML = '<option>（models 里没有模型）</option>';
      renderKbEngineOptions();          // 本机没模型也要能选外部 API
      return;
    }
    // 列所有能加载的模型：文本模型（is_chat）+ 视觉模型（vl）。
    // 视觉模型不能当文本模型用，但它可以**看图** ——
    // 想用图提问就必须选它。
    const usable = r.models.filter((m) => m.is_chat || m.vl);
    const skipped = r.models.filter((m) => !m.is_chat && !m.vl).map((m) => m.name);
    if (!usable.length) {
      sel.innerHTML = '<option>（没有可用的对话模型）</option>';
      renderKbEngineOptions();          // 本机没模型也要能选外部 API
      return;
    }
    const grp = document.createElement('optgroup');
    grp.label = '本机模型';
    sel.innerHTML = '';
    sel.appendChild(grp);
    grp.innerHTML = usable.map((m) => {
      let tag = '';
      if (m.vl) tag = ' 👁 视觉';
      else if (m.is_chat) tag = ' 📝 文本';
      return `<option value="${esc(m.path)}"${m.current ? ' selected' : ''}>`
        + `${esc(m.name)}（${m.gb} GB）${tag}</option>`;
    }).join('');
    window.__models = usable;
    if (skipped.length) {
      sel.title = '已隐藏不能用作对话的模型：' + skipped.join('、');
    }
    if (typeof updateKbGuide === 'function') updateKbGuide();
    const free = r.gpu ? ` · 显存余 ${(r.gpu / 1024).toFixed(1)} GB` : '';
    // 当前模型要是不可用（比如选成了 VL），自动切回第一个能用的
    if (!usable.some((m) => m.current)) {
      const fix = usable[0];
      try {
        await post('/api/chat/model', { path: fix.path });
        sel.value = fix.path;
        $('#kb-status').textContent =
          `已自动切回可用的文本模型：${fix.name}`;
      } catch (e) { /* 忽略 */ }
    }
    const cur = r.models.find((m) => m.current);
    if (cur && r.gpu && cur.gb * 1024 > r.gpu) {
      $('#kb-status').innerHTML +=
        `<span style="color:var(--warn)"> ⚠ 这个模型约 ${cur.gb} GB，显存可能不够`
        + `（余 ${(r.gpu / 1024).toFixed(1)} GB），可先点「卸下模型」`
        + `或把嵌入模型切「省显存」</span>`;
    }
    sel.title = `当前设备 ${r.device || '未加载'}${free}`;
  } catch (e) {
    sel.innerHTML = '<option>读取失败</option>';
  }
}

async function askKb() {
  const q = $('#kb-q').value.trim();
  if (!q) return;
  const btn = $('#kb-ask');
  btn.disabled = true;
  $('#kb-answer').innerHTML = '<div class="kbans hint">正在检索并生成…'
    + '（首次会加载对话模型，需要十几秒）</div>';
  const t0 = Date.now();
  try {
    // top_k 给大一点：默认 5 个文件常常覆盖不到真正讲这个问题的文章
    // （实测"obsidian的安装"只取 5 个文件时命中的是"基本使用"，
    //   而库里其实有"Obsidian 容器化部署"那种安装教程）
    const body = { q, history: kbHistory.slice(-4), top_k: 7,
                   images: kbImages.slice() };
    // ☁ Dify 一起查（2026-10-22 项目主）：勾上就多查一路，两套来源合并进回答 ✓
    body.dify = !!($('#kb-dify') || {}).checked;
    // 回答用谁：下拉里选的是「本机模型」（值=模型路径）还是「外部 API」（值=api:N）
    const pick = ($('#kb-model') || {}).value || '';
    if (pick.indexOf('api:') === 0) {
      // ★ 2026-10-22：现在下拉里的 API 可能来自**服务端**（服务器公用/别人共享的），
      //   那种本机 localStorage 里没有、也没有密钥 → 只把序号发回去，
      //   让服务端按同一套规则取配置（密钥不出服务器）✓
      const opt = ($('#kb-model') || {}).selectedOptions || [];
      const fromSrv = opt[0] && opt[0].dataset && opt[0].dataset.srv === '1';
      const s = rmApi(Number(pick.slice(4)));
      if (fromSrv || !s || !s.base_url || !s.model) {
        body.pick = pick;
      } else {
        let hdr = null;
        if (s.headers) { try { hdr = JSON.parse(s.headers); } catch (e) { hdr = null; } }
        body.remote = { base_url: s.base_url, model: s.model, api_key: s.api_key || '',
                        headers: hdr, max_side: 448, max_tokens: 800 };
      }
    }
    const r = await post('/api/chat', body);
    kbImages = [];
    renderKbImgs();
    updateKbGuide();
    renderKbAnswer(r);
    kbHistory.push({ role: 'user', content: q });
    kbHistory.push({ role: 'assistant', content: r.answer });
  } catch (e) {
    $('#kb-answer').innerHTML =
      `<div class="kbans" style="color:var(--bad)">${esc(e.message)}</div>`;
  }
  btn.disabled = false;
}

function bindKb() {
  const ask = $('#kb-ask');
  if (ask) ask.addEventListener('click', askKb);
  const q = $('#kb-q');
  if (q) q.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') askKb(); });
  const sel = $('#kb-model');
  if (sel) sel.addEventListener('change', async () => {
    const st = $('#kb-status');
    const v = sel.value || '';
    try { localStorage.setItem('eg2-kbengine', v); } catch (e) { /* 忽略 */ }
    if (v.indexOf('api:') === 0) {          // 选了外部 API：不用加载本机模型
      const s = rmApi(Number(v.slice(4)));
      st.textContent = '回答将用外部 API：' + (s.name || '') + ' · ' + (s.model || '');
      return;
    }
    // ★ 2026-10-21 项目主："服务器本机模型默认用户可以使用，但不能编辑"
    //   切换模型 = 卸载旧模型/加载新模型，会影响服务器上所有人 → 只有管理员能。
    //   普通用户选「本机」就**直接用当前已加载的那个**，不去动服务端 ✓
    if (VIEW_MODE === 'member') {
      try { localStorage.setItem('eg2-kbengine', v); } catch (e) { /* 忽略 */ }
      st.textContent = '将使用服务器当前的本地模型 ✓'
        + '（切换/编辑模型需要管理员）';
      return;
    }
    st.textContent = '正在切换模型…';
    try {
      const r = await post('/api/chat/model', { path: sel.value });
      st.textContent = r.message
        + (r.vram_notes && r.vram_notes.length ? ' · ' + r.vram_notes.join('；') : '');
      setTimeout(loadKbStatus, 800);
    } catch (e) { st.textContent = '切换失败：' + e.message; }
  });
  loadChatModels();
  const un = $('#kb-unload');
  if (un) un.addEventListener('click', async () => {
    try { await post('/api/chat/unload', {}); loadKbStatus(); alert('已卸下，显存释放了'); }
    catch (e) { alert('失败：' + e.message); }
  });
}


/* ---------------- 标签页 ---------------- */
const TAB_KEY = 'eg2-tab';
const SET_GRP_KEY = 'eg2-set-grp';        // 设置页 4 类
// 老标签名 → 新标签名
//   2026-10-22 第一版：把「人物」「文件夹」合成一页（library）
//   2026-10-22 项目主又定：人物挪进「设置 → 人物命名」，主标签就叫「文件夹」
//   所以 people / library 都要能落到 folders 上，老书签和 localStorage 才不会白屏 ✓
const TAB_ALIAS = { people: 'folders', library: 'folders' };

/* 当前是不是某几个页里的一页（性能相关：页面上跑着十几个轮询，
   以前不管在哪个页都照跑，慢接口会把单线程服务堵死 → 卡片一直「读取中…」） */
function tabActive(names) {
  const on = document.querySelector('.tabpane.on');
  if (!on) return false;
  return String(names).split(',').indexOf(on.dataset.pane) >= 0;
}

/* 设置页的 4 类（顺序与命名照项目主定的）：
     1 服务器与共享 / 2 模型与系统设置 / 3 人物命名 / 4 备份与恢复
   卡片本体在 search.html 里（id 不变，JS 逻辑不用改），这里只按用途归位 ✓ */
const SET_GROUPS = {
  server: [
    ['h2', '服务器连接'],
    ['#push-card'],
    ['#mine-card'],
    ['#gift-card'],
    ['#priv-card'],
  ],
  model: [
    ['h2', '运行模式与设置'],
    ['h2', '显存控制'],
    ['h2', '外部模型'],
    ['#dify-card'],
    ['h2', '图片打标'],
    ['#cpq-card'],
  ],
  people: [
    ['#people-card'],
  ],
  backup: [
    ['#snap-card'],
    ['h2', '数据库备份'],
    ['#bk-card'],
  ],
};

function bindSettingsGroups() {
  const pane = document.querySelector('.tabpane[data-pane="settings"]');
  if (!pane) return;
  const boxes = {};
  pane.querySelectorAll('#set-groups .grp').forEach(function (g) { boxes[g.dataset.grp] = g; });
  if (!boxes.server) return;
  const used = new Set();
  Object.keys(SET_GROUPS).forEach(function (g) {
    const box = boxes[g];
    if (!box) return;
    SET_GROUPS[g].forEach(function (rule) {
      let card = null;
      if (rule[0].charAt(0) === '#') {
        card = pane.querySelector(rule[0]);
      } else {
        // 按标题文字找（标题里带数量/样式，用 includes 匹配）
        const list = pane.querySelectorAll('.card');
        for (let i = 0; i < list.length; i++) {
          const h = list[i].querySelector(':scope > h2');
          if (h && (h.textContent || '').indexOf(rule[1]) >= 0 && !used.has(list[i])) { card = list[i]; break; }
        }
      }
      if (card && !used.has(card)) { used.add(card); box.appendChild(card); }
    });
  });
  // 兜底：没被任何一组认领的卡片（以后新加的），放「模型与系统设置」，绝不消失 ✓
  pane.querySelectorAll('.card').forEach(function (c) {
    if (used.has(c) || c.closest('.grp')) return;
    if (c.id === 'set-groups') return;
    boxes.model.appendChild(c);
  });
  // 组内顺序按上面配置的顺序（appendChild 已经把顺序定了）
  let saved = 'server';
  try { saved = localStorage.getItem(SET_GRP_KEY) || 'server'; } catch (e) { /* 忽略 */ }
  if (!boxes[saved]) saved = 'server';
  applySettingsGroup(saved);
  const nav = document.getElementById('set-nav');
  if (nav && !nav.dataset.bound) {
    nav.dataset.bound = '1';
    nav.addEventListener('click', function (ev) {
      const b = ev.target.closest('button[data-grp]');
      if (b) applySettingsGroup(b.dataset.grp);
    });
  }
}

function applySettingsGroup(name) {
  const pane = document.querySelector('.tabpane[data-pane="settings"]');
  if (!pane) return;
  pane.querySelectorAll('#set-groups .grp').forEach(function (g) {
    g.classList.toggle('on', g.dataset.grp === name);
  });
  pane.querySelectorAll('#set-nav button').forEach(function (b) {
    b.classList.toggle('on', b.dataset.grp === name);
  });
  try { localStorage.setItem(SET_GRP_KEY, name); } catch (e) { /* 忽略 */ }
  // 「人物命名」这一天才有人脸数据要拉（/api/faces/status 要约 9 秒，别的时候不碰）
  if (name === 'people') loadPeoplePane();
  // 「模型与系统设置」：拉一次"你能用的模型"（含服务器公用/共享的）✓
  if (name === 'model' && typeof loadSrvApiModels === 'function') {
    loadSrvApiModels().then(function () {
      try { if (typeof renderApiList === 'function') renderApiList(); } catch (e) { /* 忽略 */ }
    }).catch(function () {});
  }
}

/* 设置页当前显示的是哪一组 */
function settingsGroupOn(name) {
  const on = document.querySelector('.tabpane[data-pane="settings"] #set-groups .grp.on');
  return !!on && on.dataset.grp === name;
}

/* 拉人脸统计 + 人物卡片 */
function loadPeoplePane() {
  if (typeof loadFaceStatus !== 'function') return;
  // 人物列表是一次性拉全量（3403 人 / 约 1.2 MB），实测 4 秒左右，先给一行占位
  const grid = document.getElementById('people-grid');
  if (grid && !grid.innerHTML.trim()) {
    grid.innerHTML = '<div class="empty">正在读取人物（约 4 秒）…</div>';
  }
  // 两个接口互不依赖，**并行**发。以前是串行（先等人脸统计 9 秒再拉人物），
  // 这一下就要 11 秒才出卡片；并行后 2~3 秒就能看到人 ✓
  loadFaceStatus().catch(function () {});
  if (typeof loadPeople === 'function') loadPeople().catch(function () {});
  if (!window.__peopleFocusBound) {
    window.__peopleFocusBound = true;
    const again = function () {
      if (document.hidden) return;
      if (tabActive('settings') && settingsGroupOn('people')) loadPeoplePane();
    };
    window.addEventListener('focus', again);
    document.addEventListener('visibilitychange', again);
  }
}

/* 人物 / 文件夹 二级切换 */
/* 切到某页时，把它自己的数据拉一次（省得离开时也在后台轮询） */
function refreshTabData(name) {
  const call = function (fn) { try { if (typeof fn === 'function') fn(); } catch (e) { /* 忽略 */ } };
  if (name === 'settings') {
    call(window.loadGifts); call(window.loadMine); call(window.loadSnapshots);
    call(window.loadPushState); call(window.loadCaptionQueue);
    call(window.loadGpu); call(window.loadVl); call(window.loadSyncServer);
    if (settingsGroupOn('people')) loadPeoplePane();     // 人物命名这一组才拉人脸
  }
  if (name === 'folders') {
    // 接口忙的时候要 2~5 秒，先给个占位，别让人以为「这一页是空的」
    const box = document.getElementById('folders');
    if (box && !box.innerHTML.trim()) box.innerHTML = '<div class="empty">正在读取文件夹…</div>';
    call(window.refreshFolders); call(window.refreshNightBar);
  }
  if (name === 'kb') call(window.loadKbStatus);
}

function selectTab(name) {
  name = TAB_ALIAS[name] || name;
  if (!document.querySelector('.tabpane[data-pane="' + name + '"]')) name = 'search';
  document.querySelectorAll('#tabbar button').forEach(function (b) {
    b.classList.toggle('on', b.dataset.tab === name);
  });
  document.querySelectorAll('.tabpane').forEach(function (p) {
    p.classList.toggle('on', p.dataset.pane === name);
  });
  try { localStorage.setItem(TAB_KEY, name); } catch (e) { /* 忽略 */ }
  // ⌗ 聊天页按视口定高，切过去时重量一次（2026-10-23）
  if (typeof window.__fitChatPane === 'function') {
    try { window.__fitChatPane(); } catch (e) { /* 忽略 */ }
  }
  if (name === 'kb' && typeof loadKbStatus === 'function') {
    loadKbStatus().catch(function () {});
  }
  refreshTabData(name);
}

function bindTabs() {
  document.querySelectorAll('#tabbar button').forEach(function (b) {
    b.addEventListener('click', function () { selectTab(b.dataset.tab); });
  });
  bindSettingsGroups();
  let saved = 'search';
  try { saved = localStorage.getItem(TAB_KEY) || 'search'; } catch (e) { /* 忽略 */ }
  selectTab(saved);
  // 恢复上次选的视图（列表 / 图片墙）
  try {
    const v = localStorage.getItem('eg2-view');
    if (v === 'grid' || v === 'list') window.__viewMode = v;
  } catch (e) { /* 忽略 */ }
}

/* ---------------- 搜索 ---------------- */
async function doSearch() {
  const q = $('#q').value.trim();
  const picked = [...faceState.picked];
  if (!q && !picked.length) { $('#results').innerHTML = ''; return; }
  $('#results').innerHTML = '<div class="card"><div class="empty">搜索中…</div></div>';
  const params = new URLSearchParams({ q, top_k: $('#topk').value });
  if (picked.length) {
    params.set('persons', picked.join(','));
    params.set('person_mode', $('#person-mode').value);
  }
  if ($('#folder-filter').value) params.set('folder_ids', $('#folder-filter').value);
  if ($('#ext-filter').value) params.set('exts', $('#ext-filter').value);
  try { render(await api('/api/search?' + params.toString())); }
  catch (e) {
    $('#results').innerHTML = `<div class="card"><div class="empty" style="color:var(--bad)">${esc(e.message)}</div></div>`;
  }
}
function terms(q) { return q.split(/\s+/).filter((t) => t.length >= 1 && !/^[.,，。、]+$/.test(t)); }
function highlight(text, q) {
  let html = esc(text);
  for (const t of terms(q).sort((a, b) => b.length - a.length)) {
    if (!t) continue;
    html = html.replace(new RegExp(t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'), (m) => '<mark>' + m + '</mark>');
  }
  return html;
}
function revealResults() {
  const el = $('#results');
  if (!el.firstChild) return;
  const rect = el.getBoundingClientRect();
  // 结果本来就在搜索框正下方；只有被滚到屏幕下方时才把它带进视野
  if (rect.top < 0 || rect.top > window.innerHeight * 0.55) {
    el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

function desktopButtons(r) {
  // ★ 扫描版 PDF：给一个"单份页码"入口（优先级最高，项目主 2026-10-19 要求）✓
  //   只有 PDF 才显示 —— 别的文件没有页码概念 ✓
  const isPdf = /\.pdf$/i.test(String(r.path || ''))
    || String(r.kind || '').toLowerCase() === 'pdf';
  const pgBtn = isPdf
    ? `<button class="ghost tiny" data-ocrpages="${r.file_id ?? r.id ?? ''}"
         data-ocrpath="${esc(r.path)}"
         title="只给**这一份** PDF 指定 OCR 页码范围（优先级高于全局设置）
例：这份 300 页手册只想做第 50~80 页 ✓">📄 页码</button>`
    : '';
  if (window.__desktopOk === false) {
    const tip = esc(window.__desktopWhy || '当前环境不允许打开资源管理器');
    return `${pgBtn}<button class="ghost tiny" disabled title="${tip}">定位文件</button>
            <button class="ghost tiny" disabled title="${tip}">用默认程序打开</button>`;
  }
  return `${pgBtn}<button class="ghost tiny" data-reveal="${esc(r.path)}">定位文件</button>
          <button class="ghost tiny" data-open="${esc(r.path)}">用默认程序打开</button>`;
}

/* ---------------- 📄 单份 PDF 的 OCR 页码范围 ---------------- */
async function openPdfPages(fileId, path) {
  let info = null;
  if (fileId) {
    try { info = await api(`/api/files/${fileId}/ocr-pages`); } catch (e) { info = null; }
  }
  const pf = info ? info.page_from : 0;
  const pt = info ? info.page_to : 0;
  const g = info ? `${info.global_from} ~ ${info.global_to === 0 ? '末页' : info.global_to}` : '';
  const ov = document.createElement('div');
  ov.className = 'modal show';
  ov.innerHTML = `<div class="box" style="max-width:620px">
    <div class="head"><b>📄 单份 PDF 的 OCR 页码范围</b><span style="flex:1"></span>
      <button class="ghost tiny" id="pp-x">关闭</button></div>
    <div class="body" style="display:grid;gap:12px">
      <div style="word-break:break-all;font-size:12.5px;opacity:.85">${esc(path || (info && info.path) || '')}</div>
      <div class="hint">
        <b>这个设置优先级最高</b> —— 全局是"全部做"时，这里填 50~80 就只做这 31 页 ✓<br>
        留空/填 0~0 = 清除，恢复跟随全局（当前全局：${esc(g)}）✓
      </div>
      <div>
        <b>页码范围</b>
        <input type="number" id="pp-from" min="0" value="${pf}" style="width:90px;margin-left:8px">
        <span style="margin:0 4px">~</span>
        <input type="number" id="pp-to" min="0" value="${pt}" style="width:90px">
        <div class="hint" style="margin-top:4px">结束页 0 = 到最后一页；只填结束页 = 从头做到那儿 ✓</div>
      </div>
      ${info ? `<div class="hint">当前进度：已 OCR 到第 ${info.ocr_pages} 页 · 来源：<b>${esc(info.source)}</b></div>` : ''}
    </div>
    <div class="foot"><span class="hint" id="pp-msg"></span><span style="flex:1"></span>
      <button class="ghost" id="pp-save">保存</button></div></div>`;
  document.body.appendChild(ov);
  const close = () => ov.remove();
  ov.onclick = (ev) => { if (ev.target === ov) close(); };
  ov.querySelector('#pp-x').onclick = close;
  ov.querySelector('#pp-save').onclick = async () => {
    const msg = ov.querySelector('#pp-msg');
    if (!fileId) { msg.textContent = '这条结果没带文件 id，去「文件夹」里搜同一份试试'; return; }
    msg.textContent = '保存中…';
    try {
      const r = await api(`/api/files/${fileId}/ocr-pages`, 'PUT', {
        page_from: Number(ov.querySelector('#pp-from').value) || 0,
        page_to: Number(ov.querySelector('#pp-to').value) || 0,
      });
      msg.innerHTML = `<b style="color:#4caf50">已保存 ✓</b> ${esc(r.message || '')}`;
      setTimeout(close, 1600);
    } catch (e) {
      msg.textContent = '保存失败：' + e.message;
    }
  };
}

function render(data) {
  window.__results = data.results || [];
  window.__lastData = data;          // 切换列表/图片墙时要重新渲染，得留着
  $('#search-info').textContent = `命中 ${data.count} 个文件，用时 ${data.elapsed} 秒`;
  if (!data.results.length) {
    $('#results').innerHTML = '<div class="card"><div class="empty">没有找到匹配的文件。<br>' +
      '可以换个说法，或者检查该文件夹是否已经建立索引。</div></div>';
    revealResults();
    return;
  }
  // ---- 图片墙视图：只把图片类结果铺成缩略图墙 ----
  const view = window.__viewMode || (window.__viewMode = 'list');
  const toggle = `<button class="ghost tiny" id="view-toggle" style="float:right">`
    + (view === 'grid' ? '切换到列表' : '切换到图片墙') + '</button>';
  if (view === 'grid') {
    const imgs = data.results.filter((r) => r.kind === 'image');
    const others = data.results.length - imgs.length;
    $('#results').innerHTML = `<div class="card"><h2>搜索结果${toggle}</h2>`
      + (imgs.length
          ? `<div class="imgrid">${imgs.map((r, i) => `
              <div class="imgitem" data-preview="${r.id}" title="${esc(r.path)}">
                <img src="/api/preview/${r.id}/image?w=360" loading="lazy" alt="">
                <div class="imginfo">
                  <b>${r.score.toFixed(2)}</b>
                  <span>${highlight(r.name, data.query)}</span>
                </div>
                ${chanBadges(r.channels)}
              </div>`).join('')}</div>`
            + (others ? `<div class="hint" style="margin-top:10px">`
                + `另有 ${others} 个非图片结果，切回列表可以看</div>` : '')
          : '<div class="empty">这批结果里没有图片。<br>'
            + '切回列表看文字/文档结果。</div>')
      + '</div>';
    revealResults();
    return;
  }
  // 图片结果直接在列表里带缩略图 —— 不用切视图也能一眼看到画面。
  // 非图片结果保持原来的文字排版。
  $('#results').innerHTML = '<div class="card"><h2>搜索结果' + toggle + '</h2>'
    + data.results.map((r, i) => `
    <div class="result${r.kind === 'image' ? ' withthumb' : ''}">
      ${r.kind === 'image' ? `<img class="rthumb" loading="lazy" alt=""
          src="/api/preview/${r.id}/image?w=260"
          data-preview="${r.id}" title="点击预览">` : ''}
      <div class="rbody">
        <div class="top">
          <span class="score">${r.score.toFixed(3)}</span>
          <span class="name" data-preview="${r.id}" title="点击预览">${i + 1}. ${highlight(r.name, data.query)}</span>
          <span class="hint">${KIND[r.kind] || r.kind || ''} · ${fmtSize(r.size)} · ${fmtTime(r.mtime)}</span>
          ${r.folder_mode ? `<span class="badge mode">${esc(r.folder_mode)}</span>` : ''}
          ${chanBadges(r.channels)}
        </div>
        <div class="dir">${highlight(r.dir, data.query)}</div>
        ${r.snippet ? `<div class="snippet">${highlight(r.snippet.slice(0, 500), data.query)}</div>` : ''}
        ${r.note ? `<div class="hint" style="margin-top:6px">备注：${esc(r.note)}</div>` : ''}
        <div class="acts">
          <button class="ghost tiny" data-preview="${r.id}">预览</button>
          ${desktopButtons(r)}
          <button class="ghost tiny" data-copy="${esc(r.path)}">复制路径</button>
        </div>
      </div>
    </div>`).join('') + '</div>';
  revealResults();
}

let toastTimer = null;
function toast(text, bad) {
  const el = $('#toast');
  el.textContent = text;
  el.style.borderColor = bad ? 'var(--bad)' : 'var(--accent)';
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2600);
}

// 列表 / 图片墙 切换（事件委托，渲染后不用重新绑定）
document.addEventListener('click', (ev) => {
  if (!ev.target.closest('#view-toggle')) return;
  window.__viewMode = (window.__viewMode === 'grid') ? 'list' : 'grid';
  try { localStorage.setItem('eg2-view', window.__viewMode); } catch (e) {}
  if (window.__lastData) render(window.__lastData);
}, true);

document.addEventListener('click', async (ev) => {
  const pv = ev.target.closest('[data-preview]');
  const open = ev.target.closest('[data-open]');
  const reveal = ev.target.closest('[data-reveal]');
  const copy = ev.target.closest('[data-copy]');
  const ocrpg = ev.target.closest('[data-ocrpages]');
  try {
    if (ocrpg) {
      // 📄 单份 PDF 的 OCR 页码（优先级最高）✓
      const fid = ocrpg.dataset.ocrpages;
      await openPdfPages(fid ? Number(fid) : 0, ocrpg.dataset.ocrpath || '');
      return;
    }
    if (pv) {
      const item = (window.__results || []).find((x) => String(x.id) === pv.dataset.preview);
      if (item) openPreview(item);
      return;
    }
    if (open) {
      const r = await post('/api/open', { path: open.dataset.open });
      toast(r.message || '已打开');
    }
    if (reveal) {
      const r = await post('/api/open', { path: reveal.dataset.reveal, reveal: true });
      toast(r.message || '已定位');
    }
    if (copy) {
      await navigator.clipboard.writeText(copy.dataset.copy);
      toast('路径已复制');
    }
  } catch (e) { toast(e.message, true); }
});

/* ---------------- 预览 ---------------- */
window.pvMediaError = (el) => {
  const box = el.closest('.pv-center');
  if (box) {
    box.innerHTML = '<div class="empty" style="padding:26px 10px">' +
      '<div style="color:var(--warn);margin-bottom:8px">⚠ 这个文件无法在网页里播放</div>' +
      '可能是格式不受浏览器支持，或者文件还没同步完。<br>可以试试「用默认程序打开」。</div>';
  }
};

let pvFile = null;
const INLINE_KINDS = ['image', 'audio', 'video', 'pdf'];

/* 预览时要拿「用户这次用的查询词」：搜索页是 #q，知识库页是 #kb-q。
   以前只读 #q —— 在知识库提问、再从「来源」点开 PDF 时读到的是空串，
   于是完全不定位（模式栏提示停在默认文字、也不跳页），用户看到的就是「没反应」。

   2026-10-16 再修一层：知识库页读到的 #kb-q 是**整个问题**（「李四是谁」），
   拿整句去 PDF 里找字面文字当然找不到 ✗（界面只会提示"没有字面文字"）。
   现在从「来源」点开时，会用后端给这条来源算好的 **hit_terms**（片段里真的出现的词，
   长的优先）当定位词 ✓ —— 这才是"知识库知道出处"背后的那份信息。 */
let previewTermOverride = '';

    // ---- 界面心跳 / 关掉界面就停服务（桌面版）----
// 服务端靠"心跳还在不在"判断界面是否还开着 → 界面关了 35 秒后自动停服务 ✓
// 为什么不用"关闭时发一次通知"当主判据：实测**不可靠** ✗
//   关窗瞬间还有别的请求漏进来 → 被判成"刷新" → 服务照跑、黑框还在 ✗
// 只认这一条专用心跳，其它请求干扰不了 ✓；界面崩溃/被强杀也能识别 ✓
// 服务器上不发（desktopFlag=false）✓ —— 服务器是长期服务，不能自己停 ✗
//
// ⚠ 2026-10-17 踩坑：这段原来定义在**别的函数内部** ✗，
//    而调用它的 check() 在几千行外的另一个函数里 → 根本看不到 ✗
//    → 前端一直没发心跳，"关掉界面停服务"自然不生效 ✗。
//    现在挂到 window 上，任何地方都能调 ✓。
window.eg2StartHeartbeat = function () {
  if (window.__eg2Heartbeat) return;
  var beat = function () {
    if (window.__eg2DesktopMode !== true) return;
    fetch('/api/ui-alive', { method: 'POST', keepalive: true,
                             credentials: 'same-origin' }).catch(function () {});
  };
  beat();
  window.__eg2Heartbeat = setInterval(beat, 10000);
};
window.eg2SetDesktop = function (isDesktop) {
  window.__eg2DesktopMode = !!isDesktop;
  if (isDesktop) window.eg2StartHeartbeat();      // 确认桌面版后立刻开始 ✓
};
// 关闭界面时顺手通知服务端（快速通道；主判据还是心跳 ✓）
window.addEventListener('pagehide', function () {
  if (window.__eg2DesktopMode !== true) return;   // 未知/服务器 → 不发，避免误停 ✗
  try {
    if (navigator.sendBeacon) {
      navigator.sendBeacon('/api/ui-leaving', new Blob(['1']));
    } else {
      fetch('/api/ui-leaving', { method: 'POST', keepalive: true,
                                 credentials: 'same-origin' }).catch(function () {});
    }
  } catch (e) { /* 通知不到没关系 —— 心跳断了服务端也会停 ✓ */ }
});

function previewQuery() {
  if (previewTermOverride) return previewTermOverride;
  const active = document.querySelector('.tabpane.on');
  if (active) {
    const el = active.querySelector('#kb-q, #q');
    if (el && (el.value || '').trim()) return el.value.trim();
  }
  for (const sel of ['#kb-q', '#q']) {
    const el = document.querySelector(sel);
    if (el && (el.value || '').trim()) return el.value.trim();
  }
  return '';
}

/* PDF 预览的模式条。
   原来 pdf 直接塞一个 <iframe> 交给浏览器内置查看器 —— 版式是原样的，
   但答不出「搜索命中的那句话在第几页」，查看器打不开时也没退路。
   所以这里给三种看点，默认仍是内置查看器（最接近原文件、可缩放可搜索）：
     · 内置查看器 —— 原版式
     · 按页文字   —— 标页码，命中关键词直接跳到那一页
     · 看原版式   —— 把那一页渲染成图片（需要 PyMuPDF），查看器打不开时用 */
function pdfModeBar() {
  return `<div class="hint" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px">
      <button class="ghost tiny" id="pvpdf-mode-viewer" style="color:var(--accent)">内置查看器</button>
      <button class="ghost tiny" id="pvpdf-mode-text">按页文字（标页码）</button>
      <span style="flex:1"></span>
      <span id="pvpdf-hint">想按页码定位、或查看器打不开时，用「按页文字」</span>
    </div>`;
}

function bindPdfModeBar(item) {
  const box = document.getElementById('pv-pdf-box');
  const viewerBtn = document.getElementById('pvpdf-mode-viewer');
  const textBtn = document.getElementById('pvpdf-mode-text');
  const hint = document.getElementById('pvpdf-hint');
  if (!box || !viewerBtn || !textBtn) return;
  const q = previewQuery();
  const base = `/api/preview/${item.id}`;
  let hitPage = 0;                       // 字面命中的页；找到了就让查看器直接跳过去
  // 内置查看器：打开就整页显示（看全貌）；命中时顺带跳到那一页。
  // 换 src 一定要带上不同的 query：同一个地址只改 #片段，浏览器不会重新加载。
  const viewerSrc = () => (hitPage
    ? `${base}?hit=1#page=${hitPage}&zoom=page-fit`
    : `${base}#zoom=page-fit`);
  const iframeFor = () => `<iframe class="pv-frame" src="${viewerSrc()}"></iframe>`;
  const mark = (on, off) => {
    on.style.color = 'var(--accent)';
    off.style.color = '';
  };
  // 带着查询词进来时，先别急着渲染查看器 —— 定位就几百毫秒，
  // 免得查看器闪一下再被「看原版式」顶掉。
  box.innerHTML = q ? '<div class="empty">正在定位命中页…</div>' : iframeFor();

  if (q) {
    api(`${base}/pdf-hits?q=${encodeURIComponent(q)}`).then(async (h) => {
      // 后端会把"真正用来定位的词"回给我们（used_term）——
      // 它可能不是我们传进去的那串：传整句「李四是谁」时它会拆成「李四」✓
      const used = (h && (h.used_term || h.query)) || q;
      if (!h || !h.first_page) {
        box.innerHTML = iframeFor();               // 没有字面命中 → 普通查看器
        mark(viewerBtn, textBtn);
        if (hint) {
          const tried = (h && h.tried_terms || []).slice(0, 4);
          hint.textContent = h && h.asked
            ? `页面上没有「${h.asked}」的字面文字（这条是语义命中）`
              + (tried.length > 1 ? `；也试过 ${tried.slice(1).map((t) => `「${t}」`).join('、')}` : '')
              + '。可用「按页文字」看相关的页'
            : '没法定位（读不了 PDF 文字层），可用「按页文字」按页翻';
        }
        return;
      }
      hitPage = h.first_page;
      if (hint) {
        hint.textContent = `命中「${used}」在第 ${hitPage} 页`
          + (h.asked && h.asked !== used ? `（你问的是「${h.asked}」，用的是其中命中的词）` : '')
          + (h.hit_pages.length > 1 ? `（共 ${h.hit_pages.length} 页有命中）` : '')
          + ' · 黄框标出的就是命中位置；想看 PDF 原文点「内置查看器」';
      }
      // 直接切到「看原版式」：整页显示 + 用颜色把命中位置标出来
      // 高亮要按**定位用的词**上色（用整句会把整页都标不上 ✗）
      const ok = await loadPdfPreview(item.id, used, box, h);
      if (ok) mark(textBtn, viewerBtn);
      else { box.innerHTML = iframeFor(); mark(viewerBtn, textBtn); }
    }).catch(() => {
      box.innerHTML = iframeFor();
      mark(viewerBtn, textBtn);
    });
  } else if (hint) {
    hint.textContent = '没有查询词，没法定位到具体页 ——'
      + '先在搜索框或知识库提问框里搜一个词，再打开这个文件';
  }

  viewerBtn.addEventListener('click', () => {
    box.innerHTML = iframeFor();
    mark(viewerBtn, textBtn);
  });
  textBtn.addEventListener('click', async () => {
    const ok = await loadPdfPreview(item.id, q, box, null, 'text');   // 显式选文字，别被默认顶回图片
    if (!ok) {                                  // 按页读不了就退回查看器
      box.innerHTML = iframeFor();
      toast('按页读这个 PDF 失败了，已退回内置查看器');
      return;
    }
    mark(textBtn, viewerBtn);
  });
}

async function openPreview(item) {
  try { addOpenFileBtn(item); } catch (e) { /* 忽略 */ }
  pvFile = item;
  $('#pv-title').textContent = item.name;
  $('#pv-path').textContent = item.path;
  $('#pv-msg').textContent = `${KIND[item.kind] || item.kind} · ${fmtSize(item.size)} · ${fmtTime(item.mtime)}`;
  const url = '/api/preview/' + item.id;
  // PDF 要按**扩展名**也认一次：从知识库「来源」列表点进来的 item.kind 是 'other'
  // （见 kb 那里构造 item 的地方），只按 kind 判断就会绕过 PDF 预览这条路 ——
  // 表现为没有模式栏、也不会去定位命中页。
  const isPdf = item.kind === 'pdf' || /\.pdf$/i.test(item.path || item.name || '');

  let html;
  if (item.kind === 'image') {
    html = '<div class="pv-center"><span class="hint">正在加载图片…</span></div>';
  } else if (item.kind === 'audio') {
    html = `<div class="pv-center"><audio controls autoplay src="${url}"
      onerror="pvMediaError(this)"></audio></div>`;
  } else if (item.kind === 'video') {
    html = `<div class="pv-center"><video controls controlsList="nodownload" src="${url}"
      onerror="pvMediaError(this)"></video></div>`;
  } else if (isPdf) {
    html = pdfModeBar()
      + `<div id="pv-pdf-box"><iframe class="pv-frame" src="${url}"></iframe></div>`;
  } else {
    html = '<div class="empty">正在读取正文…</div>';
  }
  $('#pv-body').innerHTML = html;
  const dead = window.__desktopOk === false;
  $('#pv-reveal').disabled = dead;
  $('#pv-open').disabled = dead;
  $('#pv-reveal').title = dead ? (window.__desktopWhy || '') : '';
  $('#pv-open').title = dead ? (window.__desktopWhy || '') : '';
  $('#pv-modal').classList.add('show');

  if (item.kind === 'image') loadPreviewImage(item);
  else if (isPdf) bindPdfModeBar(item);
  else if (!INLINE_KINDS.includes(item.kind)) await loadPreviewText(item.id);
}

function loadPreviewImage(item) {
  // 图片统一由服务端解码后输出：既能把 HEIC/TIFF 转成能显示的格式，
  // 也能在「文件其实不是图片」时给出原因，而不是留一个空白框
  const box = $('#pv-body .pv-center');
  const url = `/api/preview/${item.id}/image?w=1600`;
  const img = new Image();
  img.className = 'pv-img';
  img.alt = item.name;
  img.onload = () => {
    if (!box) return;
    box.innerHTML = '';
    box.appendChild(img);
    $('#pv-msg').textContent += ` · 显示为 ${img.naturalWidth}×${img.naturalHeight}`;
  };
  img.onerror = async () => {
    let why = '这个文件显示不了';
    try {
      const res = await fetch(url);
      const data = await res.json().catch(() => null);
      if (data && data.detail) why = data.detail;
    } catch (e) { /* 忽略，用默认文案 */ }
    if (box) {
      box.innerHTML = `<div class="empty" style="padding:26px 10px">
        <div style="color:var(--warn);margin-bottom:8px">⚠ ${esc(why)}</div>
        该文件仍然能被搜到（按文件名），但无法在网页里显示。<br>
        可以试试下面的「用默认程序打开」。</div>`;
    }
  };
  img.src = url;
}

/* 把 ../a/b.jpg 这种相对路径拼成绝对路径（自己处理 .. ，不依赖浏览器）。
   分隔符必须**跟着传入路径的风格走**：服务器上的库是 POSIX 路径（/home/lingxi/...），
   本机是 Windows 路径（D:\...）。以前把分隔符写死成 '\\'，在服务器上就会拼出
   ".../软件与效率\_resources\x.jpg" 这种四不像 —— 后端认不出，直接 403，
   表现为「打开预览后图片全加载不出来、控制台一条 /api/raw 403」。 */
function absJoin(dir, rel) {
  const base = String(dir || '');
  const sep = base.indexOf('\\') >= 0 ? '\\' : '/';
  const p = String(rel || '').replace(/[\\/]+/g, sep);
  if (/^[a-zA-Z]:/.test(p)) return p;              // D:\... 已经是绝对路径
  if (p.charAt(0) === sep || p.charAt(0) === '/') return p;   // /home/... 同理
  const parts = (base + sep + p).split(sep);
  const out = [];
  for (const seg of parts) {
    if (!seg || seg === '.') continue;
    if (seg === '..') out.pop();
    else out.push(seg);
  }
  return (sep === '/' ? '/' : '') + out.join(sep);
}

 /* 给预览加一个「打开原文件」按钮。
   PDF/Excel 提取出来的只是文字，看不到原表格版式，
   所以给个按钮直接用系统默认程序打开。 */
function addOpenFileBtn(obj) {
  const head = document.querySelector('#preview .pvhead')
    || document.querySelector('#preview h3')
    || document.querySelector('#preview');
  if (!head) return;
  const old = document.getElementById('pv-open-file');
  if (old) old.remove();
  if (!obj || !obj.path) return;
  const b = document.createElement('button');
  b.id = 'pv-open-file';
  b.className = 'ghost tiny';
  b.style.cssText = 'float:right;margin-left:8px';
  b.textContent = '用系统程序打开';
  b.addEventListener('click', async () => {
    try {
      await post('/api/open-file', { path: obj.path });
    } catch (e) {
      alert('打不开：' + e.message + '\n\n路径：' + obj.path);
    }
  });
  head.appendChild(b);
}

/* ---------------- PDF：按页看 + 看原版式 ----------------
   索引里存的是「各页文字用换行拼起来」的一整段，页码在抽取阶段就丢了，
   表格的版式（合并单元格、对齐）更不在文字层里 —— 所以只看整段文字
   既答不出「这句话在第几页」，也永远看不到表格样式。
   这里改成预览时现读一遍：
     · 文字按页给，命中关键词直接跳到那一页，页眉标「第 N 页」
     · 装了 PyMuPDF 就能把那一页渲染成图片，看真正的版式
   不用重新索引，也不动数据库。 */
let pvPdf = null;              // { pages, total_pages, render, note, hits, first_page, query }
let pvPdfMode = 'text';        // text = 按页文字，image = 看原版式
let pvPdfFit = true;           // true = 整页（看全貌），false = 适宽（看清小字）

async function loadPdfPreview(id, q, box, presetHits, mode) {
  const body = box || $('#pv-body');
  q = q || previewQuery();            // 没传就问当前页的输入框（搜索页 #q / 知识库页 #kb-q）
  body.innerHTML = '<div class="empty">正在按页读这个 PDF…</div>';
  let data;
  try {
    data = await api(`/api/preview/${id}/pdf-pages`);
  } catch (e) {
    return false;                                  // 读不了就退回内置查看器
  }
  data.hits = {};
  data.hit_pages = [];
  data.first_page = null;
  data.query = '';
  const applyHits = (h) => {
    if (!h) return;
    data.hits = h.hits || {};
    data.hit_pages = h.hit_pages || [];
    data.first_page = h.first_page || null;
    data.query = q;
  };
  if (presetHits) {
    applyHits(presetHits);                         // 调用方已经查过定位，别重复查一遍
  } else if (q) {
    // 按字面在页面里定位（人名、编号这类最准）。失败不影响看文字。
    try {
      applyHits(await api(`/api/preview/${id}/pdf-hits?q=${encodeURIComponent(q)}`));
    } catch (e) { /* 忽略：没有 PyMuPDF 或读不了时就不定位 */ }
  }
  pvPdf = data;
  try { pvPdfMode = localStorage.getItem('eg2-pdf-view') || 'text'; } catch (e) { pvPdfMode = 'text'; }
  try { pvPdfFit = localStorage.getItem('eg2-pdf-fit') !== '0'; } catch (e) { pvPdfFit = true; }
  if (!data.render) pvPdfMode = 'text';            // 没装 PyMuPDF 就只能看文字
  // 有字面命中时默认直接看「原版式」：只有这个视图能把命中位置用颜色标在页面上，
  // 而且整页缩放显示，一眼看到全貌。想用内置查看器/纯文字都各点一下就行。
  if (data.render && data.first_page) pvPdfMode = 'image';
  if (mode) pvPdfMode = (mode === 'image' && !data.render) ? 'text' : mode;  // 用户显式选的优先
  let page = 1;
  if (data.first_page) {
    page = data.first_page;                        // 字面命中 → 直接跳那一页
  } else if (q) {
    const hit = (data.pages || []).find((p) => p.text && p.text.indexOf(q) >= 0);
    if (hit) page = hit.page;                      // 没有字面命中，退回到「哪页文字里有它」
  }
  showPdfPage(page, q, body);
  return true;
}

function showPdfPage(page, q, box) {
  if (!pvPdf) return;
  const body = box || $('#pv-body');
  const total = pvPdf.total_pages || 1;
  page = Math.min(Math.max(1, page), total);
  const cur = (pvPdf.pages || []).find((p) => p.page === page) || { text: '', chars: 0 };
  const note = pvPdf.render ? ''
    : `<div class="hint" style="margin-bottom:6px">${esc(pvPdf.note || '')}`
      + `（双击项目里的「安装PDF原版式.bat」装好后刷新页面即可）</div>`;
  // 「在哪一页、本页哪几处」——这是用户最想要的定位信息
  const hits = pvPdf.hits || {};
  const hitPages = pvPdf.hit_pages || [];
  const pageHits = hits[String(page)] || [];
  let hitNote = '';
  if (hitPages.length) {
    hitNote = pageHits.length
      ? `本页有 ${pageHits.length} 处「${esc(pvPdf.query)}」`
      : `「${esc(pvPdf.query)}」在第 ${hitPages.slice(0, 6).join('、')} 页`;
  } else if (pvPdf.query) {
    hitNote = `页面上没有「${esc(pvPdf.query)}」的字面文字（这条是语义命中）`;
  }
  const jump = (hitPages.length && page !== hitPages[0])
    ? `<button class="ghost tiny" id="pvpdf-hitjump">跳到命中页（第 ${hitPages[0]} 页）</button>` : '';
  const head = `<div class="hint" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px">
      <button class="ghost tiny" id="pvpdf-prev"${page <= 1 ? ' disabled' : ''}>上一页</button>
      <span>第 <b>${page}</b> / ${total} 页</span>
      <button class="ghost tiny" id="pvpdf-next"${page >= total ? ' disabled' : ''}>下一页</button>
      ${jump}
      <span style="flex:1"></span>
      <button class="ghost tiny" id="pvpdf-text"${pvPdfMode === 'text' ? ' style="color:var(--accent)"' : ''}>文字</button>
      <button class="ghost tiny" id="pvpdf-img"${pvPdfMode === 'image' ? ' style="color:var(--accent)"' : ''}${pvPdf.render ? '' : ' disabled'} title="把这一页渲染成图片，表格版式都看得见">看原版式</button>
    </div>`
    + (hitNote ? `<div class="hint" style="margin:-4px 0 8px">${hitNote}</div>` : '');
  let content;
  if (pvPdfMode === 'image') {
    // 高亮框：服务端给的是 0~1 比例坐标，按百分比贴上去，图片怎么缩放都对得准
    const boxes = pageHits.map((r) => `<span class="pdfhit" style="`
      + `left:${(r[0] * 100).toFixed(3)}%;top:${(r[1] * 100).toFixed(3)}%;`
      + `width:${((r[2] - r[0]) * 100).toFixed(3)}%;height:${((r[3] - r[1]) * 100).toFixed(3)}%`
      + `"></span>`).join('');
    content = `<div style="text-align:center">
        <div class="pdfpage${pvPdfFit ? '' : ' wide'}" id="pvpdf-page"
             title="点一下切换：整页看全貌 / 适宽看清小字">
          <img src="/api/preview/${pvFile.id}/pdf-page?page=${page}&w=1400"
            alt="第 ${page} 页"
            onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'hint',textContent:'这一页渲染不出来：多半是还没装 PyMuPDF，双击「安装PDF原版式.bat」后刷新重试'}))">
          ${boxes}
        </div>
      </div>`
      + `<div class="hint" style="margin-top:6px">`
      + `${pvPdfFit ? '整页视图（看全貌）' : '适宽视图（看清小字）'}`
      + `${pageHits.length ? ' · 黄色框 = 搜索命中的位置' : ''}`
      + ` · 点图片可切换缩放</div>`;
  } else {
    const text = (cur.text || '').trim();
    content = text
      ? `<pre class="pv-text">${highlight(text, q)}</pre>`
      : '<div class="empty">这一页没有文字层（多半是扫描图）。'
        + (pvPdf.render ? '点右上角「看原版式」看原图。' : '') + '</div>';
  }
  body.innerHTML = note + head + content;
  const bind = (sel, fn) => { const el = body.querySelector(sel); if (el) el.addEventListener('click', fn); };
  bind('#pvpdf-prev', () => showPdfPage(page - 1, q, body));
  bind('#pvpdf-next', () => showPdfPage(page + 1, q, body));
  bind('#pvpdf-hitjump', () => showPdfPage(hitPages[0], q, body));
  bind('#pvpdf-page', () => {
    pvPdfFit = !pvPdfFit;
    try { localStorage.setItem('eg2-pdf-fit', pvPdfFit ? '1' : '0'); } catch (e) { /* 忽略 */ }
    showPdfPage(page, q, body);
  });
  bind('#pvpdf-text', () => {
    pvPdfMode = 'text';
    try { localStorage.setItem('eg2-pdf-view', 'text'); } catch (e) { /* 忽略 */ }
    showPdfPage(page, q, body);
  });
  bind('#pvpdf-img', () => {
    pvPdfMode = 'image';
    try { localStorage.setItem('eg2-pdf-view', 'image'); } catch (e) { /* 忽略 */ }
    showPdfPage(page, q, body);
  });
  const mark = body.querySelector('mark');
  if (mark) mark.scrollIntoView({ block: 'center' });
}

async function loadPreviewText(id) {
  const q = previewQuery();
  // PDF 走「按页 + 可出图」那条路；走不通再退回下面的整段文字
  if (pvFile && /\.pdf$/i.test(pvFile.path || '')) {
    if (await loadPdfPreview(id, q)) return;
  }
  try {
    const r = await api(`/api/preview/${id}/text`);
    const text = (r.text || '').trim();
    if (!text) {
      $('#pv-body').innerHTML = '<div class="empty">这个文件没有可显示的正文' +
        (r.kind === 'other' ? '（该格式不解析正文，只能按文件名检索）' : '（内容为空）') +
        '。<br>可以用下面的「用默认程序打开」查看。</div>';
      return;
    }
    const shown = text.slice(0, 60000);
    const note = r.truncated
      ? `<div class="hint" style="margin-bottom:8px">只显示前面部分，全文共 ${r.total_chars.toLocaleString()} 字</div>`
      : '';
    // 剪藏文章的插图文件名是哈希，文字搜不到，只能靠正文里的相对路径引用。
    // 这里把 ![](../_resources/x.jpg) 解析成绝对路径，走 /api/raw 取出来显示。
    const isMd = /\.(md|markdown|txt)$/i.test((pvFile && pvFile.path) || '');
    if (isMd && shown.includes('![')) {
      const dir = String(pvFile.path).replace(/[\\/][^\\/]*$/, '');
      const segs = shown.split(/(!\[[^\]]*\]\([^)\n]+\))/g);
      const html = segs.map((seg) => {
        const m = seg.match(/^!\[[^\]]*\]\(([^)\n]+)\)$/);
        if (!m) return highlight(seg, q);
        const rel = m[1].replace(/^<|>$/g, '').trim();
        if (/^https?:/i.test(rel)) {
          return `<img class="pvimg" loading="lazy" src="${esc(rel)}">`;
        }
        const abs = absJoin(dir, rel);
        // ⚠ 2026-10-22 项目主在控制台看到成串的
        //   「Failed to load resource: 404」+「Uncaught SyntaxError: Unexpected end of input」，
        //   根子在这里：以前把文件名用 JSON.stringify(rel) 拼进 onerror 属性，
        //   而 JSON 出来的是**双引号**字符串 —— 属性本身也是双引号包的，
        //   于是属性在第一个 " 处就被截断，剩下的 handler 成了残句 → 语法错误 ✗
        //   图一旦取不到（404）就触发一次，所以一次预览能刷出一屏。
        //   改成用 this.title（已经 esc 过）取值，彻底不拼引号 ✓
        return `<img class="pvimg" loading="lazy"
                     src="/api/raw?path=${encodeURIComponent(abs)}&w=900"
                     title="${esc(rel)}"
                     onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'hint',textContent:'（图片取不到：'+this.title+'）'}))">`;
      }).join('');
      $('#pv-body').innerHTML = note + `<div class="pv-text">${html}</div>`;
    } else {
      $('#pv-body').innerHTML = note + `<pre class="pv-text">${highlight(shown, q)}</pre>`;
    }
    const mark = $('#pv-body').querySelector('mark');
    if (mark) mark.scrollIntoView({ block: 'center' });
  } catch (e) {
    $('#pv-body').innerHTML = `<div class="empty" style="color:var(--bad)">${esc(e.message)}</div>`;
  }
}

// 关预览时把"来源带来的定位词"清掉 ✓ ——
// 否则下一次从搜索结果打开文件时，会继续用上一条来源的关键词去定位 ✗
function hidePreview() {
  $('#pv-modal').classList.remove('show');
  previewTermOverride = '';
}
$('#pv-close').onclick = () => hidePreview();
$('#pv-modal').onclick = (ev) => { if (ev.target.id === 'pv-modal') hidePreview(); };
$('#pv-reveal').onclick = async () => {
  if (!pvFile) return;
  try {
    const r = await post('/api/open', { path: pvFile.path, reveal: true });
    const soft = (r.message || '').includes('没有桌面权限');
    toast(r.message, soft);
  } catch (e) {
    // 服务端会把「为什么打不开 + 怎么办」放在 detail 里
    toast(e.message, true);
  }
};
$('#pv-download').onclick = () => {
  if (!pvFile) return;
  const a = document.createElement('a');
  a.href = `/api/preview/${pvFile.id}?download=1`;
  a.download = pvFile.name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  toast('已开始下载到「下载」文件夹');
};
$('#pv-open').onclick = async () => {
  if (!pvFile) return;
  try { toast((await post('/api/open', { path: pvFile.path })).message); }
  catch (e) { toast(e.message, true); }
};
$('#pv-copy').onclick = async () => {
  if (!pvFile) return;
  try { await navigator.clipboard.writeText(pvFile.path); toast('路径已复制'); }
  catch (e) { toast('复制失败：' + e.message, true); }
};
document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape') {
    $('#pv-modal').classList.remove('show');
    $('#cfg-modal').classList.remove('show');
    $('#modal').classList.remove('show');
  }
});

/* ---------------- 多选目录 ---------------- */
function modeOptions(includeCustom) {
  return (FOLDER_MODES || []).filter((m) => includeCustom || m.id !== 'custom')
    .map((m) => `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join('');
}

/* ---- 用途类别（项目主 2026-10-17：几个默认定义值，省得不知用什么方式扫描）----
   · 类别 = 拿这个文件夹**做什么**；每个类别自带一套**经济**的默认扫描口径 ✓
   · 选完类别，下面「索引模式」默认留空 = 用该用途的推荐口径 ✓；想微调再动它 ✓ */
function catList() {
  const resp = window.__foldersResp;
  return (resp && resp.categories) || [];
}

function currentPickCat() {
  const resp = window.__foldersResp;
  const fallback = (resp && resp.default_category) || 'find';
  const cats = catList();
  if (window.__pickCat && cats.some((c) => c.id === window.__pickCat)) return window.__pickCat;
  return cats.some((c) => c.id === fallback) ? fallback : (cats[0] || {}).id;
}

function renderPickCats() {
  const cats = catList();
  const box = $('#pick-cats');
  if (!box) return;
  const cur = currentPickCat();
  window.__pickCat = cur;
  box.innerHTML = cats.map((c) => {
    const on = c.id === cur
      ? ' style="border-color:var(--accent);color:var(--fg);background:rgba(91,157,255,.18)"'
      : '';
    return `<button class="ghost tiny" data-cat="${esc(c.id)}"${on} `
      + `title="${esc(c.desc)}">${c.icon} ${esc(c.label)}`
      + `<span class="hint" style="margin-left:4px">${esc(c.cost || '')}</span></button>`;
  }).join('');
  const c = cats.find((x) => x.id === cur) || {};
  const tip = $('#cat-tip');
  if (tip) {
    tip.innerHTML = c.id
      ? `${c.icon} <b>${esc(c.label)}</b>：${esc(c.desc)}　默认口径：<b>${esc(c.mode_label || '')}</b>`
      : '';
  }
  // 索引模式下拉：默认「用该用途的推荐口径」（空值），想微调再选具体的
  const sel = $('#pick-mode');
  if (sel) {
    const keep = sel.value;
    sel.innerHTML = '<option value="">（用该用途的推荐口径）</option>' + modeOptions(false);
    sel.value = keep && [...sel.options].some((o) => o.value === keep) ? keep : '';
  }
}

$('#pick-cats').onclick = (ev) => {
  const btn = ev.target.closest('button[data-cat]');
  if (!btn) return;
  window.__pickCat = btn.dataset.cat;
  renderPickCats();
};

/* ================= 分组方式（按路径 / 按归类，都可折叠）================= */
if ($('#group-by')) {
  // 记住上次选择（项目主可能切过但界面没反应 → 现在会明确告诉他切到了哪 ✓）
  const saved = (function () {
    try { return localStorage.getItem('eg2-groupby') || ''; } catch (e) { return ''; }
  })();
  window.__groupBy = window.__groupBy || saved || 'category';
  $('#group-by').value = window.__groupBy;
  $('#group-by').onchange = (ev) => {
    window.__groupBy = ev.target.value === 'category' ? 'category' : 'path';
    try { localStorage.setItem('eg2-groupby', window.__groupBy); } catch (e) { /* 忽略 */ }
    window.__expandedGroups = new Set();          // 换分组方式时收起，界面清爽 ✓
    if (window.__folders) $('#folders').innerHTML = foldersHtml(window.__folders);
    toast(window.__groupBy === 'category'
      ? `已按「归类」分组（${catList().length} 个类别；点 ▸ 展开）`
      : '已按「路径」分组');
  };
}

/* ================= 类别设置（A 改默认口径 + B 自己加类别）=================
   入口在文件夹页的「⚙ 类别设置」按钮 ✓（管理员可改；普通用户会收到 403 提示 ✓） */
async function openCatSettings() {
  let data;
  try {
    data = await api('/api/folder-categories');
  } catch (e) { alert('读类别设置失败：' + e.message); return; }
  const builtin = data.builtin || {};
  const custom = (data.custom && data.custom.custom) || [];
  const overrides = (data.custom && data.custom.overrides) || {};
  const modes = data.modes || FOLDER_MODES || [];
  const modeSel = (val, attr) => `<select ${attr} style="min-width:150px">${
    modes.map((m) => `<option value="${esc(m.id)}" ${m.id === val ? 'selected' : ''}>${esc(m.label)}</option>`).join('')
  }</select>`;
  const rows = [];
  rows.push('<div class="hint" style="margin-bottom:8px">'
    + '<b>内置类别</b>：改这里的默认口径，以后<b>新增</b>文件夹就按你改的来 ✓'
    + '（已有文件夹不受影响 ✓）</div>');
  for (const [cid, cat] of Object.entries(builtin)) {
    const cur = Object.assign({}, cat, overrides[cid] || {});
    // ⚠ 2026-10-18 修：use_kb 在 **config 里** ✗ 不在顶层 ——
    //   以前读 cur.use_kb 永远是 undefined → 勾选框永远不勾 ✗（项目主说"改不了"就是这个）
    const curConf = cur.config || {};
    const curKb = !!curConf.use_kb;
    const curDim = curConf.index_dim || '';
    rows.push(`<div class="cfgrow"><span class="lab">${esc(cat.icon || '')} ${esc(cat.label)}</span>
      <span>${modeSel(cur.mode, `data-ov-mode="${esc(cid)}"`)}
      <label class="hint" style="margin-left:8px"><input type="checkbox" data-ov-kb="${esc(cid)}"
        ${curKb ? 'checked' : ''}> 作为知识库</label>
      <label class="hint" style="margin-left:8px">维度
        <select data-ov-dim="${esc(cid)}" style="min-width:88px">
          <option value="" ${curDim === '' ? 'selected' : ''}>不指定</option>
          ${[128, 256, 512, 768].map((n) => `<option value="${n}" ${String(curDim) === String(n) ? 'selected' : ''}>${n} 维</option>`).join('')}
        </select></label></span>
      <span class="hp">${esc(cat.desc || '')}</span></div>`);
  }
  rows.push('<div class="hint" style="margin:14px 0 8px"><b>自定义类别</b>'
    + '（自己加的用途，比如「扫描件」「设计稿」✓）</div>');
  rows.push(`<div id="cat-custom">${custom.map((c) => `
    <div class="cfgrow" data-custom="${esc(c.id)}">
      <span class="lab"><input data-c-icon value="${esc(c.icon || '🏷')}" style="width:46px">`
      + `<input data-c-label value="${esc(c.label)}" style="width:130px"></span>
      <span>${modeSel(c.mode, 'data-c-mode')}
      <label class="hint" style="margin-left:8px"><input type="checkbox" data-c-kb
        ${(c.config && c.config.use_kb) ? 'checked' : ''}> 作为知识库</label></span>
      <span><input data-c-desc value="${esc(c.desc || '')}" placeholder="一句话说明"
        style="width:220px">
      <button class="ghost tiny danger" data-c-del="${esc(c.id)}">删除</button></span>
    </div>`).join('')}</div>`);
  rows.push('<button class="ghost tiny" id="cat-add">＋ 新增自定义类别</button>');
  rows.push(`<div class="hint" style="margin-top:10px">存储位置：<code>${esc(data.path || '')}</code>
    （本机的 json，不动数据库 ✓；服务器上要各自设置 ✓）</div>`);

  const ov = document.createElement('div');
  ov.className = 'modal show';
  ov.id = 'cat-modal';
  ov.innerHTML = `<div class="box"><div class="head"><b>类别设置</b>
      <span style="flex:1"></span>
      <button class="ghost tiny" id="cat-close">关闭</button></div>
    <div class="body">${rows.join('')}</div>
    <div class="foot"><span class="hint" id="cat-msg"></span><span style="flex:1"></span>
      <button class="ghost" id="cat-cancel">取消</button>
      <button id="cat-save">保存</button></div></div>`;
  document.body.appendChild(ov);

  const close = () => ov.remove();
  ov.onclick = (ev) => { if (ev.target === ov) close(); };
  ov.querySelector('#cat-close').onclick = close;
  ov.querySelector('#cat-cancel').onclick = close;
  ov.querySelector('#cat-add').onclick = () => {
    const box = ov.querySelector('#cat-custom');
    const id = 'my' + Date.now().toString().slice(-6);
    const div = document.createElement('div');
    div.className = 'cfgrow';
    div.dataset.custom = id;
    div.innerHTML = `<span class="lab"><input data-c-icon value="🏷" style="width:46px">`
      + `<input data-c-label value="新类别" style="width:130px"></span>
      <span>${modeSel('name_only', 'data-c-mode')}
      <label class="hint" style="margin-left:8px"><input type="checkbox" data-c-kb> 作为知识库</label></span>
      <span><input data-c-desc placeholder="一句话说明" style="width:220px">
      <button class="ghost tiny danger" data-c-del="${id}">删除</button></span>`;
    box.appendChild(div);
  };
  ov.onclick = async (ev) => {
    if (ev.target === ov) { close(); return; }
    const del = ev.target.closest('[data-c-del]');
    if (del) {
      const row = del.closest('[data-custom]');
      if (row) row.remove();
      return;
    }
    if (ev.target.id === 'cat-save') {
      const overridesNew = {};
      ov.querySelectorAll('[data-ov-mode]').forEach((sel) => {
        const cid = sel.dataset.ovMode;
        const kb = ov.querySelector(`[data-ov-kb="${cid}"]`);
        const dim = ov.querySelector(`[data-ov-dim="${cid}"]`);
        const base = builtin[cid] || {};
        const baseConf = base.config || {};
        const patch = {};
        if (sel.value !== base.mode) patch.mode = sel.value;
        // ★ config 里比、config 里写 ✓（以前写在顶层，后端不认识就被丢了 ✗）
        const conf = {};
        if (!!(kb && kb.checked) !== !!baseConf.use_kb) {
          conf.use_kb = !!(kb && kb.checked);
        }
        const dimVal = dim ? String(dim.value || '') : '';
        if (dimVal && dimVal !== String(baseConf.index_dim || '')) {
          conf.index_dim = Number(dimVal);       // 空 = 不提交（保持现状，避免把配置清掉）✓
        }
        if (Object.keys(conf).length) patch.config = conf;
        if (Object.keys(patch).length) overridesNew[cid] = patch;
      });
      const customNew = [];
      ov.querySelectorAll('[data-custom]').forEach((row) => {
        const id = row.dataset.custom;
        const label = (row.querySelector('[data-c-label]') || {}).value || '';
        if (!String(label).trim()) return;
        customNew.push({
          id,
          icon: (row.querySelector('[data-c-icon]') || {}).value || '🏷',
          label: String(label).trim(),
          desc: (row.querySelector('[data-c-desc]') || {}).value || '',
          mode: (row.querySelector('[data-c-mode]') || {}).value || 'name_only',
          config: { use_kb: !!(row.querySelector('[data-c-kb]') || {}).checked },
        });
      });
      try {
        const r = await post('/api/folder-categories',
                             { overrides: overridesNew, custom: customNew }, 'PUT');
        ov.querySelector('#cat-msg').textContent = r.message || '已保存';
        toast(r.message || '类别设置已保存');
        await refreshFolders();
        renderPickCats();
        setTimeout(close, 800);
      } catch (e) {
        ov.querySelector('#cat-msg').textContent = '保存失败：' + e.message;
      }
    }
  };
}

if ($('#cat-settings')) $('#cat-settings').onclick = () => openCatSettings();


async function openPicker(path) {
  const data = await api('/api/fs/list?path=' + encodeURIComponent(path || ''));
  pickPath = data.path;
  $('#cur-path').textContent = data.path || '此电脑（先选磁盘）';
  $('#up').dataset.parent = data.parent || '';
  $('#up').disabled = !data.parent && !data.path;
  $('#here').disabled = !data.path;
  $('#dir-list').innerHTML = data.dirs.length ? data.dirs.map((d) => `
    <div class="item">
      <input type="checkbox" data-toggle="${esc(d.path)}" ${picked.has(d.path) ? 'checked' : ''}>
      <span class="nm" data-dir="${esc(d.path)}">📁 ${esc(d.name)}</span>
      ${d.indexed ? '<span class="hint">（已索引）</span>' : ''}
    </div>`).join('') : '<div class="empty">这个目录下没有子文件夹</div>';
  $('#modal').classList.add('show');
  updatePickHint();
}

function updatePickHint() {
  $('#pick-hint').textContent = `已选 ${picked.size} 个文件夹`;
  $('#confirm-pick').disabled = picked.size === 0;
  // 文案改准：添加后**不立刻扫**，所以别说"并建立索引"✗（项目主 2026-10-17）
  $('#confirm-pick').textContent = picked.size
    ? `添加 ${picked.size} 个文件夹` : '添加';
}

$('#add-folder').onclick = () => {
  if (!catList().length && window.__foldersResp === undefined) {
    // 还没拉过 /api/folders（例如页面刚开就点添加）→ 先取一次，别让用途栏空着
    refreshFolders().then(renderPickCats).catch(() => {});
  }
  $('#modal').classList.add('show');
  renderPickCats();
  openPicker('').catch((e) => alert(e.message));
};
$('#cancel-pick').onclick = () => { picked.clear(); $('#modal').classList.remove('show'); };
$('#up').onclick = () => openPicker($('#up').dataset.parent || '').catch((e) => alert(e.message));
$('#here').onclick = () => { picked.add(pickPath); updatePickHint(); };
$('#dir-list').onclick = (ev) => {
  const box = ev.target.closest('[data-toggle]');
  if (box) {
    const p = box.dataset.toggle;
    if (box.checked) picked.add(p); else picked.delete(p);
    updatePickHint();
    return;
  }
  const item = ev.target.closest('[data-dir]');
  if (item) openPicker(item.dataset.dir).catch((e) => alert(e.message));
};
$('#confirm-pick').onclick = async () => {
  const paths = [...picked];
  if (!paths.length) return;
  const mode = $('#pick-mode').value;
  const category = currentPickCat();
  $('#confirm-pick').disabled = true;
  try {
    // ⚠ 先**不扫**（项目主 2026-10-17：一加就扫，来不及改配置 ✗）——
    //   让他先设好用途类别 / 子目录归类，再点「开始索引」✓
    const body = { paths, category, scan: false };
    if (mode) body.mode = mode;          // 只在用户显式选了具体模式时才传 ✓
    const r = await post('/api/folders', body);
    picked.clear();
    $('#modal').classList.remove('show');
    $('#progress-card').style.display = '';
    $('#progress-text').textContent = r.message;
    if (r.skipped && r.skipped.length) {
      alert('部分文件夹没有添加：\n' + r.skipped.map((s) => s.path + ' —— ' + s.reason).join('\n'));
    }
    await refreshFolders();
    setIndexButtons(false);              // 没在跑 → 露出「开始索引」✓
    const n = r.added ? r.added.length : 0;
    $('#progress-text').textContent =
      `已添加 ${n} 个文件夹，**还没开始扫描**`;
    $('#progress-detail').textContent =
      '先确认用途类别 / 子目录归类（在文件夹那行点「配置」），再点右边的「▶ 开始索引」';
    toast(`已添加 ${n} 个文件夹（还没扫描）：可以先改配置，再点「▶ 开始索引」`);
  } catch (e) { alert(e.message); }
  finally { updatePickHint(); }
};

/* ---- 子目录归类（只做一层）：把该文件夹的第一层子目录列出来，每个一行下拉 ----
   没选「跟随本文件夹」的才会写进 config.subcats ✓（选空 = 清掉 ✓） */
async function fillSubcats(folder) {
  const box = $('#cfg-body') && $('#cfg-body').querySelector('#cfg-subcats');
  if (!box || !folder) return;
  const cats = catList();
  let dirs = [];
  try {
    // ⚠ 带上 folder_id：让后端按**这个文件夹自己的排除规则**过滤 ✓
    //   （2026-10-18：以前不传，排除了的目录照样列出来 ✗）
    const d = await api('/api/fs/list?path=' + encodeURIComponent(folder.path || '')
                        + '&folder_id=' + encodeURIComponent(folder.id || 0));
    dirs = (d.dirs || []).filter((x) => x && x.name && !x.name.startsWith('$'));
  } catch (e) {
    box.innerHTML = '<span class="hp">读不到子目录（' + esc(e.message) + '）</span>';
    return;
  }
  const cur = (folder.config && folder.config.subcats) || {};
  if (!dirs.length) {
    box.innerHTML = '<span class="hp">这个文件夹下面没有子目录</span>';
    return;
  }
  // 失效的配置（子目录改名/删掉了）也提示一下，别让人以为设置丢了 ✓
  const alive = new Set(dirs.map((d) => d.name));
  const dead = Object.keys(cur).filter((n) => !alive.has(n));
  box.innerHTML = dirs.map((d) => {
    const v = cur[d.name];
    const isCustom = !!(v && typeof v === 'object');       // 自定义勾选 ✓
    const on = (k, dflt) => (isCustom ? !!v[k] : !!dflt);
    return `
    <div class="sub-row" data-name="${esc(d.name)}" style="margin:5px 0">
      <div style="display:flex;gap:8px;align-items:center">
        <span style="flex:0 0 170px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
              title="${esc(d.name)}">📁 ${esc(d.name)}</span>
        <select data-subcat="${esc(d.name)}" style="flex:1">
          <option value="">跟随本文件夹</option>
          ${cats.map((c) => `<option value="${esc(c.id)}" ${v === c.id ? 'selected' : ''}>${c.icon} ${esc(c.label)}</option>`).join('')}
          <option value="__custom__" ${isCustom ? 'selected' : ''}>✏ 自定义（自己勾选扫什么）…</option>
        </select>
      </div>
      <div class="sub-custom" style="margin:5px 0 7px 178px;gap:14px;flex-wrap:wrap;font-size:12.5px;
                                     display:${isCustom ? 'flex' : 'none'}">
        <label title="读文件内容（PDF/Word/Excel/PPT/txt/md 的正文），切块后能按内容搜到。文件名永远会索引，不受影响">
          <input type="checkbox" data-sck="index_text" ${on('index_text', true) ? 'checked' : ''}> 读文字</label>
        <label title="① 图片编码成向量（能按画面内容搜）② 人脸识别（检出的人脸进「人物」页）">
          <input type="checkbox" data-sck="index_images" ${on('index_images', false) ? 'checked' : ''}> 解图片（含人脸）</label>
        <label title="音频编码成向量，能按声音内容搜。不是转成文字（没有语音转文字那一步）">
          <input type="checkbox" data-sck="index_audio" ${on('index_audio', false) ? 'checked' : ''}> 解音频</label>
        <label title="视频按抽帧处理，每帧当图片，所以很慢、很占空间">
          <input type="checkbox" data-sck="index_video" ${on('index_video', false) ? 'checked' : ''}> 解视频</label>
        <label title="勾了才参与「知识库问答」的检索；不勾也能在搜索页搜到">
          <input type="checkbox" data-sck="use_kb" ${on('use_kb', false) ? 'checked' : ''}> 当知识库</label>
        <span class="hp">（只改这几项；维度、分块仍跟随本文件夹 ✓）</span>
      </div>
    </div>`;
  }).join('')
    + (dead.length
      ? `<div class="hp" style="color:var(--warn)">这 ${dead.length} 条已失效（子目录改名或不在这个目录里了）：${esc(dead.join('、'))}</div>`
      : '');
  // 选「自定义」时才展开勾选区 ✓
  box.querySelectorAll('[data-subcat]').forEach((sel) => {
    sel.onchange = () => {
      const row = sel.closest('.sub-row');
      const cus = row && row.querySelector('.sub-custom');
      if (cus) cus.style.display = (sel.value === '__custom__') ? 'flex' : 'none';
    };
  });
}


$('#folders').onclick = async (ev) => {
  const box = ev.target.closest('[data-pick]');
  if (box) {
    const id = +box.dataset.pick;
    if (box.checked) selected.add(id); else selected.delete(id);
    box.closest('.folder').classList.toggle('sel', box.checked);
    const n = selected.size;
    $('#bulk-toolbar').style.display = n ? '' : 'none';
    $('#bulk-count').textContent = `已选中 ${n} 个文件夹`;
    return;
  }
  const btn = ev.target.closest('button[data-act]');
  // 点「27 异常」→ 看具体是哪些文件 ✓（要在按钮判断之前，因为它是 <b> 不是 button）
  const errTag = ev.target.closest('[data-err-folder]');
  if (errTag) { openErrorList(+errTag.dataset.errFolder); return; }
  if (!btn) return;
  const id = +btn.dataset.id;
  try {
    if (btn.dataset.act === 'remove') {
      // 项目主 2026-10-18：怕误点直接删 → 改成**两次确认** ✓
      const f = (folders || []).find((x) => x.id === id) || {};
      if (!confirm(`要移除这个文件夹的索引吗？\n\n${f.path || ''}\n\n`
                   + '（磁盘上的文件不会被删除，但索引和片段会清掉）')) return;
      const word = '移除';
      if (!confirm(`再确认一次：真的要移除「${f.path || '该文件夹'}」的索引吗？\n\n`
                   + '移除后要重新加回来，并重新建立索引（大文件夹可能要很久）。\n\n'
                   + `确定 = 移除\n取消 = 保留`)) return;
      await api('/api/folders/' + id, { method: 'DELETE' });
      toast(`已移除「${f.path || id}」的索引（磁盘文件没动）`);
      await refreshFolders();
    } else if (btn.dataset.act === 'scan') {
      startIndex({ folder_ids: [id] }, '正在增量更新…');
    } else if (btn.dataset.act === 'start') {
      // 项目主 2026-10-18：开始/暂停放到每个文件夹行上，紧挨着「配置」，更好点 ✓
      await startIndex({ folder_ids: [id] }, '正在索引这个文件夹…');
      await refreshFolders();
    } else if (btn.dataset.act === 'pause') {
      if (!confirm('暂停索引？\n\n已建好的部分会保留；点「▶ 开始」接着跑，不会重做 ✓')) return;
      await post('/api/index/cancel').catch(() => {});
      toast('已暂停索引（进度已保留）');
      setTimeout(checkOnce, 500);
      await refreshFolders();
    } else if (btn.dataset.act === 'config') {
      openFolderConfig(id);
    } else if (btn.dataset.act === 'paths') {
      // 🔗 本机 / 服务器 路径映射（2026-10-20 项目主要求）✓
      await openPathMap(id, btn.closest('.folder')?.dataset.folderPath || '');
    }
  } catch (e) { alert(e.message); }
};

/* ---- 批量操作完成后**自动取消勾选**（项目主 2026-10-18：怕残留勾选又误改）----
   注意：只在**成功**后清 ✓；失败时保留勾选，方便重试 ✓ */
function clearSelection() {
  selected.clear();
  const bar = $('#bulk-toolbar');
  if (bar) bar.style.display = 'none';
  const cnt = $('#bulk-count');
  if (cnt) cnt.textContent = '';
  document.querySelectorAll('#folders input[data-pick]').forEach((box) => {
    box.checked = false;
  });
  document.querySelectorAll('#folders .folder.sel').forEach((row) => {
    row.classList.remove('sel');
  });
}

$('#bulk-toolbar').onclick = async (ev) => {
  const btn = ev.target.closest('[data-bulk]');
  if (!btn) return;
  const ids = [...selected];
  const kind = btn.dataset.bulk;
  try {
    if (kind === 'clear') { clearSelection(); return refreshFolders(); }
    if (kind === 'scan') {
      // 后端本来就是**按顺序逐个扫**（indexer._run 里 for 循环）✓
      // 这里只要把选中的文件夹一次交出去即可，不用一个个点 ✓
      const r = await startIndex({ folder_ids: ids },
                                 `正在按顺序扫描 ${ids.length} 个文件夹（一个接一个自动跑）…`);
      clearSelection();                       // 交出去之后就不该再勾着 ✓
      return r;
    }
    if (kind === 'rebuild') {
      if (!confirm(`重建 ${ids.length} 个文件夹的索引？`)) return;
      const r = await startIndex({ folder_ids: ids, force: true },
                                 `正在重建 ${ids.length} 个文件夹…`);
      clearSelection();
      return r;
    }
    if (kind === 'remove') {
      // 危险操作（项目主 2026-10-18：怕误点直接删）→ **两次确认**，第二次把后果说清 ✓
      if (!confirm(`要移除这 ${ids.length} 个文件夹的索引吗？\n\n`
                   + ids.map((id) => {
                       const f = folders.find((x) => x.id === id);
                       return '· ' + (f ? f.path : '#' + id);
                     }).join('\n')
                   + '\n\n（磁盘上的文件不会被删除，但索引和片段会清掉）')) return;
      if (!confirm(`再确认一次：真的移除这 ${ids.length} 个文件夹的索引吗？\n\n`
                   + '移除后要重新加回来，并重新建立索引（大文件夹可能要很久）。\n\n'
                   + '确定 = 移除\n取消 = 保留')) return;
      await post('/api/folders/remove', { folder_ids: ids });
      toast(`已移除 ${ids.length} 个文件夹的索引（磁盘文件没动）`);
      selected.clear();
      return refreshFolders();
    }
    if (kind === 'config') {
      if (!ids.length) return;
      return openFolderConfig(ids);
    }
    // 「批量设归类」= 同一个批量弹窗（里面第一行就是用途类别）✓
    // ⚠ 2026-10-18 漏了这条分支：按钮点了 `kind==='category'` 不匹配任何分支 →
    //   静默返回 → 项目主看到的就是"点了没反应"✗
    if (kind === 'category') {
      if (!ids.length) return;
      return openFolderConfig(ids);
    }
  } catch (e) { alert(e.message); }
};

$('#reindex').onclick = () => startIndex({}, '正在增量更新全部…');
$('#rebuild').onclick = () => {
  if (!confirm('重建会重新扫描全部文件夹，大文件夹会比较慢。确定继续？')) return;
  startIndex({ force: true }, '正在重建全部索引…');
};

async function startIndex(payload, text) {
  try {
    const r = await post('/api/index/start', payload);
    $('#progress-card').style.display = '';
    $('#progress-text').textContent = r.started ? text : (r.message || '已有任务在跑');
    toast(r.started ? (text || '已开始更新，进度会显示在文件夹那一行') : (r.message || '已有任务在跑'),
          !r.started);
    if (!polling) polling = setInterval(checkOnce, 1200);
    await checkOnce();          // 立刻拉一次状态，马上显示「更新中」
    setTimeout(checkOnce, 500);
  } catch (e) { toast('启动失败：' + e.message, true); }
}

/* ---------------- 文件夹配置（单个 / 批量） ---------------- */
function openFolderConfig(targets) {
  const ids = Array.isArray(targets) ? targets : [targets];
  const folders = window.__folders || [];
  cfgTargets = ids;
  $('#cfg-msg').textContent = '';

  if (ids.length === 1) {
    const folder = folders.find((f) => f.id === ids[0]);
    if (!folder || !FOLDER_SCHEMA) return;
    $('#cfg-title').textContent = folder.path;

    const cfg = folder.config || {};
    const cats = catList();
    const curCat = folder.category || '';
    const catRow = `<div class="cfgrow"><span class="lab">用途类别</span>
      <select id="cfg-cat">
        <option value="">未分类（不动扫描口径）</option>
        ${cats.map((c) => `<option value="${esc(c.id)}" ${c.id === curCat ? 'selected' : ''}>${c.icon} ${esc(c.label)} · ${esc(c.mode_label)}</option>`).join('')}
      </select>
      <span class="hp">换个用途 = 套用它的默认扫描口径（你调过的排除规则、分块等**保留**）；
        如果口径变了，会提示你重建索引。这里是「重新分类」的入口。</span></div>`;
    const rows = [catRow, `<div class="cfgrow"><span class="lab">索引模式</span>
      <select id="cfg-mode">${modeOptions(true)}</select>
      <span class="hp">选预设会覆盖下面的模态开关；改下面的开关会自动变成「自定义」</span></div>`,
    // ---- 子目录归类（2026-10-17，只做一层）----
    // 同一个文件夹里的第一层子目录可以各自用不同口径 ✓（内容异步填，见 fillSubcats）
    `<div class="cfgrow"><span class="lab">子目录归类</span>
      <span id="cfg-subcats" style="display:block;width:100%;max-height:250px;overflow:auto"></span>
      <span class="hp">给**第一层**子目录单独指定用途（只做一层）。没说的一律跟随本文件夹。
        子目录只改「要不要读文字 / 解码图片 / 当知识库」，<b>不改向量维度</b>（同一个文件夹必须同维度）。
        改完需要重建索引才会生效；子目录改名或移动后这条会失效，重新选一次即可。</span></div>`];

    for (const [key, spec] of Object.entries(FOLDER_SCHEMA)) {
      // ⚠ mode 和 subcats 都**不在这里渲染**（各有专门的一行）✗ ——
      //   以前只跳过了 mode，于是 subcats 落到下面几个分支都不匹配 → ctl=undefined
      //   → 界面上多出一行「子目录归类  undefined」✗（项目主截图看到的）
      if (key === 'mode' || key === 'subcats') continue;
      const label = FIELD_LABEL[key] || spec.label || key;
      const help = FIELD_HELP[key] || spec.help || '';
      let ctl;
      if (spec.type === 'switch') {
        const on = cfg[key] !== undefined ? cfg[key] : _inheritGuess(key);
        ctl = `<label class="sw"><input type="checkbox" data-fkey="${key}" ${on ? 'checked' : ''}><span class="sl"></span></label>`;
      } else if (spec.type === 'number') {
        const val = cfg[key] !== undefined ? cfg[key] : '';
        ctl = `<input type="number" data-fkey="${key}" value="${esc(val)}" placeholder="跟随全局"
          min="${spec.min}" max="${spec.max}">`;
      } else if (spec.type === 'text') {
        const val = cfg[key] !== undefined ? String(cfg[key]) : '';
        ctl = `<textarea class="fx" data-fkey="${key}" rows="5"
          placeholder="一行一条，留空 = 不排除">${esc(val)}</textarea>`;
      } else if (spec.type === 'select') {
        const val = cfg[key] !== undefined ? String(cfg[key]) : '';
        // ⚠ 原来这里把选项文本硬编码成「${o} 维」✗ —— 只适合 index_dim；
        //   新加的「PDF 插图打标」会显示成「off 维 / queue 维 / auto 维」✗
        //   现在：有 spec.labels 就用中文标签；只有 index_dim 才加「维」✓
        const names = spec.labels || {};
        const dimOnly = (key === 'index_dim');
        const emptyText = names[''] || '跟随全局';
        ctl = `<select data-fkey="${key}"><option value="">${esc(emptyText)}</option>
          ${spec.options.map((o) => {
            const t = names[o] || (dimOnly ? `${o} 维` : o);
            return `<option value="${o}" ${String(o) === val ? 'selected' : ''}>${esc(t)}</option>`;
          }).join('')}
        </select>`;
      }
      rows.push(`<div class="cfgrow"><span class="lab" title="${esc(label)}">${esc(label)}</span>
        <span title="${esc(help)}">${ctl}</span><span class="hp" title="${esc(help)}">${esc(help).replace(/\n/g, '<br>')}</span></div>`);
    }
    $('#cfg-body').innerHTML = rows.join('');
    fillSubcats(folder);            // 异步把第一层子目录的归类下拉填上 ✓
    bindModeLinkage();              // 模式变了就同步下面的开关 ✓（内部自己取联动表）

    // ---- 只读模式（多人）：这个文件夹不归我管就不能改 ----
    // 典型场景：管理员把文件夹标成「知识库」共享给所有人（只读 ✓）。
    // 之前这些开关对普通用户照样可点，点保存只会收到 403 ✗（体验很差）。
    // 现在控件直接置灰 + 顶部说明；后端本来也拦（403），这里只是别让人白点 ✓
    if (folder.writable === false) {
      const tip = document.createElement('div');
      tip.style.cssText = 'margin:0 0 12px;padding:9px 11px;border-radius:9px;'
        + 'border:1px solid var(--line,#2a3340);background:rgba(120,160,220,.10);'
        + 'font-size:13px;line-height:1.7';
      tip.innerHTML = '<b>这个文件夹由管理员维护（共享只读）</b><br>'
        + '你能搜索、问答、预览里面的内容 ✓，但不能改配置、也不能更新索引 —— '
        + '要改请让管理员在<b>共享设置</b>里操作。';
      $('#cfg-body').insertBefore(tip, $('#cfg-body').firstChild);
      $('#cfg-body').querySelectorAll('input,select,textarea,button').forEach((el) => {
        el.disabled = true;
      });
      $('#cfg-save').disabled = true;
      $('#cfg-save').title = '只有管理员或文件夹归属人能改';
    } else {
      $('#cfg-save').disabled = false;
      $('#cfg-save').title = '';
    }

    // 给「排除规则」加一个预览按钮，能先用没保存的规则试算
    const exBox = $('#cfg-body').querySelector('[data-fkey="exclude"]');
    if (exBox) {
      const wrap = document.createElement('div');
      wrap.style.cssText = 'margin-top:8px;display:flex;gap:10px;align-items:center;flex-wrap:wrap';
      wrap.innerHTML = '<button class="ghost tiny" id="ex-preview">预览排除效果</button>'
        + '<span class="hint" id="ex-result"></span>';
      exBox.parentElement.appendChild(wrap);
    // ---- 「从文件夹里选」排除项 ----
    const pickBtn = document.createElement('button');
    pickBtn.className = 'ghost tiny';
    pickBtn.textContent = '📁 从文件夹里选';
    pickBtn.style.marginLeft = '8px';
    wrap.appendChild(pickBtn);

    const panel = document.createElement('div');
    panel.id = 'ex-panel';
    panel.style.display = 'none';
    panel.style.cssText = 'display:none;margin-top:10px;border:1px solid var(--line);'
      + 'border-radius:10px;padding:10px;max-height:340px;overflow:auto;background:var(--bg)';
    wrap.parentElement.appendChild(panel);

    pickBtn.addEventListener('click', async () => {
      if (panel.style.display === 'block') { panel.style.display = 'none'; return; }
      panel.style.display = 'block';
      panel.innerHTML = '<div class="hint">正在读取文件夹…</div>';
      try {
        const r = await api(`/api/folders/${cfgTargets[0]}/exclude-candidates`);
        const cur = new Set(($('#cfg-body').querySelector('[data-fkey="exclude"]').value || '')
          .split(/[\n,]/).map((x) => x.trim().toLowerCase()).filter(Boolean));
        const extHtml = (r.exts || []).map((x) => {
          const on = cur.has(x.ext.toLowerCase()) ? ' checked' : '';
          return `<label class="exi"><input type="checkbox" data-ex="${esc(x.ext)}"${on}>`
            + `<span>${esc(x.ext)}</span><em>${x.count}</em></label>`;
        }).join('');
        const dirHtml = (r.dirs || []).map((d) => {
          const on = cur.has(d.rel.toLowerCase()) ? ' checked' : '';
          return `<label class="exi"><input type="checkbox" data-ex="${esc(d.rel)}"${on}>`
            + `<span>${esc(d.name)}/</span><em>${d.files}</em></label>`;
        }).join('');
        panel.innerHTML =
          `<div class="hint" style="margin-bottom:8px">点一下就把规则加进输入框；再点取消。`
          + `共扫描 ${r.scanned} 个文件${r.truncated ? '（太多，只统计了一部分）' : ''}`
          + `${r.dirs_total > 80 ? ` · 子目录只列了前 80 个（共 ${r.dirs_total}）` : ''}</div>`
          + `<div class="exhead">按扩展名排除</div><div class="exgrid">${extHtml || '<span class="hint">没有</span>'}</div>`
          + `<div class="exhead" style="margin-top:12px">按子目录排除</div><div class="exgrid">${dirHtml || '<span class="hint">没有子目录</span>'}</div>`;

        panel.querySelectorAll('input[data-ex]').forEach((cb) => {
          cb.addEventListener('change', () => {
            const ta = $('#cfg-body').querySelector('[data-fkey="exclude"]');
            const items = ta.value.split(/[\n,]/).map((x) => x.trim()).filter(Boolean);
            const val = cb.dataset.ex;
            const idx = items.findIndex((x) => x.toLowerCase() === val.toLowerCase());
            if (cb.checked) { if (idx < 0) items.push(val); }
            else if (idx >= 0) items.splice(idx, 1);
            ta.value = items.join('\n');
            ta.dispatchEvent(new Event('change', { bubbles: true }));
          });
        });
      } catch (e) {
        panel.innerHTML = `<div class="hint" style="color:var(--bad)">读取失败：${esc(e.message)}</div>`;
      }
    });

      wrap.querySelector('#ex-preview').addEventListener('click', async () => {
        const tip = $('#ex-result');
        const btn = wrap.querySelector('#ex-preview');
        btn.disabled = true; tip.textContent = '正在统计…';
        try {
          const exVal = $('#cfg-body').querySelector('[data-fkey="exclude"]').value;
          const r = await post(`/api/folders/${cfgTargets[0]}/preview-exclude`,
                               { config: { exclude: exVal } });
          let msg = `扫描 ${r.scanned} 项 → 保留 ${r.kept}，排除 ${r.excluded}`
            + `（目录 ${r.excluded_dirs} 个）`;
          if (r.top_ext && r.top_ext.length) {
            msg += ' · 主要排除类型：'
              + r.top_ext.slice(0, 4).map((x) => `${x.ext} ${x.count}`).join('、');
          }
          if (r.truncated) msg += ' · （数量太多，只统计了一部分）';
          tip.textContent = msg;
          window.__exPreview = r;
          tip.style.cursor = 'pointer';
          tip.title = '点击看前几条被排除的路径';
          tip.onclick = () => {
            const lines = (r.samples || []).slice(0, 15)
              .map((s) => `· ${s.path}   —— ${s.reason}`).join('\n');
            alert(lines || '没有被排除的项');
          };
        } catch (e) { tip.textContent = '预览失败：' + e.message; }
        btn.disabled = false;
      });
    }
    $('#cfg-body').querySelector('#cfg-mode').value = cfg.mode || 'inherit';
  } else {
    $('#cfg-title').textContent = `批量设置 ${ids.length} 个文件夹`;
    $('#cfg-body').innerHTML = `
      <div class="cfgrow"><span class="lab">用途类别</span>
        <select id="cfg-cat-bulk">
          <option value="">（不变）</option>
          ${catList().map((c) => `<option value="${esc(c.id)}">${c.icon} ${esc(c.label)} · ${esc(c.mode_label)}</option>`).join('')}
        </select>
        <span class="hp">选一个用途 = 这 ${ids.length} 个文件夹都套用它的默认口径 ✓
          （你给各自调过的排除规则等会保留 ✓）；改完需要重建的会提示你 ✓</span></div>
      <div class="cfgrow"><span class="lab">索引模式</span>
        <select id="cfg-mode"><option value="">（不变）</option>${modeOptions(false)}</select>
        <span class="hp">想更细地改口径再用这个；两个都选时**先套归类、再套模式** ✓</span></div>
      <div class="cfgrow"><span class="hp">${esc(ids.map((id) => {
        const f = folders.find((x) => x.id === id); return f ? f.path : id;
      }).join('\n'))}</span></div>`;
    // 批量里只要有改不了的（管理员共享的），就整体置灰 —— 免得改一半、报一堆 403 ✗
    const locked = ids.filter((id) => {
      const f = folders.find((x) => x.id === id);
      return f && f.writable === false;
    });
    if (locked.length) {
      const tip = document.createElement('div');
      tip.style.cssText = 'margin:0 0 12px;padding:9px 11px;border-radius:9px;'
        + 'border:1px solid var(--line,#2a3340);background:rgba(120,160,220,.10);'
        + 'font-size:13px;line-height:1.7';
      tip.innerHTML = `<b>其中 ${locked.length} 个由管理员维护（共享只读）</b>，`
        + '没法批量改 ✗；要改请让管理员在<b>共享设置</b>里操作。';
      $('#cfg-body').insertBefore(tip, $('#cfg-body').firstChild);
      $('#cfg-body').querySelectorAll('input,select,textarea,button').forEach((el) => {
        el.disabled = true;
      });
      $('#cfg-save').disabled = true;
      $('#cfg-save').title = '选中的文件夹里有你不能改的';
    } else {
      $('#cfg-save').disabled = false;
      $('#cfg-save').title = '';
    }
  }
  $('#cfg-modal').classList.add('show');
}

function _inheritGuess(key) { return ['index_text'].includes(key); }

$('#cfg-close').onclick = $('#cfg-cancel').onclick = () => $('#cfg-modal').classList.remove('show');
$('#cfg-body').addEventListener('change', (ev) => {
  const el = ev.target.closest('[data-fkey]');
  if (!el) return;
  const modeSel = $('#cfg-mode');
  if (modeSel.value && modeSel.value !== 'custom') modeSel.value = 'custom';
});
$('#cfg-save').onclick = async () => {
  if (!cfgTargets.length) return;
  const mode = $('#cfg-mode').value;
  const single = cfgTargets.length === 1;
  const config = {};
  if (single) {
    $('#cfg-body').querySelectorAll('[data-fkey]').forEach((el) => {
      const key = el.dataset.fkey;
      if (el.type === 'checkbox') config[key] = el.checked;
      else if (el.value !== '') config[key] = el.value;
    });
    // 子目录归类（只收"改了"的那些；全空 = 清掉这张表 ✓）
    // 值有两种：预设归类 id（"kb"）或自定义勾选（{index_text: true, ...}）✓
    const subs = {};
    $('#cfg-body').querySelectorAll('.sub-row').forEach((row) => {
      const sel = row.querySelector('[data-subcat]');
      if (!sel || !sel.value) return;
      if (sel.value === '__custom__') {
        const one = {};
        row.querySelectorAll('[data-sck]').forEach((ck) => {
          one[ck.dataset.sck] = ck.checked;
        });
        subs[sel.dataset.subcat] = one;
        return;
      }
      subs[sel.dataset.subcat] = sel.value;
    });
    config.subcats = subs;
  }
  try {
    $('#cfg-save').disabled = true;
    // ---- 「重新分类」（项目主要求）：类别变了就先走这个接口 ✓ ----
    // 这时**不能**再提交上面控件里的旧值 ✗（那会把刚套上的类别默认真空覆盖回去）
    const catSel = single ? $('#cfg-cat') : null;
    const oldFolder = (window.__folders || []).find((f) => f.id === cfgTargets[0]) || {};
    const newCat = catSel ? catSel.value : null;
    if (single && newCat !== null && newCat !== (oldFolder.category || '')) {
      const rc = await post(`/api/folders/${cfgTargets[0]}/category`,
                            { category: newCat }, 'PUT');
      $('#cfg-msg').textContent = rc.message || '已重新分类';
      await refreshFolders();
      if (rc.need_reindex
          && confirm((rc.message || '扫描口径变了，需要重建索引') + '\n\n现在重建吗？')) {
        $('#cfg-modal').classList.remove('show');
        startIndex({ folder_ids: cfgTargets, force: true }, '正在重建索引…');
      } else {
        setTimeout(() => $('#cfg-modal').classList.remove('show'), 900);
      }
      return;
    }
    let r;
    if (single) {
      r = await post(`/api/folders/${cfgTargets[0]}/config`, { mode, config }, 'PUT');
    } else {
      // 批量（项目主 2026-10-18）：先套「用途归类」，再套「索引模式」✓
      const catBulk = ($('#cfg-cat-bulk') || {}).value || '';
      const parts = [];
      let anyReindex = false;
      if (catBulk) {
        const rc = await post('/api/folders/categories',
                              { folder_ids: cfgTargets, category: catBulk }, 'PUT');
        parts.push(rc.message || '');
        anyReindex = anyReindex || (rc.need_reindex || []).length > 0;
      }
      if (mode) {
        const rm = await post('/api/folders/config', { folder_ids: cfgTargets, mode });
        parts.push(rm.message || '');
        anyReindex = anyReindex || !!(rm.need_reindex && rm.need_reindex.length);
      }
      if (!parts.length) {
        $('#cfg-msg').textContent = '什么都没选：归类或模式至少选一个';
        $('#cfg-save').disabled = false;
        return;
      }
      // 批量改动影响面大（可能触发重建）→ 执行前给一次确认缓冲 ✓（项目主 2026-10-18）
      const catName = catBulk
        ? ((catList().find((c) => c.id === catBulk) || {}).label || catBulk) : '';
      const modeName = mode
        ? ((FOLDER_MODES.find((m) => m.id === mode) || {}).label || mode) : '';
      const what = [catBulk ? `归类设为「${catName}」` : '',
                    mode ? `索引模式设为「${modeName}」` : ''].filter(Boolean).join('、');
      if (!confirm(`要把这 ${cfgTargets.length} 个文件夹的${what}吗？\n\n`
                   + '· 会套用该设置的默认扫描口径\n'
                   + '· 你给各个文件夹单独调过的排除规则等会保留\n'
                   + '· 改了口径的文件夹需要重建索引（下一步会问你）')) {
        $('#cfg-save').disabled = false;
        return;
      }
      r = { message: parts.filter(Boolean).join('；'),
            need_reindex: anyReindex ? [1] : [] };
    }
    $('#cfg-msg').textContent = r.message || '已保存';
    await refreshFolders();
    // 批量改完就**取消勾选** ✓（项目主 2026-10-18：残留勾选容易又误改）
    if (!single) {
      clearSelection();
      toast(r.message || '已保存（已取消勾选）');
    }
    const need = single ? r.need_reindex : (r.need_reindex && r.need_reindex.length);
    if (need) {
      if (confirm((r.message || '需要重建索引') + '\n\n现在重建吗？')) {
        $('#cfg-modal').classList.remove('show');
        startIndex({ folder_ids: cfgTargets, force: true }, '正在重建索引…');
      }
    } else {
      setTimeout(() => $('#cfg-modal').classList.remove('show'), 700);
    }
  } catch (e) { $('#cfg-msg').textContent = e.message; }
  finally { $('#cfg-save').disabled = false; }
};

/* ---------------- 初始化 ---------------- */
$('#go').onclick = doSearch;
$('#q').onkeydown = (ev) => { if (ev.key === 'Enter') doSearch(); };
/* ---- 索引的开始 / 暂停（项目主 2026-10-17 要求）----
   后端本来就有 start / cancel；cancel 的语义**正好等于暂停**：
   它是"停下当前任务"，已建好的进度都落了库 ✓，再 start 就是增量续跑
   （已索引的文件会被跳过，不会重做）✓ —— 所以不需要动后端 ✓ */
function setIndexButtons(running) {
  const s = $('#start-index'), c = $('#cancel-index');
  // ⚠ 2026-10-18 项目主："暂停按钮怎么没有了"
  //   以前是"跑着就隐藏开始、没跑就隐藏暂停"（互相顶替）✗ ——
  //   只要状态判断错一下，你想点的那个就不见了 ✗，而且还在跑的时候去别处找。
  //   现在：**两个都常显** ✓ 只按状态置灰 ✓ → 永远不会"消失"
  if (s) { s.hidden = false; s.disabled = !!running; }
  if (c) { c.hidden = false; c.disabled = !running; }
}

$('#cancel-index').onclick = async () => {
  if (!confirm('暂停索引？\n\n已建好的部分会保留；点「开始索引」接着跑，不会重做 ✓')) return;
  await post('/api/index/cancel').catch(() => {});
  toast('已暂停索引（进度已保留）');
  setTimeout(checkOnce, 600);
};

$('#start-index').onclick = async () => {
  try {
    const r = await post('/api/index/start', {});
    if (r && r.started === false) toast(r.message || '已经有任务在跑');
    else toast('已开始索引：已建好的部分会跳过 ✓');
    $('#progress-card').style.display = '';
    setIndexButtons(true);
    if (!polling) polling = setInterval(checkOnce, 1200);
    setTimeout(checkOnce, 600);
  } catch (e) { alert(e.message); }
};

$('#modal').onclick = (ev) => { if (ev.target.id === 'modal') { picked.clear(); $('#modal').classList.remove('show'); } };
$('#cfg-modal').onclick = (ev) => { if (ev.target.id === 'cfg-modal') $('#cfg-modal').classList.remove('show'); };

window.EG2OnSettingsChanged = () => {
  refreshHealth().catch(() => {});
  refreshFolders().catch(() => {});
  checkOnce();
};

// 初始化。
// 关键：**先把界面搭起来，再去拿数据**。
// 之前是 await 完三个接口才 bindTabs()，只要其中任何一个卡住
// （服务在扫描时请求会排队），分页就永远不会显示 —— 表现为
// 「一片空白 + 一直连接中」，而控制台里什么错都没有，极难排查。
(async () => {
  // 每一个绑定都单独 try —— 任何一个抛错都不能拖垮后面的初始化。
  // 之前 bindPeople 一旦出错，后面的 refreshHealth 就永远不执行，
  // 表现为「界面出来了但一直显示连接中」，而且控制台一片安静。
  const safe = (fn, name) => {
    try { fn(); } catch (e) {
      console.error('[初始化] ' + name + ' 失败：', e);
    }
  };
  safe(bindTabs, 'bindTabs');        // 第一件事：让界面可见
  safe(bindPeople, 'bindPeople');
  // 你能用的 API 模型（自己的 + 服务器公用/别人共享的）—— 普通用户靠它才能选到 llama.cpp ✓
  try { loadSrvApiModels().then(() => { try { renderApiList(); } catch (e) {} }); }
  catch (e) { /* 忽略 */ }
  safe(bindKb, 'bindKb');
  safe(bindVl, 'bindVl');
  safe(bindGpu, 'bindGpu');
  safe(bindRemote, 'bindRemote');
  safe(bindKbImages, 'bindKbImages');
  safe(() => EG2Settings.mount('settings'), 'EG2Settings');
  safe(bindCardFoldingLazy, 'bindCardFolding');

  // 下面这些互相独立，谁失败都不影响别人，也不阻塞后续
  refreshHealth().catch((e) => {
    console.error('[初始化] refreshHealth 失败：', e);
    const b = document.getElementById('health');
    if (b) { b.textContent = '连不上服务'; b.className = 'badge err'; }
  });
  refreshFolders().catch((e) => {
    const box = $('#folders');
    if (box) box.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  });
  pollStatus().catch(() => {});
  loadKbStatus().catch(() => {});
  // 人脸状态要 8~9 秒，只有停在「设置 → 人物命名」时才拉（以前一进页面就拉，
  // 一来就把服务堵住 → 状态徽章一直是「连接中…」）✓
  if (tabActive('settings') && settingsGroupOn('people')) loadPeoplePane();

  setInterval(() => refreshHealth().catch(() => {}), 15000);
  // 每 4 秒探一次，服务关掉后约 12 秒内给出提示
  setInterval(() => { pingAlive(); }, 4000);
  // 文件夹列表只在这两页用得上（搜索页的文件夹筛选 + 文件夹页）
  setInterval(() => {
    if (!polling && tabActive('search,folders')) refreshFolders().catch(() => {});
  }, 30000);
})();
;
/* 未登录就把人引到登录页（多人 P1 的入口；真正的**接口拦截**在 P2/P3 和权限一起上）。
   两条铁律：
     ① **失败放行** —— 状态接口拿不到就什么都不做，绝不能因为一次请求失败把人挡在界面外；
     ② **留跳过口** —— 地址后加 ?skip=1 直接进界面（`/static/login.html` 里也放了链接）。 */
(function () {
  var skip = location.search.indexOf('skip=1') >= 0;
  try {
    fetch('/api/auth/status', { credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;                           // 拿不到就放行
        if (d.need_bootstrap || !d.user) {
          if (!skip) location.href = '/static/login.html';
          return;
        }
        // 已登录：页头右侧挂个用户小牌，带「退出」按钮（之前只有登录页有，主界面没有）
        // ★ 2026-10-22：原来用 position:fixed 浮在右上角，会**压住**页头的
        //   「嵌入测试页 / API 文档」两个链接，看着就乱。现在塞进页头的 #eg2-user-slot ✓
        var desktopMode = null;      // null=还不知道；true=桌面版（关界面会停服务）；false=服务器
        var el = document.createElement('div');
        el.id = 'eg2-userchip';
        el.style.cssText = 'display:flex;gap:8px;align-items:center;';
        var who = document.createElement('span');
        who.textContent = '👤 ' + d.user.name
          + (d.user.role === 'admin' ? ' · 管理员' : '')
          + (d.user.state === 'active' ? '' : '（' + d.user.state + '）');
        var out = document.createElement('button');
        out.textContent = '退出';
        out.className = 'ghost tiny u-out';
        out.style.cursor = 'pointer';
        out.addEventListener('click', function () {
          fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' })
            .then(function () { location.href = '/static/login.html'; })
            .catch(function () { location.href = '/static/login.html'; });
        });
        el.appendChild(who);
        if (d.user.role === 'admin' && VIEW_MODE === 'admin') {
          // 管理员**版**才给这两个入口：
          //   · 个人版（本机）虽然登录的也是管理员，但这台机器就我一个人用，
          //     不需要"用户管理/共享设置" ✓（项目主 2026-10-20 定的）
          //   · 后端仍会再校验角色，前端只是不展示 ✓
          var link = document.createElement('a');
          link.href = '/static/users.html';
          link.textContent = '用户管理';
          link.setAttribute('data-admin-entry', '1');
          link.style.cssText = 'color:var(--accent,#5b9dff);text-decoration:none';
          el.appendChild(link);
          var link2 = document.createElement('a');
          link2.href = '/static/shared.html';
          link2.textContent = '共享设置';
          link2.title = '设置哪些文件夹是"共享"（所有人可见）、哪些是"私有"（只有归属人可见）';
          link2.setAttribute('data-admin-entry', '1');
          link2.style.cssText = 'color:var(--accent,#5b9dff);text-decoration:none';
          el.appendChild(link2);
        }
        var sb = document.createElement('button');
        sb.textContent = '同步';
        sb.className = 'ghost tiny';
        sb.title = '立刻同步一次（文件双向 + 推库 + 账号合并），不用等定时任务';
        sb.style.cursor = 'pointer';
        // 界面**不做**「关闭」按钮（2026-10-17 项目主定的方案）：
        //   · 有按钮就得同时去掉窗口的叉 —— 而那个叉由浏览器/系统画，网页去不掉 ✗
        //   · 所以走另一条：**叉掉界面 = 顺手停服务** ✓（见下面的 pagehide 通知）
        //   规则：只对**桌面版**生效 ✓（服务器上界面关了完全不影响服务 ✓）
        // 上色规则（一眼可辨）：
        //   红 = 有还没同步的改动   绿 = 刚同步成功   灰 = 正在同步
        var paint = function (kind, text) {
          sb.style.borderColor = kind === 'red' ? 'var(--bad,#ff8f8f)'
            : kind === 'green' ? 'var(--ok,#3ddc97)' : '';
          sb.style.color = kind === 'red' ? 'var(--bad,#ff8f8f)'
            : kind === 'green' ? 'var(--ok,#3ddc97)' : '';
          if (text) sb.textContent = text;
        };
        var check = function () {
          fetch('/api/admin/sync-check', { credentials: 'same-origin' })
            .then(function (r) { return r.json(); })
            .then(function (c) {
              // 记住"这台是不是桌面版"（有同步脚本 = 建库机）。**只有它才在关闭界面时停服务** ✓
              // 服务器上：界面关掉完全不影响服务（长期服务 ✓ 项目主要求）
              if (c) window.eg2SetDesktop(c.can_run_here !== false);   // 全局记录 + 起心跳 ✓
              if (c && c.can_run_here === false) {
                // 这台是服务器：把"更新/重建索引"按钮**变灰不可点**，
                // 鼠标移上去仍然显示说明（disabled 的按钮在 Chrome 里照样出 tooltip）。
                var guardTip = '这台是服务器，不能在这里建索引。\n\n'
                  + '索引以「建库的那台机器」为权威 —— 服务器上建完，'
                  + '下次同步会被整份覆盖（白做一场），而且服务器没显卡、慢几十倍。\n'
                  + '请在你自己的电脑上打开界面，在那里更新索引。';
                var mark = function () {
                  var bs = document.querySelectorAll('button');
                  for (var i = 0; i < bs.length; i++) {
                    var b = bs[i];
                    if (b.dataset.eg2IndexGuarded === '1') continue;
                    var s = (b.textContent || '') + ' ' + (b.title || '');
                    if (!/更新索引|增量更新|重建索引|全部重建|全部重扫|重新扫描/.test(s)) continue;
                    b.dataset.eg2IndexGuarded = '1';
                    b.disabled = true;
                    b.style.opacity = '.45';
                    b.style.cursor = 'not-allowed';
                    b.title = guardTip;
                  }
                };
                mark();
                if (!window.__eg2IndexMarkTimer) {
                  // 文件夹列表是动态渲染的，隔几秒补一遍（幂等，不会重复处理）
                  window.__eg2IndexMarkTimer = setInterval(mark, 3000);
                }
              }
              if (!c || sb.disabled) return;
              if (c.just_synced) paint('green', '同步✓');
              else if (c.dirty) paint('red', '同步');
              else paint('', '同步');
              sb.title = (c.reasons && c.reasons.length)
                ? '有改动待同步：' + c.reasons.join('、')
                : '已是最新' + (c.last_sync ? '（上次 ' + c.last_sync + '）' : '');
            })
            .catch(function () { /* 拿不到就保持原样 */ });
        };
        check();
        setInterval(check, 20000);
        sb.addEventListener('click', function () {
          sb.disabled = true;
          paint('', '同步中…');
          fetch('/api/admin/sync-now', { method: 'POST', credentials: 'same-origin' })
            .then(function (r) { return r.json(); })
            .then(function (d) {
              if (d && d.note) sb.title = d.note;
              if (d && d.requested) {
                // 服务器上没有同步脚本 → 这个按钮只是**记下请求** ✓
                // 以前这里会继续轮询状态，拿到 result_code=null（从未执行 ✗）
                // 就当成失败涂成红色 ✗ → 用户以为"同步报错、同步不了" ✓
                // 现在如实显示：橙色 + 告诉他在本机点 ✓
                sb.disabled = false;
                sb.style.borderColor = 'var(--warn,#ffb454)';
                sb.style.color = 'var(--warn,#ffb454)';
                sb.textContent = '已请求同步';
                sb.title = '已在服务器记下同步请求 ✓\n'
                  + '本机（建库那台机器）最多 1 分钟内自动执行 ✓\n'
                  + '执行完在本机界面会显示「同步✓」；服务器这边保持橙色表示"请求已收到"。';
                return;
              }
              var tick = setInterval(function () {
                fetch('/api/admin/sync-status', { credentials: 'same-origin' })
                  .then(function (r) { return r.json(); })
                  .then(function (s) {
                    if (s.running) {
                      paint('', '同步中 ' + Math.round(s.elapsed || 0) + 's');
                      return;
                    }
                    clearInterval(tick);
                    sb.disabled = false;
                    if (s.result_code === 0) {
                      paint('green', '同步✓');
                      setTimeout(function () { paint('', '同步'); }, 6000);
                    } else {
                      paint('red', '同步');
                    }
                  })
                  .catch(function () { clearInterval(tick); sb.disabled = false; paint('', '同步'); });
              }, 1500);
            })
            .catch(function () { sb.disabled = false; paint('', '同步'); });
        });
        el.appendChild(sb);
        el.appendChild(out);
        // 有 #eg2-user-slot 就塞进页头（正常情况）；老页面没有这个槽位时退回浮动定位 ✓
        var slot = document.getElementById('eg2-user-slot');
        if (slot) {
          el.style.cssText += 'padding:2px 4px 2px 10px;border-radius:999px;'
            + 'background:var(--panel2);border:1px solid var(--line);font-size:12.5px;';
          slot.appendChild(el);
        } else {
          el.style.cssText += 'position:fixed;right:12px;top:10px;z-index:9998;'
            + 'padding:5px 10px;border-radius:999px;font-size:13px;'
            + 'background:rgba(22,27,34,.92);border:1px solid var(--line,#2a3340);'
            + 'color:var(--fg,#e7ecf5)';
          document.body.appendChild(el);
        }
      })
      .catch(function () { /* 拿不到就放行 */ });
  } catch (e) { /* 放行 */ }
})();

/* ---------------- 🏷 版本号徽标（2026-10-23 项目主要求）----------------
 * "界面上应该相应标出版本号，不然都分不清版本、需不需要升级"
 *
 * 做法：
 *   · 先用手里的 EG2_VER 立刻画出来（不等网络）
 *   · 再问一次 /health 拿"正在跑的服务"的 version / build
 *   · 两个不一致 → 说明页面是旧缓存（服务已升级）→ 徽标变黄、文案变
 *     "v新 ← 刷新"，点一下直接强刷 ✓
 *   · 点一下徽标本身：显示版本 + build 时间（不弹窗打断，用 tooltip + 状态栏）
 */
(function paintVersionChip() {
  var el = document.getElementById('ver-chip');
  if (!el) return;
  var pageVer = (typeof EG2_VER !== 'undefined') ? EG2_VER : '';
  var build = (typeof EG2_BUILD !== 'undefined') ? EG2_BUILD : '';
  var srvVer = '';
  var srvBuild = '';

  function draw(warn) {
    if (warn && srvVer) {
      el.textContent = pageVer
        ? ('v' + pageVer + ' → v' + srvVer + ' 刷新')
        : ('v' + srvVer + ' 刷新');
      el.classList.add('warn');
      el.title = '服务已经升到 v' + srvVer + '（build ' + srvBuild + '），'
        + '页面还是 v' + pageVer + ' 的旧缓存 —— 点一下强制刷新';
    } else {
      el.textContent = 'v' + (srvVer || pageVer);
      el.classList.remove('warn');
      el.title = '宇之语·灵犀 v' + (srvVer || pageVer)
        + (srvBuild || build ? ('　build ' + (srvBuild || build)) : '')
        + '　（点一下可复制版本信息）';
    }
  }

  draw(false);

  try {
    fetch('/health', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        srvVer = String((j && j.version) || '');
        srvBuild = String((j && j.build) || '');
        if (!srvVer) { draw(false); return; }
        draw(!!pageVer && pageVer !== srvVer);
      })
      .catch(function () { /* 拿不到就显示页面自己的版本 */ });
  } catch (e) { /* 忽略 */ }

  el.addEventListener('click', function (ev) {
    ev.preventDefault();
    if (el.classList.contains('warn')) { location.reload(); return; }
    var info = '宇之语·灵犀 v' + (srvVer || pageVer)
      + '\nbuild ' + (srvBuild || build)
      + (srvVer
        ? '\n服务端版本：v' + srvVer
        : '\n（没问到服务端版本）');
    try { navigator.clipboard.writeText(info); } catch (e2) { /* 忽略 */ }
    el.title = info.replace(/\n/g, '　');
    // 顺手在状态徽标那行给个提示，不打断操作
    var st = document.getElementById('status');
    if (st) {
      var old = st.textContent;
      st.textContent = '版本信息已复制';
      setTimeout(function () { st.textContent = old; }, 1600);
    }
  });
})();

/* ---------------- 📐 聊天页自适应高度（2026-10-23 项目主要求）----------------
 * "对话做成内滚动，别让整个页面无限延长、输入框被挤到最下面"
 *
 * 做法：只给「聊天」这一页设高 = 视口高度 − 这一页离页面顶端的距离 − 一点余量。
 *   页头 / tab 高度都是会变的（用户牌、窗口宽度都能影响排版），
 *   所以不写死数字，直接量 `getBoundingClientRect().top` ✓
 *   窗口缩放、切到聊天页时各重算一次。
 */
(function fitChatPane() {
  function pane() { return document.querySelector('.tabpane[data-pane="chat2"]'); }

  function apply() {
    var p = pane();
    if (!p || !p.classList.contains('on')) return;
    var top = p.getBoundingClientRect().top + window.scrollY;
    var gap = 18;
    var h = Math.max(420, Math.round(window.innerHeight - top - gap));
    p.style.height = h + 'px';
    // ★ 二次校正：页脚/外边距之类也会把页面撑高几像素 —— 多出来的高度从聊天页里扣掉，
    //   保证"整页不出现滚动条，只有消息区滚"（实测 1440×900 曾多出 52px）
    var over = document.documentElement.scrollHeight - window.innerHeight;
    if (over > 0) {
      p.style.height = Math.max(380, h - over - 2) + 'px';
    }
  }

  window.__fitChatPane = apply;          // 切标签时由主脚本调
  try { apply(); } catch (e) { /* 忽略 */ }
  window.addEventListener('resize', function () {
    try { apply(); } catch (e) { /* 忽略 */ }
  });
  // 首次渲染后（字体/图片撑开高度）再量一次
  setTimeout(function () { try { apply(); } catch (e) { /* 忽略 */ } }, 600);
})();

/* ---------------- 🔄 重新检查目录 / ♻ 重启服务（2026-10-27）----------------
 * 项目主："服务器版的如何手动重启？因为 NAS 重启滞后，导致网络连接 NAS 硬盘显示不存在"
 *
 * 两件事分开做：
 *   · 重新检查目录 —— 轻量、随时点：立刻重查每个文件夹在不在、网络盘挂不挂得上，
 *     顺手刷新列表（不用等 30 秒轮询）✓
 *   · 重启服务 —— 只在**服务器版的管理员**界面上出现：NAS 重启后句柄失效时，
 *     光"重查"可能还是假的，重启进程最干脆。点了会先回响应再退出，systemd 拉起来 ✓
 */
(function bindFolderRecovery() {
  function q(id) { return document.getElementById(id); }

  var btn = q('folders-recheck');
  var note = q('recheck-note');
  if (btn) {
    btn.addEventListener('click', function () {
      btn.disabled = true;
      if (note) { note.textContent = '正在检查…'; note.style.color = ''; }
      // 用 GET：POST + JSON body 在这个页面上会被浏览器挂住（实测请求发出了、
      // 但一直等不到响应），GET 跟其它轮询一样稳 ✓
      api('/api/folders/recheck').then(function (r) {
        var dead = r.dead || [];
        if (note) {
          if (dead.length) {
            note.innerHTML = '<span style="color:var(--warn)">'
              + r.alive + '/' + r.total + ' 个可用；不可用：'
              + dead.map(function (x) {
                var why = x.reason === 'windows_path' ? '（本机 Windows 路径，服务器上没有）'
                  : x.reason === 'not_mounted' ? '（网络盘挂载点不在了）'
                    : x.reason === 'timeout' ? '（检查超时，NAS 可能没响应）'
                      : '（目录不存在）';
                return esc(x.path) + why;
              }).join('、')
              + '　<span style="opacity:.75">NAS 没起来就等一会儿再点；'
              + '一直这样可以在服务器上重启服务或 sudo mount -o remount</span></span>';
          } else {
            note.innerHTML = '<span style="color:var(--ok)">'
              + r.alive + '/' + r.total + ' 个都正常 ✓</span>';
          }
        }
        // 结果一拿到就把按钮放开；列表刷新（可能几秒）在后台继续 ✓
        btn.disabled = false;
        return refreshFolders();
      }).catch(function (e) {
        if (note) note.innerHTML = '<span style="color:var(--bad)">检查失败：'
          + esc(e.message) + '</span>';
      }).then(function () { btn.disabled = false; });
    });
  }

  var rb = q('svc-restart');
  if (rb) {
    rb.addEventListener('click', function () {
      if (!confirm('确定重启灵犀服务？\n\n'
        + '· 大约 25~40 秒不能访问（模型要重新加载）\n'
        + '· 网络盘（NAS）卡住时，这个最有效')) return;
      rb.disabled = true;
      rb.textContent = '♻ 正在重启…';
      post('/api/admin/restart', { confirm: 'restart' }, 'POST').then(function () {
        var t0 = Date.now();
        var timer = setInterval(function () {
          fetch('/health', { cache: 'no-store' }).then(function (r) {
            if (r.ok) { clearInterval(timer); location.reload(); }
          }).catch(function () { /* 还没起来，继续等 */ });
          if (Date.now() - t0 > 90000) {
            clearInterval(timer);
            rb.textContent = '♻ 重启超时，请手动刷新';
            rb.disabled = false;
          }
        }, 3000);
      }).catch(function (e) {
        alert('重启失败：' + e.message);
        rb.disabled = false;
        rb.textContent = '♻ 重启服务';
      });
    });
  }
})();
