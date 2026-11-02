/* EmbeddingGemma 2 —— 可复用的「运行模式 / 设置」面板
   用法：<div id="settings"></div> 然后 EG2Settings.mount('settings')  */
(function () {
  const CSS = `
  .eg2s{font:14px/1.6 "Segoe UI","Microsoft YaHei",system-ui,sans-serif;color:var(--fg,#e7ecf5)}
  .eg2s *{box-sizing:border-box}
  .eg2s .head{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .eg2s .sum{font-size:13px;color:var(--fg,#e7ecf5)}
  .eg2s .sum b{color:var(--accent,#5b9dff);font-weight:600}
  .eg2s .modes{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px;margin-top:12px}
  .eg2s .mode{background:var(--panel2,#1e2739);border:1px solid var(--line,#2b3648);border-radius:10px;
              padding:12px 14px;cursor:pointer;transition:.15s;position:relative}
  .eg2s .mode:hover{border-color:var(--accent,#5b9dff)}
  .eg2s .mode.active{border-color:var(--accent,#5b9dff);box-shadow:0 0 0 1px var(--accent,#5b9dff) inset;
                     background:rgba(91,157,255,.10)}
  .eg2s .mode .t{font-weight:600;display:flex;align-items:center;gap:8px}
  .eg2s .mode .d{font-size:12px;color:var(--muted,#93a1b8);margin-top:5px}
  .eg2s .mode .tick{font-size:11px;color:var(--accent,#5b9dff)}
  .eg2s .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:14px}
  .eg2s .msg{font-size:12.5px;color:var(--muted,#93a1b8)}
  .eg2s .msg.ok{color:var(--ok,#3ddc97)}
  .eg2s .msg.warn{color:var(--warn,#ffb454)}
  .eg2s .msg.bad{color:var(--bad,#ff8f8f)}
  .eg2s button{font:inherit}
  .eg2s .btn{background:var(--accent,#5b9dff);color:#fff;border:0;border-radius:8px;padding:8px 16px;
             font-weight:600;cursor:pointer}
  .eg2s .btn.ghost{background:var(--panel2,#1e2739);border:1px solid var(--line,#2b3648);
                   color:var(--fg,#e7ecf5);font-weight:400}
  .eg2s .btn:disabled{opacity:.45;cursor:default}
  .eg2s .groups{margin-top:14px;display:grid;gap:12px}
  .eg2s .group{background:var(--panel2,#1e2739);border:1px solid var(--line,#2b3648);
               border-radius:10px;padding:12px 14px}
  .eg2s .group>.h{font-size:12.5px;color:var(--muted,#93a1b8);margin-bottom:10px}
  .eg2s .item{display:flex;gap:12px;align-items:center;padding:7px 0;border-top:1px dashed
              rgba(255,255,255,.06);flex-wrap:wrap}
  .eg2s .item:first-of-type{border-top:0}
  .eg2s .item .lab{flex:0 0 190px;font-size:13px}
  .eg2s .item .ctl{flex:0 0 auto}
  .eg2s .item .hp{flex:1;min-width:140px;font-size:11.5px;color:var(--muted,#93a1b8)}
  .eg2s select,.eg2s input[type=number]{background:var(--bg,#0f1420);border:1px solid var(--line,#2b3648);
      color:var(--fg,#e7ecf5);border-radius:7px;padding:6px 9px;font:inherit;outline:none;min-width:110px}
  .eg2s input[type=number]{width:92px}
  .eg2s select:focus,.eg2s input:focus{border-color:var(--accent,#5b9dff)}
  .eg2s .sw{position:relative;display:inline-block;width:46px;height:25px;flex:0 0 auto}
  .eg2s .sw input{opacity:0;width:0;height:0}
  .eg2s .sl{position:absolute;inset:0;background:#2b3648;border-radius:99px;transition:.2s;cursor:pointer}
  .eg2s .sl:before{content:"";position:absolute;height:19px;width:19px;left:3px;top:3px;background:#fff;
                   border-radius:50%;transition:.2s}
  .eg2s .sw input:checked+.sl{background:var(--ok,#3ddc97)}
  .eg2s .sw input:checked+.sl:before{transform:translateX(21px)}
  .eg2s .badge{font-size:11px;padding:1px 7px;border-radius:99px;background:#233046;
               color:var(--muted,#93a1b8);margin-left:6px}
  .eg2s .badge.reload{background:rgba(255,180,84,.18);color:var(--warn,#ffb454)}
  .eg2s .badge.reindex{background:rgba(255,120,120,.18);color:var(--bad,#ff8f8f)}
  .eg2s .banner{margin-top:12px;padding:10px 13px;border-radius:9px;font-size:13px;display:none;
                align-items:center;gap:12px;flex-wrap:wrap}
  .eg2s .banner.show{display:flex}
  .eg2s .banner.reload{background:rgba(255,180,84,.13);border:1px solid rgba(255,180,84,.35)}
  .eg2s .banner.reindex{background:rgba(255,120,120,.13);border:1px solid rgba(255,120,120,.35)}
  .eg2s .spin{display:inline-block;width:12px;height:12px;border:2px solid rgba(255,255,255,.3);
              border-top-color:#fff;border-radius:50%;animation:eg2spin .8s linear infinite;vertical-align:-2px}
  @keyframes eg2spin{to{transform:rotate(360deg)}}
  `;

  const EFFECT_LABEL = { reload: '需重载模型', reindex: '需重建索引' };

  function injectCss() {
    if (document.getElementById('eg2s-css')) return;
    const style = document.createElement('style');
    style.id = 'eg2s-css';
    style.textContent = CSS;
    document.head.appendChild(style);
  }

  async function api(path, opt) {
    const r = await fetch(path, opt);
    const t = await r.text();
    let j = null;
    try { j = JSON.parse(t); } catch (e) { j = { detail: t }; }
    if (!r.ok) throw new Error(j.detail || t || ('HTTP ' + r.status));
    return j;
  }
  const post = (p, body) => api(p, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {})
  });
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  function mount(containerId, options) {
    injectCss();
    const opts = options || {};
    const collapsed = opts.collapsed !== false;      // 默认收起，页面更紧凑
    const box = document.getElementById(containerId);
    if (!box) return;
    box.classList.add('eg2s');
    box.innerHTML = `
      <div class="banner" id="eg2s-banner"></div>
      <div class="head">
        <span class="sum" id="eg2s-sum">读取中…</span>
        <span style="flex:1"></span>
        <span class="msg" id="eg2s-msg"></span>
        <button class="btn ghost" id="eg2s-fold">${collapsed ? '展开调整 ▾' : '收起 ▴'}</button>
      </div>
      <div id="eg2s-main" ${collapsed ? 'hidden' : ''}>
        <div class="modes" id="eg2s-modes"></div>
        <div class="row">
          <button class="btn ghost" id="eg2s-toggle">详细设置 ▾</button>
          <span style="flex:1"></span>
          <button class="btn" id="eg2s-apply" disabled>应用设置</button>
        </div>
        <div class="groups" id="eg2s-detail" hidden></div>
      </div>`;

    let state = null;               // 服务端返回的快照
    let draft = {};                 // 用户改过、还没提交的值
    let reloadPolling = null;

    const $ = (s) => box.querySelector(s);
    const msg = (text, cls) => { const el = $('#eg2s-msg'); el.textContent = text || ''; el.className = 'msg ' + (cls || ''); };

    function dirtyKeys() { return Object.keys(draft).filter((k) => draft[k] !== state.values[k]); }

    function syncDirty() {
      const n = dirtyKeys().length;
      $('#eg2s-apply').disabled = n === 0;
      if (n) msg(`已修改 ${n} 项，点「应用设置」保存`, 'warn');
    }

    function renderModes() {
      $('#eg2s-modes').innerHTML = state.presets.map((p) => `
        <div class="mode ${state.active_preset === p.id ? 'active' : ''}" data-preset="${p.id}">
          <div class="t">${esc(p.name)}
            ${state.active_preset === p.id ? '<span class="tick">✓ 当前</span>' : ''}</div>
          <div class="d">${esc(p.desc)}</div>
        </div>`).join('') + `
        <div class="mode ${state.active_preset ? '' : 'active'}" data-preset="">
          <div class="t">自定义
            ${state.active_preset ? '' : '<span class="tick">✓ 当前</span>'}</div>
          <div class="d">下面的每一项都可以单独调</div>
        </div>`;
    }

    function control(item) {
      const value = draft[item.key] !== undefined ? draft[item.key] : state.values[item.key];
      if (item.type === 'switch') {
        return `<label class="sw"><input type="checkbox" data-key="${item.key}"
          ${value === 'yes' ? 'checked' : ''}><span class="sl"></span></label>`;
      }
      if (item.type === 'select') {
        return `<select data-key="${item.key}">${item.options.map((o) =>
          `<option value="${esc(o.value)}" ${String(o.value) === String(value) ? 'selected' : ''}>
            ${esc(o.label)}</option>`).join('')}</select>`;
      }
      return `<input type="number" data-key="${item.key}" value="${esc(value)}"
        min="${item.min}" max="${item.max}" step="${item.step || 1}">`;
    }

    function renderDetail() {
      $('#eg2s-detail').innerHTML = state.schema.map((group) => `
        <div class="group">
          <div class="h">${esc(group.group)}${group.note ? ' · ' + esc(group.note) : ''}</div>
          ${group.items.map((item) => `
            <div class="item">
              <span class="lab">${esc(item.label)}
                ${item.effect !== 'live'
                  ? `<span class="badge ${item.effect}">${EFFECT_LABEL[item.effect] || ''}</span>` : ''}
              </span>
              <span class="ctl">${control(item)}</span>
              <span class="hp">${esc(item.help || '')}</span>
            </div>`).join('')}
        </div>`).join('');
    }

    function banner(kind, text, buttons) {
      const el = $('#eg2s-banner');
      el.className = 'banner show ' + kind;
      el.innerHTML = `<span>${esc(text)}</span><span style="flex:1"></span>` +
        buttons.map((b) => `<button class="btn ${b.ghost ? 'ghost' : ''}" data-act="${b.act}">${esc(b.label)}</button>`).join('');
    }

    function hideBanner() { $('#eg2s-banner').className = 'banner'; }

    function renderSummary() {
      const preset = state.presets.find((p) => p.id === state.active_preset);
      const modeName = preset ? preset.name : '自定义';
      const idx = state.index || {};
      $('#eg2s-sum').innerHTML =
        `当前模式：<b>${esc(modeName)}</b> · ${esc(state.model.device || '')} / ${esc(state.model.dtype || '')}` +
        ` · ${esc(state.values['model.modality'] || '')}` +
        (idx.files !== undefined ? ` · 已索引 ${idx.files} 个文件` : '');
    }

    async function load() {
      state = await api('/api/settings');
      draft = {};
      renderModes();
      renderDetail();
      renderSummary();
      syncDirty();
      if (state.model.reloading) {
        banner('reload', '模型正在重新加载…', []);
        startReloadPolling();
      } else {
        hideBanner();
      }
    }

    function startReloadPolling() {
      if (reloadPolling) return;
      reloadPolling = setInterval(async () => {
        try {
          const st = await api('/api/model/status');
          if (st.reloading) {
            banner('reload', `模型正在重新加载…（${st.device || ''} ${st.dtype || ''}）`, []);
            msg('模型重载中，搜索会暂时不可用', 'warn');
          } else {
            clearInterval(reloadPolling);
            reloadPolling = null;
            await load();
            if (st.error) msg('重载失败：' + st.error, 'bad');
            else msg(`模型已就绪：${st.device} / ${st.dtype} / ${st.modality}`, 'ok');
            if (window.EG2OnSettingsChanged) window.EG2OnSettingsChanged();
          }
        } catch (e) { /* 服务可能正在重启，继续等 */ }
      }, 2000);
    }

    // ---- 事件 ----
    box.addEventListener('click', async (ev) => {
      const mode = ev.target.closest('[data-preset]');
      if (mode) {
        const id = mode.dataset.preset;
        if (!id) { $('#eg2s-detail').hidden = false; $('#eg2s-toggle').textContent = '详细设置 ▴'; return; }
        try {
          msg('正在切换模式…');
          const r = await post('/api/settings', { preset: id });
          await load();
          if (r.need_reload) {
            banner('reload', r.message || '需要重新加载模型', [
              { act: 'reload', label: '立即重载模型' },
              { act: 'later', label: '稍后', ghost: true }]);
            msg('已切换到「' + mode.querySelector('.t').textContent.trim().replace('✓ 当前', '') + '」，需重载模型生效', 'warn');
          } else if (r.need_reindex) {
            banner('reindex', r.message || '需要重建索引', [
              { act: 'reindex', label: '全部重建索引' }, { act: 'later', label: '稍后', ghost: true }]);
          } else {
            msg('模式已切换，立即生效', 'ok');
          }
        } catch (e) { msg(e.message, 'bad'); }
        return;
      }

      const act = ev.target.closest('[data-act]');
      if (act) {
        const kind = act.dataset.act;
        if (kind === 'later') { hideBanner(); return; }
        if (kind === 'reload') {
          try {
            hideBanner();
            await post('/api/model/reload');
            msg('正在重新加载模型，约 40 秒…', 'warn');
            startReloadPolling();
          } catch (e) { msg(e.message, 'bad'); }
          return;
        }
        if (kind === 'reindex') {
          try {
            hideBanner();
            await post('/api/index/start', { force: true });
            msg('已开始重建索引，可在下方查看进度', 'ok');
            if (window.EG2OnSettingsChanged) window.EG2OnSettingsChanged();
          } catch (e) { msg(e.message, 'bad'); }
          return;
        }
      }

      if (ev.target.id === 'eg2s-fold') {
        const main = $('#eg2s-main');
        main.hidden = !main.hidden;
        ev.target.textContent = main.hidden ? '展开调整 ▾' : '收起 ▴';
        return;
      }

      if (ev.target.id === 'eg2s-toggle') {
        const d = $('#eg2s-detail');
        d.hidden = !d.hidden;
        ev.target.textContent = d.hidden ? '详细设置 ▾' : '详细设置 ▴';
        return;
      }

      if (ev.target.id === 'eg2s-apply') {
        const keys = dirtyKeys();
        if (!keys.length) return;
        const values = {};
        keys.forEach((k) => { values[k] = draft[k]; });

        const dimChanged = values['search.index_dim'] !== undefined;
        if (dimChanged && !confirm('修改索引向量维度会清空现有索引，需要重新建立。确定继续吗？')) return;

        try {
          $('#eg2s-apply').disabled = true;
          const r = await post('/api/settings', { values });
          await load();
          if (r.need_reindex) {
            banner('reindex', r.message || '索引已清空，需要重建', [
              { act: 'reindex', label: '全部重建索引' }, { act: 'later', label: '稍后', ghost: true }]);
            msg('设置已保存，但需要重建索引', 'warn');
          } else if (r.need_reload) {
            banner('reload', r.message || '需要重新加载模型', [
              { act: 'reload', label: '立即重载模型' }, { act: 'later', label: '稍后', ghost: true }]);
            msg('设置已保存，需重载模型生效', 'warn');
          } else {
            msg('设置已保存，立即生效', 'ok');
          }
          if (window.EG2OnSettingsChanged) window.EG2OnSettingsChanged();
        } catch (e) { msg(e.message, 'bad'); }
      }
    });

    box.addEventListener('change', (ev) => {
      const el = ev.target.closest('[data-key]');
      if (!el) return;
      const key = el.dataset.key;
      draft[key] = el.type === 'checkbox' ? (el.checked ? 'yes' : 'no') : el.value;
      syncDirty();
    });

    box.addEventListener('input', (ev) => {
      const el = ev.target.closest('input[type=number][data-key]');
      if (!el) return;
      draft[el.dataset.key] = el.value;
      syncDirty();
    });

    load().catch((e) => msg('读取设置失败：' + e.message, 'bad'));
    return { reload: load };
  }

  window.EG2Settings = { mount };
})();
