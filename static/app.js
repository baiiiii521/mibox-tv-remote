/* 电视遥控器 —— 前端逻辑 */
(() => {
  'use strict';
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const dot = $('#dot');
  const devName = $('#devName');
  const devStatus = $('#devStatus');
  const appsEl = $('#apps');
  const toastEl = $('#toast');
  const banner = $('#banner');

  let online = false;
  let appsRendered = false;
  let errShownAt = 0;
  let lastPkg = '';
  let pollTimer = null;

  /* ---------------- toast ---------------- */
  let toastTimer;
  function toast(msg, isErr = false) {
    toastEl.textContent = msg;
    toastEl.classList.toggle('err', isErr);
    toastEl.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove('show'), isErr ? 4000 : 1400);
  }

  /* ---------------- API ---------------- */
  async function api(path, body, timeout = 12000) {
    const opt = body === undefined ? {} : {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    };
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeout);
    try {
      const res = await fetch(path, { ...opt, signal: ctrl.signal, cache: 'no-store' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
      return data;
    } finally {
      clearTimeout(timer);
    }
  }

  function reportError(err) {
    const now = Date.now();
    if (now - errShownAt < 2500) return;
    errShownAt = now;
    toast(err && err.name === 'AbortError' ? '电视盒子没有响应' : (err.message || '操作失败'), true);
    refresh();
  }

  async function sendKey(key, n = 1) {
    try {
      await api('/api/key', { key, n }, 9000);
      setOnline(true);
    } catch (e) {
      setOnline(false);
      reportError(e);
    }
  }

  /* ---------------- 按压绑定 ----------------
     离散按钮一律用 click：手指在屏幕上滑动（滚页面）时浏览器不会派发 click，
     这样"划一下"就不会误触发按键/开 App。长按键才用 pointerdown（要立刻有反馈），
     并且手指移动超过 12px 就取消，避免滑动时连发。 */
  function bindTap(el, fire) {
    el.addEventListener('click', (ev) => {
      ev.preventDefault();
      el.classList.add('pressed');
      setTimeout(() => el.classList.remove('pressed'), 120);
      if (navigator.vibrate) navigator.vibrate(6);
      fire();
    });
  }

  function bindHold(el, fire) {
    let down = false, moved = false, x0 = 0, y0 = 0, delayTimer, repeatTimer;
    const stop = () => {
      if (!down) return;
      down = false;
      el.classList.remove('pressed');
      clearTimeout(delayTimer);
      clearInterval(repeatTimer);
    };
    el.addEventListener('pointerdown', (ev) => {
      if (ev.pointerType === 'mouse' && ev.button !== 0) return;
      down = true;
      moved = false;
      x0 = ev.clientX; y0 = ev.clientY;
      el.classList.add('pressed');
      if (navigator.vibrate) navigator.vibrate(6);
      fire();
      delayTimer = setTimeout(() => {
        repeatTimer = setInterval(() => { if (down && !moved) fire(); }, 160);
      }, 420);
    });
    el.addEventListener('pointermove', (ev) => {
      if (!down) return;
      if (Math.abs(ev.clientX - x0) > 12 || Math.abs(ev.clientY - y0) > 12) {
        moved = true;
        stop();
      }
    });
    ['pointerup', 'pointercancel', 'pointerleave', 'blur'].forEach(ev =>
      el.addEventListener(ev, stop));
    el.addEventListener('contextmenu', e => e.preventDefault());
  }

  function bindButton(el, fire) {
    if (el.hasAttribute('data-hold')) bindHold(el, fire);
    else bindTap(el, fire);
  }

  $$('[data-key]').forEach(el => {
    const key = el.dataset.key;
    bindButton(el, () => sendKey(key));
  });

  /* ---------------- 状态 ---------------- */
  function setOnline(ok) {
    online = ok;
    dot.classList.toggle('on', ok);
    dot.classList.toggle('off', !ok);
    banner.hidden = ok;
    if (!ok) devStatus.textContent = '未连接 · 点此重连';
  }

  function setStatus(s) {
    if (s.model) devName.textContent = s.model;
    setOnline(!!s.online);
    if (s.online) {
      const ms = s.key_ms || s.latency_ms || 0;
      const app = (s.current_name && s.current_name !== '—') ? ' · ' + s.current_name : '';
      devStatus.textContent = '在线 · ' + (ms ? ms + 'ms' : '就绪') + app;
    }
    if (Array.isArray(s.apps) && s.apps.length && !appsRendered) {
      renderApps(s.apps);
      appsRendered = true;
    }
    if ((s.current_pkg || '') !== lastPkg) {
      lastPkg = s.current_pkg || '';
      markActive(lastPkg);
    }
  }

  function markActive(pkg) {
    $$('.app').forEach(el => el.classList.toggle('active', el.dataset.pkg === pkg));
  }

  function scheduleNext(delay) {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(refresh, delay);
  }

  async function refresh() {
    let delay = 6000;
    try {
      const s = await api('/api/status', undefined, 9000);
      setStatus(s);
      delay = s.online ? 6000 : 3000;
    } catch (e) {
      setOnline(false);
      devStatus.textContent = '桥接服务未响应';
      delay = 3000;
    }
    if (!document.hidden) scheduleNext(delay);
  }

  async function reconnect() {
    devStatus.textContent = '正在重连…';
    try {
      await api('/api/connect', {}, 20000);
      toast('已重新连接');
    } catch (e) {
      try {
        const d = await api('/api/discover', {}, 40000);
        toast(d.found ? '盒子换地址了，已找到 ' + d.found : '没找到盒子，检查电视是否开机', !d.found);
      } catch (e2) {
        toast('重连失败，电视可能关机了', true);
      }
    }
    refresh();
  }

  $('#device').addEventListener('click', () => { if (!online) reconnect(); });
  banner.addEventListener('click', reconnect);

  /* ---------------- 应用 ---------------- */
  function renderApps(apps) {
    appsEl.innerHTML = '';
    apps.forEach(app => {
      const btn = document.createElement('button');
      btn.className = 'app';
      btn.dataset.pkg = app.pkg;
      btn.style.setProperty('--h', app.hue || 210);
      btn.innerHTML = '<span class="ic" aria-hidden="true">' + app.name.slice(0, 1) + '</span>' +
        '<span class="nm">' + app.name + '</span>';
      bindButton(btn, async () => {
        try {
          await api('/api/app', { pkg: app.pkg }, 15000);
          toast('打开 ' + app.name);
          setTimeout(refresh, 1200);
        } catch (e) { reportError(e); }
      });
      appsEl.appendChild(btn);
    });
  }

  /* ---------------- 文字输入 ---------------- */
  $('#textForm').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const input = $('#textInput');
    const text = input.value.trim();
    if (!text) return;
    if (/[^\x20-\x7E]/.test(text)) {
      toast('只能输入英文和数字', true);
      return;
    }
    try {
      await api('/api/text', { text });
      toast('已输入到电视');
      input.value = '';
      input.blur();
    } catch (e) { reportError(e); }
  });

  /* ---------------- 截屏面板 ---------------- */
  const sheet = $('#sheet');
  const shot = $('#shot');
  const shotLoading = $('#shotLoading');
  let shotCtl = null;

  function loadShot() {
    shot.classList.remove('ready');
    shotLoading.hidden = false;
    shotLoading.innerHTML = '正在截取电视画面…<br><small>约需 5 秒</small>';
    shot.removeAttribute('src');
    const ctrl = new AbortController();
    shotCtl = ctrl;
    fetch('/api/shot?t=' + Date.now(), { cache: 'no-store', signal: ctrl.signal })
      .then(res => {
        if (!res.ok) throw new Error('截屏失败 HTTP ' + res.status);
        return res.blob();
      })
      .then(blob => {
        if (shotCtl !== ctrl) return;
        shot.src = URL.createObjectURL(blob);
        shot.classList.add('ready');
        shotLoading.hidden = true;
      })
      .catch(err => {
        if (err.name === 'AbortError') return;
        shotLoading.textContent = '截屏失败：' + err.message;
        toast('截屏失败', true);
      });
  }

  function openSheet() {
    sheet.hidden = false;
    document.body.style.overflow = 'hidden';
    loadShot();
  }
  function closeSheet() {
    sheet.hidden = true;
    document.body.style.overflow = '';
    if (shotCtl) shotCtl.abort();
    shotCtl = null;
  }
  $('#btnShot').addEventListener('click', openSheet);
  $('#btnClose').addEventListener('click', closeSheet);
  $('#btnReload').addEventListener('click', loadShot);
  sheet.addEventListener('click', (ev) => { if (ev.target === sheet) closeSheet(); });

  /* ---------------- 添加到主屏幕提示 ---------------- */
  const hint = $('#installHint');
  try {
    if (!localStorage.getItem('tvremote.hinted') && !navigator.standalone) hint.hidden = false;
  } catch (e) { /* 无痕模式忽略 */ }
  hint.addEventListener('click', () => {
    hint.hidden = true;
    try { localStorage.setItem('tvremote.hinted', '1'); } catch (e) {}
  });

  /* ---------------- 其他 ---------------- */
  document.addEventListener('gesturestart', e => e.preventDefault());
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refresh();
  });
  window.addEventListener('blur', () => {
    $$('.pressed').forEach(el => el.classList.remove('pressed'));
  });
  window.addEventListener('online', refresh);

  /* ---------------- 盒子 / 电视 双模式 ---------------- */
  const mainEl = $('main');
  Array.from(mainEl.children).forEach(el => {
    if (el.id !== 'modes') el.classList.add('box-only');
  });

  const VIDEO_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 9.5h3.5L12.5 5v14L7.5 14.5H5z"/></svg>';

  const tvPanel = document.createElement('div');
  tvPanel.className = 'tv-only off';
  tvPanel.innerHTML = [
    '<section class="hero"><div class="wheel">',
    '<button class="dir up" data-tvkey="UP" data-hold aria-label="上"><svg viewBox="0 0 24 24"><path d="M6 15l6-7 6 7"/></svg></button>',
    '<button class="dir left" data-tvkey="LEFT" data-hold aria-label="左"><svg viewBox="0 0 24 24"><path d="M15 6l-7 6 7 6"/></svg></button>',
    '<button class="dir right" data-tvkey="RIGHT" data-hold aria-label="右"><svg viewBox="0 0 24 24"><path d="M9 6l7 6-7 6"/></svg></button>',
    '<button class="dir down" data-tvkey="DOWN" data-hold aria-label="下"><svg viewBox="0 0 24 24"><path d="M6 9l6 7 6-7"/></svg></button>',
    '<button class="ok" data-tvkey="OK" aria-label="确认">OK</button>',
    '</div></section>',
    '<section class="volbar">',
    '<button class="vol" data-tvkey="VOL_DOWN" data-hold aria-label="音量减"><svg viewBox="0 0 24 24"><path d="M5 12h14"/></svg><span>音量减</span></button>',
    '<button class="vol mute" data-tvkey="MUTE" aria-label="静音"><svg viewBox="0 0 24 24"><path d="M4 9.5h3.5L12.5 5v14L7.5 14.5H4z"/><path d="M17 9.5l4 5M21 9.5l-4 5"/></svg><span>静音</span></button>',
    '<button class="vol" data-tvkey="VOL_UP" data-hold aria-label="音量加"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg><span>音量加</span></button>',
    '</section>',
    '<p class="hint" id="tvHint">三星电视 · 连接中…</p>',
    '<section class="grid4">',
    '<button class="tile" data-tvkey="POWER" aria-label="电视电源"><svg viewBox="0 0 24 24"><path d="M12 3.5v8"/><path d="M6.6 6.6a7.5 7.5 0 1 0 10.8 0"/></svg><span>电源</span></button>',
    '<button class="tile" data-tvkey="SOURCE" aria-label="信号源"><svg viewBox="0 0 24 24"><path d="M4 7h11v10H4z"/><path d="M15 11l5-3v8l-5-3"/></svg><span>信号源</span></button>',
    '<button class="tile" data-tvkey="HOME" aria-label="主页"><svg viewBox="0 0 24 24"><path d="M4 11l8-7 8 7"/><path d="M6.5 9.8V20h11V9.8"/></svg><span>主页</span></button>',
    '<button class="tile" data-tvkey="BACK" aria-label="返回"><svg viewBox="0 0 24 24"><path d="M9 5l-6 7 6 7"/><path d="M3 12h13a5 5 0 0 1 0 10"/></svg><span>返回</span></button>',
    '</section>',
    '<section class="media">',
    '<button class="tile flat" data-tvkey="CH_DOWN" aria-label="频道减"><span>频道 −</span></button>',
    '<button class="tile flat big" data-tvkey="MENU" aria-label="菜单"><span>菜单</span></button>',
    '<button class="tile flat" data-tvkey="CH_UP" aria-label="频道加"><span>频道 ＋</span></button>',
    '</section>',
  ].join('');
  const footEl = $('#foot');
  mainEl.insertBefore(tvPanel, footEl || null);

  const tvHint = $('#tvHint');
  let tvPaired = false;

  function paintTv(state, err) {
    tvPaired = state === 'paired';
    if (tvPaired) {
      tvHint.textContent = '三星 UA55RU7200 · 已连接';
    } else if (state === 'pairing') {
      tvHint.textContent = '等电视上点「允许」…';
    } else if (state === 'unavailable') {
      tvHint.textContent = '电视模块不可用';
    } else {
      tvHint.textContent = '三星 UA55RU7200 · 未连接（点这里配对）' + (err ? '：' + err : '');
    }
  }

  async function refreshTv() {
    try {
      const s = await api('/api/tv/status', undefined, 8000);
      paintTv((s.tv && s.tv.state) || 'idle', s.tv && s.tv.error);
    } catch (e) {
      tvHint.textContent = '桥接服务未响应';
    }
  }

  async function sendTvKey(key) {
    try {
      await api('/api/tv/key', { key }, 12000);
      if (!tvPaired) paintTv('paired');
    } catch (e) {
      const msg = (e && e.message) || '';
      if (/未连接|拒绝|连接/.test(msg)) {
        toast('电视没连上，点下面那行字重新配对', true);
        paintTv('idle', msg);
      } else {
        toast('电视没有响应', true);
      }
    }
  }

  tvPanel.querySelectorAll('[data-tvkey]').forEach(el => {
    bindButton(el, () => sendTvKey(el.dataset.tvkey));
  });

  tvHint.addEventListener('click', async () => {
    if (tvPaired) return;
    tvHint.textContent = '正在连电视… 电视上会弹窗，用三星遥控器点「允许」';
    try {
      const r = await api('/api/tv/pair', {}, 95000);
      paintTv(r.ok ? 'paired' : 'idle');
      toast(r.ok ? '电视已连接' : '配对失败，看下电视上有没有弹窗', !r.ok);
    } catch (e) {
      paintTv('idle', e.message);
      toast('配对失败：' + (e.message || ''), true);
    }
  });

  function setMode(mode) {
    document.body.dataset.mode = mode;
    $$('.box-only').forEach(el => el.classList.toggle('off', mode !== 'box'));
    $$('.tv-only').forEach(el => el.classList.toggle('off', mode !== 'tv'));
    $$('.mode').forEach(b => b.classList.toggle('active', b.dataset.mode === mode));
    try { localStorage.setItem('tvremote.mode', mode); } catch (e) {}
    if (mode === 'tv') refreshTv(); else refresh();
  }

  $$('.mode').forEach(btn => {
    btn.addEventListener('click', () => setMode(btn.dataset.mode));
  });
  let savedMode = 'box';
  try { savedMode = localStorage.getItem('tvremote.mode') || 'box'; } catch (e) {}
  setMode(savedMode);

  refresh();
})();
