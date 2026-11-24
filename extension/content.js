// Projecter: a "Project" button that appears over video players, plus small notifications.
(() => {
  if (window.__projecter) return;
  window.__projecter = true;
  if (/^https?:\/\/(localhost|127\.0\.0\.1):8765/.test(location.href)) return;

  let enabled = true;
  chrome.storage.sync.get({overlay: true}).then(s => { enabled = s.overlay; });
  chrome.storage.onChanged.addListener(c => { if (c.overlay) { enabled = c.overlay.newValue; if (!enabled) hide(true); } });

  // ---------------------------------------------------------------- UI (isolated in a shadow root)
  const host = document.createElement('projecter-ui');
  host.style.cssText = 'all:initial;position:fixed;left:0;top:0;width:0;height:0;z-index:2147483647;';
  const root = host.attachShadow({mode: 'closed'});
  root.innerHTML = `<style>
    *{box-sizing:border-box;font-family:Inter,"Segoe UI",system-ui,sans-serif}
    .btn{position:fixed;display:none;align-items:center;gap:7px;padding:7px 12px 7px 9px;border:0;border-radius:999px;cursor:pointer;
      color:#fff;font-size:13px;font-weight:700;letter-spacing:.2px;
      background:linear-gradient(120deg,#ff3d6e,#7a5cff 55%,#22d3ee);box-shadow:0 8px 24px -6px rgba(122,92,255,.7),0 0 0 1px rgba(255,255,255,.15) inset;
      opacity:0;transform:translateY(-4px) scale(.96);transition:opacity .18s,transform .18s}
    .btn.show{display:flex}
    .btn.in{opacity:1;transform:none}
    .btn:hover{filter:brightness(1.08)}
    .btn svg{width:16px;height:16px}
    .menu{position:fixed;display:none;min-width:230px;padding:6px;border-radius:14px;background:rgba(18,20,28,.97);
      border:1px solid rgba(255,255,255,.12);box-shadow:0 24px 60px -10px rgba(0,0,0,.8);backdrop-filter:blur(16px)}
    .menu.show{display:block;animation:pop .16s ease}
    @keyframes pop{from{opacity:0;transform:translateY(-4px)}}
    .hd{padding:8px 10px 6px;font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:#8b90a0}
    .tv{display:flex;align-items:center;gap:10px;width:100%;padding:9px 10px;border:0;border-radius:10px;background:none;color:#f2f3f7;cursor:pointer;text-align:left;font-size:14px}
    .tv:hover{background:rgba(255,255,255,.08)}
    .tv[disabled]{opacity:.4;cursor:default;background:none}
    .ic{width:30px;height:30px;flex:none;border-radius:9px;display:grid;place-items:center;background:rgba(34,211,238,.14);color:#8eeafc}
    .tv.roku .ic{background:rgba(122,92,255,.18);color:#b9a8ff}
    .ic svg{width:16px;height:16px}
    .nm{font-weight:600}.st{font-size:11.5px;color:#8b90a0;margin-top:1px}
    .msg{padding:10px;font-size:13px;color:#c9ccd6;line-height:1.4}
    .msg button{margin-top:8px;padding:7px 12px;border:0;border-radius:9px;background:#7a5cff;color:#fff;font-weight:600;cursor:pointer}
    .toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);max-width:min(520px,calc(100vw - 32px));padding:11px 16px;border-radius:12px;
      background:rgba(18,20,28,.96);color:#f2f3f7;font-size:14px;border:1px solid rgba(255,255,255,.12);box-shadow:0 18px 50px -10px rgba(0,0,0,.8);
      display:flex;gap:10px;align-items:flex-start;animation:pop .25s ease}
    .toast::before{content:"";flex:none;width:8px;height:8px;margin-top:6px;border-radius:50%;background:#34d399}
    .toast.err::before{background:#f87171}
    .spin{width:14px;height:14px;border-radius:50%;border:2px solid rgba(255,255,255,.35);border-top-color:#fff;animation:r .8s linear infinite}
    @keyframes r{to{transform:rotate(360deg)}}
  </style>
  <button class="btn" id="btn"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="14" rx="2.5"/><path d="M8 21h8"/><path d="m10 8.5 4.5 2.5-4.5 2.5z" fill="currentColor"/></svg><span id="lbl">Project</span></button>
  <div class="menu" id="menu"></div>
  <div id="toasts"></div>`;
  const $ = id => root.getElementById(id);
  const TV = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="5" width="20" height="13" rx="2.5"/><path d="M8 21h8"/></svg>';
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));

  function mount() {
    const parent = document.fullscreenElement || document.documentElement;
    if (host.parentNode !== parent) parent.appendChild(host);
  }
  document.addEventListener('fullscreenchange', mount);

  // ---------------------------------------------------------------- tracking the video under the mouse
  let current = null, hideTimer = null, menuOpen = false;

  function videoAt(x, y) {
    let best = null;
    for (const v of document.querySelectorAll('video')) {
      const r = v.getBoundingClientRect();
      if (r.width < 220 || r.height < 124) continue;
      if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) {
        if (!best || r.width * r.height > best.r.width * best.r.height) best = {v, r};
      }
    }
    return best;
  }

  function place() {
    if (!current) return;
    const r = current.getBoundingClientRect();
    const b = $('btn');
    b.style.left = Math.max(8, r.right - b.offsetWidth - 14) + 'px';
    b.style.top = Math.max(8, r.top + 14) + 'px';
    const m = $('menu');
    m.style.left = Math.max(8, Math.min(window.innerWidth - 246, r.right - 244)) + 'px';
    m.style.top = (Math.max(8, r.top + 14) + b.offsetHeight + 8) + 'px';
  }

  function show(v) {
    if (!enabled) return;
    mount();
    current = v;
    const b = $('btn');
    b.classList.add('show');
    place();
    requestAnimationFrame(() => b.classList.add('in'));
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => hide(), 2600);
  }

  function hide(force) {
    if (menuOpen && !force) return;
    const b = $('btn');
    b.classList.remove('in');
    setTimeout(() => { if (!b.classList.contains('in')) b.classList.remove('show'); }, 200);
    closeMenu();
  }

  let raf = 0;
  document.addEventListener('mousemove', e => {
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = 0;
      if (!enabled) return;
      const hit = videoAt(e.clientX, e.clientY);
      if (hit) show(hit.v);
    });
  }, {passive: true, capture: true});
  window.addEventListener('scroll', place, {passive: true, capture: true});
  window.addEventListener('resize', place, {passive: true});

  const btn = $('btn');
  btn.addEventListener('mouseenter', () => clearTimeout(hideTimer));
  btn.addEventListener('mouseleave', () => { hideTimer = setTimeout(() => hide(), 1800); });
  btn.addEventListener('click', e => {
    e.preventDefault(); e.stopPropagation();
    menuOpen ? closeMenu() : openMenu();
  });
  for (const ev of ['mousedown', 'pointerdown', 'dblclick']) btn.addEventListener(ev, e => e.stopPropagation());

  // ---------------------------------------------------------------- the TV menu
  const send = msg => chrome.runtime.sendMessage(msg).then(r => { if (!r || !r.ok) throw new Error(r ? r.error : 'No reply'); return r; });

  async function openMenu() {
    menuOpen = true;
    clearTimeout(hideTimer);
    const m = $('menu');
    m.innerHTML = '<div class="msg" style="display:flex;gap:10px;align-items:center"><div class="spin"></div>Finding your TVs…</div>';
    m.classList.add('show');
    place();
    try {
      let {state} = await send({type: 'state'});
      if (!state) {
        m.innerHTML = '<div class="msg" style="display:flex;gap:10px;align-items:center"><div class="spin"></div>Starting Projecter…</div>';
        ({state} = await send({type: 'start'}));
      }
      renderMenu(state);
    } catch (e) {
      m.innerHTML = `<div class="msg">${esc(e.message)}</div>`;
    }
  }

  function renderMenu(state) {
    const m = $('menu');
    if (!state.devices.length) { m.innerHTML = '<div class="msg">No TVs found. Make sure they\'re on.</div>'; return; }
    m.innerHTML = '<div class="hd">Play this video on</div>' + state.devices.map(d => `
      <button class="tv ${d.kind}" data-id="${esc(d.id)}" ${d.status === 'offline' ? 'disabled' : ''}>
        <span class="ic">${TV}</span><span><div class="nm">${esc(d.name)}</div>
        <div class="st">${d.playing ? 'Playing: ' + esc((d.title || '').slice(0, 34)) : {online: 'Ready', standby: 'Standby (will turn on)', offline: 'Off'}[d.status]}</div></span>
      </button>`).join('');
    m.querySelectorAll('.tv').forEach(b => b.addEventListener('click', e => { e.stopPropagation(); castTo(b.dataset.id, b.querySelector('.nm').textContent); }));
  }

  function closeMenu() { menuOpen = false; $('menu').classList.remove('show'); }
  document.addEventListener('mousedown', e => { if (menuOpen && !e.composedPath().includes(host)) { closeMenu(); hide(); } }, true);

  async function castTo(device, name) {
    closeMenu();
    const v = current;
    const video = v ? {src: v.currentSrc || v.src || '', time: v.currentTime || 0, poster: v.poster || '', frame: location.href,
                       duration: isFinite(v.duration) ? v.duration : 0} : null;
    $('lbl').textContent = 'Sending…';
    const t = toast(`Sending to ${name}…`, true, 60000);
    try {
      const r = await send({type: 'cast', device, video});
      t.remove();
      toast(`Playing on ${r.device}`, true);
    } catch (e) {
      t.remove();
      toast(e.message, false, 8000);
    } finally {
      $('lbl').textContent = 'Project';
      hide(true);
    }
  }

  // ---------------------------------------------------------------- notifications
  function toast(text, ok, ms = 3500) {
    mount();
    const el = document.createElement('div');
    el.className = 'toast' + (ok ? '' : ' err');
    el.textContent = text;
    $('toasts').appendChild(el);
    setTimeout(() => el.remove(), ms);
    return el;
  }
  chrome.runtime.onMessage.addListener(msg => {
    if (msg.type === 'toast' && window === window.top) toast(msg.text, msg.ok, msg.ok ? 3000 : 8000);
  });
})();
