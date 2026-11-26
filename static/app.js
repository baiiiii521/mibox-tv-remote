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
  let currentDevice = '';    // 服务端此刻真正在控的设备 id
  let currentMode = 'mibox'; // 页面选中的页签（可能是 tv）
  let pollTimer = null;
  let switchSeq = 0;         // 切页签/切设备时自增：在飞的旧状态响应一律作废
  let pendingSwitch = null;  // 切换中的 Promise：按键必须等它落定，否则会打到上一台

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
    toast(err && err.name === 'AbortError' ? '设备没有响应' : (err.message || '操作失败'), true);
    refresh();
  }

  async function sendKey(key, n = 1) {
    try {
      // 刚点了别的设备、切换还没落定时，先等它：不然这一下会发给上一台
      if (pendingSwitch) await pendingSwitch;
      await api('/api/key', { key, n }, 9000);
      if (currentMode !== 'tv') setOnline(true);
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

  // 顶部电源键不在这里绑：它要按当前页签决定发给盒子还是电视，见文件末尾
  $$('[data-key]').forEach(el => {
    const key = el.dataset.key;
    bindButton(el, () => sendKey(key));
  });

  /* ---------------- 状态 ---------------- */
  function setOnline(ok) {
    online = ok;
    if (currentMode === 'tv') return;   // 电视那档由 paintTv 自己画，别让盒子状态盖掉
    dot.classList.toggle('on', ok);
    dot.classList.toggle('off', !ok);
    banner.hidden = ok;
    if (!ok) devStatus.textContent = '未连接 · 点此重连';
  }

  function setStatus(s) {
    if (currentMode === 'tv') return;                 // 电视模式：顶部归电视管
    if (s.device && s.device !== currentMode) return; // 服务端还停在别的设备：这一条是过期的
    if (s.device && s.device !== currentDevice) {
      currentDevice = s.device;      // 服务端换了设备：应用列表要重画
      appsRendered = false;
      lastPkg = '';
    }
    if (!s.online) banner.textContent = (s.device_label || '设备') + ' 未连接 · 点这里重连';
    devName.textContent = s.model || s.device_label || devName.textContent;
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
    // 电视模式不轮询盒子：轮询回来的旧模型会把顶部标题刷成盒子（这就是那个
    // "切到电视还显示盒子" 的 bug）
    if (currentMode === 'tv') {
      scheduleNext(10000);
      return refreshTv();
    }
    const seq = ++switchSeq;
    let delay = 6000;
    try {
      const s = await api('/api/status', undefined, 9000);
      if (seq !== switchSeq) return;               // 已有更新的一轮在飞，它会负责排下一轮
      if (currentMode === 'tv') return;
      if (s.device && s.device !== currentMode) {
        delay = 1200;                              // 服务端还没跟上：马上再问一次
      } else {
        setStatus(s);
        delay = s.online ? 6000 : 3000;
      }
    } catch (e) {
      if (seq !== switchSeq) return;
      if (currentMode === 'tv') return;
      setOnline(false);
      devStatus.textContent = '桥接服务未响应';
      delay = 3000;
    }
    if (!document.hidden) scheduleNext(delay);
  }

  async function reconnect() {
    if (currentMode === 'tv') return refreshTv();
    devStatus.textContent = '正在重连…';
    try {
      await api('/api/connect', {}, 20000);
      toast('已重新连接');
    } catch (e) {
      try {
        const d = await api('/api/discover', {}, 40000);
        toast(d.found ? '设备换地址了，已找到 ' + d.found : '没找到设备，检查是否开机', !d.found);
      } catch (e2) {
        toast('重连失败，设备可能关机了', true);
      }
    }
    refresh();
  }

  $('#device').addEventListener('click', () => {
    if (currentMode === 'tv') refreshTv();
    else if (!online) reconnect();
  });
  banner.addEventListener('click', () => reconnect());

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
          if (pendingSwitch) await pendingSwitch;
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
      toast('已输入到设备');
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

  /* ---------------- 盒子 / 投影仪 / 电视 三档 ---------------- */
  const mainEl = $('main');
  Array.from(mainEl.children).forEach(el => {
    if (el.id !== 'modes') el.classList.add('box-only');
  });

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
    if (currentMode !== 'tv') return;      // 电视档没选中时不动顶部
    devName.textContent = '三星电视';
    dot.classList.toggle('on', tvPaired);
    dot.classList.toggle('off', !tvPaired);
    devStatus.textContent = tvPaired ? '已连接'
      : (state === 'pairing' ? '等待电视确认…' : '未连接');
  }

  async function refreshTv() {
    try {
      const s = await api('/api/tv/status', undefined, 8000);
      paintTv((s.tv && s.tv.state) || 'idle', s.tv && s.tv.error);
    } catch (e) {
      tvHint.textContent = '桥接服务未响应';
      if (currentMode === 'tv') devStatus.textContent = '桥接服务未响应';
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

  /* ---------------- 页签切换 ---------------- */
  const MODES = { mibox: 'mibox', projector: 'projector', tv: 'tv' };

  function labelOf(id) {
    const b = $('.mode[data-mode="' + id + '"]');
    return b ? b.textContent.trim() : id;
  }

  // 只改界面，不发请求
  function applyMode(mode) {
    if (!MODES[mode]) mode = 'mibox';
    currentMode = mode;
    switchSeq++;               // 作废在飞的状态响应：切走前发出的那条不能再画回来
    const visual = mode === 'tv' ? 'tv' : 'box';
    document.body.dataset.mode = visual;
    $$('.box-only').forEach(el => el.classList.toggle('off', visual !== 'box'));
    $$('.tv-only').forEach(el => el.classList.toggle('off', visual !== 'tv'));
    $$('.mode').forEach(b => b.classList.toggle('active', b.dataset.mode === mode));
    try { localStorage.setItem('tvremote.mode', mode); } catch (e) {}
    $('#btnShot').classList.toggle('off', mode === 'tv');   // 电视截不了图，别让它拍到盒子
    if (mode === 'tv') {
      devName.textContent = '三星电视';
      devStatus.textContent = '遥控电视';
      dot.classList.remove('on', 'off');
      banner.hidden = true;
      refreshTv();
      return;
    }
    devName.textContent = labelOf(mode);
    devStatus.textContent = (mode === currentDevice) ? '连接中…' : '正在切换…';
  }

  async function switchDevice(id, silent) {
    // 串行化：连点两个设备时按顺序生效，不会出现后发的请求先到、再被前一条翻回去
    const prev = pendingSwitch;
    const p = (async () => {
      if (prev) { try { await prev; } catch (e) {} }
      try {
        return await api('/api/switch', { id }, 20000);
      } catch (e) {
        return { ok: false, device: id, error: e.message || '切换失败' };
      }
    })();
    pendingSwitch = p;
    const r = await p;
    if (pendingSwitch === p) pendingSwitch = null;

    if (r.error) {
      if (!silent) toast('切换失败：' + r.error, true);
      currentDevice = '';
      refresh();
      return;
    }
    currentDevice = r.device || id;
    appsRendered = false;
    lastPkg = '';
    if (!silent) {
      const label = r.label || labelOf(id);
      toast(r.ok ? '已切到' + label : label + ' 没连上，先开机', !r.ok);
    }
    if (currentMode === currentDevice) refresh();
  }

  function setMode(mode, silent) {
    if (!MODES[mode]) mode = 'mibox';
    applyMode(mode);
    if (mode === 'tv') {
      scheduleNext(10000);
      return;
    }
    if (mode !== currentDevice) switchDevice(mode, silent);
    else refresh();
  }

  $$('.mode').forEach(btn => {
    btn.addEventListener('click', () => setMode(btn.dataset.mode));
  });

  // 顶部电源键：跟着当前页签走 —— 电视档发三星的 POWER，盒子/投影仪档发 adb 的 POWER
  bindTap($('#btnPower'), () => {
    if (currentMode === 'tv') sendTvKey('POWER');
    else sendKey('POWER');
  });

  /* ---------------- 启动 ----------------
     首屏以服务端当前设备为准；localStorage 只决定要不要进电视档，
     绝不拿它去反向切服务器的设备（旧版会在打开页面时偷偷把设备切回去）。 */
  let savedMode = 'mibox';
  try { savedMode = localStorage.getItem('tvremote.mode') || 'mibox'; } catch (e) {}
  if (savedMode === 'box') savedMode = 'mibox';     // 老版本存的值

  (async function boot() {
    let s = null;
    try { s = await api('/api/status', undefined, 9000); } catch (e) {}
    currentDevice = (s && s.device) || (savedMode === 'tv' ? 'mibox' : savedMode);
    if (savedMode === 'tv') {
      applyMode('tv');
      scheduleNext(10000);
      return;
    }
    applyMode(currentDevice);
    if (s) setStatus(s);
    scheduleNext(s && s.online ? 6000 : 3000);
  })();
})();
