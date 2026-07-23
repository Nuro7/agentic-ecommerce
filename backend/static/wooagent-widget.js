(function () {
  'use strict';

  const CFG = window.wooagent_config || {
    agent_api_url: 'http://localhost:8000',
    rest_url: '',
    nonce: '',
    store_name: 'Store',
    primary_color: '#6366f1',
    widget_position: 'bottom-right',
    enable_voice: true,
    language: 'en',
    platform: 'woocommerce',
    live_navigation: true,
  };

  // Live Shopping Navigator: agent-driven page navigation (search/product/cart).
  // Enabled unless the merchant config explicitly sets live_navigation: false.
  const LIVE_NAV = CFG.live_navigation !== false;

  // Platform flags — widget behaviour adapts based on this, never on hardcoded checks
  const IS_SHOPIFY = String(CFG.platform || '').toLowerCase() === 'shopify';
  const IS_WOOCOMMERCE = !IS_SHOPIFY;

  // Tenant identifier appended to EVERY direct call to the Speako backend. Shopify
  // identifies by shop domain (CFG.shop); WooCommerce/custom by CFG.tenant_id. In
  // production the backend rejects calls with no resolvable tenant, so this must be
  // present on greet/chat, /cart, the WS token fetch, AND the WS stream URL.
  // `hasQuery` = true when the URL already contains a '?'.
  function tenantQS(hasQuery) {
    const id = CFG.shop
      ? 'shop=' + encodeURIComponent(CFG.shop)
      : (CFG.tenant_id ? 'tenant_id=' + encodeURIComponent(CFG.tenant_id) : '');
    return id ? ((hasQuery ? '&' : '?') + id) : '';
  }

  // Persist session ID in localStorage so it survives page navigation
  const S = {
    open: false,
    loading: false,
    recording: false,
    speaking: false,
    muted: false,
    _requestingMic: false,
    greeted: false,
    sessionId: (() => {
      // Use localStorage so session persists across page navigation within same origin
      let id = localStorage.getItem('_wa_sid_v2');
      if (!id) {
        id = 'wa_' + Date.now() + '_' + Math.random().toString(36).slice(2, 9);
        localStorage.setItem('_wa_sid_v2', id);
      }
      return id;
    })(),
    language: CFG.language || (navigator.language || 'en').slice(0, 2),
    mediaRecorder: null,
    audioChunks: [],
    currentAudio: null,
    addressState: localStorage.getItem('_wa_addr_state') || 'idle',
    addressDraft: (() => { try { return JSON.parse(localStorage.getItem('_wa_addr_draft') || '{}'); } catch(e) { return {}; } })(),
    cartCount: parseInt(localStorage.getItem('_wa_cart_count') || '0', 10),
    cartSnapshot: (() => { try { return JSON.parse(localStorage.getItem('_wa_cart_snap') || '{}'); } catch(e) { return {}; } })(),
    pendingProducts: [],
    // Restore conversation so chat doesn't reset on page nav
    conversation: (() => { try { return JSON.parse(localStorage.getItem('_wa_conv') || '[]'); } catch(e) { return []; } })(),
    greeted: localStorage.getItem('_wa_greeted') === '1',
  };

  // ── Primary-colour shades, precomputed in JS ────────────────────────────────
  // The core tokens used to rely on CSS color-mix(), which is unsupported in older
  // Safari and many in-app webviews (a real slice of Shopify mobile traffic) — when
  // it fails, --p2/--p-lo/--p-md become invalid and the whole palette collapses
  // ("colors missing"). Computing them here makes the primary palette resolve
  // everywhere, independent of browser color-mix support.
  function _waHexToRgb(hex) {
    let h = String(hex || '#6366f1').trim().replace('#', '');
    if (h.length === 3) h = h.split('').map(c => c + c).join('');
    const n = parseInt(h, 16);
    if (h.length !== 6 || isNaN(n)) return { r: 99, g: 102, b: 241 };
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
  }
  function _waMixBlack(hex, pct) {           // pct = % of the original colour kept
    const c = _waHexToRgb(hex); const f = pct / 100;
    return `rgb(${Math.round(c.r * f)},${Math.round(c.g * f)},${Math.round(c.b * f)})`;
  }
  function _waAlpha(hex, pct) {
    const c = _waHexToRgb(hex);
    return `rgba(${c.r},${c.g},${c.b},${pct / 100})`;
  }
  function _waBlend(a, b, pctA) {            // pctA% of a, rest b (opaque)
    const ca = _waHexToRgb(a), cb = _waHexToRgb(b), f = pctA / 100;
    const m = (x, y) => Math.round(x * f + y * (1 - f));
    return `rgb(${m(ca.r, cb.r)},${m(ca.g, cb.g)},${m(ca.b, cb.b)})`;
  }
  const PC = CFG.primary_color || '#6366f1';

  const host = document.createElement('div');
  host.id = '_wooagent_root';
  document.body.appendChild(host);
  const shadow = host.attachShadow({ mode: 'open' });

  const css = document.createElement('style');
  css.textContent = `
    :host { all: initial; font-family: -apple-system,BlinkMacSystemFont,'SF Pro Text','Inter','Segoe UI',system-ui,sans-serif; }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    .wa {
      --p:    ${PC};
      --p2:   ${_waMixBlack(PC, 72)};
      --p-lo: ${_waAlpha(PC, 14)};
      --p-md: ${_waAlpha(PC, 28)};
      /* ── Dark theme tokens (default) ── */
      --bg0:  #08080f;
      --bg1:  #0e0e1c;
      --bg2:  #141426;
      --bg3:  #1c1c30;
      --bg4:  #242438;
      --line: rgba(255,255,255,0.07);
      --line2:rgba(255,255,255,0.13);
      --text: #f0f0f8;
      --text2:#8888a8;
      --text3:#44445a;
      --ok:   #34d399;
      --warn: #fbbf24;
      --err:  #f87171;
      --r-xl: 28px;
      --r-lg: 18px;
      --r-md: 13px;
      --r-sm: 9px;
      --shadow: 0 0 0 1px rgba(255,255,255,0.06),
                0 40px 100px rgba(0,0,0,0.92),
                0 12px 40px rgba(0,0,0,0.72);
      /* Theme-adaptive tokens */
      --fade-end:  rgba(8,8,15,0.96);
      --fade-mid:  rgba(8,8,15,0.72);
      --bar-bg:    #0e0e1c;
      --input-bg:  #1a1a2e;
      --input-bg-focus: #20203c;
      --input-border: rgba(255,255,255,0.18);
      --input-ph:  #6868a0;
      --header-bg: linear-gradient(180deg, rgba(255,255,255,0.04) 0%, rgba(255,255,255,0.01) 100%);
      --bot-bubble-bg: #141426;
      --bot-bubble-border: rgba(255,255,255,0.08);
    }

    /* ── Light theme overrides ───────────────────────────── */
    .wa[data-theme="light"] {
      --bg0:  #ffffff;
      --bg1:  #f5f5fc;
      --bg2:  #ededf8;
      --bg3:  #e4e4f2;
      --bg4:  #d8d8ec;
      --line: rgba(0,0,0,0.07);
      --line2:rgba(0,0,0,0.12);
      --text: #18182c;
      --text2:#5a5a7a;
      --text3:#9090b0;
      --shadow: 0 0 0 1px rgba(0,0,0,0.08),
                0 32px 80px rgba(0,0,0,0.18),
                0 8px 32px rgba(0,0,0,0.10);
      --fade-end:  rgba(245,245,252,0.97);
      --fade-mid:  rgba(245,245,252,0.72);
      --bar-bg:    #ededf8;
      --input-bg:  #ffffff;
      --input-bg-focus: #f5f5ff;
      --input-border: rgba(0,0,0,0.16);
      --input-ph:  #9090b8;
      --header-bg: linear-gradient(180deg, rgba(255,255,255,0.9) 0%, rgba(245,245,252,0.6) 100%);
      --bot-bubble-bg: #ededf8;
      --bot-bubble-border: rgba(0,0,0,0.08);
    }

    /* ── FAB ──────────────────────────────────────────────── */
    .wa-fab {
      position: fixed;
      ${CFG.widget_position === 'bottom-left' ? 'left:24px' : 'right:24px'};
      bottom: 26px;
      width: 60px; height: 60px;
      border-radius: 50%;
      border: none;
      background: linear-gradient(140deg, var(--p, #6366f1) 0%, var(--p2, #4f46e5) 100%);
      cursor: pointer;
      display: flex; align-items: center; justify-content: center;
      box-shadow: 0 0 0 0 var(--p-md), 0 8px 28px rgba(0,0,0,0.5);
      transition: transform .22s cubic-bezier(.34,1.56,.64,1), box-shadow .22s ease;
      z-index: 2147483646;
      outline: none;
    }
    .wa-fab:hover {
      transform: scale(1.08);
      box-shadow: 0 0 0 10px var(--p-lo), 0 10px 36px rgba(0,0,0,0.55);
    }
    .wa-fab:active { transform: scale(0.93); }
    .wa-fab.open { box-shadow: 0 0 0 0 transparent, 0 6px 24px rgba(0,0,0,0.4); }

    .wa-fab::before {
      content:''; position: absolute; inset: -8px;
      border-radius: 50%; border: 1.5px solid var(--p);
      opacity: 0; animation: wa-ripple 3.5s ease-out infinite;
    }
    .wa-fab.speaking::before {
      animation: wa-ripple-fast 1.1s ease-out infinite;
      border-color: var(--ok);
    }
    @keyframes wa-ripple {
      0%   { opacity:0.5; transform:scale(0.82); }
      100% { opacity:0;   transform:scale(1.35); }
    }
    @keyframes wa-ripple-fast {
      0%,100% { opacity:0.7; transform:scale(0.86); }
      50%     { opacity:0.2; transform:scale(1.18); }
    }

    .wa-fab-icon { transition: all .3s cubic-bezier(.34,1.56,.64,1); }
    .wa-fab.open .wa-fab-icon { transform: rotate(45deg) scale(0.88); }

    .wa-badge {
      position: absolute; top: -3px; right: -3px;
      min-width: 19px; height: 19px;
      background: var(--err); color: #fff;
      font-size: 10px; font-weight: 700; border-radius: 10px;
      border: 2px solid #080810;
      display: flex; align-items: center; justify-content: center;
      padding: 0 4px;
      transform: scale(0); transition: transform .2s cubic-bezier(.34,1.56,.64,1);
    }
    .wa-badge.on { transform: scale(1); }

    /* ── PANE ─────────────────────────────────────────────── */
    .wa-pane {
      position: fixed;
      ${CFG.widget_position === 'bottom-left' ? 'left:20px' : 'right:20px'};
      bottom: 94px;
      width: 380px;
      height: min(660px, calc(100dvh - 108px));
      background: var(--bg0, #08080f);
      border: 1px solid var(--line2, rgba(255,255,255,0.13));
      border-radius: var(--r-xl, 28px);
      box-shadow: var(--shadow);
      display: flex; flex-direction: column;
      overflow: hidden;
      z-index: 2147483645;
      opacity: 0;
      transform: translateY(16px) scale(0.96);
      pointer-events: none;
      transition: opacity .28s ease, transform .28s cubic-bezier(.34,1.56,.64,1);
    }
    /* Ambient glow at the bottom of the pane */
    .wa-pane::before {
      content: '';
      position: absolute; bottom: 0; left: 0; right: 0; height: 200px;
      background: radial-gradient(ellipse at 50% 110%,
        ${_waAlpha(PC, 16)} 0%,
        ${_waAlpha(PC, 6)} 55%,
        transparent 100%);
      pointer-events: none; z-index: 0;
    }
    .wa-pane.open {
      opacity: 1; transform: translateY(0) scale(1); pointer-events: auto;
    }

    @media (max-width: 480px) {
      .wa-pane {
        left:0 !important; right:0 !important; bottom:0 !important;
        width:100% !important; height:95dvh !important;
        border-radius: 20px 20px 0 0 !important; border-bottom:none !important;
      }
      .wa-fab { right:14px !important; left:auto !important; bottom:16px !important; }
      .wa-card { width: 144px !important; }
      .wa-card-img-wrap { width: 142px !important; }
    }

    /* ── HEADER ───────────────────────────────────────────── */
    .wa-header {
      display: flex; align-items: center; justify-content: space-between;
      padding: 12px 12px 11px;
      border-bottom: 1px solid var(--line);
      flex-shrink: 0;
      background: var(--header-bg);
      backdrop-filter: blur(16px);
      position: relative; z-index: 10;
    }
    .wa-header::after {
      content: '';
      position: absolute; bottom: 0; left: 16px; right: 16px; height: 1px;
      background: linear-gradient(90deg, transparent, var(--p-lo), transparent);
      opacity: 0.7;
    }
    .wa-header-title {
      font-size: 16px; font-weight: 700;
      letter-spacing: -0.02em; text-align: center; flex: 1;
      display: flex; align-items: center; justify-content: center; gap: 7px;
    }
    .wa-header-brand {
      background: linear-gradient(135deg, var(--p) 0%, ${_waBlend(PC, '#c084fc', 55)} 100%);
      -webkit-background-clip: text; -webkit-text-fill-color: transparent;
      background-clip: text;
    }
    .wa-header-status {
      width: 7px; height: 7px; border-radius: 50%;
      background: #34d399; flex-shrink: 0;
      box-shadow: 0 0 6px rgba(52,211,153,0.7);
      animation: wa-pulse-dot 2.5s ease infinite;
    }
    .wa-header-status.thinking { background: #fbbf24; box-shadow: 0 0 6px rgba(251,191,36,0.7); }
    .wa-header-status.offline  { background: #f87171; box-shadow: 0 0 6px rgba(248,113,113,0.7); animation: none; }
    .wa-header-btns { display: flex; gap: 6px; }
    .wa-hbtn {
      width: 34px; height: 34px; border: 1px solid var(--line2);
      border-radius: 50%; background: var(--bg3); color: var(--text2);
      cursor: pointer; display: flex; align-items: center; justify-content: center;
      transition: background .15s, border-color .15s, color .15s, transform .12s;
      flex-shrink: 0;
    }
    .wa-hbtn:hover { background: var(--bg4); border-color: var(--line2); color: var(--text); transform: scale(1.08); }
    .wa-hbtn:active { transform: scale(0.92); }

    /* ── VOICE ZONE — hidden, kept for JS compat ─────────── */
    .wa-voice-zone { display: none; }
    .wa-status-dot {
      width: 6px; height: 6px; border-radius: 50%; background: var(--ok);
      animation: wa-pulse-dot 2.5s ease infinite;
    }
    @keyframes wa-pulse-dot {
      0%,100% { opacity:1; transform:scale(1); }
      50%     { opacity:0.4; transform:scale(0.8); }
    }

    /* Recording strip — shown above voice bar when mic is active */
    .wa-record-strip {
      display: none; align-items: center; gap: 10px;
      padding: 8px 14px;
      background: linear-gradient(90deg,
        rgba(248,113,113,0.08) 0%, transparent 100%);
      border-top: 1px solid rgba(248,113,113,0.12);
      flex-shrink: 0; position: relative; z-index: 1;
    }
    .wa-record-strip.active { display: flex; }
    .wa-rec-dot {
      width: 8px; height: 8px; border-radius: 50%;
      background: var(--err); flex-shrink: 0;
      animation: wa-rec-blink 1s ease infinite;
    }
    @keyframes wa-rec-blink {
      0%,100% { opacity:1; } 50% { opacity:0.3; }
    }
    .wa-rec-label {
      font-size: 12px; font-weight: 600; color: var(--err);
      flex: 1; letter-spacing: 0.01em;
    }
    .wa-waveform {
      width: 100px; height: 24px; opacity: 0; transition: opacity .3s;
    }
    .wa-waveform.active { opacity: 1; }

    /* ── MESSAGES ─────────────────────────────────────────── */
    .wa-msgs {
      flex:1; overflow-y:auto; overflow-x:hidden;
      padding: 14px 14px 20px; display:flex; flex-direction:column; gap:10px;
      scroll-behavior:smooth; position: relative; z-index: 1;
      scroll-padding-bottom: 20px;
    }
    .wa-msgs::-webkit-scrollbar { width:2px; }
    .wa-msgs::-webkit-scrollbar-thumb {
      background: var(--bg4); border-radius:4px;
    }

    @keyframes wa-in {
      from { opacity:0; transform:translateY(10px) scale(0.97); }
      to   { opacity:1; transform:translateY(0)    scale(1);    }
    }

    /* ── BUBBLE ROW (avatar + bubble side-by-side) ─────────── */
    .wa-bubble-row {
      display: flex; align-items: flex-end; gap: 9px;
      animation: wa-in .2s ease-out;
    }
    .wa-bubble-row.user { flex-direction: row-reverse; }

    /* ── BOT AVATAR (mini holographic orb) ─────────────────── */
    .wa-bot-avatar {
      width: 32px; height: 32px; border-radius: 50%; flex-shrink: 0;
      background:
        radial-gradient(circle at 50% 50%, transparent 32%, rgba(8,12,45,0.78) 62%, rgba(2,4,18,0.96) 88%), #040618;
      box-shadow: 0 0 0 1px rgba(130,155,255,0.45), 0 0 10px rgba(80,110,255,0.50), 0 2px 8px rgba(0,0,0,0.6);
      position: relative; overflow: hidden;
      animation: wa-avatar-pulse 4s ease-in-out infinite;
    }
    .wa-bot-avatar::before {
      content:''; position:absolute; inset:0; border-radius:50%;
      background:
        radial-gradient(ellipse 48% 42% at 20% 74%, rgba(255,155,50,0.80) 0%, rgba(210,80,180,0.60) 38%, transparent 62%),
        radial-gradient(ellipse 58% 54% at 60% 28%, rgba(48,105,255,0.92) 0%, rgba(68,52,228,0.76) 30%, transparent 65%),
        radial-gradient(ellipse 30% 36% at 74% 52%, rgba(28,165,255,0.70) 0%, rgba(48,78,238,0.52) 42%, transparent 64%);
      animation: wa-swirl-fwd 9s linear infinite;
    }
    .wa-bot-avatar::after {
      content:''; position:absolute; inset:0; border-radius:50%;
      background:
        radial-gradient(ellipse 18% 30% at 56% 28%, rgba(255,255,255,0.90) 0%, rgba(210,225,255,0.45) 38%, transparent 65%),
        radial-gradient(ellipse 38% 34% at 44% 56%, rgba(4,6,28,0.80) 0%, rgba(4,6,28,0.52) 40%, transparent 62%);
      animation: wa-swirl-back 13s linear infinite;
    }
    @keyframes wa-avatar-pulse {
      0%,100% { box-shadow: 0 0 0 1px rgba(130,155,255,0.45), 0 0 10px rgba(80,110,255,0.50), 0 2px 8px rgba(0,0,0,0.6); }
      50%     { box-shadow: 0 0 0 1px rgba(150,175,255,0.60), 0 0 16px rgba(100,130,255,0.65), 0 3px 10px rgba(0,0,0,0.6); }
    }
    /* Light mode bot avatar — warm holographic palette */
    .wa[data-theme="light"] .wa-bot-avatar {
      background:
        radial-gradient(circle at 50% 50%, transparent 32%, rgba(30,8,45,0.78) 62%, rgba(12,4,22,0.96) 88%), #100618;
      box-shadow: 0 0 0 1px rgba(200,130,255,0.45), 0 0 10px rgba(160,80,255,0.50), 0 2px 8px rgba(0,0,0,0.4);
    }
    .wa[data-theme="light"] .wa-bot-avatar::before {
      background:
        radial-gradient(ellipse 48% 42% at 20% 74%, rgba(255,130,80,0.80) 0%, rgba(240,60,160,0.60) 38%, transparent 62%),
        radial-gradient(ellipse 58% 54% at 60% 28%, rgba(120,80,255,0.92) 0%, rgba(160,50,230,0.76) 30%, transparent 65%),
        radial-gradient(ellipse 30% 36% at 74% 52%, rgba(80,140,255,0.70) 0%, rgba(100,60,240,0.52) 42%, transparent 64%);
    }
    .wa[data-theme="light"] .wa-bot-avatar::after {
      background:
        radial-gradient(ellipse 18% 30% at 56% 28%, rgba(255,255,255,0.90) 0%, rgba(255,220,245,0.45) 38%, transparent 65%),
        radial-gradient(ellipse 38% 34% at 44% 56%, rgba(20,4,28,0.80) 0%, rgba(20,4,28,0.52) 40%, transparent 62%);
    }

    /* ── USER AVATAR ───────────────────────────────────────── */
    .wa-user-avatar {
      width: 32px; height: 32px; border-radius: 50%; flex-shrink: 0;
      background: linear-gradient(140deg, #a78bfa 0%, #7c3aed 100%);
      display: flex; align-items: center; justify-content: center;
      color: rgba(255,255,255,0.9); font-size: 13px; font-weight: 600;
      box-shadow: 0 2px 10px rgba(124,58,237,0.4);
      overflow: hidden;
    }

    .wa-bubble {
      max-width: 75%; padding: 12px 16px;
      font-size: 14px; line-height: 1.6;
      word-wrap: break-word; letter-spacing: -0.005em;
    }
    .wa-bubble.bot {
      background: var(--bot-bubble-bg, #141426);
      border: 1px solid var(--bot-bubble-border, rgba(255,255,255,0.08));
      color: var(--text, #f0f0f8);
      border-radius: 4px 20px 20px 20px;
      box-shadow: 0 2px 12px rgba(0,0,0,0.10), inset 2px 0 0 var(--p-lo);
    }
    .wa-bubble.user {
      background: linear-gradient(135deg, var(--p, #6366f1) 0%, var(--p2, #4f46e5) 100%);
      color: #fff; border-radius: 20px 4px 20px 20px;
      box-shadow: 0 4px 18px var(--p-md);
    }
    /* Orphan bubbles (live transcript) appended directly to msgs */
    .wa-msgs > .wa-bubble.user { align-self: flex-end; animation: wa-in .2s ease-out; }
    .wa-msgs > .wa-bubble.bot  { align-self: flex-start; animation: wa-in .2s ease-out; }
    .wa-bubble.system {
      align-self: center; max-width:90%;
      background: transparent; border: 1px solid var(--bg4);
      color: var(--text3); font-size: 11px; border-radius: 8px;
      text-align: center; padding: 5px 12px;
      animation: wa-in .2s ease-out;
    }

    /* ── TYPING INDICATOR ─────────────────────────────────── */
    .wa-typing {
      display: flex; align-items: flex-end; gap: 9px;
      animation: wa-in .2s ease-out;
    }
    .wa-typing-inner {
      background: var(--bg2); border: 1px solid var(--line);
      border-radius: 4px 20px 20px 20px;
      padding: 14px 18px; display:flex; gap:6px;
    }
    .wa-dot {
      width: 7px; height: 7px; border-radius:50%;
      background: var(--text3);
      animation: wa-dot-bounce 1.4s ease infinite;
    }
    .wa-dot:nth-child(2) { animation-delay:.18s; }
    .wa-dot:nth-child(3) { animation-delay:.36s; }
    @keyframes wa-dot-bounce {
      0%,60%,100% { transform:translateY(0);   background:var(--text3); }
      30%         { transform:translateY(-8px); background:var(--p); }
    }

    /* ── PRODUCT CARDS ────────────────────────────────────── */
    .wa-products-wrap {
      align-self: stretch;
      display: flex; flex-direction:column; gap:7px;
      animation: wa-in .22s ease-out;
    }
    .wa-products-label {
      font-size: 10px; color: var(--text3);
      letter-spacing: 0.08em; text-transform: uppercase; padding:0 2px;
    }
    .wa-products-scroll {
      display: flex; gap: 10px;
      overflow-x: auto; scroll-snap-type: x mandatory;
      padding-bottom: 4px;
    }
    .wa-products-scroll::-webkit-scrollbar { height:0; }

    .wa-card {
      flex-shrink:0; width: 152px;
      background: var(--bg2, #141426); border: 1px solid var(--line2, rgba(255,255,255,0.13));
      border-radius: var(--r-lg, 18px); overflow:hidden;
      scroll-snap-align:start;
      transition: border-color .22s, transform .22s cubic-bezier(.34,1.56,.64,1),
                  box-shadow .22s ease;
      cursor:default;
      box-shadow: 0 2px 14px rgba(0,0,0,0.12);
    }
    .wa-card:hover {
      border-color: ${_waAlpha(PC, 55)};
      transform: translateY(-4px);
      box-shadow: 0 14px 36px rgba(0,0,0,0.20), 0 0 0 1px ${_waAlpha(PC, 28)};
    }
    .wa-card-img-wrap { width:150px; height:130px; position:relative; overflow:hidden; }
    .wa-card-img {
      width:100%; height:100%; object-fit:cover; display:block;
      background:var(--bg3); transition: transform .38s ease;
    }
    .wa-card:hover .wa-card-img { transform: scale(1.05); }
    .wa-card-sale-tag {
      position:absolute; top:7px; left:7px;
      background: linear-gradient(135deg, #ef4444, #dc2626);
      color:#fff; font-size:9px; font-weight:700;
      padding: 2px 6px; border-radius:4px; letter-spacing:0.04em;
    }
    .wa-card-body { padding:9px 10px 10px; display:flex; flex-direction:column; gap:5px; }
    .wa-card-name {
      font-size: 11.5px; font-weight:600; color:var(--text, #f0f0f8);
      line-height:1.38; letter-spacing:-0.005em;
      display:-webkit-box; -webkit-line-clamp:2;
      -webkit-box-orient:vertical; overflow:hidden;
    }
    .wa-card-prices { display:flex; align-items:baseline; gap:5px; }
    .wa-card-price  { font-size:14px; font-weight:700; color:var(--p, #6366f1); letter-spacing:-0.02em; }
    .wa-card-reg    { font-size:10.5px; color:var(--text3); text-decoration:line-through; }
    .wa-card-stock  {
      display:flex; align-items:center; gap:4px;
      font-size:10px; color:var(--text2);
    }
    .wa-card-meta {
      font-size:10px; line-height:1.4; color:var(--text3);
      min-height:22px; overflow:hidden;
    }
    .wa-card-variant-row { display:flex; gap:5px; }
    .wa-card-select {
      width:100%; border:1px solid var(--line); background:var(--bg1);
      color:var(--text2); border-radius:7px; padding:3px 7px;
      font-size:10px; outline:none; cursor:pointer;
    }
    .wa-card-select:focus {
      border-color: ${_waAlpha(PC, 55)};
      color: var(--text);
    }
    .wa-stock-dot { width:5px; height:5px; border-radius:50%; flex-shrink:0; }
    .wa-stock-dot.in  { background:var(--ok); }
    .wa-stock-dot.low { background:var(--warn); }
    .wa-stock-dot.out { background:var(--err); }

    .wa-card-add {
      width:100%; padding:8px 0;
      border:none; border-radius:var(--r-sm);
      background: var(--p-lo);
      color:var(--p); font-size:11px; font-weight:600;
      cursor:pointer; transition:background .18s, color .18s, transform .1s, box-shadow .18s;
      letter-spacing:0.02em;
    }
    .wa-card-add:hover {
      background: linear-gradient(135deg, var(--p) 0%, var(--p2) 100%);
      color:#fff; box-shadow: 0 4px 12px var(--p-md);
    }
    .wa-card-add:active { transform:scale(0.96); }
    .wa-card-add.disabled {
      background:var(--bg3); color:var(--text3); cursor:not-allowed;
    }
    .wa-card-view {
      font-size:10px; color:var(--text3); text-decoration:none;
      text-align:right; display:block; transition:color .15s;
    }
    .wa-card-view:hover { color:var(--text2); }

    /* ── STORE INFO CARD ──────────────────────────────────── */
    .wa-sinfo-card {
      align-self:stretch;
      background:var(--bg2); border:1px solid var(--line);
      border-radius:var(--r-lg); padding:15px;
      display:flex; flex-direction:column; gap:10px;
      animation:wa-in .22s ease-out;
    }
    .wa-sinfo-header {
      font-size:14px; font-weight:700; color:var(--text);
      letter-spacing:-0.01em; padding-bottom:8px;
      border-bottom:1px solid var(--line);
    }
    .wa-sinfo-body { display:flex; flex-direction:column; gap:8px; }
    .wa-sinfo-row {
      display:flex; align-items:flex-start; gap:8px;
      font-size:12px; color:var(--text2); line-height:1.5;
    }
    .wa-sinfo-icon { flex-shrink:0; font-size:14px; margin-top:1px; }

    /* ── CART CARD ────────────────────────────────────────── */
    .wa-cart-card {
      align-self:stretch;
      background:var(--bg2); border:1px solid var(--line);
      border-radius:var(--r-lg); padding:15px;
      display:flex; flex-direction:column; gap:11px;
      animation:wa-in .22s ease-out;
    }
    .wa-cart-head  { display:flex; align-items:center; justify-content:space-between; }
    .wa-cart-title { font-size:13px; font-weight:650; color:var(--text); letter-spacing:-0.01em; }
    .wa-cart-pill  {
      background: var(--p-lo); color:var(--p);
      font-size:11px; font-weight:700; padding:3px 10px; border-radius:12px;
    }
    .wa-cart-items { display:flex; flex-direction:column; gap:7px; }
    .wa-cart-item-row {
      display:flex; justify-content:space-between; align-items:center;
      font-size:12px; color:var(--text2);
      padding-bottom:7px; border-bottom:1px solid var(--line);
    }
    .wa-cart-item-row:last-child { border-bottom:none; padding-bottom:0; }
    .wa-cart-total-row {
      display:flex; justify-content:space-between;
      padding-top:5px; border-top:1px solid var(--line2);
    }
    .wa-cart-total-label { font-size:13px; font-weight:600; color:var(--text); }
    .wa-cart-total-val   { font-size:17px; font-weight:700; color:var(--p); letter-spacing:-0.02em; }
    .wa-checkout-btn {
      width:100%; padding:12px;
      border:none; border-radius:var(--r-md);
      background: linear-gradient(135deg, var(--p) 0%, var(--p2) 100%);
      color:#fff; font-size:13px; font-weight:650;
      cursor:pointer; transition:opacity .15s, transform .1s;
      letter-spacing:0.01em;
      box-shadow: 0 4px 16px var(--p-md);
    }
    .wa-checkout-btn:hover  { opacity:0.9; }
    .wa-checkout-btn:active { transform:scale(0.98); }

    /* ── ADDRESS PROGRESS ─────────────────────────────────── */
    .wa-addr-progress {
      align-self:stretch;
      background:var(--bg2); border:1px solid var(--line);
      border-radius:var(--r-lg); padding:14px;
      display:flex; flex-direction:column; gap:10px;
      animation:wa-in .22s ease-out;
    }
    .wa-addr-title  { font-size:11px; color:var(--text2); letter-spacing:0.04em; text-transform:uppercase; }
    .wa-addr-steps  { display:flex; gap:4px; }
    .wa-addr-step   { flex:1; height:3px; border-radius:2px; background:var(--bg4); transition:background .3s; }
    .wa-addr-step.done   { background:var(--ok); }
    .wa-addr-step.active { background:var(--p);  }

    /* ── TEXT BAR ─────────────────────────────────────────── */
    .wa-text-bar {
      display: none; align-items: flex-end; gap: 8px;
      padding: 10px 14px 12px;
      border-top: 2px solid var(--p-lo);
      background: var(--bar-bg);
      flex-shrink: 0; position: relative; z-index: 5;
      animation: wa-in .2s ease-out;
    }
    .wa-text-bar.visible { display: flex; }
    .wa-text-input {
      flex: 1;
      background: var(--input-bg);
      border: 1.5px solid ${_waAlpha(PC, 38)};
      border-radius: 22px;
      color: var(--text);
      font-size: 14px; font-family: inherit;
      padding: 11px 16px; resize: none; outline: none;
      max-height: 90px; min-height: 42px; line-height: 1.45;
      transition: border-color .18s, background .18s, box-shadow .18s;
      letter-spacing: -0.005em;
    }
    .wa-text-input::placeholder { color: var(--input-ph); }
    .wa-text-input:focus {
      background: var(--input-bg-focus);
      border-color: ${_waAlpha(PC, 80)};
      box-shadow: 0 0 0 3px var(--p-lo);
      outline: none;
    }
    .wa-send-btn {
      width:40px; height:40px; border-radius:50%;
      border:none;
      background: linear-gradient(135deg, var(--p) 0%, var(--p2) 100%);
      color:#fff; cursor:pointer;
      display:flex; align-items:center; justify-content:center;
      flex-shrink:0; transition:transform .12s, opacity .15s, box-shadow .15s;
      box-shadow: 0 4px 14px var(--p-md);
    }
    .wa-send-btn:hover  { transform:scale(1.1); box-shadow:0 6px 20px var(--p-md); }
    .wa-send-btn:active { transform:scale(0.9); }
    .wa-send-btn:disabled { opacity:0.32; cursor:not-allowed; transform:none; box-shadow:none; }

    /* ── VOICE ZONE (compact stage at bottom) ────────────── */
    .wa-voice-bar {
      flex-shrink: 0; height: 168px;
      display: flex; flex-direction: column;
      align-items: center; justify-content: center;
      padding: 10px 16px 50px;
      position: relative; z-index: 2;
      /* Dark mode: deep space purple-left, teal-right */
      background:
        radial-gradient(ellipse 110% 90% at 22% 58%, rgba(88,25,195,0.60) 0%, transparent 58%),
        radial-gradient(ellipse  90% 70% at 78% 38%, rgba(0,115,182,0.42) 0%, transparent 55%),
        radial-gradient(ellipse  65% 50% at 50% 95%, rgba(12,44,138,0.34) 0%, transparent 52%),
        #07071a;
    }
    /* Light mode: soft lavender — blends seamlessly with chat area */
    .wa[data-theme="light"] .wa-voice-bar {
      background:
        radial-gradient(ellipse 110% 90% at 22% 60%, rgba(120,60,240,0.10) 0%, transparent 60%),
        radial-gradient(ellipse  90% 70% at 78% 40%, rgba(60,100,230,0.07) 0%, transparent 55%),
        linear-gradient(to bottom, #f5f5fc 0%, #eceaf8 55%, #e4e0f4 100%);
    }

    /* ── Zone control buttons (pinned to bottom of voice zone) ── */
    .wa-zone-btns {
      position: absolute; bottom: 12px; left: 16px; right: 16px;
      display: flex; justify-content: space-between; align-items: center;
    }
    .wa-zone-btn {
      width: 40px; height: 40px; border-radius: 50%;
      border: 1px solid rgba(255,255,255,0.14);
      background: rgba(255,255,255,0.07);
      color: rgba(255,255,255,0.60);
      cursor: pointer;
      display: flex; align-items: center; justify-content: center;
      transition: background .15s, transform .12s;
      flex-shrink: 0;
    }
    .wa-zone-btn:hover { background: rgba(255,255,255,0.14); color:#fff; transform:scale(1.06); }
    .wa-zone-btn:active { transform:scale(0.92); }
    .wa-zone-btn.active {
      background: rgba(99,102,241,0.25); border-color: rgba(99,102,241,0.55);
      color: rgba(190,180,255,0.95);
    }

    /* Pane fade from messages into voice zone — dark mode only */
    .wa-pane::after {
      content: '';
      position: absolute; bottom: 168px; left: 0; right: 0; height: 52px;
      background: linear-gradient(to bottom,
        transparent 0%,
        rgba(7,7,26,0.18) 30%,
        rgba(7,7,26,0.60) 62%,
        rgba(7,7,26,0.90) 100%);
      pointer-events: none; z-index: 1;
    }
    /* Light mode: voice zone is light, no dark overlay needed */
    .wa[data-theme="light"] .wa-pane::after { display: none; }
    .wa-pane:has(.wa-text-bar.visible)::after { bottom: 234px; height: 44px; }

    /* ── ORB STAGE ────────────────────────────────────────── */
    .wa-orb-stage {
      position: relative; width: 80px; height: 80px;
      display: flex; align-items: center; justify-content: center;
    }

    /* ── Voice wave rings (expand outward when active) ───── */
    .wa-wave-ring {
      position: absolute; inset: 0; border-radius: 50%;
      border: 1.5px solid rgba(0,210,240,0.55);
      pointer-events: none; opacity: 0;
    }
    .wa-orb-stage:has(.wa-orb.live) .wa-wave-ring,
    .wa-orb-stage:has(.wa-orb.recording) .wa-wave-ring,
    .wa-orb-stage:has(.wa-orb.speaking) .wa-wave-ring {
      animation: wa-wave-out 2.2s ease-out infinite;
    }
    /* Priority order: live > speaking > recording */
    .wa-orb-stage:has(.wa-orb.recording:not(.live):not(.speaking)) .wa-wave-ring { border-color: rgba(255,100,60,0.5); }
    .wa-orb-stage:has(.wa-orb.speaking) .wa-wave-ring  { border-color: rgba(52,211,153,0.5); }
    .wa-orb-stage:has(.wa-orb.live)     .wa-wave-ring  { border-color: rgba(0,210,240,0.55); }
    .wa-wave-ring:nth-child(2) { animation-delay:.7s !important; }
    .wa-wave-ring:nth-child(3) { animation-delay:1.4s !important; }
    @keyframes wa-wave-out {
      0%   { transform:scale(1);   opacity:.65; }
      100% { transform:scale(2.4); opacity:0;   }
    }

    /* ── ORB — holographic iridescent 3D sphere ──────────── */
    .wa-orb {
      width: 80px; height: 80px; border-radius: 50%;
      border: none; cursor: pointer; flex-shrink: 0;
      position: relative; overflow: hidden;
      /* Glass sphere boundary — dark rim fades in from edge */
      background:
        radial-gradient(circle at 50% 50%,
          transparent 36%,
          rgba(8,12,45,0.70) 60%,
          rgba(3,5,20,0.92) 80%,
          rgba(1,2,10,0.98) 95%),
        #040618;
      box-shadow:
        /* Iridescent rim ring */
        0 0 0 1px rgba(130,155,255,0.55),
        0 0 0 2px rgba(180,90,255,0.20),
        /* Blue-violet glow — scaled to 80px orb */
        0 0 14px rgba(80,110,255,0.62),
        0 0 36px rgba(110,60,210,0.36),
        /* Pink accent glow (upper-left) */
        -5px -4px 18px rgba(180,80,255,0.22),
        /* Orange-gold accent glow (lower-right) */
        5px 6px 16px rgba(200,120,40,0.14),
        /* Drop shadow */
        0 8px 26px rgba(0,0,0,0.95);
      transition: transform .22s cubic-bezier(.34,1.56,.64,1), box-shadow .28s ease;
      animation: wa-orb-breathe 4s ease-in-out infinite;
    }
    /* Iridescent fluid color patches — forward spin */
    .wa-orb::before {
      content:''; position:absolute; inset:0; border-radius:50%;
      background:
        /* Orange-gold iridescent patch (lower-left warm edge) */
        radial-gradient(ellipse 48% 42% at 20% 74%, rgba(255,155,50,0.84) 0%, rgba(210,80,180,0.64) 38%, transparent 62%),
        /* Pink-rose bridge */
        radial-gradient(ellipse 36% 32% at 36% 60%, rgba(225,95,210,0.70) 0%, rgba(165,55,230,0.50) 40%, transparent 62%),
        /* Main cobalt-blue dominant form */
        radial-gradient(ellipse 58% 54% at 58% 28%, rgba(48,105,255,0.94) 0%, rgba(68,52,228,0.80) 28%, rgba(108,42,200,0.54) 55%, transparent 72%),
        /* Bright teal-blue secondary ribbon */
        radial-gradient(ellipse 32% 38% at 75% 50%, rgba(28,165,255,0.74) 0%, rgba(48,78,238,0.56) 40%, transparent 64%),
        /* Deep purple volume fill */
        radial-gradient(ellipse 75% 70% at 46% 54%, rgba(52,14,112,0.64) 0%, rgba(16,6,50,0.84) 56%, transparent 80%);
      animation: wa-swirl-fwd 9s linear infinite;
    }
    /* Specular highlights + depth shadows — counter-spin */
    .wa-orb::after {
      content:''; position:absolute; inset:0; border-radius:50%;
      background:
        /* Primary white specular (peak of main ribbon) */
        radial-gradient(ellipse 18% 32% at 56% 26%, rgba(255,255,255,0.94) 0%, rgba(210,225,255,0.55) 35%, transparent 65%),
        /* Secondary specular on lower ribbon */
        radial-gradient(ellipse 10% 18% at 40% 54%, rgba(255,255,255,0.80) 0%, rgba(200,215,255,0.38) 40%, transparent 62%),
        /* Glint on right ribbon tip */
        radial-gradient(ellipse 7% 12% at 67% 65%, rgba(255,255,255,0.62) 0%, transparent 55%),
        /* Depth shadow between forms */
        radial-gradient(ellipse 42% 36% at 46% 56%, rgba(4,6,28,0.84) 0%, rgba(4,6,28,0.58) 40%, transparent 62%),
        /* Dark side shadow (left recess) */
        radial-gradient(ellipse 28% 22% at 26% 40%, rgba(6,10,38,0.70) 0%, transparent 55%);
      animation: wa-swirl-back 13s linear infinite;
    }
    @keyframes wa-swirl-fwd  { from{transform:rotate(0deg)}  to{transform:rotate(360deg)}  }
    @keyframes wa-swirl-back { from{transform:rotate(0deg)}  to{transform:rotate(-360deg)} }

    /* ── Light mode orb — same holographic style, warmer palette ── */
    .wa[data-theme="light"] .wa-orb {
      background:
        radial-gradient(circle at 50% 50%,
          transparent 36%,
          rgba(30,8,45,0.72) 60%,
          rgba(12,4,22,0.92) 80%,
          rgba(4,1,10,0.98) 95%),
        #100618;
      box-shadow:
        0 0 0 1px rgba(200,130,255,0.58),
        0 0 0 2px rgba(255,100,200,0.20),
        0 0 14px rgba(160,80,255,0.58),
        0 0 36px rgba(200,50,180,0.30),
        -5px -4px 18px rgba(100,120,255,0.20),
        5px 6px 16px rgba(255,140,60,0.14),
        0 7px 24px rgba(0,0,0,0.50);
    }
    .wa[data-theme="light"] .wa-orb::before {
      background:
        radial-gradient(ellipse 48% 42% at 20% 74%, rgba(255,130,80,0.84) 0%, rgba(240,60,160,0.64) 38%, transparent 62%),
        radial-gradient(ellipse 36% 32% at 36% 60%, rgba(255,80,180,0.70) 0%, rgba(200,40,220,0.52) 40%, transparent 62%),
        radial-gradient(ellipse 58% 54% at 58% 28%, rgba(120,80,255,0.94) 0%, rgba(160,50,230,0.80) 28%, rgba(200,60,200,0.54) 55%, transparent 72%),
        radial-gradient(ellipse 32% 38% at 75% 50%, rgba(80,140,255,0.74) 0%, rgba(100,60,240,0.56) 40%, transparent 64%),
        radial-gradient(ellipse 75% 70% at 46% 54%, rgba(80,14,80,0.64) 0%, rgba(30,6,30,0.84) 56%, transparent 80%);
    }
    .wa[data-theme="light"] .wa-orb::after {
      background:
        radial-gradient(ellipse 18% 32% at 56% 26%, rgba(255,255,255,0.94) 0%, rgba(255,220,245,0.55) 35%, transparent 65%),
        radial-gradient(ellipse 10% 18% at 40% 54%, rgba(255,255,255,0.80) 0%, rgba(255,210,240,0.38) 40%, transparent 62%),
        radial-gradient(ellipse 7% 12% at 67% 65%, rgba(255,255,255,0.62) 0%, transparent 55%),
        radial-gradient(ellipse 42% 36% at 46% 56%, rgba(20,4,28,0.84) 0%, rgba(20,4,28,0.58) 40%, transparent 62%),
        radial-gradient(ellipse 28% 22% at 26% 40%, rgba(16,6,38,0.70) 0%, transparent 55%);
    }
    .wa[data-theme="light"] .wa-orb:hover {
      box-shadow:
        0 0 0 2px rgba(200,100,255,0.70),
        0 0 18px rgba(180,70,255,0.72),
        0 0 42px rgba(220,50,180,0.36),
        0 8px 24px rgba(0,0,0,0.48);
    }
    /* Light mode zone buttons — match lavender bg */
    .wa[data-theme="light"] .wa-zone-btn {
      border-color: rgba(100,50,220,0.22);
      background: rgba(100,50,220,0.08);
      color: rgba(80,40,180,0.65);
    }
    .wa[data-theme="light"] .wa-zone-btn:hover {
      background: rgba(100,50,220,0.16); color: rgba(80,40,180,0.90);
    }
    /* Light mode wave rings — purple on lavender */
    .wa[data-theme="light"] .wa-wave-ring { border-color: rgba(130,60,230,0.40); }
    /* Light mode live transcript text — dark (readable on lavender) */
    .wa[data-theme="light"] .wa-live-speech {
      color: #18182c;
      text-shadow: 0 1px 8px rgba(200,190,255,0.80);
    }
    .wa[data-theme="light"] .wa-live-speech .wa-live-interim-word {
      color: rgba(80,40,180,0.55);
    }
    /* Light mode orb label already handled below */

    .wa-orb:hover {
      transform:scale(1.08);
      box-shadow:
        0 0 0 1.5px rgba(160,175,255,0.70),
        0 0 0 3px rgba(200,100,255,0.24),
        0 0 18px rgba(100,130,255,0.72),
        0 0 44px rgba(130,60,220,0.42),
        -6px -5px 22px rgba(200,90,255,0.24),
        6px 8px 20px rgba(220,130,50,0.16),
        0 9px 28px rgba(0,0,0,0.92);
    }
    .wa-orb:active { transform:scale(0.92); }
    @keyframes wa-orb-breathe {
      0%,100% { transform:scale(1);     }
      50%     { transform:scale(1.025); }
    }
    /* Recording — red/amber pulse + sphere turns orange */
    .wa-orb.recording {
      box-shadow:
        0 0 0 2px rgba(255,100,60,0.58),
        0 0 16px rgba(255,80,40,0.65),
        0 0 38px rgba(220,60,20,0.32),
        0 6px 22px rgba(0,0,0,0.95);
      animation: wa-orb-pulse-rec 1.2s ease-in-out infinite;
    }
    .wa-orb.recording::before {
      background:
        radial-gradient(ellipse 58% 58% at 72% 28%, rgba(255,140,40,1) 0%, rgba(220,80,0,0.88) 38%, transparent 65%),
        radial-gradient(ellipse 58% 58% at 28% 72%, rgba(200,40,60,1) 0%, rgba(160,20,30,0.88) 38%, transparent 65%);
    }
    @keyframes wa-orb-pulse-rec {
      0%,100% { transform:scale(1);    }
      50%     { transform:scale(1.07); }
    }
    /* Speaking — green glow + sphere turns green/teal */
    .wa-orb.speaking {
      box-shadow:
        0 0 0 2px rgba(52,211,153,0.58),
        0 0 16px rgba(52,211,153,0.60),
        0 0 38px rgba(16,185,129,0.30),
        0 6px 22px rgba(0,0,0,0.95);
    }
    .wa-orb.speaking::before {
      background:
        radial-gradient(ellipse 58% 58% at 72% 28%, rgba(0,220,140,1) 0%, rgba(0,170,100,0.88) 38%, transparent 65%),
        radial-gradient(ellipse 58% 58% at 28% 72%, rgba(0,180,220,1) 0%, rgba(0,120,180,0.88) 38%, transparent 65%);
    }
    /* Thinking — amber pulse + sphere turns amber */
    .wa-orb.thinking { animation:wa-orb-think 1.8s ease-in-out infinite; }
    .wa-orb.thinking::before {
      background:
        radial-gradient(ellipse 58% 58% at 72% 28%, rgba(250,190,40,1) 0%, rgba(210,130,0,0.88) 38%, transparent 65%),
        radial-gradient(ellipse 58% 58% at 28% 72%, rgba(200,80,10,1) 0%, rgba(160,40,0,0.88) 38%, transparent 65%);
    }
    @keyframes wa-orb-think {
      0%,100% { transform:scale(0.97); }
      50%     { transform:scale(1.04); }
    }
    /* Live — faster breathe, keep galaxy colors */
    .wa-orb.live { animation:wa-orb-breathe 2.4s ease-in-out infinite; }

    /* ── ORB LABEL — doubles as live transcript / agent subtitle ── */
    /* ── ORB HINT — state label below orb ───────────────────── */
    .wa-orb-label {
      margin-top: 6px; font-size: 11px; font-weight: 500;
      color: rgba(255,255,255,0.38);
      letter-spacing: 0.03em; text-align: center;
      height: 18px; line-height: 18px; overflow: hidden;
      padding: 0 14px;
      user-select: none;
      transition: color .2s;
      white-space: nowrap;
    }
    .wa[data-theme="light"] .wa-orb-label { color: rgba(30,30,70,0.45); }
    .wa-orb-label strong { font-weight:700; color:rgba(255,255,255,0.80); }
    .wa[data-theme="light"] .wa-orb-label strong { color:rgba(30,30,70,0.80); }
    .wa-orb-label .wa-live-badge { color:var(--ok); }
    .wa-voice-bar:has(.wa-orb.speaking) .wa-orb-label { color:rgba(52,211,153,0.80); }
    .wa[data-theme="light"] .wa-voice-bar:has(.wa-orb.speaking) .wa-orb-label { color:rgba(52,211,153,0.90); }

    /* ── LIVE TRANSCRIPT PILL — appears below hint while recording ── */
    .wa-live-pill {
      display: none;
      margin: 5px 14px 0;
      padding: 9px 14px;
      background: rgba(255,255,255,0.05);
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: 14px;
      font-size: 13px; font-weight: 400;
      color: rgba(255,255,255,0.82);
      line-height: 1.5; text-align: center;
      min-height: 38px;
      word-break: break-word;
      animation: wa-in .2s ease;
      transition: background .2s;
      position: relative; z-index: 1;
    }
    .wa[data-theme="light"] .wa-live-pill {
      background: rgba(99,102,241,0.06);
      border-color: rgba(99,102,241,0.15);
      color: rgba(30,30,70,0.82);
    }
    .wa-live-pill.active { display: block; }
    .wa-live-pill .wa-interim { color: rgba(180,180,255,0.55); font-style: italic; }
    .wa[data-theme="light"] .wa-live-pill .wa-interim { color: rgba(99,102,241,0.50); }

    /* Old overlay — fully retired, kept in DOM for compatibility but hidden */
    .wa-live-interim-word { color: rgba(180,180,255,0.55); font-style: italic; }
    .wa-live-overlay { display: none !important; }
    .wa-live-response { display: none; }
    .wa-live-speech { display: none; }

    /* ── VOICE BAR SIDE BUTTONS ───────────────────────────── */
    .wa-bar-btn {
      width: 44px; height: 44px; border-radius: 50%;
      border: 1.5px solid rgba(255,255,255,0.15);
      background: rgba(255,255,255,0.08);
      color: rgba(255,255,255,0.7);
      cursor: pointer; display: flex; align-items: center; justify-content: center;
      transition: background .15s, color .15s, transform .12s, border-color .15s;
      flex-shrink: 0;
    }
    .wa[data-theme="light"] .wa-bar-btn {
      border-color: rgba(99,102,241,0.25);
      background: rgba(99,102,241,0.08);
      color: rgba(99,102,241,0.7);
    }
    .wa-bar-btn:hover {
      background: rgba(255,255,255,0.14); color: #fff; transform: scale(1.06);
    }
    .wa[data-theme="light"] .wa-bar-btn:hover {
      background: rgba(99,102,241,0.15); color: rgba(99,102,241,0.9);
    }
    .wa-bar-btn:active { transform: scale(0.92); }
    .wa-bar-btn.active {
      background: rgba(99,102,241,0.25);
      border-color: rgba(99,102,241,0.6);
      color: #a78bfa;
    }

    /* ── TOAST ────────────────────────────────────────────── */
    .wa-toast {
      position:fixed;
      ${CFG.widget_position === 'bottom-left' ? 'left:24px' : 'right:24px'};
      bottom:100px;
      background:var(--bg3); border:1px solid var(--line2);
      color:var(--text); padding:10px 16px;
      border-radius:12px; font-size:13px; font-weight:500;
      z-index:2147483648;
      box-shadow: 0 8px 32px rgba(0,0,0,0.55);
      animation:wa-in .3s ease;
      max-width:300px; letter-spacing:-0.005em;
    }

    /* ── QUICK REPLIES ────────────────────────────────────── */
    .wa-suggestions {
      display:flex; gap:6px; flex-wrap:wrap;
      padding: 6px 14px 10px; flex-shrink:0;
      animation: wa-in .25s ease-out;
      position: relative; z-index: 1;
    }
    .wa-sug-btn {
      flex-shrink:0; padding:7px 16px;
      background: var(--p-lo); border:1px solid ${_waAlpha(PC, 28)};
      color:var(--p); font-size:12px; font-weight:500;
      font-family:inherit; border-radius:20px; cursor:pointer;
      transition: background .15s, border-color .15s, color .15s, transform .12s, box-shadow .15s;
      white-space:nowrap; outline:none;
      box-shadow: 0 1px 6px var(--p-lo);
    }
    .wa-sug-btn:hover {
      background: ${_waAlpha(PC, 22)};
      border-color: ${_waAlpha(PC, 55)};
      color:var(--p); transform: translateY(-2px);
      box-shadow: 0 4px 14px var(--p-md);
    }
    .wa-sug-btn:active { transform:scale(0.95); }

    /* ── LIVE VOICE ───────────────────────────────────────── */
    .wa-live-badge {
      display:inline-flex; align-items:center; gap:5px;
      font-size:10px; font-weight:700; color:var(--ok);
      letter-spacing:0.06em; text-transform:uppercase;
    }
    .wa-live-badge::before {
      content:''; width:7px; height:7px; border-radius:50%;
      background:var(--ok); animation:wa-pulse-dot 1s ease-in-out infinite;
    }
    /* Hidden in msgs while live — text appears in the overlay above the orb */
    .wa-bubble.wa-live-transcript { display: none; }
    .wa-bubble.wa-live-transcript .wa-interim { display: none; }

    /* ── DUAL-MODE MENU ───────────────────────────────────── */
    .wa-menu {
      position: fixed;
      ${CFG.widget_position === 'bottom-left' ? 'left:29px' : 'right:29px'};
      bottom: 94px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      z-index: 2147483646;
      opacity: 0;
      transform: translateY(16px) scale(0.85);
      pointer-events: none;
      transition: opacity .22s cubic-bezier(.34,1.56,.64,1), transform .22s cubic-bezier(.34,1.56,.64,1);
    }
    .wa-menu.open {
      opacity: 1;
      transform: translateY(0) scale(1);
      pointer-events: auto;
    }
    .wa-menu-btn {
      width: 50px;
      height: 50px;
      border-radius: 50%;
      border: 1px solid var(--line2, rgba(255,255,255,0.13));
      background: var(--bg2, #141426);
      color: var(--text, #f0f0f8);
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 4px 16px rgba(0,0,0,0.3);
      transition: background .2s, color .2s, transform .2s cubic-bezier(.34,1.56,.64,1), border-color .2s;
    }
    .wa-menu-btn:hover {
      background: var(--p);
      color: #fff;
      border-color: var(--p);
      transform: scale(1.1);
    }
    .wa-menu-btn:active {
      transform: scale(0.93);
    }
    .wa-menu-btn.active {
      background: var(--p);
      color: #fff;
      border-color: var(--p);
    }
    /* Pulse glow for Voice Nav Active state on main FAB */
    .wa-fab.voice-nav-active {
      animation: wa-pulse-glow 1.5s ease-in-out infinite alternate;
      background: linear-gradient(140deg, var(--ok) 0%, var(--p2) 100%) !important;
    }
    @keyframes wa-pulse-glow {
      0% { box-shadow: 0 0 0 0 rgba(52,211,153,0.6), 0 8px 28px rgba(0,0,0,0.5); }
      100% { box-shadow: 0 0 0 12px rgba(52,211,153,0), 0 8px 28px rgba(0,0,0,0.5); }
    }

    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after {
        animation-duration:0.01ms !important;
        transition-duration:0.01ms !important;
      }
    }
  `;
  shadow.appendChild(css);

  const root = document.createElement('div');
  root.className = 'wa';
  root.setAttribute('data-theme', localStorage.getItem('_wa_theme') || 'dark');
  root.innerHTML = `
    <!-- Dual-Mode Sub-Buttons Menu -->
    <div class="wa-menu" id="wa-menu">
      <button class="wa-menu-btn chat" id="wa-menu-chat" title="Chat Mode" aria-label="Open Chat Mode">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" style="pointer-events: none;">
          <path d="M20 2H4c-1.1 0-1.99.9-1.99 2L2 22l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zM6 9h12v2H6V9zm8 5H6v-2h8v2zm4-6H6V6h12v2z"/>
        </svg>
      </button>
      <button class="wa-menu-btn mic" id="wa-menu-mic" title="Voice Navigation Mode" aria-label="Open Voice Navigation Mode">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" style="pointer-events: none;">
          <path d="M12 14c1.66 0 2.99-1.34 2.99-3L15 5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3zm5.3-3c0 3-2.54 5.1-5.3 5.1S6.7 14 6.7 11H5c0 3.41 2.72 6.23 6 6.72V21h2v-3.28c3.28-.48 6-3.3 6-6.72h-1.7z"/>
        </svg>
      </button>
    </div>

    <button class="wa-fab" id="wa-fab" aria-label="Open AI Shopping Assistant" aria-expanded="false">
      <svg class="wa-fab-icon" width="26" height="26" viewBox="0 0 24 24" fill="none">
        <path d="M12 3L14.2 9.8L21 12L14.2 14.2L12 21L9.8 14.2L3 12L9.8 9.8L12 3Z" fill="white" opacity="0.95"/>
        <circle cx="19" cy="5" r="1.5" fill="white" opacity="0.6"/>
        <circle cx="5" cy="19" r="1" fill="white" opacity="0.4"/>
      </svg>
      <span class="wa-badge" id="wa-badge">0</span>
    </button>

    <div class="wa-pane" id="wa-pane" role="dialog" aria-label="Shopping Assistant">

      <!-- ── HEADER ── -->
      <div class="wa-header">
        <button class="wa-hbtn" id="wa-mute" title="Mute voice" aria-label="Toggle mute">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
            <path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02z"/>
          </svg>
        </button>
        <div class="wa-header-title">
          <span class="wa-header-brand">${CFG.store_name || 'WooAgent'}</span>
          <span class="wa-header-status" id="wa-header-status"></span>
        </div>
        <div class="wa-header-btns">
          <button class="wa-hbtn" id="wa-theme" title="Toggle light/dark mode" aria-label="Toggle theme">
            <svg id="wa-theme-icon" width="15" height="15" viewBox="0 0 24 24" fill="currentColor">
              <path d="M12 3a9 9 0 1 0 9 9c0-.46-.04-.92-.1-1.36a5.389 5.389 0 0 1-4.4 2.26 5.403 5.403 0 0 1-3.14-9.8c-.44-.06-.9-.1-1.36-.1z"/>
            </svg>
          </button>
          <button class="wa-hbtn" id="wa-clear" title="Clear chat" aria-label="Clear chat">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
              <path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/>
            </svg>
          </button>
          <button class="wa-hbtn" id="wa-close" title="Close" aria-label="Close">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
              <path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/>
            </svg>
          </button>
        </div>
      </div>

      <!-- ── VOICE ZONE (kept for JS compat, hidden) ── -->
      <div class="wa-voice-zone">
        <div class="wa-status-dot" id="wa-status-dot"></div>
        <span id="wa-status-text"></span>
      </div>

      <!-- ── MESSAGES ── -->
      <div class="wa-msgs" id="wa-msgs" role="log" aria-live="polite" aria-label="Conversation"></div>

      <!-- ── RECORDING STRIP (shown above voice bar when mic active) ── -->
      <div class="wa-record-strip" id="wa-record-strip">
        <div class="wa-rec-dot"></div>
        <span class="wa-rec-label">Listening…</span>
        <canvas class="wa-waveform" id="wa-waveform" width="100" height="24"></canvas>
      </div>

      <!-- ── TEXT INPUT BAR (hidden by default) ── -->
      <div class="wa-text-bar" id="wa-text-bar">
        <textarea class="wa-text-input" id="wa-input" placeholder="Type your message…" rows="1" aria-label="Message input"></textarea>
        <button class="wa-send-btn" id="wa-send" disabled aria-label="Send">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
            <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/>
          </svg>
        </button>
      </div>

      <!-- ── VOICE BAR ── -->
      <div class="wa-voice-bar">
        <div class="wa-orb-stage">
          <div class="wa-wave-ring"></div>
          <div class="wa-wave-ring"></div>
          <div class="wa-wave-ring"></div>
          <button class="wa-orb" id="wa-orb" aria-label="Tap to speak"></button>
        </div>
        <p class="wa-orb-label" id="wa-orb-hint">Tap to speak</p>
        <!-- ── LIVE TRANSCRIPT PILL — shows SR interim text while recording ── -->
        <div class="wa-live-pill" id="wa-live-pill"></div>
        <!-- Legacy overlays hidden -->
        <div class="wa-live-overlay" id="wa-live-overlay">
          <div class="wa-live-response" id="wa-live-response"></div>
          <div class="wa-live-speech" id="wa-live-speech"></div>
        </div>
        <div class="wa-zone-btns">
          <button class="wa-zone-btn" aria-label="Attachment" style="visibility:hidden">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>
            </svg>
          </button>
          <button class="wa-zone-btn" id="wa-keyboard" aria-label="Toggle keyboard">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 20h9"/>
              <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>
            </svg>
          </button>
        </div>
      </div>

    </div>
  `;
  shadow.appendChild(root);

  const $ = id => shadow.getElementById(id);
  const menu = $('wa-menu');
  const menuChat = $('wa-menu-chat');
  const menuMic = $('wa-menu-mic');
  const fab = $('wa-fab');
  const pane = $('wa-pane');
  const msgs = $('wa-msgs');
  const orb = $('wa-orb');
  const waveform = $('wa-waveform');
  const orbHint = $('wa-orb-hint');
  const livePill = $('wa-live-pill');
  const input = $('wa-input');
  const sendBtn = $('wa-send');
  const badge = $('wa-badge');
  const muteBtn = $('wa-mute');
  const closeBtn = $('wa-close');
  const clearBtn = $('wa-clear');
  const statusTxt = $('wa-status-text');

  function b64ToObjectUrl(b64, format) {
    try {
      const mime = (format === 'mp3') ? 'audio/mpeg' : 'audio/wav';
      const binary = atob(b64);
      const len = binary.length;
      const bytes = new Uint8Array(len);
      for (let i = 0; i < len; i += 1) {
        bytes[i] = binary.charCodeAt(i);
      }
      return URL.createObjectURL(new Blob([bytes], { type: mime }));
    } catch (error) {
      return null;
    }
  }

  function playAudioB64(b64, format) {
    if (S.muted || !b64) return Promise.resolve(false);
    const fmt = format || 'wav';
    return new Promise(resolve => {
      stopCurrentAudio();

      let finished = false;
      let started = false;
      let objectUrl = null;
      let startWatchdog = null;
      let maxWatchdog = null;

      const audio = new Audio();
      audio.preload = 'auto';
      audio.playsInline = true;
      objectUrl = b64ToObjectUrl(b64, fmt);
      const mime = (fmt === 'mp3') ? 'audio/mpeg' : 'audio/wav';
      audio.src = objectUrl || (`data:${mime};base64,` + b64);
      S.currentAudio = audio;

      const cleanup = (ok) => {
        if (finished) return;
        finished = true;
        if (startWatchdog) clearTimeout(startWatchdog);
        if (maxWatchdog) clearTimeout(maxWatchdog);
        fab.classList.remove('speaking');
        orb.classList.remove('speaking');
        S.speaking = false;
        if (S.currentAudio === audio) S.currentAudio = null;
        if (objectUrl) URL.revokeObjectURL(objectUrl);
        resolve(Boolean(ok));
      };

      // Expose cleanup so stopCurrentAudio() can resolve this promise
      // instead of leaving it pending when audio is interrupted externally.
      audio._cleanup = cleanup;

      fab.classList.add('speaking');
      orb.classList.add('speaking');
      S.speaking = true;
      // Stop mic while bot speaks — prevents the bot's own audio from being
      // picked up by the microphone and sent back as a user message.
      // Null BEFORE abort so the onend callback skips the auto-restart path.
      if (liveRecognition) {
        const _r = liveRecognition;
        liveRecognition = null;
        try { _r.abort(); } catch(e) {}
      }

      audio.addEventListener('play', () => { started = true; }, { once: true });
      audio.addEventListener('playing', () => { started = true; }, { once: true });
      audio.addEventListener('ended', () => cleanup(true), { once: true });
      audio.addEventListener('error', (e) => {
        console.warn('[WooAgent] audio error', e);
        cleanup(false);
      }, { once: true });

      // Watchdog 1: audio hasn't started within 8 s → give up
      startWatchdog = setTimeout(() => {
        if (!started) { console.warn('[WooAgent] audio start watchdog'); cleanup(false); }
      }, 8000);

      // Watchdog 2: hard cap of 90 s — prevents S.speaking getting stuck forever
      // if 'ended' never fires (e.g. network stall mid-play with no error event).
      maxWatchdog = setTimeout(() => {
        if (!finished) { console.warn('[WooAgent] audio max-duration watchdog'); cleanup(false); }
      }, 90000);

      audio.load();
      audio.play().catch((e) => { console.warn('[WooAgent] play() rejected:', e); cleanup(false); });
    });
  }

  function stopCurrentAudio() {
    if (S.currentAudio) {
      const a = S.currentAudio;
      S.currentAudio = null; // clear BEFORE calling _cleanup to avoid re-entry
      if (a._cleanup) a._cleanup(false); // resolve the pending playAudioB64 promise
      try { a.pause(); } catch (e) {}
    }
    if (window.speechSynthesis) window.speechSynthesis.cancel();
    fab.classList.remove('speaking');
    orb.classList.remove('speaking');
    S.speaking = false;
  }



  // Callers may pass (audioB64, text, lang, format) — text/lang unused since browser TTS removed.
  async function speakWithFallback(audioB64, _text, _lang, format) {
    if (S.muted || !audioB64) return;
    await playAudioB64(audioB64, format || 'wav');
  }

  function getSupportedMimeType() {
    const candidates = [
      'audio/webm;codecs=opus',
      'audio/webm',
      'audio/ogg;codecs=opus',
      'audio/ogg',
      'audio/mp4;codecs=mp4a.40.2',
      'audio/mp4',
    ];
    for (const type of candidates) {
      try {
        if (typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported(type)) return type;
      } catch (e) { /* ignore */ }
    }
    return '';
  }

  function hexToRgba(hex, alpha) {
    try {
      const h = (hex || '#6366f1').replace('#', '');
      const r = parseInt(h.slice(0, 2), 16);
      const g = parseInt(h.slice(2, 4), 16);
      const b = parseInt(h.slice(4, 6), 16);
      return `rgba(${r},${g},${b},${alpha})`;
    } catch (e) {
      return `rgba(99,102,241,${alpha})`;
    }
  }

  function primeAudioEngines() {
    // Unlock Web Audio API (needed for analyser/waveform)
    try {
      const Ctx = window.AudioContext || window['webkitAudioContext'];
      if (Ctx) {
        const ctx = new Ctx();
        if (ctx.state === 'suspended') {
          ctx.resume().catch(() => {}).finally(() => ctx.close().catch(() => {}));
        } else {
          ctx.close().catch(() => {});
        }
      }
    } catch (e) {}
    // Unlock HTMLAudioElement playback (iOS/Safari require a play() call
    // inside a synchronous user-gesture handler before async play() works).
    try {
      const a = new Audio();
      // 44-byte silent WAV — just enough to satisfy the browser's autoplay policy.
      a.src = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=';
      a.volume = 0;
      a.play().catch(() => {});
    } catch (e) {}
  }

  // Initialize state machine properties in S
  S.menuOpen = false;
  S.mode = 'idle'; // 'idle', 'chat', 'voice_nav'

  function toggleMenu() {
    S.menuOpen = !S.menuOpen;
    if (S.menuOpen) {
      menu.classList.add('open');
      fab.classList.add('open');
      fab.setAttribute('aria-expanded', 'true');
    } else {
      menu.classList.remove('open');
      fab.classList.remove('open');
      fab.setAttribute('aria-expanded', 'false');
    }
  }

  function closeMenu() {
    S.menuOpen = false;
    menu.classList.remove('open');
    fab.classList.remove('open');
    fab.setAttribute('aria-expanded', 'false');
  }

  function startChatMode() {
    primeAudioEngines();
    closeMenu();
    S.mode = 'chat';
    openPane();
  }

  function startVoiceNavMode() {
    primeAudioEngines();
    closeMenu();
    if (S.open) {
      closePane();
    }
    S.mode = 'voice_nav';
    fab.classList.add('voice-nav-active');
    startLiveMode();
    showToast('🎙️ Voice Navigation Mode Active');
  }

  function resumeVoiceNavMode() {
    closeMenu();
    if (S.open) {
      closePane();
    }
    S.mode = 'voice_nav';
    fab.classList.add('voice-nav-active');
    showToast('🎙️ Voice Navigation active. Tap anywhere to talk.');

    const resumeGesture = () => {
      primeAudioEngines();
      startLiveMode();
      document.body.removeEventListener('click', resumeGesture);
      document.body.removeEventListener('touchend', resumeGesture);
    };

    document.body.addEventListener('click', resumeGesture);
    document.body.addEventListener('touchend', resumeGesture, { passive: true });
  }

  function stopVoiceNavMode() {
    S.mode = 'idle';
    fab.classList.remove('voice-nav-active');
    stopLiveMode();
    showToast('🎙️ Voice Navigation Mode Stopped');
  }

  fab.addEventListener('click', () => {
    if (S.open) {
      closePane();
      return;
    }
    if (S.mode === 'voice_nav') {
      stopVoiceNavMode();
      return;
    }
    toggleMenu();
  });

  menuChat.addEventListener('click', startChatMode);
  menuMic.addEventListener('click', startVoiceNavMode);

  closeBtn.addEventListener('click', closePane);

  // ── Theme toggle ─────────────────────────────────────────────────────
  const themeBtn = $('wa-theme');
  const themeIcon = shadow.getElementById('wa-theme-icon');
  const MOON_PATH = 'M12 3a9 9 0 1 0 9 9c0-.46-.04-.92-.1-1.36a5.389 5.389 0 0 1-4.4 2.26 5.403 5.403 0 0 1-3.14-9.8c-.44-.06-.9-.1-1.36-.1z';
  const SUN_PATH  = 'M12 7c-2.76 0-5 2.24-5 5s2.24 5 5 5 5-2.24 5-5-2.24-5-5-5zM2 13h2c.55 0 1-.45 1-1s-.45-1-1-1H2c-.55 0-1 .45-1 1s.45 1 1 1zm18 0h2c.55 0 1-.45 1-1s-.45-1-1-1h-2c-.55 0-1 .45-1 1s.45 1 1 1zM11 2v2c0 .55.45 1 1 1s1-.45 1-1V2c0-.55-.45-1-1-1s-1 .45-1 1zm0 18v2c0 .55.45 1 1 1s1-.45 1-1v-2c0-.55-.45-1-1-1s-1 .45-1 1zM5.99 4.58a.996.996 0 0 0-1.41 0 .996.996 0 0 0 0 1.41l1.06 1.06c.39.39 1.03.39 1.41 0s.39-1.03 0-1.41L5.99 4.58zm12.37 12.37a.996.996 0 0 0-1.41 0 .996.996 0 0 0 0 1.41l1.06 1.06c.39.39 1.03.39 1.41 0a.996.996 0 0 0 0-1.41l-1.06-1.06zm1.06-10.96a.996.996 0 0 0 0-1.41.996.996 0 0 0-1.41 0l-1.06 1.06c-.39.39-.39 1.03 0 1.41s1.03.39 1.41 0l1.06-1.06zM7.05 18.36a.996.996 0 0 0 0-1.41.996.996 0 0 0-1.41 0l-1.06 1.06c-.39.39-.39 1.03 0 1.41s1.03.39 1.41 0l1.06-1.06z';

  function applyTheme(mode) {
    root.setAttribute('data-theme', mode);
    localStorage.setItem('_wa_theme', mode);
    if (themeIcon) {
      const pathEl = themeIcon.querySelector('path');
      if (pathEl) pathEl.setAttribute('d', mode === 'dark' ? MOON_PATH : SUN_PATH);
    }
    themeBtn && (themeBtn.title = mode === 'dark' ? 'Switch to light mode' : 'Switch to dark mode');
  }

  // Sync icon to initial stored theme
  applyTheme(localStorage.getItem('_wa_theme') || 'dark');

  if (themeBtn) {
    themeBtn.addEventListener('click', () => {
      const next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      applyTheme(next);
    });
  }

  // Keyboard toggle — show/hide the text input bar
  const keyboardBtn = $('wa-keyboard');
  const textBar = $('wa-text-bar');
  if (keyboardBtn && textBar) {
    // Restore user's last text-bar preference, or auto-show if voice is disabled
    const _isMobileUA = /iPhone|iPad|iPod|Android/i.test(navigator.userAgent);
    if (!CFG.enable_voice || localStorage.getItem('_wa_text_mode') === '1') {
      textBar.classList.add('visible');
      keyboardBtn.classList.add('active');
    }
    keyboardBtn.addEventListener('click', () => {
      const visible = textBar.classList.toggle('visible');
      keyboardBtn.classList.toggle('active', visible);
      localStorage.setItem('_wa_text_mode', visible ? '1' : '0');
      if (visible && !_isMobileUA) input.focus();
    });
  }

  muteBtn.addEventListener('click', () => {
    S.muted = !S.muted;
    muteBtn.style.color = S.muted ? 'var(--err)' : '';
    muteBtn.title = S.muted ? 'Unmute' : 'Mute sound';
    if (S.muted) {
      stopCurrentAudio();
    }
  });

  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      stopCurrentAudio();

      // Wipe conversation from memory and localStorage
      S.conversation = [];
      localStorage.removeItem('_wa_conv');
      localStorage.removeItem('_wa_greeted');

      // New session ID so backend starts a fresh context (no old history leakage)
      const newId = 'wa_' + Date.now() + '_' + Math.random().toString(36).slice(2, 9);
      S.sessionId = newId;
      localStorage.setItem('_wa_sid_v2', newId);

      // Clear message DOM
      msgs.innerHTML = '';

      // Re-run greeting through the existing path
      S.greeted = true;
      localStorage.setItem('_wa_greeted', '1');
      fetchGreeting();
    });
  }

  function openPane() {
    primeAudioEngines();
    S.open = true;
    pane.classList.add('open');
    fab.classList.add('open');
    fab.setAttribute('aria-expanded', 'true');
    // Only focus text input when it's actually visible (hidden by default in voice-first mode)
    if (textBar && textBar.classList.contains('visible')) input.focus();
    if (!S.greeted) {
      S.greeted = true;
      localStorage.setItem('_wa_greeted', '1');
      fetchGreeting();
      // Pre-connect WebSocket so text input works immediately without tapping orb
      if (A2A_ENABLED) setTimeout(() => { if (S.open && !isA2AConnected) _startA2AForText(); }, 800);
    } else {
      // Restore last conversation messages from localStorage when re-opening
      let saved = [];
      try { saved = JSON.parse(localStorage.getItem('_wa_conv') || '[]'); } catch (e) { }
      if (saved.length && msgs.children.length === 0) {
        const toShow = saved.slice(-6);
        toShow.forEach(m => {
          if (m.role === 'user' || m.role === 'assistant') {
            addBubble(m.role === 'user' ? 'user' : 'bot', m.content);
          }
        });
      }
      // Auto-start listening when re-opening (greeting already done)
      if (CFG.enable_voice && !isLiveMode) {
        setTimeout(() => { if (S.open && !isLiveMode) startLiveMode(); }, 600);
      }
    }
  }

  function closePane() {
    S.open = false;
    S.mode = 'idle';
    S._requestingMic = false; // cancel any pending getUserMedia guard
    pane.classList.remove('open');
    fab.classList.remove('open');
    fab.setAttribute('aria-expanded', 'false');
    stopCurrentAudio();
    if (isLiveMode) stopLiveMode();
  }

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && S.open) closePane();
  });

  async function fetchGreeting() {
    // Guard: backend URL must be configured in WP admin → WooAgent settings
    if (!CFG.agent_api_url || CFG.agent_api_url === '') {
      addBubble('bot', '⚙️ Assistant is not configured yet. Please set the backend URL in your store settings.');
      setStatus('Not configured');
      return;
    }
    setStatus('Connecting...');
    try {
      const r = await api('/greet', {
        session_id: S.sessionId,
        store_name: CFG.store_name,
        language: S.language,
        current_page: {
          url: location.href,
          title: document.title,
          product_id: detectProductId(),
          product_name: detectProductName()
        }
      });

      S.language = r.language_detected || r.language || S.language;
      if (r.has_cart && r.cart_summary) {
        updateBadge(r.cart_summary.item_count);
      }

      addBubble('bot', r.greeting_text || `Hi! Welcome to ${CFG.store_name}. How can I help you today?`);
      setStatus('Online · ' + CFG.store_name);

      if (Array.isArray(r.suggested_replies) && r.suggested_replies.length) {
        renderSuggestedReplies(r.suggested_replies);
      }

      // Start live mode; after greeting audio finishes, resume mic via onSpeakingEnd.
      if (CFG.enable_voice && S.open && !isLiveMode) {
        setTimeout(() => { if (S.open && !isLiveMode) startLiveMode(); }, 400);
      }
      // Play greeting audio (non-blocking).
      // onSpeakingEnd() must be called after audio ends so mic restarts —
      // fetchGreeting has no outer .then() like sendToAgent does.
      speakWithFallback(
        r.audio_base64,
        r.greeting_text || `Hi! Welcome to ${CFG.store_name}. How can I help you today?`,
        S.language,
        r.audio_format
      ).then(() => {
        if (!S.speaking && !S.loading) onSpeakingEnd();
      }).catch(() => {
        if (!S.speaking && !S.loading) onSpeakingEnd();
      });
    } catch (error) {
      addBubble('bot', `Hi! Welcome to ${CFG.store_name}. How can I help you today?`);
      setStatus('Online · ' + CFG.store_name);
      if (CFG.enable_voice && S.open && !isLiveMode) {
        setTimeout(() => { if (S.open && !isLiveMode) startLiveMode(); }, 400);
      }
    }
  }

  let waveAnimId = null;
  let analyser = null;
  let audioContext = null;
  let audioSource = null;

  // Live voice mode state
  let isLiveMode = false;
  let liveRecognition = null;
  let liveTranscriptEl = null;
  let liveRetryCount = 0;
  const LIVE_MAX_RETRIES = 5;

  // Language establishment flag — true only after Whisper has confirmed the real language.
  // Until then, we always use Whisper (push-to-talk) to avoid Chrome SR transcribing
  // Malayalam/Tamil/etc. as garbage English phonetics.
  // Initialized to true only if language is already a confirmed Dravidian/non-English language
  // (e.g. restored from localStorage) since those always use Whisper anyway.
  let _langEstablished = !!(S.language && S.language !== 'en' && S.language !== 'auto');

  function drawRounded(c, x, y, w, h, r) {
    if (typeof c.roundRect === 'function') {
      c.beginPath();
      c.roundRect(x, y, w, h, r);
      c.fill();
      return;
    }
    c.beginPath();
    c.moveTo(x + r, y);
    c.lineTo(x + w - r, y);
    c.quadraticCurveTo(x + w, y, x + w, y + r);
    c.lineTo(x + w, y + h - r);
    c.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    c.lineTo(x + r, y + h);
    c.quadraticCurveTo(x, y + h, x, y + h - r);
    c.lineTo(x, y + r);
    c.quadraticCurveTo(x, y, x + r, y);
    c.closePath();
    c.fill();
  }

  const SILENCE_THRESHOLD = 18;   // 0-255 — raised from 6 to ignore ambient noise / fan hum
  const SILENCE_DURATION  = 2500; // ms of silence before auto-stop

  function startWaveform(stream) {
    stopWaveform(); // cancel any lingering animation loop from a previous recording
    try {
      audioContext = audioContext || new (window.AudioContext || window['webkitAudioContext'])();
      audioSource = audioContext.createMediaStreamSource(stream);
      analyser = audioContext.createAnalyser();
      analyser.fftSize = 64;
      audioSource.connect(analyser);
      const bufLen = analyser.frequencyBinCount;
      const data = new Uint8Array(bufLen);
      const canvas = waveform;
      const c = canvas.getContext('2d');
      waveform.classList.add('active');

      // null = no sound heard yet; silence timer only starts after FIRST sound
      let lastSoundTime = null;
      const recordingStartTime = Date.now();
      const MIN_RECORD_MS = 1500; // never auto-stop in the first 1.5s

      (function draw() {
        waveAnimId = requestAnimationFrame(draw);
        analyser.getByteFrequencyData(data);

        // Voice activity detection — auto-stop on sustained silence
        const maxVal = Math.max.apply(null, data);
        const MAX_RECORD_MS = 15000; // 15 seconds hard cap to prevent getting stuck
        if (maxVal > SILENCE_THRESHOLD) {
          lastSoundTime = Date.now();
        } else if (
          S.recording &&
          lastSoundTime !== null && // only count silence AFTER first speech detected
          (Date.now() - recordingStartTime) > MIN_RECORD_MS &&
          (Date.now() - lastSoundTime) > SILENCE_DURATION
        ) {
          stopRecording();
          return;
        }

        // Hard cap watchdog fallback
        if (S.recording && (Date.now() - recordingStartTime) > MAX_RECORD_MS) {
          console.warn('[WooAgent] Max recording duration reached');
          stopRecording();
          return;
        }

        c.clearRect(0, 0, waveform.width, waveform.height);
        const barW = 4;
        const gap = 3;
        const total = Math.floor(160 / (barW + gap));
        const step = Math.max(1, Math.floor(bufLen / total));

        for (let i = 0; i < total; i++) {
          const val = data[Math.min(i * step, data.length - 1)] / 255;
          const h = Math.max(3, val * 28);
          const y = (32 - h) / 2;
          const alpha = 0.4 + val * 0.6;
          c.fillStyle = hexToRgba(CFG.primary_color || '#6366f1', alpha);
          drawRounded(c, i * (barW + gap), y, barW, h, 2);
        }
      })();
    } catch (error) {
      // ignore waveform failures
    }
  }

  function stopWaveform() {
    if (waveAnimId) {
      cancelAnimationFrame(waveAnimId);
      waveAnimId = null;
    }
    if (audioSource) {
      try { audioSource.disconnect(); } catch (e) { /* ignore */ }
      audioSource = null;
    }
    if (audioContext) {
      try { audioContext.close(); } catch (e) { /* ignore */ }
      audioContext = null;
    }
    analyser = null;
    waveform.classList.remove('active');
    const c = waveform.getContext('2d');
    c.clearRect(0, 0, waveform.width, waveform.height);
  }

  async function startRecording() {
    // Guard: don't start while already recording, awaiting mic permission,
    // loading (agent thinking), speaking (bot audio playing), or muted.
    if (S.recording || S._requestingMic || S.loading || S.speaking || !CFG.enable_voice || S.muted) return;
    stopCurrentAudio();
    // mediaDevices is only available on HTTPS or localhost
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      addBubble('bot', 'Voice input requires a secure (HTTPS) connection. Please type your message below.');
      return;
    }
    S._requestingMic = true; // prevent concurrent getUserMedia calls during permission prompt
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, sampleRate: 16000 }
      });
      // After await: check guards again — user may have closed pane or muted while prompt was showing
      if (!CFG.enable_voice || S.muted || (!S.open && S.mode !== 'voice_nav')) {
        stream.getTracks().forEach(t => t.stop());
        S._requestingMic = false;
        return;
      }
      S.audioChunks = [];
      const detectedMime = getSupportedMimeType();
      S._recordingMimeType = detectedMime || 'audio/webm';
      S.mediaRecorder = new MediaRecorder(stream, detectedMime ? { mimeType: detectedMime } : {});
      S.mediaRecorder.ondataavailable = e => {
        if (e.data && e.data.size > 0) S.audioChunks.push(e.data);
      };
      S.mediaRecorder.onstop = async () => {
        stream.getTracks().forEach(t => t.stop());
        stopWaveform();
        await processVoice();
      };
      S.mediaRecorder.start(100);
      S.recording = true;
      orb.classList.add('recording');
      orbHint.innerHTML = isLiveMode
        ? '<span class="wa-live-badge">Live</span> <strong>Listening…</strong>'
        : '<strong>Listening…</strong> Tap to stop';
      const recStrip = $('wa-record-strip'); if (recStrip) recStrip.classList.add('active');
      startWaveform(stream);
    } catch (err) {
      S.recording = false;
      orb.classList.remove('recording');
      orbHint.innerHTML = isLiveMode ? '<strong>Tap to speak</strong> · tap again to stop' : '<strong>Tap to speak</strong> · or type below';
      console.warn('[WooAgent] Mic error:', err && err.name, err);
      let errMsg = '';
      if (err && (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError')) {
        errMsg = 'Mic access was denied. Please allow microphone access in your browser settings, then try again.';
      } else if (err && err.name === 'NotFoundError') {
        errMsg = 'No microphone found. Please connect a mic or type below.';
      } else if (err && err.name === 'NotReadableError') {
        errMsg = 'Microphone is in use by another app. Please close it and try again.';
      } else if (err && err.name === 'SecurityError') {
        errMsg = 'Voice requires a secure (HTTPS) connection. You can type below instead.';
      } else {
        errMsg = 'Could not start recording (' + (err && err.name ? err.name : 'unknown') + '). Please try again or type below.';
      }
      if (S.open) {
        addBubble('bot', errMsg);
      } else {
        showToast('❌ ' + errMsg);
      }
    } finally {
      S._requestingMic = false;
    }
  }

  function stopRecording() {
    if (!S.recording) return;
    S.recording = false;
    S._requestingMic = false; // safety reset
    const recorder = S.mediaRecorder;
    S.mediaRecorder = null;
    orb.classList.remove('recording');
    const recStrip2 = $('wa-record-strip'); if (recStrip2) recStrip2.classList.remove('active');
    orbHint.innerHTML = 'Processing…';
    try {
      if (recorder) recorder.stop();
    } catch (e) {
      // ignore stop race
    }
  }

  // Tap orb = push-to-talk (Whisper path) or mute/unmute (SR path).
  function orbTap() {
    primeAudioEngines();
    if (!isLiveMode) {
      stopRecording();
      startLiveMode();
      return;
    }

    // ── NEW: A2A mode ──
    if (A2A_ENABLED) {
      if (isA2AConnected && !a2aStream) {
        // WebSocket open (text-only) — user tapped orb to add voice, start mic now
        isLiveMode = true;
        orb.classList.add('live');
        orbHint.innerHTML = '<span class="wa-live-badge">Live</span> <strong>Listening…</strong>';
        _a2aStartCapture();
      } else {
        stopLiveMode();
      }
      return;
    }

    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    const useWhisper = _useWhisperForLang(S.language) || !SR || !_langEstablished;
    // Whisper path: orb tap = push-to-talk toggle (start/stop recording)
    if (useWhisper) {
      if (S.recording) {
        stopRecording(); // stop → transcribe → send
      } else if (!S.loading && !S.speaking && !S.muted) {
        startRecording(); // start listening
      }
      return;
    }
    // SR path: tap to start listening if idle, or stop live mode if already running.
    // Muting is handled by the dedicated mute button — not the orb.
    if (S.muted) return; // muted: ignore orb tap
    if (!liveRecognition && !S.loading && !S.speaking) {
      // Recognition stopped (e.g. after audio played): restart it immediately
      startLiveRecognition();
    } else if (liveRecognition) {
      // Already listening: tap stops live mode (back to idle)
      stopLiveMode();
    }
    // If loading or speaking: ignore tap (let the agent finish)
  }
  orb.addEventListener('click', orbTap);
  orb.addEventListener('touchstart', e => { e.preventDefault(); primeAudioEngines(); }, { passive: false });
  orb.addEventListener('touchend', e => { e.preventDefault(); orbTap(); }, { passive: false });

  async function processVoice() {
    if (!S.audioChunks.length) return;
    // Widget may have been closed while recording was still in-flight
    if (!S.open && S.mode !== 'voice_nav') { S.audioChunks = []; return; }
    const mimeType = S._recordingMimeType || 'audio/webm';
    const blob = new Blob(S.audioChunks, { type: mimeType });
    S.audioChunks = []; // clear immediately so re-entrant calls don't reprocess same data
    if (blob.size < 1000) {
      if (isLiveMode) {
        orbHint.innerHTML = '<span class="wa-live-badge">Live</span> <strong>Listening…</strong>';
        setTimeout(() => { if (isLiveMode && (S.open || S.mode === 'voice_nav') && !S.loading && !S.speaking && !S.recording) startRecording(); }, 1000);
      }
      return;
    }

    // Block other sends during transcription — prevents a simultaneous text
    // message from grabbing S.loading and causing the voice transcript to be dropped.
    S.loading = true;
    sendBtn.disabled = true;
    orb.classList.add('thinking');
    showTyping();
    orbHint.innerHTML = 'Processing...';

    try {
      const ext = mimeType.includes('mp4') ? 'mp4' : mimeType.includes('ogg') ? 'ogg' : 'webm';
      const form = new FormData();
      form.append('audio', blob, `voice.${ext}`);
      form.append('session_id', S.sessionId);
      // Send language hint when language is confirmed — speeds up Whisper and avoids hallucination.
      // When _langEstablished=false (first utterance), send NO hint so Whisper auto-detects the real language.
      if (_langEstablished && S.language && S.language !== 'auto') {
        form.append('language', S.language);
      }

      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 20000); // Groq Whisper on long clips can take 8-15 s
      const res = await fetch(`${CFG.agent_api_url}/api/v1/transcribe`, {
        method: 'POST',
        headers: {
          'X-WooAgent-Session': S.sessionId,
          'X-WooAgent-Nonce': CFG.nonce || ''
        },
        body: form,
        signal: controller.signal
      });
      clearTimeout(timer);

      const data = await res.json();
      removeTyping();
      orbHint.innerHTML = '<strong>Tap to speak</strong> · tap again to stop';

      // Threshold: 0.1 (very permissive) — Whisper logprob for Indian languages can be low
      // even for accurate transcriptions. Only reject truly unintelligible audio.
      if (!data.transcript || Number(data.confidence || 0) < 0.1) {
        // Release loading lock before restarting mic
        S.loading = false;
        sendBtn.disabled = !input.value.trim();
        orb.classList.remove('thinking');
        const errMsg = "Couldn't catch that clearly. Could you try again?";
        if (S.open) {
          addBubble('bot', errMsg);
        } else {
          showToast('❌ ' + errMsg);
        }
        if (isLiveMode && (S.open || S.mode === 'voice_nav')) {
          orbHint.innerHTML = '<span class="wa-live-badge">Live</span> <strong>Listening…</strong>';
          setTimeout(() => { if (isLiveMode && (S.open || S.mode === 'voice_nav') && !S.loading && !S.speaking && !S.recording) startRecording(); }, 1200);
        }
        return;
      }

      if (data.language && data.language !== 'unknown') {
        S.language = String(data.language).slice(0, 2);
        _langEstablished = true; // real language confirmed via Whisper — unlock SR for English
      }

      // Release loading lock — sendToAgent will re-acquire it immediately
      S.loading = false;
      sendBtn.disabled = !input.value.trim();
      orb.classList.remove('thinking');

      addBubble('user', data.transcript);
      await sendToAgent(data.transcript);
    } catch (error) {
      S.loading = false;
      sendBtn.disabled = !input.value.trim();
      orb.classList.remove('thinking');
      removeTyping();
      const errMsg = 'Voice processing failed. Please try again.';
      if (S.open) {
        addBubble('bot', errMsg);
      } else {
        showToast('❌ ' + errMsg);
      }
      if (isLiveMode) {
        orbHint.innerHTML = '<span class="wa-live-badge">Live</span> <strong>Listening…</strong>';
        setTimeout(() => { if (isLiveMode && (S.open || S.mode === 'voice_nav') && !S.loading && !S.speaking && !S.recording) startRecording(); }, 1500);
      }
    }
  }

  input.addEventListener('input', () => {
    sendBtn.disabled = !input.value.trim() || S.loading;
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 90) + 'px';
  });

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (!sendBtn.disabled) sendText();
    }
  });

  sendBtn.addEventListener('click', sendText);

  function sendText() {
    const text = input.value.trim();
    if (!text || S.loading) return;
    input.value = '';
    input.style.height = 'auto';
    sendBtn.disabled = true;
    addBubble('user', text);
    // New user turn — reset the streaming bot bubble so the next response gets a fresh one
    _a2aStreamBubble = null;
    _a2aStreamText   = '';
    // Keep focus so the user can immediately type the next message (desktop only)
    if (!/iPhone|iPad|iPod|Android/i.test(navigator.userAgent)) input.focus();
    sendToAgent(text);
  }

  async function sendToAgent(message) {
    if (S.loading) return;
    clearSuggestions();
    // Clear live transcript pill — agent is now processing
    if (livePill) { livePill.classList.remove('active'); livePill.innerHTML = ''; }

    // Typed text now goes over plain HTTP (POST /api/v1/chat → the Brain), NOT the
    // voice WebSocket. Routing text through Gemini Live made it chat instead of
    // search, and the socket's drops/reconnects caused lost turns + latency. Live
    // VOICE (mic) still uses the WebSocket; only typed/suggested/programmatic text
    // falls through to the reliable HTTP path below.

    S.loading = true;
    sendBtn.disabled = true;
    setStatus('Thinking...');
    orb.classList.add('thinking');
    showTyping();
    // Capture address draft from current state BEFORE sending (for fields already being collected)
    const stateBeforeRequest = S.addressState;
    S.conversation.push({ role: 'user', content: String(message || '') });
    S.conversation = S.conversation.slice(-20);
    // Persist conversation to localStorage
    try { localStorage.setItem('_wa_conv', JSON.stringify(S.conversation)); } catch (e) { }

    try {
      const payload = {
        session_id: S.sessionId,
        message: message,
        message_type: 'text',
        language: S.language || 'en',
        store_url: location.origin,
        store_name: CFG.store_name,
        cart_context: (S.cartSnapshot && typeof S.cartSnapshot === 'object' && !Array.isArray(S.cartSnapshot)) ? S.cartSnapshot : {},
        current_page: {
          url: location.href,
          title: document.title,
          product_id: detectProductId(),
          product_name: detectProductName()
        }
      };

      const r = await apiChat(payload);

      removeTyping();
      setStatus('Online · ' + CFG.store_name);

      if (r.language) S.language = r.language;
      if (r.address_state) {
        S.addressState = r.address_state;
        localStorage.setItem('_wa_addr_state', r.address_state);
      }

      // Capture address data AFTER agent response — address_state is now the NEW step
      // e.g. if agent just asked for email, stateBeforeRequest was collecting_phone
      // Capture the user's message against what the agent was collecting
      captureAddressDraftFromStep(stateBeforeRequest, message);

      if (r.address_data && typeof r.address_data === 'object') {
        const addr = Object.assign({}, S.addressDraft || {}, r.address_data || {});
        S.addressDraft = addr;
        localStorage.setItem('_wa_addr_draft', JSON.stringify(addr));
        persistCheckoutAddress({ billing: addr, shipping: addr });
        if (isCheckoutPage()) {
          applyStoredCheckoutAddress();
        }
      }

      // Always re-apply checkout address on checkout page (WC Blocks re-renders quickly)
      if (isCheckoutPage()) {
        applyStoredCheckoutAddress();
      }

      if (r.text || r.response_text) {
        addBubble('bot', r.text || r.response_text);
        // Clear live overlay speech text while agent responds
        if (isLiveMode) {
          updateLiveSpeech('', '');
        }
      }
      if (r.text || r.response_text) {
        S.conversation.push({ role: 'assistant', content: String(r.text || r.response_text || '') });
        S.conversation = S.conversation.slice(-20);
        // Persist conversation
        try { localStorage.setItem('_wa_conv', JSON.stringify(S.conversation)); } catch (e) { }
      }

      // Deduplicate actions — show_products / show_product_detail / show_availability share one slot.
      // show_availability wins over show_products (has precise stock info) so sort it first.
      const PRODUCT_DISPLAY_TYPES = new Set(['show_products', 'show_product_detail', 'show_availability']);
      const rawActions = r.ui_actions || r.actions || [];
      const sortedActions = [
        ...rawActions.filter(a => a && a.type === 'show_availability'),
        ...rawActions.filter(a => a && a.type !== 'show_availability'),
      ];
      const seenActionTypes = new Set();
      const dedupedActions = [];
      for (const act of sortedActions) {
        const typeKey = act.type;
        const dedupKey = PRODUCT_DISPLAY_TYPES.has(typeKey) ? 'product_display' : typeKey;
        if (seenActionTypes.has(dedupKey)) continue;
        seenActionTypes.add(dedupKey);
        dedupedActions.push(act);
      }

      for (const act of dedupedActions) {
        await processAction(act);
      }

      if (r.address_state && r.address_state !== 'idle' && r.address_state !== 'complete') {
        renderAddressProgress(r.address_state);
      }

      if (Array.isArray(r.suggested_replies) && r.suggested_replies.length) {
        renderSuggestedReplies(r.suggested_replies);
      }

      // Release loading lock NOW — user can type/interact while audio plays.
      // This eliminates the lag where UI was frozen for the entire TTS duration.
      S.loading = false;
      sendBtn.disabled = !input.value.trim();
      orb.classList.remove('thinking');
      // Re-focus text input on desktop so user can immediately type
      if (!/iPhone|iPad|iPod|Android/i.test(navigator.userAgent) && textBar && textBar.classList.contains('visible')) {
        input.focus();
      }

      // Play audio independently — does NOT block new user input.
      // onSpeakingEnd() will resume mic after speech completes.
      speakWithFallback(
        r.audio_base64,
        r.speech_text || r.text || r.response_text,
        S.language,
        r.audio_format
      ).then(() => {
        // After audio finishes: resume mic if not already speaking/loading.
        if (!S.speaking && !S.loading) onSpeakingEnd();
      }).catch(() => {
        if (!S.speaking && !S.loading) onSpeakingEnd();
      });
    } catch (error) {
      removeTyping();
      setStatus('Online · ' + CFG.store_name);
      showToast('⚠ Connection issue — please try again.');
      console.error('[WooAgent]', error);
      S.loading = false;
      sendBtn.disabled = !input.value.trim();
      orb.classList.remove('thinking');
      if (!S.speaking) onSpeakingEnd();
    } finally {
      // Safety net: ensure loading is always released
      S.loading = false;
      sendBtn.disabled = !input.value.trim();
      orb.classList.remove('thinking');
    }
  }

  async function processAction(act) {
    switch (act.type) {
      case 'show_products':
        renderProducts((act.payload && act.payload.products) || []);
        break;

      case 'show_product_detail':
        if (act.payload && act.payload.product) renderProducts([act.payload.product]);
        break;

      case 'add_to_cart':
        if (act.payload && act.payload.product_id) {
          try {
            await addToCartDispatch(act.payload);
          } catch (error) {
            const message = (error && error.message) ? String(error.message) : 'Could not add to cart.';
            addBubble('bot', message);
            showToast(message);
          }
        }
        break;

      case 'remove_from_cart':
        if (act.payload && act.payload.cart_item_key) {
          try {
            if (IS_SHOPIFY) {
              await removeFromCartShopify(act.payload.cart_item_key);
            } else {
              await removeFromCartViaWoo(act.payload.cart_item_key);
            }
          } catch (error) {
            showToast('Could not remove item from cart.');
          }
        }
        break;

      case 'cart_updated': {
        const c = act.payload || {};
        const cart = c.cart || {};
        const cnt = c.cart_count || c.item_count || cart.item_count || cart.totalQuantity || 0;
        updateBadge(cnt);
        if (cart.checkout_url) S.checkoutUrl = cart.checkout_url;
        if (c.product_id) {
          showToast('Added to cart!');
        } else if (c.message) {
          showToast(c.message);
        }
        if (window.jQuery) {
          window.jQuery(document.body).trigger('wc_fragment_refresh');
          window.jQuery(document.body).trigger('update_checkout');
        }
        if (window.location.pathname.includes('/cart') || window.location.pathname.includes('/checkout')) {
          setTimeout(() => {
            window.location.reload();
          }, 1500);
        }
        break;
      }

      case 'show_cart':
        // Shopify: render the REAL cart (/cart.js), not the backend's payload —
        // post-unification the authoritative cart lives in the storefront, so the
        // backend's cart snapshot can be stale/empty.
        if (IS_SHOPIFY) {
          await fetchCart();
        } else {
          if (act.payload && act.payload.cart) S.cartSnapshot = act.payload.cart;
          renderCart(act.payload && act.payload.cart);
        }
        break;

      case 'show_orders':
        renderOrders((act.payload && act.payload.orders) || []);
        break;

      case 'show_comparison':
        renderComparison((act.payload && act.payload.items) || []);
        break;

      case 'show_variants':
        if (act.payload && act.payload.variations) {
          renderVariantSelector(act.payload);
        }
        break;

      case 'show_availability': {
        const prod = (act.payload && act.payload.product) || {};
        const inv = (act.payload && act.payload.inventory) || {};
        if (prod && prod.id) {
          // Merge precise inventory data into product so the card renders correct stock status
          renderProducts([{
            ...prod,
            stock_status: inv.in_stock ? 'instock' : 'outofstock',
            stock_quantity: inv.stock_quantity != null ? inv.stock_quantity : prod.stock_quantity,
          }]);
        }
        break;
      }

      case 'show_reviews':
        renderReviews(act.payload || {});
        break;

      case 'review_submitted':
        showToast('⭐️ Review submitted! Thank you.');
        break;

      case 'show_best_coupon':
        if (act.payload && act.payload.code) {
          // Only show toast — LLM response text already mentions the coupon
          showToast(`🎁 Coupon available: ${act.payload.code} — ${act.payload.amount}${act.payload.discount_type === 'percent' ? '%' : ''} off`);
        }
        // No addBubble here — would duplicate LLM's response
        break;

      case 'coupon_applied':
        showToast(`🏷️ ${act.payload.code} applied — ${act.payload.discount}`);
        break;

      case 'prefill_address':
        persistCheckoutAddress({ billing: act.payload, shipping: act.payload });
        applyStoredCheckoutAddress();
        window.parent.postMessage({
          type: 'wooagent_prefill_address',
          payload: act.payload
        }, '*');
        break;

      case 'redirect_checkout_with_address':
        persistCheckoutAddress(act.payload);
        if (isCheckoutPage()) {
          applyStoredCheckoutAddress();
        } else if (IS_SHOPIFY) {
          // Shopify: can't script the checkout page — prefill via URL params.
          const addr = act.payload.billing || act.payload.shipping || act.payload || {};
          try {
            if (S.mode === 'voice_nav') {
              localStorage.setItem('_wa_voice_nav_resume', '1');
            } else {
              localStorage.setItem('_wa_reopen', '1');
            }
          } catch (e) {}
          setTimeout(() => { window.location.href = _shopifyCheckoutUrl(addr); }, 1000);
        } else {
          try {
            if (S.mode === 'voice_nav') {
              localStorage.setItem('_wa_voice_nav_resume', '1');
            } else {
              localStorage.setItem('_wa_reopen', '1');
            }
          } catch (e) {}
          let targetUrl = act.payload.url || '/checkout';
          try {
            if (targetUrl.startsWith('http://') || targetUrl.startsWith('https://')) {
              const parsed = new URL(targetUrl);
              targetUrl = parsed.pathname + parsed.search + parsed.hash;
            }
          } catch (e) {}
          setTimeout(() => {
            window.location.href = targetUrl;
          }, 1200);
        }
        break;
 
      case 'redirect_checkout':
      case 'redirect': {
        const p = act.payload || {};
        // Live Shopping Navigator redirects carry a reason (search|product|cart).
        const navReason = String(p.reason || '');
        const isLiveNav = navReason === 'search' || navReason === 'product' || navReason === 'cart';
        if (isLiveNav && !LIVE_NAV) break; // flag off → cards only, no navigation
        if (isLiveNav) {
          // Show the customer what the agent is doing, then persist a resume flag
          // so the widget re-opens (and voice resumes) after the page loads.
          const label = navReason === 'search'
            ? ('🔎 Opening the store search for "' + (p.query || '') + '"…')
            : (navReason === 'product'
              ? '🛍️ Taking you to the product page…'
              : '🛒 Taking you to your cart…');
          if (S.open) {
            try { addBubble('bot', label); } catch (e) { }
          } else {
            showToast(label);
          }
          try {
            if (S.mode === 'voice_nav') {
              localStorage.setItem('_wa_voice_nav_resume', '1');
              // ── Edge case: Checkout→Search interrupt ──────────────────────────
              // If the user is leaving from a /checkout URL during voice navigation,
              // persist the halted checkout context so the AI knows to guide back
              // after they add the new product.
              const isCheckoutHalt = /\/checkout|\/cart/.test(location.pathname);
              if (isCheckoutHalt && navReason === 'search') {
                try {
                  localStorage.setItem('_wa_checkout_halted', JSON.stringify({
                    from_url: location.href,
                    query: p.query || '',
                    halted_at: Date.now()
                  }));
                } catch (e) { }
              }
            } else {
              localStorage.setItem('_wa_reopen', '1');
            }
          } catch (e) { }
        }
        let targetUrl = p.url || '/checkout';
        try {
          if (targetUrl.startsWith('http://') || targetUrl.startsWith('https://')) {
            const parsed = new URL(targetUrl);
            targetUrl = parsed.pathname + parsed.search + parsed.hash;
          }
        } catch (e) {}
        const performRedirect = () => {
          if (IS_SHOPIFY && !isLiveNav && (!p.url || p.url === '/checkout')) {
            goToCheckout();
          } else {
            window.location.href = targetUrl;
          }
        };

        const checkAndRedirect = () => {
          if (S.speaking) {
            setTimeout(checkAndRedirect, 100);
          } else {
            performRedirect();
          }
        };

        setTimeout(checkAndRedirect, p.delay_ms || 800);
        break;
      }

      case 'show_store_info':
        renderStoreInfo(act.payload || {});
        break;

      case 'store_event': {
        // Generic store event dispatch — lets Speako trigger ANY store UI action
        // by firing a custom DOM event that the store theme or an app listens for.
        // Built-in Shopify events handled directly:
        const ev = act.payload || {};
        const eventName = ev.event || '';
        const detail = ev.detail || {};

        if (eventName === 'speako:open_cart_drawer') {
          // Shopify cart drawer — try common theme selectors
          const drawers = [
            document.querySelector('[data-cart-drawer-toggle]'),
            document.querySelector('[data-cart-toggle]'),
            document.querySelector('.cart-drawer__toggle'),
            document.querySelector('.js-cart-drawer-trigger'),
            document.querySelector('.header__icon--cart'),
            document.querySelector('a[href="/cart"]'),
          ];
          for (const btn of drawers) {
            if (btn) { btn.click(); break; }
          }
        } else if (eventName === 'speako:open_product_modal') {
          // Open product quick-view / modal on the current page
          const pid = detail.product_id;
          const triggers = [
            document.querySelector(`[data-product-id="${pid}"] [data-quick-view]`),
            document.querySelector(`[data-product-id="${pid}"] .quick-view`),
            document.querySelector(`a[href*="/products/"][data-modal]`),
          ];
          for (const el of triggers) {
            if (el) { el.click(); break; }
          }
        } else if (eventName === 'speako:select_variant') {
          // Select a specific variant option on the product page
          const opts = detail.options || {};
          for (const [label, value] of Object.entries(opts)) {
            const radios = document.querySelectorAll(
              `[data-option-value]:not(.hidden):not([disabled])`
            );
            for (const radio of radios) {
              if (radio.textContent.trim().toLowerCase() === String(value).toLowerCase()) {
                radio.click();
                break;
              }
            }
          }
        } else if (eventName === 'speako:scroll_to') {
          // Scroll to a specific section (e.g., reviews, description)
          const target = detail.selector || '';
          if (target) {
            const el = document.querySelector(target);
            if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
          }
        }

        // Always dispatch as custom event for merchant-installed listeners
        window.dispatchEvent(new CustomEvent(eventName, { detail }));
        break;
      }
    }
  }

  async function addToCartViaWoo(payload) {
    const endpoint = String(CFG.rest_url || '').replace(/\/$/, '') + '/cart/add';
    const productId = parseInt(payload && payload.product_id, 10);
    if (!Number.isInteger(productId) || productId <= 0) {
      throw new Error('Invalid product id');
    }
    const body = {
      session_id: S.sessionId,
      product_id: productId,
      variation_id: payload.variation_id || 0,
      quantity: payload.quantity || 1,
      variation: payload.variation || {},
      nonce: CFG.nonce || ''
    };

    const _doFetch = async (b) => fetch(endpoint, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-WooAgent-Nonce': CFG.nonce || '',
        'X-WP-Nonce': CFG.wp_rest_nonce || ''
      },
      body: JSON.stringify(b)
    });

    let res = await _doFetch(body);
    let data = await res.json().catch(() => null);

    // Retry once: let PHP pick the best variation (pass variation_id=0)
    // This helps when the variation_id passed is invalid or attributes mismatch
    if ((!res.ok || !data) && body.variation_id > 0) {
      res = await _doFetch({ ...body, variation_id: 0 });
      data = await res.json().catch(() => null);
    }

    if (!res.ok || !data || !data.success) {
      // PHP error_response puts message in data.error (top level)
      const errMsg = (data && (data.error || (data.data && data.data.message) || data.message)) || 'Add to cart failed';
      throw new Error(errMsg);
    }

    // Update cart from the add response immediately — no extra GET round trip needed
    const addedCart = data.data && data.data.cart;
    if (addedCart) {
      const itemCount = typeof data.data.cart_count === 'number'
        ? data.data.cart_count
        : (addedCart.count || addedCart.item_count || 0);
      const cartNorm = {
        items: addedCart.items || [],
        count: itemCount,
        total: addedCart.total || '₹0',
        item_count: itemCount
      };
      S.cartSnapshot = cartNorm;
      try { localStorage.setItem('_wa_cart_snap', JSON.stringify(cartNorm)); } catch (e) {}
      updateBadge(itemCount);
      renderCart({ is_empty: !itemCount, item_count: itemCount, total: cartNorm.total, items: cartNorm.items });
    } else {
      // Fallback if add response didn't include cart data
      await fetchCart();
    }

    showToast('🛒 Added to cart');
    return data;
  }

  // ── Shopify native AJAX cart ────────────────────────────────────────────────
  // Aria must operate the SAME cart the customer sees on /cart. The widget runs
  // same-origin on the storefront, so it talks to Shopify's native Cart API
  // (/cart/add.js, /cart.js) directly. The old code POSTed to the WooCommerce
  // '/cart/add' path on Shopify too — that returns an HTML page, and res.json()
  // then threw "Unexpected token '<'". This replaces that path for Shopify.

  function _shopifyMoney(cents) {
    const val = (Number(cents) || 0) / 100;
    return (CFG.currency || '₹') + val.toLocaleString('en-IN', {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    });
  }

  function _normalizeShopifyCart(raw) {
    const items = ((raw && raw.items) || []).map(it => ({
      cart_item_key: it.key,
      product_id: it.product_id,
      variation_id: it.variant_id,
      name: it.product_title || it.title || '',
      quantity: it.quantity || 1,
      price: _shopifyMoney(it.final_line_price != null ? it.final_line_price : it.line_price),
      image_url: it.image || '',
    }));
    const count = (raw && raw.item_count != null)
      ? raw.item_count
      : items.reduce((s, i) => s + (i.quantity || 0), 0);
    return {
      items,
      item_count: count,
      count,
      is_empty: count === 0,
      total: _shopifyMoney(raw && raw.total_price || 0),
    };
  }

  // Guard JSON parsing so a non-JSON (HTML error/redirect) response never throws a
  // raw "Unexpected token '<'" at the user — surface a clean error instead.
  // Shopify's AJAX endpoints (/cart/add.js, /cart.js, /products/*.js) sometimes
  // return JSON with a `text/javascript` content-type, so we DON'T reject on the
  // content-type header alone — we try to parse the body and only fail if it isn't
  // valid JSON (i.e. an actual HTML error/redirect page).
  async function _parseJsonSafe(res) {
    const txt = await res.text().catch(() => '');
    try {
      return JSON.parse(txt);
    } catch (_) {
      throw new Error('Unexpected non-JSON response (' + res.status + ')' +
        (txt ? ': ' + txt.slice(0, 80) : ''));
    }
  }

  // Remember product_id → handle as cards render, so a later agent-driven
  // "add it to cart" (whose payload has no handle) can still resolve a variant.
  function _extractHandle(p) {
    if (!p) return '';
    if (p.handle) return String(p.handle);
    const m = String(p.permalink || '').match(/\/products\/([^/?#]+)/);
    return m ? m[1] : '';
  }
  function _rememberProduct(p) {
    const id = p && (p.id || p.product_id);
    const handle = _extractHandle(p);
    if (id && handle) {
      S.productHandles = S.productHandles || {};
      S.productHandles[String(id)] = handle;
    }
  }

  async function resolveShopifyVariantId(payload) {
    // Single-variant products (or cards without a picker) arrive with no variant
    // id. Fetch the product JSON by handle and pick the first available variant.
    const handle = payload && (payload.handle || payload.product_handle);
    if (!handle) return 0;
    try {
      const res = await fetch('/products/' + encodeURIComponent(handle) + '.js',
        { credentials: 'same-origin', headers: { 'Accept': 'application/json' } });
      if (!res.ok) return 0;
      const prod = await _parseJsonSafe(res);
      const variants = (prod && prod.variants) || [];
      const avail = variants.find(v => v.available) || variants[0];
      return avail ? (parseInt(avail.id, 10) || 0) : 0;
    } catch (e) {
      return 0;
    }
  }

  // silent=true updates the snapshot + badge only (no cart card in the chat) —
  // used to seed S.cartSnapshot on page load so the first cart_context we send
  // the backend already reflects the real cart.
  async function fetchCartShopify(silent) {
    const res = await fetch('/cart.js',
      { credentials: 'same-origin', headers: { 'Accept': 'application/json' } });
    if (!res.ok) throw new Error('cart.js ' + res.status);
    const cart = _normalizeShopifyCart(await _parseJsonSafe(res));
    S.cartSnapshot = cart;
    try { localStorage.setItem('_wa_cart_snap', JSON.stringify(cart)); } catch (e) {}
    updateBadge(cart.item_count);
    if (!silent) renderCart(cart);
    return cart;
  }

  async function addToCartShopify(payload) {
    let variantId = parseInt(payload && (payload.variation_id || payload.variant_id), 10);
    if (!Number.isInteger(variantId) || variantId <= 0) {
      // No explicit variant: resolve from the handle. Prefer the payload handle,
      // else the handle we remembered when this product's card was rendered,
      // else extract from permalink or current page URL.
      let handle = (payload && (payload.handle || payload.product_handle)) || '';
      if (!handle && payload && payload.permalink) {
        const m = String(payload.permalink).match(/\/products\/([^/?#]+)/);
        handle = m ? m[1] : '';
      }
      if (!handle && payload && payload.product_id) {
        handle = (S.productHandles || {})[String(payload.product_id)] || '';
      }
      if (!handle) {
        const m = location.pathname.match(/\/products\/([^/?#]+)/);
        handle = m ? m[1] : '';
      }
      variantId = await resolveShopifyVariantId({ handle });
    }
    if (!Number.isInteger(variantId) || variantId <= 0) {
      throw new Error('Please choose a product option first.');
    }
    const res = await fetch('/cart/add.js', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify({
        id: variantId,
        quantity: Math.max(1, parseInt(payload.quantity, 10) || 1),
      }),
    });
    if (!res.ok) {
      let msg = 'Add to cart failed';
      try { const e = await res.json(); msg = e.description || e.message || msg; } catch (_) {}
      throw new Error(msg);
    }
    // The add already succeeded (res.ok). We don't need the returned line item —
    // we re-read the whole cart next — so DON'T parse the body. Shopify returns it
    // with a non-JSON content-type, and parsing it used to throw a false
    // "Unexpected non-JSON response" error into the chat AFTER a successful add.
    await fetchCartShopify();
    showToast('🛒 Added to cart');
  }

  // Platform-aware add-to-cart: native AJAX on Shopify, WP REST on WooCommerce.
  async function addToCartDispatch(payload) {
    if (IS_SHOPIFY) return addToCartShopify(payload);
    return addToCartViaWoo(payload);
  }

  // Build a Shopify checkout URL with prefilled shipping fields. Shopify can't be
  // scripted on its hosted checkout (non-Plus), but it DOES accept checkout[...]
  // query params to pre-populate the form — that's how we honour details the
  // customer already gave Aria.
  function _shopifyCheckoutUrl(addr) {
    addr = addr || {};
    const get = (...keys) => {
      for (const k of keys) { if (addr[k]) return String(addr[k]).trim(); }
      return '';
    };
    const p = new URLSearchParams();
    const email = get('email');
    if (email) p.set('checkout[email]', email);
    const map = {
      'checkout[shipping_address][first_name]': get('first_name'),
      'checkout[shipping_address][last_name]':  get('last_name'),
      'checkout[shipping_address][address1]':   get('address_1', 'address_line1'),
      'checkout[shipping_address][city]':       get('city'),
      'checkout[shipping_address][province]':   get('state', 'province'),
      'checkout[shipping_address][zip]':        get('postcode', 'zip', 'pincode'),
      'checkout[shipping_address][phone]':      get('phone'),
      'checkout[shipping_address][country]':    get('country'),
    };
    Object.keys(map).forEach(k => { if (map[k]) p.set(k, map[k]); });
    const qs = p.toString();
    return '/checkout' + (qs ? ('?' + qs) : '');
  }

  // Take the customer to the REAL checkout. On Shopify this is the native
  // checkout for the current cart (prefilled when we know the address); on
  // WooCommerce we still let the agent drive (its address flow differs).
  function goToCheckout() {
    if (IS_SHOPIFY) {
      const addr = (S.addressDraft && typeof S.addressDraft === 'object') ? S.addressDraft : {};
      window.location.href = _shopifyCheckoutUrl(addr);
      return;
    }
    addBubble('user', 'I want to checkout');
    sendToAgent('I want to checkout now');
  }

  // Native Shopify remove — sets the line item quantity to 0 via /cart/change.js.
  // (The backend remove_from_cart is client-side only, like add; without this the
  // agent's "remove X" never actually changed the real cart.)
  async function removeFromCartShopify(cartItemKey) {
    if (!cartItemKey) return;
    const res = await fetch('/cart/change.js', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify({ id: String(cartItemKey), quantity: 0 }),
    });
    if (!res.ok) throw new Error('remove failed (' + res.status + ')');
    await _parseJsonSafe(res);
    await fetchCartShopify();
    showToast('🗑️ Removed from cart');
  }

  async function removeFromCartViaWoo(cartItemKey) {
    const endpoint = String(CFG.rest_url || '').replace(/\/$/, '') + '/cart/remove';
    const body = { session_id: S.sessionId, cart_item_key: cartItemKey, nonce: CFG.nonce || '' };

    let res = await fetch(endpoint, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-WooAgent-Nonce': CFG.nonce || '', 'X-WP-Nonce': CFG.wp_rest_nonce || '' },
      body: JSON.stringify(body)
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    await fetchCart();
    showToast('🗑️ Removed from cart');
  }

  async function fetchCart() {
    // Shopify: read the customer's REAL cart (/cart.js) so the widget display AND
    // the cart_context we send to the backend match what's on the /cart page.
    // This is the single source of truth — agent and direct adds both write here.
    if (IS_SHOPIFY) {
      try { return await fetchCartShopify(); } catch (e) { return null; }
    }
    let cart = null;
    try {
      if (!CFG.rest_url) {
        // Custom platform: backend handles cart transparently
        const cartRes = await fetch(
          `${CFG.agent_api_url}/api/v1/cart?session_id=${encodeURIComponent(S.sessionId)}${tenantQS(true)}`,
          { headers: { 'Content-Type': 'application/json' } }
        );
        if (cartRes.ok) cart = await cartRes.json();
      } else {
        // WooCommerce with WordPress REST bridge
        const cartRes = await fetch(
          String(CFG.rest_url || '').replace(/\/$/, '') + `/cart?session_id=${encodeURIComponent(S.sessionId)}`,
          {
            method: 'GET',
            credentials: 'same-origin',
            headers: {
              'X-WooAgent-Nonce': CFG.nonce || '',
              'X-WP-Nonce': CFG.wp_rest_nonce || ''
            }
          }
        );
        const cartData = await cartRes.json();
        if (cartRes.ok && cartData && cartData.data) cart = cartData.data;
      }
    } catch (e) {
      // ignore — cart display will use last known snapshot
    }

    if (cart) {
      const itemCount = cart.item_count || cart.count || 0;
      const cartNorm = {
        items: cart.items || [],
        count: itemCount,
        total: cart.total || '0',
        item_count: itemCount
      };
      S.cartSnapshot = cartNorm;
      try { localStorage.setItem('_wa_cart_snap', JSON.stringify(cartNorm)); } catch (e) {}
      updateBadge(itemCount);
      renderCart({
        is_empty: !itemCount,
        item_count: itemCount,
        total: cart.total || '0',
        items: cart.items || []
      });
    }
    return cart;
  }


  function renderBotMarkdown(text) {
    // Escape HTML special chars first to prevent XSS from any stray user input echoed back
    const escaped = String(text || '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    return escaped
      // Bold **text** or __text__
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/__(.+?)__/g, '<strong>$1</strong>')
      // Italic *text* or _text_ (not inside words)
      .replace(/(?<!\w)\*(.+?)\*(?!\w)/g, '<em>$1</em>')
      .replace(/(?<!\w)_(.+?)_(?!\w)/g, '<em>$1</em>')
      // Headers → bold
      .replace(/^#{1,6}\s+(.+)$/gm, '<strong>$1</strong>')
      // Bullet lists • - *
      .replace(/^[\-•*]\s+(.+)$/gm, '&bull; $1')
      // Numbered list items
      .replace(/^\d+\.\s+(.+)$/gm, '&bull; $1')
      // Double newline → paragraph break
      .replace(/\n\n+/g, '<br><br>')
      // Single newline → line break
      .replace(/\n/g, '<br>');
  }

  function addBubble(who, text) {
    if (who === 'user' || who === 'bot') {
      // Save to conversation history so it remains accessible if user opens chatbox later
      S.conversation.push({ role: who === 'user' ? 'user' : 'assistant', content: text });
      S.conversation = S.conversation.slice(-20);
      try { localStorage.setItem('_wa_conv', JSON.stringify(S.conversation)); } catch (e) {}
    }

    // In Voice Navigation mode when chatbox is closed, do not append text bubbles into the chatbox panel.
    // Responses are spoken via audio, and actions move the storefront page directly.
    if (S.mode === 'voice_nav' && !S.open) {
      return null;
    }

    if (who === 'system') {
      const el = document.createElement('div');
      el.className = 'wa-bubble system';
      el.textContent = text;
      msgs.appendChild(el);
      scrollBottom();
      return el;
    }
    const row = document.createElement('div');
    row.className = `wa-bubble-row ${who}`;
    const av = document.createElement('div');
    av.className = who === 'bot' ? 'wa-bot-avatar' : 'wa-user-avatar';
    if (who === 'user') av.textContent = '✦';
    row.appendChild(av);
    const el = document.createElement('div');
    el.className = `wa-bubble ${who}`;
    if (who === 'bot') {
      el.innerHTML = renderBotMarkdown(text);
    } else {
      el.textContent = text;
    }
    row.appendChild(el);
    msgs.appendChild(row);
    scrollBottom();
    return el;
  }

  function showTyping() {
    removeTyping();
    const row = document.createElement('div');
    row.className = 'wa-typing';
    row.id = 'wa-typing-ind';
    const av = document.createElement('div');
    av.className = 'wa-bot-avatar';
    row.appendChild(av);
    const inner = document.createElement('div');
    inner.className = 'wa-typing-inner';
    inner.innerHTML = '<div class="wa-dot"></div><div class="wa-dot"></div><div class="wa-dot"></div>';
    row.appendChild(inner);
    msgs.appendChild(row);
    scrollBottom();
  }

  function removeTyping() {
    const el = $('wa-typing-ind');
    if (el) el.remove();
  }

  function normalizeVariationAttrKey(key) {
    return String(key || '')
      .toLowerCase()
      .replace(/^attribute_/, '')
      .replace(/^pa_/, '')
      .replace(/^attribute_pa_/, '')
      .replace(/[-\s]+/g, '_')
      .trim();
  }

  function getVariationAttrValue(attributes, targetName) {
    if (!attributes || typeof attributes !== 'object') return '';
    const needle = normalizeVariationAttrKey(targetName);
    const keys = Object.keys(attributes);
    for (let i = 0; i < keys.length; i += 1) {
      const rawKey = keys[i];
      const key = normalizeVariationAttrKey(rawKey);
      if (key === needle || key.includes(needle) || needle.includes(key)) {
        const val = attributes[rawKey];
        return val === null || typeof val === 'undefined' ? '' : String(val).trim();
      }
    }
    return '';
  }

  function buildVariationMeta(product) {
    // variations_summary is an array of variation objects, each with an 'attributes' field.
    // The attributes field can be:
    //   A) [{name: "Size", option: "L"}, ...] (from PHP plugin / WC variations endpoint)
    //   B) {"pa_size": "L", "pa_color": "Gold"} (key-value dict)
    const summary = Array.isArray(product && product.variations_summary) ? product.variations_summary : [];
    const attributesMap = {};

    for (let i = 0; i < summary.length; i += 1) {
      const item = summary[i];
      if (!item || typeof item !== 'object') continue;
      const attrs = item.attributes;

      // Format A: array of {name, option} objects
      if (Array.isArray(attrs)) {
        attrs.forEach(attr => {
          if (!attr || typeof attr !== 'object') return;
          const rawKey = String(attr.name || attr.attribute || '').trim();
          const val = String(attr.option || attr.value || '').trim();
          if (!rawKey || !val) return;
          const normKey = normalizeVariationAttrKey(rawKey);
          if (!attributesMap[normKey]) {
            const label = rawKey.replace(/^pa_/, '').replace(/[-_]+/g, ' ').trim();
            attributesMap[normKey] = { label: label ? label.charAt(0).toUpperCase() + label.slice(1) : normKey, options: [] };
          }
          if (!attributesMap[normKey].options.includes(val)) {
            attributesMap[normKey].options.push(val);
          }
        });
      }
      // Format B: plain key-value dict like {"pa_size": "L"}
      else if (attrs && typeof attrs === 'object') {
        Object.keys(attrs).forEach(rawKey => {
          const rawVal = attrs[rawKey];
          // Guard: only use primitive values (string, number) — skip objects
          if (rawVal === null || typeof rawVal === 'undefined' || typeof rawVal === 'object') return;
          const val = String(rawVal).trim();
          if (!val) return;
          const normKey = normalizeVariationAttrKey(rawKey);
          if (!attributesMap[normKey]) {
            const label = rawKey.replace(/^attribute_pa_/, '').replace(/^attribute_/, '').replace(/[-_]+/g, ' ').trim();
            attributesMap[normKey] = { label: label ? label.charAt(0).toUpperCase() + label.slice(1) : normKey, options: [] };
          }
          if (!attributesMap[normKey].options.includes(val)) {
            attributesMap[normKey].options.push(val);
          }
        });
      }
    }

    return attributesMap;
  }

  function buildCardMeta(product, attributesMap) {
    // Priority 1: use the parsed variation attributes map
    const pieces = [];
    Object.keys(attributesMap).forEach(key => {
      const attr = attributesMap[key];
      if (attr.options.length) {
        // Ensure all option values are strings before joining
        const safeOpts = attr.options
          .filter(o => o !== null && o !== undefined && typeof o !== 'object')
          .map(o => String(o))
          .slice(0, 4);
        if (safeOpts.length) pieces.push(`${attr.label}: ${safeOpts.join(', ')}`);
      }
    });
    if (pieces.length) return pieces.join(' • ');

    // Priority 2: use root-level product.attributes (WC format: [{name, options:[...]}])
    if (Array.isArray(product && product.attributes)) {
      const attrs = product.attributes.slice(0, 2).map(a => {
        const name = a && (a.name || a.label);
        if (!name) return '';
        let options = '';
        if (Array.isArray(a.options)) {
          // options can be strings or {id, name, slug} objects
          options = a.options
            .slice(0, 3)
            .map(o => (o && typeof o === 'object') ? String(o.name || o.slug || '') : String(o))
            .filter(Boolean)
            .join(', ');
        }
        if (!options && typeof a.option === 'string') options = a.option;
        return options ? `${name}: ${options}` : String(name);
      }).filter(Boolean);
      if (attrs.length) return attrs.join(' • ');
    }
    return '';
  }

  function getVariationImageUrl(product) {
    const summary = Array.isArray(product && product.variations_summary) ? product.variations_summary : [];
    for (let i = 0; i < summary.length; i += 1) {
      const item = summary[i];
      if (!item || typeof item !== 'object') continue;
      if (item.image_url) return String(item.image_url);
      if (item.image && typeof item.image === 'object' && item.image.src) return String(item.image.src);
    }
    return '';
  }

  function renderProducts(products) {
    if (!Array.isArray(products) || !products.length) return;

    // Deduplicate by product ID
    const seen = new Set();
    const unique = products.filter(p => {
      const id = p && (p.id || p.product_id);
      if (!id || seen.has(id)) return false;
      seen.add(id);
      return true;
    });
    if (!unique.length) return;

    const wrap = document.createElement('div');
    wrap.className = 'wa-products-wrap';

    const label = document.createElement('div');
    label.className = 'wa-products-label';
    label.textContent = `${unique.length} product${unique.length > 1 ? 's' : ''} found`;
    wrap.appendChild(label);

    const scroll = document.createElement('div');
    scroll.className = 'wa-products-scroll';

    unique.forEach(p => {
      _rememberProduct(p);
      const stockStatus = String(p.stock_status || '').toLowerCase();
      // onbackorder = purchasable in WooCommerce; empty = assume in stock
      const inStock = !stockStatus || stockStatus === 'instock' || stockStatus === 'onbackorder';
      const isBackorder = stockStatus === 'onbackorder';
      const qty = p.stock_quantity;
      const lowStock = qty !== null && qty !== undefined && qty > 0 && qty < 5;
      const onSale = !!(p.on_sale || (p.sale_price && p.sale_price !== p.regular_price));
      const displayPrice = onSale ? p.sale_price : (p.price || p.regular_price || 0);
      const variationMeta = buildVariationMeta(p);
      const cardMeta = buildCardMeta(p, variationMeta);
      const attrsKeys = Object.keys(variationMeta);
      const hasVariants = attrsKeys.length > 0;
      const fmtPrice = n => {
        const val = Number(String(n || '0').replace(/[^\d.]/g, '')) || 0;
        return (CFG.currency || '₹') + val.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      };

      const card = document.createElement('div');
      card.className = 'wa-card';

      const imgSrc = normalizeImageUrl(
        p.image_url ||
        getVariationImageUrl(p) ||
        (p.images && p.images[0] && p.images[0].src) ||
        ''
      );

      card.innerHTML = `
        <div class="wa-card-img-wrap">
          ${imgSrc
          ? `<img class="wa-card-img" src="${escAttr(imgSrc)}" alt="${escAttr(p.name)}" loading="lazy" onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
             <div class="wa-card-img" style="display:none;align-items:center;justify-content:center;color:#52525b;font-size:28px">🛍️</div>`
          : `<div class="wa-card-img" style="display:flex;align-items:center;justify-content:center;color:#52525b;font-size:28px">🛍️</div>`
        }
          ${onSale ? '<div class="wa-card-sale-tag">SALE</div>' : ''}
        </div>
        <div class="wa-card-body">
          <div class="wa-card-name">${esc(p.name)}</div>
          <div class="wa-card-prices">
            <span class="wa-card-price">${fmtPrice(displayPrice)}</span>
            ${onSale ? `<span class="wa-card-reg">${fmtPrice(p.regular_price || p.price)}</span>` : ''}
          </div>
          <div class="wa-card-stock">
            <div class="wa-stock-dot ${!inStock ? 'out' : lowStock ? 'low' : 'in'}"></div>
            ${!inStock ? 'Out of stock' : lowStock ? `Only ${qty} left` : isBackorder ? 'Available (backorder)' : 'In stock'}
          </div>
          ${cardMeta ? `<div class="wa-card-meta">${esc(cardMeta)}</div>` : ''}
          ${hasVariants ? `
            <div class="wa-card-variant-row" style="flex-wrap: wrap;">
              ${attrsKeys.map(k => `
                <select class="wa-card-select wa-card-attr" data-key="${escAttr(k)}" aria-label="${escAttr(variationMeta[k].label)}">
                  ${variationMeta[k].options.map(opt => `<option value="${escAttr(opt)}">${esc(opt)}</option>`).join('')}
                </select>
              `).join('')}
            </div>
          ` : ''}
          ${inStock ? `
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px;">
              <label style="font-size:11px;color:var(--text2);">Qty:</label>
              <input class="wa-card-qty" type="number" min="1" max="20" value="1"
                style="width:48px;padding:3px 6px;border:1px solid var(--line);border-radius:6px;font-size:12px;background:var(--bg3);color:var(--text);text-align:center;" />
            </div>
          ` : ''}
          <button class="wa-card-add ${!inStock ? 'disabled' : ''}" ${!inStock ? 'disabled' : ''} data-id="${escAttr(p.id)}" data-name="${escAttr(p.name)}" data-price="${escAttr(displayPrice || '')}">
            ${!inStock ? 'Out of stock' : '+ Add to Cart'}
          </button>
          <a class="wa-card-view" href="${safeUrl(p.permalink)}" target="_blank" rel="noopener">View details ↗</a>
        </div>
      `;

      const cardImg = card.querySelector('.wa-card-img');
      if (cardImg) {
        cardImg.addEventListener('error', () => {
          cardImg.src = '';
          cardImg.style.display = 'none';
          const ph = document.createElement('div');
          ph.className = 'wa-card-img';
          ph.style.display = 'flex';
          ph.style.alignItems = 'center';
          ph.style.justifyContent = 'center';
          ph.style.color = '#52525b';
          ph.style.fontSize = '28px';
          ph.textContent = '🛍️';
          const wrap = card.querySelector('.wa-card-img-wrap');
          if (wrap && !wrap.querySelector('div.wa-card-img')) wrap.appendChild(ph);
        });
      }

      const addBtn = card.querySelector('.wa-card-add:not(.disabled)');
      if (addBtn) {
        addBtn.addEventListener('click', async () => {
          const name = addBtn.dataset.name;
          const id = parseInt(addBtn.dataset.id, 10);
          if (!Number.isInteger(id) || id <= 0) {
            showToast('This product cannot be added right now.');
            return;
          }
          const selects = card.querySelectorAll('.wa-card-attr');
          const selectedAttributes = {};
          selects.forEach(sel => {
            selectedAttributes[sel.dataset.key] = String(sel.value || '').trim();
          });
          const preferredVariation = resolvePreferredVariationPayload(p, selectedAttributes);
          const qtyInput = card.querySelector('.wa-card-qty');
          const qty = qtyInput ? Math.max(1, Math.min(20, parseInt(qtyInput.value, 10) || 1)) : 1;
          addBtn.textContent = '...';
          addBtn.disabled = true;
          const selectedBits = Object.values(selectedAttributes).filter(Boolean).join(' / ');
          const qtyLabel = qty > 1 ? ` × ${qty}` : '';
          addBubble('user', selectedBits
            ? `Add "${name}" (${selectedBits})${qtyLabel} to my cart`
            : `Add "${name}"${qtyLabel} to my cart`);
          try {
            await addToCartDispatch({
              product_id: id,
              variation_id: preferredVariation.variation_id,
              variation: preferredVariation.variation,
              handle: p.handle || '',
              quantity: qty
            });
            addBtn.textContent = '✓ Added';
            addBtn.style.background = 'var(--ok)';
            addBtn.style.color = '#fff';
            addBubble('bot', `${name} added to your cart.`);
          } catch (error) {
            addBtn.textContent = '+ Add to Cart';
            addBtn.disabled = false;
            const msg = (error && error.message) ? String(error.message) : 'Add to cart failed. Please try again.';
            showToast(msg);
            addBubble('bot', msg);
          }
        });
      }

      scroll.appendChild(card);
    });

    wrap.appendChild(scroll);
    msgs.appendChild(wrap);
    scrollBottom();
  }

  function renderStoreInfo(info) {
    const name = info.store_name || 'Our Store';
    const about = info.about || '';
    const currency = info.currency || '₹';
    const shipping = info.shipping || '';
    const returns = info.returns || '';
    const payments = info.payment_methods || '';

    const rows = [];
    if (about)    rows.push(`<div class="wa-sinfo-row"><span class="wa-sinfo-icon">ℹ️</span><span>${esc(about)}</span></div>`);
    if (shipping) rows.push(`<div class="wa-sinfo-row"><span class="wa-sinfo-icon">🚚</span><span>${esc(shipping)}</span></div>`);
    if (returns)  rows.push(`<div class="wa-sinfo-row"><span class="wa-sinfo-icon">↩️</span><span>${esc(returns)}</span></div>`);
    if (payments) rows.push(`<div class="wa-sinfo-row"><span class="wa-sinfo-icon">💳</span><span>${esc(payments)}</span></div>`);
    rows.push(`<div class="wa-sinfo-row"><span class="wa-sinfo-icon">💰</span><span>Currency: ${esc(currency)}</span></div>`);

    const el = document.createElement('div');
    el.className = 'wa-sinfo-card';
    el.innerHTML = `
      <div class="wa-sinfo-header">🏪 ${esc(name)}</div>
      <div class="wa-sinfo-body">${rows.join('')}</div>
    `;
    msgs.appendChild(el);
    scrollBottom();
  }

  function renderCart(cart) {
    if (!cart) return;
    S.cartSnapshot = cart;
    if (cart.is_empty || !cart.item_count) {
      const el = document.createElement('div');
      el.className = 'wa-cart-card';
      el.innerHTML = `<div class="wa-cart-head"><span class="wa-cart-title">🛒 Your Cart</span></div><div style="color:var(--text2);font-size:13px;padding:8px 0;">Your cart is empty.</div>`;
      msgs.appendChild(el);
      scrollBottom();
      return;
    }
    const el = document.createElement('div');
    el.className = 'wa-cart-card';

    const itemsHtml = (cart.items || []).map(item => {
      let img = item.images && Array.isArray(item.images) && item.images[0] ? (item.images[0].src || item.images[0].thumbnail || item.images[0].url || '') : '';
      if (!img && item.images && typeof item.images === 'string') img = item.images;
      if (!img && item.image_url) img = item.image_url;
      if (!img && item.image && typeof item.image === 'object') img = item.image.src || item.image.thumbnail || '';
      img = normalizeImageUrl(img);
      return `
      <div class="wa-cart-item-row" style="display:flex;align-items:center;gap:12px;margin-bottom:8px;">
        ${img ? `<img src="${escAttr(img)}" alt="${escAttr(item.name)}" style="width:40px;height:40px;object-fit:cover;border-radius:6px;background:var(--bg);" loading="lazy">` : `<div style="width:40px;height:40px;border-radius:6px;background:var(--bg2);display:flex;align-items:center;justify-content:center;font-size:16px;">🛍️</div>`}
        <div style="flex:1;display:flex;flex-direction:column;">
          <strong style="font-weight:600;font-size:13px;">${esc(item.name)}</strong>
          <span style="color:var(--text3);font-size:11px;margin-top:2px;">Qty: ${item.quantity || 1}</span>
        </div>
        <span style="color:var(--text);font-weight:600;font-size:13px;">${item.price || item.line_total || item.line_subtotal || ''}</span>
      </div>
      `;
    }).join('');

    el.innerHTML = `
      <div class="wa-cart-head">
        <span class="wa-cart-title">🛒 Your Cart</span>
        <span class="wa-cart-pill">${cart.item_count} item${cart.item_count !== 1 ? 's' : ''}</span>
      </div>
      <div class="wa-cart-items">${itemsHtml}</div>
      <div class="wa-cart-total-row">
        <span class="wa-cart-total-label">Total</span>
        <span class="wa-cart-total-val">${esc(cart.total)}</span>
      </div>
      <button class="wa-checkout-btn">Proceed to Checkout →</button>
    `;

    el.querySelector('.wa-checkout-btn').addEventListener('click', () => {
      goToCheckout();
    });

    msgs.appendChild(el);
    scrollBottom();
  }

  function resolvePreferredVariationPayload(product, selectedAttributes) {
    const summary = Array.isArray(product && product.variations_summary) ? product.variations_summary : [];
    if (!summary.length) {
      return { variation_id: 0, variation: {} };
    }

    const matchesSelection = (attrs) => {
      if (!attrs || typeof attrs !== 'object') return Object.keys(selectedAttributes || {}).length === 0;
      let matches = true;
      Object.keys(selectedAttributes || {}).forEach(k => {
        const need = String(selectedAttributes[k] || '').toLowerCase().trim();
        if (!need) return;
        const val = getVariationAttrValue(attrs, k).toLowerCase();
        if (val !== need && !val.includes(need) && !need.includes(val)) matches = false;
      });
      return matches;
    };

    let selected = null;
    for (let i = 0; i < summary.length; i += 1) {
      const item = summary[i];
      if (item && typeof item === 'object' && item.is_in_stock && matchesSelection(item.attributes || {})) {
        selected = item;
        break;
      }
    }
    if (!selected) {
      for (let i = 0; i < summary.length; i += 1) {
        const item = summary[i];
        if (item && typeof item === 'object' && matchesSelection(item.attributes || {})) {
          selected = item;
          break;
        }
      }
    }
    if (!selected && summary.length > 0) selected = summary[0];

    if (typeof selected === 'number') {
      return { variation_id: selected, variation: {} };
    }

    if (!selected || typeof selected !== 'object') {
      return { variation_id: 0, variation: {} };
    }

    const rawAttrs = selected.attributes && typeof selected.attributes === 'object' ? selected.attributes : {};
    const variation = {};
    Object.keys(rawAttrs).forEach(key => {
      const value = rawAttrs[key];
      if (value === null || typeof value === 'undefined' || value === '') return;
      variation[key] = String(value);
    });

    return {
      variation_id: parseInt(selected.variation_id || selected.id || 0, 10) || 0,
      variation
    };
  }

  function renderOrders(orders) {
    orders.forEach(order => {
      const statusColor = {
        completed: 'var(--ok)',
        processing: 'var(--p)',
        'on-hold': 'var(--warn)',
        cancelled: 'var(--err)'
      }[order.status] || 'var(--text2)';

      const el = document.createElement('div');
      el.className = 'wa-cart-card';
      el.innerHTML = `
        <div class="wa-cart-head">
          <span class="wa-cart-title">📦 Order #${esc(order.number || order.order_number || order.order_id || '')}</span>
          <span class="wa-cart-pill" style="background:var(--bg3, rgba(128,128,128,0.12));color:${statusColor}">
            ${esc(order.status || '')}
          </span>
        </div>
        <div style="font-size:11px;color:var(--text3);margin-top:-4px">${esc(order.date || order.date_created || '')}</div>
        <div class="wa-cart-items">
          ${(order.items || []).map(i => `<div class="wa-cart-item-row"><span>${esc(i.name)} × ${i.quantity}</span></div>`).join('')}
        </div>
        <div class="wa-cart-total-row">
          <span class="wa-cart-total-label">Total</span>
          <span class="wa-cart-total-val">${esc(order.currency_symbol || CFG.currency || '₹')}${esc(order.total || '')}</span>
        </div>
      `;
      msgs.appendChild(el);
    });
    scrollBottom();
  }

  function renderComparison(items) {
    if (!items || !items.length) return;
    const wrap = document.createElement('div');
    wrap.className = 'wa-products-wrap';

    const label = document.createElement('div');
    label.className = 'wa-products-label';
    label.textContent = 'PRODUCT COMPARISON';
    wrap.appendChild(label);

    // Pick the "best" item: prefer in-stock + lower price (simple heuristic)
    const inStockItems = items.filter(i => i.in_stock);
    const priceOf = i => Number(String(i.sale_price || i.price || '0').replace(/[^\d.]/g, '')) || 0;
    let bestId = null;
    if (inStockItems.length === 1) {
      bestId = inStockItems[0].id;
    } else if (inStockItems.length > 1) {
      const sorted = [...inStockItems].sort((a, b) => priceOf(a) - priceOf(b));
      bestId = sorted[0].id;
    }

    const scroll = document.createElement('div');
    scroll.className = 'wa-products-scroll';

    items.forEach(item => {
      const inStock = !!item.in_stock;
      const isBest = item.id && item.id === bestId;
      const fmtPrice = n => {
        const val = Number(String(n || '0').replace(/[^\d.]/g, '')) || 0;
        return (CFG.currency || '\u20b9') + val.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      };
      const card = document.createElement('div');
      card.className = 'wa-card';
      if (isBest) card.style.cssText = 'border:2px solid var(--accent);position:relative;';
      const imgSrc = normalizeImageUrl(item.image_url || (item.images && item.images[0] && item.images[0].src) || '');
      card.innerHTML = `
        ${isBest ? `<div style="position:absolute;top:-1px;left:50%;transform:translateX(-50%);background:var(--accent);color:#fff;font-size:10px;font-weight:700;padding:2px 10px;border-radius:0 0 6px 6px;white-space:nowrap;">BEST VALUE</div>` : ''}
        <div class="wa-card-img-wrap" style="${isBest ? 'margin-top:16px;' : ''}">
          ${imgSrc
          ? `<img class="wa-card-img" src="${escAttr(imgSrc)}" alt="${escAttr(item.name)}" loading="lazy">`
          : `<div class="wa-card-img" style="display:flex;align-items:center;justify-content:center;color:#52525b;font-size:28px">\ud83d\udecd\ufe0f</div>`
        }
        </div>
        <div class="wa-card-body">
          <div class="wa-card-name">${esc(item.name || 'Product')}</div>
          <div class="wa-card-prices">
            <span class="wa-card-price">${fmtPrice(item.sale_price || item.price)}</span>
            ${item.sale_price && item.sale_price !== item.price ? `<span class="wa-card-reg">${fmtPrice(item.price)}</span>` : ''}
          </div>
          ${item.rating ? `<div style="color:#f59e0b;font-size:12px;margin-bottom:2px;">${'★'.repeat(Math.round(Number(item.rating) || 0))} ${item.rating}</div>` : ''}
          <div class="wa-card-stock">
            <div class="wa-stock-dot ${inStock ? 'in' : 'out'}"></div>
            ${inStock ? 'In stock' : 'Out of stock'}
          </div>
          ${item.id ? `<button class="wa-card-add ${!inStock ? 'disabled' : ''}" ${!inStock ? 'disabled' : ''} data-id="${escAttr(item.id)}" data-name="${escAttr(item.name)}">+ Add to Cart</button>` : ''}
          ${item.permalink ? `<a class="wa-card-view" href="${safeUrl(item.permalink)}" target="_blank" rel="noopener">View details \u2197</a>` : ''}
        </div>
      `;
      const addBtn = card.querySelector('.wa-card-add:not(.disabled)');
      if (addBtn) {
        addBtn.addEventListener('click', () => {
          const name = addBtn.dataset.name;
          const id = parseInt(addBtn.dataset.id, 10);
          if (!Number.isInteger(id) || id <= 0) return;
          // Route through agent so variants are shown (same as product card flow)
          const msg = `I want to buy ${name}`;
          addBubble('user', msg);
          sendToAgent(msg);
        });
      }
      scroll.appendChild(card);
    });

    wrap.appendChild(scroll);
    msgs.appendChild(wrap);
    scrollBottom();
  }

  function renderReviews(payload) {
    const reviews = payload.reviews || [];
    const avg = payload.average_rating || 0;
    const count = payload.count || reviews.length;
    if (!count) {
      addBubble('bot', 'No reviews yet for this product.');
      return;
    }
    const wrap = document.createElement('div');
    wrap.className = 'wa-products-wrap';
    const label = document.createElement('div');
    label.className = 'wa-products-label';
    const stars = '★'.repeat(Math.round(avg)) + '☆'.repeat(5 - Math.round(avg));
    label.textContent = `${stars}  ${avg}/5 · ${count} REVIEW${count !== 1 ? 'S' : ''}`;
    label.style.color = '#f59e0b';
    wrap.appendChild(label);
    const scroll = document.createElement('div');
    scroll.className = 'wa-products-scroll';
    scroll.style.flexDirection = 'column';
    scroll.style.gap = '8px';
    reviews.forEach(r => {
      const card = document.createElement('div');
      card.style.cssText = 'background:var(--bg2);border-radius:10px;padding:10px 12px;min-width:220px;max-width:280px;flex-shrink:0;';
      const rStars = '★'.repeat(Math.min(5, r.rating || 0)) + '☆'.repeat(Math.max(0, 5 - (r.rating || 0)));
      card.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
          <span style="font-weight:600;font-size:12px;color:var(--text);">${esc(r.reviewer || 'Customer')}</span>
          <span style="color:#f59e0b;font-size:13px;">${rStars}</span>
        </div>
        <div style="font-size:12px;color:var(--text2);line-height:1.4;">${esc(r.review || '')}</div>
        ${r.date ? `<div style="font-size:10px;color:var(--text2);margin-top:4px;opacity:0.6;">${esc(r.date)}${r.verified ? ' · Verified' : ''}</div>` : ''}
      `;
      scroll.appendChild(card);
    });
    wrap.appendChild(scroll);
    msgs.appendChild(wrap);
    scrollBottom();
  }

  function renderVariantSelector(payload) {
    const p = payload.product || {};
    const vars = payload.variations || [];
    if (!vars.length) return;

    const el = document.createElement('div');
    el.className = 'wa-bubble bot';
    el.style.maxWidth = '90%';
    el.style.borderRadius = '12px';

    const attrs = {};
    vars.forEach(v => {
      Object.keys(v.attributes || {}).forEach(k => {
        const label = k.replace('attribute_pa_', '').replace('attribute_', '').replace('pa_', '').replace(/[-_]+/g, ' ').toUpperCase();
        if (!attrs[k]) attrs[k] = { label, options: new Set() };
        attrs[k].options.add(v.attributes[k]);
      });
    });

    let html = `<div style="font-weight:600;margin-bottom:8px;font-size:13px;">Please select options for ${esc(p.name || 'this product')}:</div>`;
    Object.keys(attrs).forEach(k => {
      html += `
        <div style="margin-bottom:8px;">
          <div style="font-size:11px;color:var(--text2);margin-bottom:4px;">${esc(attrs[k].label)}</div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;">
            ${Array.from(attrs[k].options).map(opt => `
              <button class="wa-variant-opt" data-key="${escAttr(k)}" data-val="${escAttr(opt)}" style="background:var(--bg3);border:1px solid var(--line);color:var(--text);padding:4px 10px;border-radius:6px;font-size:12px;cursor:pointer;">${esc(opt)}</button>
            `).join('')}
          </div>
        </div>
      `;
    });
    html += `
      <div style="display:flex;align-items:center;gap:8px;margin-top:8px;">
        <label style="font-size:12px;color:var(--text2);">Qty:</label>
        <input class="wa-variant-qty" type="number" min="1" max="20" value="1"
          style="width:56px;padding:4px 6px;border:1px solid var(--line);border-radius:6px;font-size:13px;background:var(--bg3);color:var(--text);text-align:center;" />
        <button class="wa-variant-add" style="flex:1;background:var(--p);color:#fff;border:none;padding:8px;border-radius:8px;font-weight:600;cursor:pointer;">Add to Cart</button>
      </div>`;

    el.innerHTML = html;

    const btns = el.querySelectorAll('.wa-variant-opt');
    const selected = {};
    btns.forEach(b => {
      b.addEventListener('click', () => {
        const key = b.dataset.key;
        el.querySelectorAll(`.wa-variant-opt[data-key="${key}"]`).forEach(eb => {
          eb.style.background = 'var(--bg3)';
          eb.style.borderColor = 'var(--line)';
        });
        b.style.background = 'var(--p)';
        b.style.borderColor = 'var(--p)';
        selected[key] = b.dataset.val;
      });
      // Select first option by default
      if (!selected[b.dataset.key]) b.click();
    });

    el.querySelector('.wa-variant-add').addEventListener('click', async () => {
      const addBtn = el.querySelector('.wa-variant-add');
      const qtyEl = el.querySelector('.wa-variant-qty');
      const qty = Math.max(1, Math.min(20, parseInt(qtyEl && qtyEl.value, 10) || 1));
      addBtn.disabled = true;
      addBtn.textContent = 'Adding...';
      try {
        // Resolve variation_id from the variations list using the selected attributes
        const matchedVar = vars.find(v => {
          const vAttrs = v.attributes || {};
          return Object.keys(selected).every(k => {
            const vVal = String(vAttrs[k] || '').trim().toLowerCase();
            const sVal = String(selected[k] || '').trim().toLowerCase();
            return !sVal || vVal === sVal;
          });
        });
        const resolvedVarId = matchedVar
          ? (parseInt(matchedVar.id || matchedVar.variation_id || 0, 10) || 0)
          : 0;
        await addToCartDispatch({
          product_id: p.id,
          variation_id: resolvedVarId,
          variation: selected,
          handle: p.handle || '',
          quantity: qty
        });
        addBtn.textContent = '✓ Added to Cart';
        addBtn.style.background = 'var(--ok)';
        setTimeout(() => el.remove(), 2000);
      } catch (e) {
        addBtn.disabled = false;
        addBtn.textContent = 'Add to Cart';
        showToast('Failed to add. Please try again.');
      }
    });

    msgs.appendChild(el);
    scrollBottom();
  }

  // ── Live voice mode ───────────────────────────────────────────────────────

  // Returns a Promise<boolean> — true if mic permission is already granted.
  // Chrome won't show the permission dialog when SpeechRecognition.start() or
  // getUserMedia() is called from a setTimeout (not a synchronous user gesture).
  // We use this to decide whether to auto-start or show "Tap to speak" instead.
  async function _micPermissionGranted() {
    if (!navigator.permissions) return false;
    try {
      const r = await navigator.permissions.query({ name: 'microphone' });
      return r.state === 'granted';
    } catch {
      return false;
    }
  }

  function _langCodeForSR(lang) {
    const map = {
      en: 'en-IN', hi: 'hi-IN', ml: 'ml-IN',
      ta: 'ta-IN', te: 'te-IN', bn: 'bn-BD',
      kn: 'kn-IN', gu: 'gu-IN', pa: 'pa-IN'
    };
    return map[lang] || 'en-IN';
  }

  function hideLiveOverlay() {
    if (livePill) { livePill.classList.remove('active'); livePill.innerHTML = ''; }
  }

  function updateLiveSpeech(final, interim) {
    if (!livePill) return;
    if (!final && !interim) {
      // Nothing being said — hide the pill
      livePill.classList.remove('active');
      livePill.innerHTML = '';
      return;
    }
    // Show transcript in pill below orb
    let html = '';
    if (final) html += esc(final);
    if (interim) html += '<span class="wa-interim"> ' + esc(interim) + '</span>';
    livePill.innerHTML = html;
    livePill.classList.add('active');
  }

  // Languages where Web Speech API is unreliable — use Whisper (VAD recording) instead.
  // Chrome/Safari don't have reliable offline models for Dravidian scripts.
  const WHISPER_ONLY_LANGS = new Set(['ml', 'ta', 'te', 'kn', 'gu', 'pa', 'bn']);

  function _useWhisperForLang(lang) {
    return WHISPER_ONLY_LANGS.has(lang);
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // NEW: Gemini 3.1 Flash-Live A2A WebSocket Streaming Mode
  // ───────────────────────────────────────────────────────────────────────────
  // Replaces the old 3-step HTTP flow:
  //   OLD: MediaRecorder blob → POST /transcribe (Groq Whisper) → text
  //        → POST /chat (LLM Router) → response text
  //        → Google Cloud TTS → audio_base64 → play
  //
  //   NEW: MediaRecorder chunks (250ms) → WebSocket binary stream → Gemini Live
  //        ← Gemini Live PCM audio chunks ← WebSocket binary stream ← play
  //        ← WebSocket JSON {"type":"ui_action"} ← widget renders cards/cart
  //
  // Feature flag: set A2A_ENABLED = false to fall back to the old HTTP mode.
  // ═══════════════════════════════════════════════════════════════════════════

  const A2A_ENABLED        = true;  // ← flip to false to revert to HTTP (transcribe+chat)
  const A2A_MAX_RECONNECTS = 5;     // max auto-reconnect attempts before giving up

  // A2A state variables
  let geminiSocket        = null;   // Active WebSocket to the backend relay
  let a2aRecorder         = null;   // MediaRecorder streaming 250ms chunks
  let a2aStream           = null;   // getUserMedia mic stream
  let a2aAudioCtx         = null;   // AudioContext for PCM playback
  let a2aAudioQueue       = [];     // Queued PCM ArrayBuffers waiting to play
  let a2aIsPlaying        = false;  // Playback mutex flag
  let a2aCurrentSource    = null;   // [1] Active AudioBufferSourceNode (needed for barge-in stop)
  let isA2AConnected      = false;  // WebSocket open + recorder running
  let a2aReconnectCount   = 0;      // [2] Reconnect attempt counter
  let a2aWsToken          = '';     // [4] Short-lived HMAC token for WS auth
  let a2aTokenFetchedAt   = 0;      // [4] Epoch ms when token was last fetched
  let a2aTokenSessionId   = '';     // [4] session_id the cached token was minted for
  // Streaming transcript accumulator — chunks from Gemini arrive word-by-word;
  // we append into one bubble and only finalise it on turn_complete / barge-in.
  let _a2aStreamBubble    = null;   // current live DOM element being updated
  let _a2aStreamText      = '';     // accumulated text for this turn

  // Convert backend HTTP URL → WebSocket URL (http→ws, https→wss)
  function _a2aWsUrl(token) {
    const base = (CFG.agent_api_url || 'http://localhost:8000').replace(/\/$/, '');
    const sid  = encodeURIComponent(S.sessionId);
    const tok  = encodeURIComponent(token || '');
    return base.replace(/^http/, 'ws') + '/wooagent/stream?session_id=' + sid + '&token=' + tok + tenantQS(true);
  }

  // ── [4] _fetchWsToken: get a short-lived HMAC token before connecting ────
  // Calls GET /wooagent/ws-token once per 90s (token TTL is 120s).
  // Returns a promise that resolves to the token string (empty string on failure).
  function _fetchWsToken() {
    const tokenAge = Date.now() - a2aTokenFetchedAt;
    // Reuse the cached token ONLY if it's still fresh AND was minted for the
    // CURRENT session_id. A new/cleared session_id (clear-chat, reopen) makes the
    // cached token stale — the server binds each token to its session, so sending
    // a token from a previous session yields a 403 "bad token" and a retry storm.
    if (a2aWsToken && tokenAge < 90000 && a2aTokenSessionId === S.sessionId) {
      return Promise.resolve(a2aWsToken);  // reuse cached token
    }
    const base = (CFG.agent_api_url || 'http://localhost:8000').replace(/\/$/, '');
    const url  = base + '/wooagent/ws-token?session_id=' + encodeURIComponent(S.sessionId) + tenantQS(true);
    // No custom headers on this request — avoids CORS preflight (simple GET).
    // ngrok-skip-browser-warning bypasses the ngrok interstitial page for API calls.
    return fetch(url, {
      method: 'GET',
      headers: { 'ngrok-skip-browser-warning': '1' },
    })
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(data => {
        a2aWsToken        = data.token || '';
        a2aTokenFetchedAt = Date.now();
        a2aTokenSessionId = S.sessionId;   // bind the cached token to this session
        return a2aWsToken;
      })
      .catch(err => {
        console.warn('[WooAgent A2A] Token fetch failed — connecting without token (dev mode):', err);
        return '';
      });
  }

  // Pending text message to send as soon as the WebSocket connects
  let _a2aPendingTextMsg = null;

  // ── startA2AMode: fetch token first, then open WebSocket (with mic) ──────
  // [2] Reconnect-aware: called both on first connect and after backoff delay.
  function startA2AMode() {
    if (isA2AConnected) return;
    _fetchWsToken().then(token => _openA2AWebSocket(token, true));
  }

  // ── _startA2AForText: open WebSocket for text-only (no mic) ──────────────
  // Called when the user types a message and the WebSocket is not yet open.
  // Mic capture starts separately when the user taps the orb.
  function _startA2AForText() {
    if (isA2AConnected) return;
    _fetchWsToken().then(token => _openA2AWebSocket(token, false));
  }

  // ── _openA2AWebSocket: create WebSocket with auth token ─────────────────
  function _openA2AWebSocket(token, startMic) {
    if (isA2AConnected) return;

    let ws;
    try {
      ws = new WebSocket(_a2aWsUrl(token));
    } catch (e) {
      console.warn('[WooAgent A2A] WebSocket constructor failed:', e);
      _a2aFallback();
      return;
    }

    ws.binaryType = 'arraybuffer';
    geminiSocket  = ws;

    ws.onopen = () => {
      isA2AConnected    = true;
      a2aReconnectCount = 0;  // [2] reset counter on successful connection
      _a2aStreamBubble  = null;
      _a2aStreamText    = '';

      // Send initial page_update control frame so the backend Turn Coordinator
      // knows the current URL, cart, and any interrupted flow context.
      try {
        if (ws.readyState === WebSocket.OPEN) {
          // Check if this is a post-checkout-interrupt resume
          let interruptedFlow = null;
          try {
            const haltedRaw = localStorage.getItem('_wa_checkout_halted');
            if (haltedRaw) {
              const halted = JSON.parse(haltedRaw);
              // Only use the state if it was set within the last 30 minutes
              if (halted.halted_at && (Date.now() - halted.halted_at) < 1800000) {
                interruptedFlow = { from: 'checkout', from_url: halted.from_url, query: halted.query };
                localStorage.removeItem('_wa_checkout_halted');
              } else {
                localStorage.removeItem('_wa_checkout_halted'); // stale, discard
              }
            }
          } catch (e) { }

          ws.send(JSON.stringify({
            type: 'page_update',
            page_context: {
              url: location.href,
              title: document.title,
              product_id: typeof detectProductId === 'function' ? detectProductId() : null,
              product_name: typeof detectProductName === 'function' ? detectProductName() : null,
              interrupted_flow: interruptedFlow
            },
            cart_context: (S.cartSnapshot && typeof S.cartSnapshot === 'object' && !Array.isArray(S.cartSnapshot)) ? S.cartSnapshot : {}
          }));
        }
      } catch (e) {
        console.warn('[WooAgent A2A] Failed to send page_update frame:', e);
      }

      if (startMic) {
        isLiveMode = true;
        orb.classList.add('live');
        orbHint.innerHTML = '<span class="wa-live-badge">Live</span> <strong>Listening…</strong>';
        _a2aStartCapture();
      } else {
        orbHint.innerHTML = '<span class="wa-live-badge">Live</span> <strong>Ready</strong>';
      }
      // Flush any message typed while the connection was being established
      if (_a2aPendingTextMsg) {
        const pending = _a2aPendingTextMsg;
        _a2aPendingTextMsg = null;
        setTimeout(() => sendTextToA2A(pending), 150);
      }
    };

    ws.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) {
        // ── Binary: PCM 16-bit 24kHz mono from Gemini — play immediately ──
        _a2aEnqueueAudio(event.data);
      } else if (typeof event.data === 'string') {
        // ── Text: JSON control message from backend relay ──
        try {
          const msg = JSON.parse(event.data);

          if (msg.type === 'ui_action' && msg.action) {
            // Render product cards, cart updates, etc. — same pipeline as HTTP mode
            processAction(msg.action).catch(() => {});
          }

          // ── [1] Barge-in: backend detected user interruption ──────────
          // Gemini stopped sending audio; clear any queued PCM buffers so
          // the AI goes silent instantly when the user starts speaking.
          if (msg.type === 'flush_audio') {
            flushAudioQueue();
            // Barge-in: user interrupted — seal whatever text arrived so far
            _a2aStreamBubble = null;
            _a2aStreamText   = '';
          }

          // ── Gemini 3.1: transcript — chunks arrive word-by-word ──────────
          // Accumulate text; render DOM bubble inside chatbox only when chatbox panel is open (S.open)
          if (msg.type === 'transcript' && msg.text) {
            _a2aStreamText += msg.text;
            if (S.open) {
              if (!_a2aStreamBubble) {
                // Create the bubble once; subsequent chunks update it in-place
                const row = document.createElement('div');
                row.className = 'wa-bubble-row bot';
                const av = document.createElement('div');
                av.className = 'wa-bot-avatar';
                row.appendChild(av);
                const el = document.createElement('div');
                el.className = 'wa-bubble bot';
                row.appendChild(el);
                msgs.appendChild(row);
                _a2aStreamBubble = el;
              }
              _a2aStreamBubble.innerHTML = renderBotMarkdown(_a2aStreamText);
              scrollBottom();
            }
          }

          // ── Turn complete: finalise streaming text into history ──────────
          if (msg.type === 'turn_complete') {
            const finalText = _a2aStreamText.trim();
            if (finalText) {
              S.conversation.push({ role: 'assistant', content: finalText });
              S.conversation = S.conversation.slice(-20);
              try { localStorage.setItem('_wa_conv', JSON.stringify(S.conversation)); } catch (e) {}
            }
            _a2aStreamBubble = null;
            _a2aStreamText   = '';
          }

        } catch (e) { /* ignore malformed frames */ }
      }
    };

    ws.onclose = (ev) => {
      isA2AConnected = false;
      geminiSocket   = null;
      _a2aStopCapture();

      // 1008 = policy violation (wrong model / API version / billing) — do not retry, it will never succeed
      const intentional = (ev.code === 1000 || ev.code === 1001 || ev.code === 4003 || ev.code === 1008);
      if (ev.code === 1008) {
        console.error('[WooAgent A2A] Model/API error — check GEMINI_LIVE_MODEL and billing:', ev.reason);
        addBubble('bot', 'Voice assistant unavailable — model configuration error. Text chat still works.');
      }

      // ── [2] Auto-reconnect with exponential backoff ───────────────────
      // Reconnect if widget is open or voice_nav mode is active and the close was unintentional,
      // regardless of whether live/mic mode is active (text also needs WS).
      if (!intentional && (S.open || S.mode === 'voice_nav') && a2aReconnectCount < A2A_MAX_RECONNECTS) {
        a2aReconnectCount++;
        // Force a fresh token on reconnect: the close may have been a token/auth
        // rejection (403 bad token). Reusing the cached token would just fail again
        // and hammer the endpoint into a rate limit — clearing it makes reconnects
        // self-healing (_fetchWsToken re-mints for the current session).
        a2aWsToken        = '';
        a2aTokenFetchedAt = 0;
        const delayMs = Math.min(1000 * Math.pow(2, a2aReconnectCount - 1), 16000); // 1s,2s,4s,8s,16s
        orbHint.innerHTML = `<span class="wa-live-badge">Live</span> Reconnecting… (${a2aReconnectCount}/${A2A_MAX_RECONNECTS})`;
        console.warn(`[WooAgent A2A] Closed (${ev.code}). Reconnecting in ${delayMs}ms (attempt ${a2aReconnectCount})`);
        setTimeout(() => {
          if ((S.open || S.mode === 'voice_nav') && !isA2AConnected) {
            // Re-open with mic if live mode was active, text-only otherwise
            if (isLiveMode) startA2AMode(); else _startA2AForText();
          }
        }, delayMs);
      } else {
        // Intentional close or retry limit reached — exit live mode cleanly
        if (isLiveMode) {
          orb.classList.remove('live');
          isLiveMode = false;
          orbHint.innerHTML = '<strong>Tap to speak</strong>';
        }
        if (a2aReconnectCount >= A2A_MAX_RECONNECTS) {
          console.warn('[WooAgent A2A] Max reconnects reached — falling back to HTTP mode');
          a2aReconnectCount = 0;
          _a2aFallback();
        }
      }
    };

    ws.onerror = () => {
      // onerror is always followed by onclose — let onclose handle reconnect logic
      console.warn('[WooAgent A2A] WebSocket error');
    };
  }

  // ── _a2aStartCapture: mic → AudioWorklet 16kHz PCM → WebSocket ───────
  let a2aMicCtx, a2aMicSource, a2aMicWorklet;

  async function _a2aStartCapture() {
    if (a2aStream) return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, sampleRate: 16000, channelCount: 1 }
      });
      a2aStream = stream;
      startWaveform(stream);

      a2aMicCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
      a2aMicSource = a2aMicCtx.createMediaStreamSource(stream);

      // ── Inline AudioWorklet for zero-latency PCM conversion ──
      const workletCode = `
        class PcmProcessor extends AudioWorkletProcessor {
          constructor() {
            super();
            this.buffer = new Int16Array(320); // 320 samples = 640 bytes = 20ms at 16kHz (was 256ms — too large for Gemini 3.1 VAD)
            this.offset = 0;
          }
          process(inputs, outputs, parameters) {
            const input = inputs[0];
            if (input && input.length > 0 && input[0]) {
              const float32Array = input[0];
              for (let i = 0; i < float32Array.length; i++) {
                const s = Math.max(-1, Math.min(1, float32Array[i]));
                this.buffer[this.offset++] = s < 0 ? s * 0x8000 : s * 0x7FFF;
                if (this.offset >= this.buffer.length) {
                  const copy = new Int16Array(this.buffer);
                  this.port.postMessage(copy.buffer, [copy.buffer]);
                  this.offset = 0;
                }
              }
            }
            return true;
          }
        }
        registerProcessor('pcm-processor', PcmProcessor);
      `;
      // Create a blob URL so we don't need a standalone processor.js file on the server
      const blob = new Blob([workletCode], { type: 'application/javascript' });
      const workletUrl = URL.createObjectURL(blob);

      await a2aMicCtx.audioWorklet.addModule(workletUrl);
      a2aMicWorklet = new AudioWorkletNode(a2aMicCtx, 'pcm-processor');

      a2aMicWorklet.port.onmessage = (e) => {
        if (geminiSocket && geminiSocket.readyState === WebSocket.OPEN) {
          geminiSocket.send(e.data); // e.data is the raw PCM Int16 ArrayBuffer
        }
      };

      a2aMicSource.connect(a2aMicWorklet);
      a2aMicWorklet.connect(a2aMicCtx.destination); // Required to keep Node alive in Chrome

    } catch (err) {
      console.warn('[WooAgent A2A] Mic error:', err);
      stopA2AMode();
      _a2aFallback();
    }
  }

  // ── _a2aStopCapture: stop mic and AudioWorklet encode ──────────────────
  function _a2aStopCapture() {
    if (a2aMicWorklet) {
      try { a2aMicWorklet.disconnect(); } catch (e) {}
      a2aMicWorklet = null;
    }
    if (a2aMicSource) {
      try { a2aMicSource.disconnect(); } catch (e) {}
      a2aMicSource = null;
    }
    if (a2aMicCtx && a2aMicCtx.state !== 'closed') {
      try { a2aMicCtx.close(); } catch (e) {}
      a2aMicCtx = null;
    }
    if (a2aStream) {
      a2aStream.getTracks().forEach(t => t.stop());
      a2aStream = null;
    }
    stopWaveform();  // existing helper
  }

  // ── PCM playback queue (Gemini outputs raw PCM 16-bit 24kHz mono) ────────
  function _a2aEnqueueAudio(arrayBuffer) {
    a2aAudioQueue.push(arrayBuffer);
    if (!a2aIsPlaying) _a2aPlayNext();
  }

  function _a2aPlayNext() {
    a2aCurrentSource = null;  // [1] clear reference — previous source has ended

    if (a2aAudioQueue.length === 0) {
      a2aIsPlaying = false;
      S.speaking   = false;
      onSpeakingEnd();  // existing hook — resumes mic after bot finishes speaking
      return;
    }
    a2aIsPlaying = true;
    S.speaking   = true;

    // Lazy-init AudioContext (must be after user gesture)
    if (!a2aAudioCtx || a2aAudioCtx.state === 'closed') {
      const AC = window.AudioContext || window.webkitAudioContext;
      a2aAudioCtx = new AC({ sampleRate: 24000 });
    }
    if (a2aAudioCtx.state === 'suspended') {
      a2aAudioCtx.resume().catch(() => {});
    }

    const buffer  = a2aAudioQueue.shift();
    const pcm16   = new Int16Array(buffer);
    const float32 = new Float32Array(pcm16.length);
    for (let i = 0; i < pcm16.length; i++) {
      float32[i] = pcm16[i] / 32768.0;  // 16-bit signed → float [-1, 1]
    }

    const audioBuffer = a2aAudioCtx.createBuffer(1, float32.length, 24000);
    audioBuffer.getChannelData(0).set(float32);

    const source  = a2aAudioCtx.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(a2aAudioCtx.destination);
    source.onended = _a2aPlayNext;  // chain next chunk automatically
    a2aCurrentSource = source;      // [1] store ref so flushAudioQueue can stop it
    source.start();
  }

  // ── [1] flushAudioQueue: barge-in handler — instantly silence the AI ─────
  // Called when backend sends {"type":"flush_audio"} (Gemini detected interruption).
  // Stops the currently playing AudioBufferSourceNode and drains the queue so
  // the AI goes silent the moment the user starts speaking over it.
  function flushAudioQueue() {
    // Stop the currently playing source node immediately
    if (a2aCurrentSource) {
      try {
        a2aCurrentSource.onended = null;  // prevent _a2aPlayNext from firing after stop
        a2aCurrentSource.stop();
      } catch (e) { /* already stopped */ }
      a2aCurrentSource = null;
    }
    // Drain all queued chunks — they belong to the interrupted utterance
    a2aAudioQueue = [];
    a2aIsPlaying  = false;
    S.speaking    = false;
    // Do NOT call onSpeakingEnd() here — the user is actively speaking,
    // so mic is already capturing. onSpeakingEnd would cause a double-start.
  }

  // ── stopA2AMode: tear everything down cleanly (intentional user stop) ────
  function stopA2AMode() {
    isA2AConnected    = false;
    a2aReconnectCount = 0;  // [2] reset so next startA2AMode gets fresh attempts
    flushAudioQueue();      // [1] stop any playing audio immediately
    _a2aStopCapture();
    if (geminiSocket) {
      try { geminiSocket.close(1000, 'user stopped'); } catch (e) {}
      geminiSocket = null;
    }
    if (a2aAudioCtx) {
      try { a2aAudioCtx.close(); } catch (e) {}
      a2aAudioCtx = null;
    }
    S.speaking = false;
  }

  // ── sendTextToA2A: route typed/suggested-reply messages through WebSocket ─
  // OLD: sendToAgent(text) → POST /chat HTTP request
  // NEW: if A2A is active, inject text directly into the live Gemini session
  function sendTextToA2A(text) {
    if (!geminiSocket || geminiSocket.readyState !== WebSocket.OPEN) return false;
    try {
      // Include the real cart snapshot so the Brain reasons about the actual cart
      // (over the WS path there's no HTTP cart_context — without this the agent
      // would think the cart is empty).
      const cart = (S.cartSnapshot && typeof S.cartSnapshot === 'object' && !Array.isArray(S.cartSnapshot))
        ? S.cartSnapshot : {};
      geminiSocket.send(JSON.stringify({ type: 'text_input', text, cart_context: cart }));
      return true;
    } catch (e) {
      return false;
    }
  }

  // ── _a2aFallback: gracefully degrade to old HTTP mode ───────────────────
  function _a2aFallback() {
    // OLD startLiveMode path (Speech Recognition + HTTP) — called on A2A failure
    startLiveModeHTTP();
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // END: Gemini A2A WebSocket block
  // ═══════════════════════════════════════════════════════════════════════════

  // Renamed: old startLiveMode body preserved here, called as fallback
  function startLiveModeHTTP() {
    if (!CFG.enable_voice) return;
    isLiveMode = true;
    orb.classList.add('live');
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    // OLD: Use Whisper (push-to-talk) when:
    //  - Language is a Dravidian/non-English language (SR unreliable)
    //  - Language not yet established (_langEstablished=false) — prevents Chrome SR from
    //    transcribing Malayalam/Tamil/etc. as garbage English phonetics on first utterance
    //  - SR not available in this browser
    if (!SR || _useWhisperForLang(S.language) || !_langEstablished) {
      orbHint.innerHTML = '<strong>Tap to speak</strong> · tap again to stop';
      return;
    }
    // OLD: SR path (English, language confirmed): auto-start if permission already granted.
    _micPermissionGranted().then(granted => {
      if (!isLiveMode) return;
      if (granted) {
        startLiveRecognition();
      } else {
        orbHint.innerHTML = '<span class="wa-live-badge">Live</span> <strong>Tap to speak</strong>';
      }
    });
  }

  // ── NEW: startLiveMode — primary entry point (routes to A2A or HTTP) ──────
  // OLD startLiveMode body is preserved above as startLiveModeHTTP() for rollback.
  function startLiveMode() {
    if (!CFG.enable_voice) return;
    isLiveMode = true;
    orb.classList.add('live');

    if (A2A_ENABLED) {
      // NEW path: open persistent WebSocket → Gemini Live A2A stream
      startA2AMode();
    } else {
      // OLD path: Speech Recognition + HTTP (transcribe → chat)
      startLiveModeHTTP();
    }
  }

  // ── NEW: stopLiveMode — tears down A2A or HTTP mode ──────────────────────
  // OLD stopLiveMode body preserved below as stopLiveModeHTTP() for rollback.
  function stopLiveMode() {
    isLiveMode = false;
    if (A2A_ENABLED && isA2AConnected) {
      // NEW path: close WebSocket gracefully
      stopA2AMode();
    } else {
      // OLD path: abort SR + stop MediaRecorder
      stopLiveModeHTTP();
    }
    orb.classList.remove('live');
    orbHint.innerHTML = '<strong>Tap to speak</strong>';
    if (liveTranscriptEl) { liveTranscriptEl.remove(); liveTranscriptEl = null; }
    hideLiveOverlay();
  }

  function startLiveRecognition() {
    // A2A mode: Gemini handles STT natively via continuous PCM stream.
    // Starting Chrome's SpeechRecognition here would compete with the A2A mic
    // and produce double/garbled transcription. Block it entirely.
    if (A2A_ENABLED && isA2AConnected) return;

    if (!isLiveMode || S.loading || S.speaking || S.muted) return;

    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    // Use Whisper (VAD recording) if: Dravidian language, language not confirmed yet, or SR unavailable.
    // This prevents Chrome SR from hallucinating English text from Malayalam/Tamil speech.
    if (!SR || _useWhisperForLang(S.language) || !_langEstablished) {
      startRecording();
      return;
    }

    liveRecognition = new SR();
    liveRecognition.continuous = false;
    liveRecognition.interimResults = true;
    liveRecognition.lang = _langCodeForSR(S.language);
    liveRecognition.maxAlternatives = 1;

    let committedFinal = '';
    let speechDetected = false;

    // ── Background audio capture for Whisper language-fallback ──────────────
    // When the user speaks a language SR can't handle (e.g. Malayalam while
    // S.language='en'), SR fires onend with no text but speechDetected=true.
    // We hand the captured audio to processVoice() with no language hint so
    // Whisper auto-detects the language, updates S.language, and future turns
    // use the correct path (Whisper for Dravidian, SR for English/Hindi).
    let bgChunks = [], bgRecorder = null, bgStream = null, bgMime = 'audio/webm';

    liveRecognition.onspeechstart = () => { speechDetected = true; };

    liveRecognition.onstart = () => {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return;
      navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, sampleRate: 16000 }
      }).then(stream => {
        if (!liveRecognition) { stream.getTracks().forEach(t => t.stop()); return; }
        bgStream = stream;
        bgChunks = [];
        bgMime = getSupportedMimeType() || 'audio/webm';
        bgRecorder = new MediaRecorder(stream, bgMime ? { mimeType: bgMime } : {});
        bgRecorder.ondataavailable = e => { if (e.data && e.data.size > 0) bgChunks.push(e.data); };
        bgRecorder.start(100);
      }).catch(() => {});
    };

    // cleanupBg(useForWhisper, callback?) — stops background recorder.
    // If useForWhisper=true, waits for onstop (gets last chunk), copies to S.audioChunks.
    function cleanupBg(useForWhisper, cb) {
      if (!bgRecorder) {
        if (bgStream) { bgStream.getTracks().forEach(t => t.stop()); bgStream = null; }
        if (cb) cb(false);
        return;
      }
      if (useForWhisper) {
        bgRecorder.onstop = () => {
          if (bgStream) { bgStream.getTracks().forEach(t => t.stop()); bgStream = null; }
          const ok = bgChunks.length > 2; // >200ms of audio (at 100ms intervals)
          if (ok) { S.audioChunks = bgChunks.slice(); S._recordingMimeType = bgMime; }
          bgChunks = []; bgRecorder = null;
          if (cb) cb(ok);
        };
        try { bgRecorder.stop(); } catch (e) { bgRecorder = null; if (cb) cb(false); }
      } else {
        try { bgRecorder.stop(); } catch (e) {}
        if (bgStream) { bgStream.getTracks().forEach(t => t.stop()); bgStream = null; }
        bgChunks = []; bgRecorder = null;
        if (cb) cb(false);
      }
    }
    // ────────────────────────────────────────────────────────────────────────

    liveRecognition.onresult = (event) => {
      let interim = '', final = '';
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const t = event.results[i][0].transcript;
        if (event.results[i].isFinal) final += t;
        else interim += t;
      }
      if (final) committedFinal += final;
      updateLiveSpeech(committedFinal, interim);
    };

    liveRecognition.onerror = (event) => {
      if (event.error === 'aborted') { cleanupBg(false); return; }

      if (event.error === 'no-speech') {
        cleanupBg(false);
        setTimeout(() => { if (isLiveMode && !S.loading && !S.speaking) startLiveRecognition(); }, 300);
        return;
      }

      if (event.error === 'not-allowed' || event.error === 'audio-capture') {
        cleanupBg(false);
        stopLiveMode();
        const isInsecureOrigin = location.protocol === 'http:' &&
          !['localhost', '127.0.0.1'].includes(location.hostname);
        addBubble('bot', isInsecureOrigin
          ? 'Mic needs a secure connection. In Chrome: chrome://flags → "Insecure origins treated as secure" → add ' + location.origin + ', then refresh.'
          : "Mic was blocked. Click the lock icon in Chrome's address bar, set Microphone to Allow, then tap the orb to try again."
        );
        return;
      }

      cleanupBg(false);
      if (liveRetryCount >= LIVE_MAX_RETRIES) {
        console.warn('[WooAgent] SR retry limit reached, falling back to VAD recording');
        liveRetryCount = 0;
        startRecording();
        return;
      }
      liveRetryCount++;
      setTimeout(() => { if (isLiveMode && !S.loading && !S.speaking) startLiveRecognition(); }, 800);
    };

    liveRecognition.onend = () => {
      if (!isLiveMode) { cleanupBg(false); return; }
      liveRetryCount = 0;

      const text = committedFinal.trim();

      if (text) {
        // SR got text. Sanity check: if background audio exists and is substantial,
        // the user may have switched language mid-session (e.g. 'en' → 'ml').
        // In that case, route to Whisper for language re-detection.
        const bgHasAudio = bgChunks.length > 2;
        if (!_langEstablished && bgHasAudio) {
          // Language not confirmed — verify via Whisper (ignores garbage SR text)
          cleanupBg(true, (hasAudio) => {
            if (hasAudio) processVoice();
            else { updateLiveSpeech('', ''); addBubble('user', text); sendToAgent(text); }
          });
        } else {
          // Language confirmed — trust SR text
          cleanupBg(false);
          updateLiveSpeech('', '');
          addBubble('user', text);
          orbHint.innerHTML = 'Processing...';
          sendToAgent(text);
        }
        return;
      }

      if (speechDetected) {
        // SR heard speech but produced no text — language SR can't handle
        // (e.g. user switched to Malayalam during an English session).
        // Send captured audio to Whisper for auto-detection.
        cleanupBg(true, (hasAudio) => {
          if (hasAudio) {
            _langEstablished = false; // reset so next turn re-routes correctly
            processVoice();
          } else if (isLiveMode && !S.loading && !S.speaking && !S.muted) {
            setTimeout(() => { if (isLiveMode) startLiveRecognition(); }, 400);
          }
        });
        return;
      }

      // No speech — restart SR
      cleanupBg(false);
      if (!S.loading && !S.speaking && !S.muted) {
        setTimeout(() => { if (isLiveMode && !S.loading && !S.speaking) startLiveRecognition(); }, 200);
      }
    };

    try {
      liveRecognition.start();
    } catch (e) {
      cleanupBg(false);
      if (e && (e.name === 'NotAllowedError' || e.name === 'SecurityError')) {
        liveRecognition = null;
        orbHint.innerHTML = '<span class="wa-live-badge">Live</span> <strong>Tap to speak</strong>';
        return;
      }
      if (liveRetryCount >= LIVE_MAX_RETRIES) {
        liveRetryCount = 0;
        startRecording();
      } else {
        liveRetryCount++;
        setTimeout(() => { if (isLiveMode && !S.loading && !S.speaking) startLiveRecognition(); }, 400);
      }
    }
  }

  // OLD stopLiveMode body preserved for rollback (called by new stopLiveMode when A2A_ENABLED=false)
  function stopLiveModeHTTP() {
    liveRetryCount = 0;
    if (liveRecognition) {
      try { liveRecognition.abort(); } catch (e) { /* ignore */ }
      liveRecognition = null;
    }
    stopRecording(); // stops MediaRecorder + mic stream
    stopWaveform();  // explicit safety net in case onstop chain didn't fire
  }

  // Called whenever S.speaking becomes false — resumes live listening if active.
  // 450ms delay lets speaker echo/reverb decay so the mic doesn't pick up the
  // tail of the bot's own voice and transcribe it as a user message.
  function onSpeakingEnd() {
    // ── A2A guard ──────────────────────────────────────────────────────────
    // In A2A mode the ScriptProcessorNode keeps the mic open CONTINUOUSLY.
    // Calling startRecording() or startLiveRecognition() here would start a
    // competing capture pipeline (old SpeechRecognition API or MediaRecorder)
    // on top of the live A2A stream, producing garbled/double transcription.
    // Just update the hint and return — Gemini is already listening.
    if (A2A_ENABLED && isA2AConnected) {
      orbHint.innerHTML = '<span class="wa-live-badge">Live</span> <strong>Listening…</strong>';
      return;
    }
    // ── OLD HTTP path: restart SR or VAD recording after bot finishes speaking ──
    if (!isLiveMode || S.loading) return;
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    const useWhisper = _useWhisperForLang(S.language) || !SR || !_langEstablished;
    orbHint.innerHTML = useWhisper
      ? '<span class="wa-live-badge">Live</span> <strong>Listening…</strong>'
      : '<span class="wa-live-badge">Live</span> <strong>Listening</strong> · tap to stop';
    setTimeout(() => {
      if (!isLiveMode || S.loading || S.speaking || S.muted || S.recording) return;
      if (useWhisper) {
        startRecording();
      } else {
        _micPermissionGranted().then(granted => {
          if (!isLiveMode || S.loading || S.speaking || S.muted) return;
          if (granted) {
            startLiveRecognition();
          } else {
            orbHint.innerHTML = '<span class="wa-live-badge">Live</span> <strong>Tap to speak</strong>';
          }
        });
      }
    }, 450);
  }

  // ─────────────────────────────────────────────────────────────────────────

  function clearSuggestions() {
    shadow.querySelectorAll('.wa-suggestions').forEach(el => el.remove());
  }

  function renderSuggestedReplies(replies) {
    clearSuggestions();
    if (!Array.isArray(replies) || !replies.length) return;
    const wrap = document.createElement('div');
    wrap.className = 'wa-suggestions';
    replies.slice(0, 5).forEach(text => {
      const btn = document.createElement('button');
      btn.className = 'wa-sug-btn';
      btn.textContent = text;
      btn.addEventListener('click', () => {
        clearSuggestions();
        addBubble('user', text);
        sendToAgent(text);
      });
      wrap.appendChild(btn);
    });
    msgs.appendChild(wrap);
    scrollBottom();
  }

  function renderAddressProgress(state) {
    const steps = ['collecting_name', 'collecting_address_line1', 'collecting_city', 'collecting_state', 'collecting_pincode', 'collecting_phone', 'confirming'];
    const idx = steps.indexOf(state);
    const labels = ['Name', 'Address', 'City', 'State', 'PIN', 'Phone', 'Confirm'];

    const existing = shadow.querySelector('.wa-addr-progress');
    if (existing) existing.remove();

    const el = document.createElement('div');
    el.className = 'wa-addr-progress';
    el.innerHTML = `
      <div class="wa-addr-title">COLLECTING DELIVERY ADDRESS</div>
      <div class="wa-addr-steps">
        ${steps.map((_, i) => `<div class="wa-addr-step ${i < idx ? 'done' : i === idx ? 'active' : ''}" title="${escAttr(labels[i])}"></div>`).join('')}
      </div>
    `;
    msgs.appendChild(el);
    scrollBottom();
  }

  function updateBadge(n) {
    S.cartCount = Number(n || 0);
    badge.textContent = S.cartCount;
    badge.classList.toggle('on', S.cartCount > 0);
    // Persist cart count across page navigations
    try { localStorage.setItem('_wa_cart_count', String(S.cartCount)); } catch (e) { }
  }

  function showToast(msg) {
    shadow.querySelectorAll('.wa-toast').forEach(t => t.remove());
    const el = document.createElement('div');
    el.className = 'wa-toast';
    el.textContent = msg;
    shadow.appendChild(el);
    setTimeout(() => {
      if (!el.isConnected) return;
      el.style.opacity = '0';
      el.style.transition = 'opacity .3s';
      setTimeout(() => { if (el.isConnected) el.remove(); }, 300);
    }, 3000);
  }

  function scrollBottom() {
    requestAnimationFrame(() => {
      msgs.scrollTop = msgs.scrollHeight;
    });
  }

  function setStatus(text) {
    if (statusTxt) statusTxt.textContent = text;
    const dot = $('wa-header-status');
    if (!dot) return;
    dot.className = 'wa-header-status';
    if (text && (text.includes('Thinking') || text.includes('Processing'))) {
      dot.classList.add('thinking');
    }
  }

  function detectProductId() {
    if (window.Shopify && window.Shopify.analytics && window.Shopify.analytics.meta && window.Shopify.analytics.meta.product) {
      return window.Shopify.analytics.meta.product.id;
    }
    if (window.meta && window.meta.product) {
      return window.meta.product.id;
    }
    const match = document.body.className.match(/postid-(\d+)/);
    return match ? parseInt(match[1], 10) : null;
  }

  function detectProductName() {
    const h1 = document.querySelector('h1.product_title, .product_title, h1.entry-title, h1.product-title, .product-single__title, h1.product__title, h1');
    return h1 ? h1.textContent.trim() : null;
  }

  function esc(str) {
    const d = document.createElement('div');
    d.textContent = str || '';
    return d.innerHTML;
  }

  // Safe for HTML attribute values — also escapes double-quotes
  function escAttr(str) {
    return esc(str).replace(/"/g, '&quot;');
  }

  // Safe href value: allow only http(s), protocol-relative, or site-relative URLs.
  // Blocks javascript:/data:/vbscript: (quote-breakout isn't needed for those, so
  // escAttr alone won't stop them). Returns '#' for anything else/empty.
  function safeUrl(u) {
    const s = String(u || '').trim();
    if (!s) return '#';
    if (/^(https?:)?\/\//i.test(s)) return s;   // http(s):// or //host
    if (/^\/[^/]/.test(s) || s === '/') return s; // site-relative /path
    return '#';
  }

  function normalizeImageUrl(src) {
    const raw = String(src || '').trim();
    if (!raw) return '';
    try {
      const u = new URL(raw, window.location.href);
      if (window.location.protocol === 'https:' && u.protocol === 'http:' && u.host === window.location.host) {
        u.protocol = 'https:';
      }
      return encodeURI(u.toString());
    } catch (e) {
      return encodeURI(raw);
    }
  }

  function persistCheckoutAddress(payload) {
    if (!payload || typeof payload !== 'object') return;
    const envelope = { payload, saved_at: Date.now() };
    const encoded = encodeURIComponent(JSON.stringify(envelope));
    try {
      sessionStorage.setItem('_wa_addr', JSON.stringify(envelope));
    } catch (e) { }
    try {
      localStorage.setItem('_wa_addr_backup', JSON.stringify(envelope));
    } catch (e) { }
    try {
      document.cookie = `_wa_addr=${encoded}; path=/; max-age=7200; SameSite=Lax`;
    } catch (e) { }
  }

  function readStoredCheckoutAddress() {
    const parseEnvelope = (raw) => {
      if (!raw) return null;
      try {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === 'object' && parsed.payload) {
          return parsed.payload;
        }
        return parsed;
      } catch (e) {
        return null;
      }
    };

    let data = parseEnvelope(sessionStorage.getItem('_wa_addr'));
    if (!data) {
      data = parseEnvelope(localStorage.getItem('_wa_addr_backup'));
    }
    if (!data) {
      const cookieMatch = document.cookie.match(/(?:^|;\s*)_wa_addr=([^;]+)/);
      const cookieRaw = cookieMatch ? decodeURIComponent(cookieMatch[1]) : '';
      data = parseEnvelope(cookieRaw);
    }
    return data && typeof data === 'object' ? data : null;
  }

  function mapIndiaStateToCode(value) {
    const raw = String(value || '').trim();
    if (!raw) return '';
    const normalized = raw.toLowerCase().replace(/\./g, '').replace(/\s+/g, ' ').trim();
    const map = {
      'andhra pradesh': 'AP',
      'arunachal pradesh': 'AR',
      'assam': 'AS',
      'bihar': 'BR',
      'chhattisgarh': 'CG',
      'goa': 'GA',
      'gujarat': 'GJ',
      'haryana': 'HR',
      'himachal pradesh': 'HP',
      'jharkhand': 'JH',
      'karnataka': 'KA',
      'kerala': 'KL',
      'madhya pradesh': 'MP',
      'maharashtra': 'MH',
      'manipur': 'MN',
      'meghalaya': 'ML',
      'mizoram': 'MZ',
      'nagaland': 'NL',
      'odisha': 'OR',
      'orissa': 'OR',
      'punjab': 'PB',
      'rajasthan': 'RJ',
      'sikkim': 'SK',
      'tamil nadu': 'TN',
      'telangana': 'TS',
      'tripura': 'TR',
      'uttar pradesh': 'UP',
      'uttarakhand': 'UK',
      'west bengal': 'WB',
      'andaman and nicobar islands': 'AN',
      'chandigarh': 'CH',
      'dadra and nagar haveli and daman and diu': 'DH',
      'delhi': 'DL',
      'jammu and kashmir': 'JK',
      'ladakh': 'LA',
      'lakshadweep': 'LD',
      'puducherry': 'PY'
    };
    if (map[normalized]) return map[normalized];
    if (/^[A-Za-z]{2}$/.test(raw)) return raw.toUpperCase();
    return raw;
  }

  function setSelectByValueOrLabel(el, value) {
    const raw = String(value || '').trim();
    if (!raw) return false;
    const candidates = [raw, mapIndiaStateToCode(raw)];
    for (let i = 0; i < candidates.length; i += 1) {
      const candidate = String(candidates[i] || '').trim();
      if (!candidate) continue;
      let matched = false;
      const opts = Array.from(el.options || []);
      for (let j = 0; j < opts.length; j += 1) {
        const opt = opts[j];
        if (
          String(opt.value || '').toLowerCase() === candidate.toLowerCase() ||
          String(opt.text || '').toLowerCase() === candidate.toLowerCase()
        ) {
          el.value = opt.value;
          matched = true;
          break;
        }
      }
      if (matched) return true;
    }
    return false;
  }

  function setElementValue(el, value) {
    if (!el) return false;
    const next = String(value == null ? '' : value);
    const tag = String(el.tagName || '').toLowerCase();

    if (tag === 'select') {
      const ok = setSelectByValueOrLabel(el, next);
      if (!ok) return false;
    } else {
      const proto = Object.getPrototypeOf(el);
      const descriptor = proto ? Object.getOwnPropertyDescriptor(proto, 'value') : null;
      const nativeSetter = descriptor && descriptor.set ? descriptor.set : null;
      if (nativeSetter) {
        nativeSetter.call(el, next);
      } else {
        el.value = next;
      }
    }

    el.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
    el.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
    el.dispatchEvent(new Event('blur', { bubbles: true, composed: true }));
    return true;
  }

  function applyWooBlocksStoreAddress(payload) {
    try {
      if (!payload || typeof payload !== 'object') return false;
      const billing = payload.billing || payload;
      const shipping = payload.shipping || billing;

      const b = {
        first_name: billing.first_name || '',
        last_name: billing.last_name || '',
        address_1: billing.address_1 || '',
        address_2: billing.address_2 || '',
        city: billing.city || '',
        state: mapIndiaStateToCode(billing.state || '') || billing.state || '',
        postcode: billing.postcode || '',
        country: billing.country || 'IN',
        phone: billing.phone || '',
        email: billing.email || ''
      };
      const s = {
        first_name: shipping.first_name || b.first_name,
        last_name: shipping.last_name || b.last_name,
        address_1: shipping.address_1 || b.address_1,
        address_2: shipping.address_2 || b.address_2,
        city: shipping.city || b.city,
        state: mapIndiaStateToCode(shipping.state || '') || shipping.state || b.state,
        postcode: shipping.postcode || b.postcode,
        country: shipping.country || b.country
      };

      if (!window.wp || !window.wp.data) return false;

      let applied = false;
      // WooCommerce Blocks 8+ uses wc/store/cart or wc/store/checkout
      const storeIds = ['wc/store/checkout', 'wc/store/cart'];
      for (let i = 0; i < storeIds.length; i += 1) {
        const id = storeIds[i];
        let dispatch;
        try { dispatch = window.wp.data.dispatch(id); } catch (e) { continue; }
        if (!dispatch) continue;

        // Try all known dispatch method signatures across WC Blocks versions
        const candidates = [
          ['setBillingAddress', b],
          ['setShippingAddress', s],
          ['__experimentalSetBillingAddress', b],
          ['__experimentalSetShippingAddress', s],
          ['setCustomerData', { billingAddress: b, shippingAddress: s }],
          ['__experimentalSetCustomerData', { billingAddress: b, shippingAddress: s }],
          ['setBillingData', b],
          ['setShippingData', s],
        ];

        for (let j = 0; j < candidates.length; j += 1) {
          const name = candidates[j][0];
          const arg = candidates[j][1];
          if (typeof dispatch[name] === 'function') {
            try { dispatch[name](arg); applied = true; } catch (e) { /* skip */ }
          }
        }
      }

      // WC Blocks keeps email separate from billing in some versions — set it directly
      if (b.email) {
        const emailSelectors = [
          '#email', '[name="email"]', '[type="email"]',
          '#billing-email', '#billing_email', '[name="billing_email"]',
          '[autocomplete="email"]', '[autocomplete="billing email"]',
        ];
        for (let i = 0; i < emailSelectors.length; i++) {
          const el = document.querySelector(emailSelectors[i]);
          if (el && !el.value) { setElementValue(el, b.email); applied = true; break; }
        }
      }

      return applied;
    } catch (e) {
      return false;
    }
  }

  function fillCheckoutValue(selectors, value) {
    if (!value || !selectors) return false;
    const selectorList = Array.isArray(selectors) ? selectors : [selectors];
    let filled = false;
    for (let i = 0; i < selectorList.length; i += 1) {
      const el = document.querySelector(selectorList[i]);
      if (!el) continue;
      filled = setElementValue(el, value) || filled;
    }
    return filled;
  }

  function fillCheckoutByHint(hints, value) {
    if (!value || !Array.isArray(hints) || !hints.length) return false;
    const fields = Array.from(document.querySelectorAll('input, textarea, select'));
    for (let i = 0; i < fields.length; i += 1) {
      const el = fields[i];
      const haystack = [
        el.id || '',
        el.name || '',
        el.placeholder || '',
        el.getAttribute('aria-label') || '',
        el.getAttribute('autocomplete') || '',
        el.getAttribute('data-id') || '',
      ].join(' ').toLowerCase();

      let match = false;
      for (let j = 0; j < hints.length; j += 1) {
        const hint = String(hints[j] || '').toLowerCase();
        if (hint && haystack.includes(hint)) {
          match = true;
          break;
        }
      }
      if (!match) continue;

      if (setElementValue(el, value)) return true;
    }
    return false;
  }

  function isCheckoutPage() {
    const path = window.location.pathname;
    // Works for WooCommerce (/checkout), Shopify (/checkouts/...), and custom stores
    return path.includes('checkout') || document.body.classList.contains('woocommerce-checkout');
  }

  function captureAddressDraftFromStep(step, message) {
    const text = String(message || '').trim();
    if (!text) return;
    const draft = Object.assign({}, S.addressDraft || {});

    if (step === 'collecting_name') {
      const parts = text.split(/\s+/).filter(Boolean);
      draft.first_name = parts[0] || draft.first_name || '';
      draft.last_name = parts.length > 1 ? parts.slice(1).join(' ') : (draft.last_name || '');
    } else if (step === 'collecting_last_name') {
      draft.last_name = text;
    } else if (step === 'collecting_address_line1') {
      draft.address_1 = text;
    } else if (step === 'collecting_city') {
      draft.city = text;
    } else if (step === 'collecting_state') {
      draft.state = mapIndiaStateToCode(text);
    } else if (step === 'collecting_pincode') {
      const digits = (text.match(/\d+/g) || []).join('').slice(0, 6);
      if (digits.length === 6) draft.postcode = digits;
    } else if (step === 'collecting_phone') {
      const digits = (text.match(/\d+/g) || []).join('');
      if (digits.length >= 10) draft.phone = digits.slice(-10);
    } else if (step === 'collecting_email') {
      const emailMatch = text.toLowerCase().match(/[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}/);
      if (emailMatch) draft.email = emailMatch[0];
    }

    draft.country = draft.country || 'IN';
    S.addressDraft = draft;

    if (draft.first_name || draft.address_1 || draft.city || draft.state || draft.postcode || draft.phone || draft.email) {
      persistCheckoutAddress({ billing: draft, shipping: draft });
      if (isCheckoutPage()) applyStoredCheckoutAddress();
    }
  }

  function applyCheckoutAddressPayload(payload) {
    if (!payload || typeof payload !== 'object') return false;
    const b = payload.billing || payload;
    const s = payload.shipping || b;
    applyWooBlocksStoreAddress(payload);

    const shipToggle = document.querySelector('#ship-to-different-address-checkbox');
    if (shipToggle && !shipToggle.checked) {
      shipToggle.checked = true;
      shipToggle.dispatchEvent(new Event('change', { bubbles: true }));
    }

    let changed = false;

    // Email — classic + blocks selectors
    changed = fillCheckoutValue(['#billing_email', '#billing-email', '[name="billing_email"]', '[name="billing-email"]', '[autocomplete="email"]'], b.email) ||
      fillCheckoutByHint(['billing_email', 'email address', 'email', 'autocomplete email'], b.email) || changed;

    // First name
    changed = fillCheckoutValue(['#billing_first_name', '#billing-first_name', '[name="billing_first_name"]', '[autocomplete="given-name"]', '[autocomplete="billing given-name"]'], b.first_name) ||
      fillCheckoutByHint(['billing_first_name', 'first name', 'given-name'], b.first_name) || changed;

    // Last name
    changed = fillCheckoutValue(['#billing_last_name', '#billing-last_name', '[name="billing_last_name"]', '[autocomplete="family-name"]', '[autocomplete="billing family-name"]'], b.last_name) ||
      fillCheckoutByHint(['billing_last_name', 'last name', 'family-name'], b.last_name) || changed;

    // Address
    changed = fillCheckoutValue(['#billing_address_1', '#billing-address_1', '[name="billing_address_1"]', '[autocomplete="address-line1"]', '[autocomplete="billing address-line1"]'], b.address_1) ||
      fillCheckoutByHint(['billing_address_1', 'address', 'address-line1'], b.address_1) || changed;

    // City
    changed = fillCheckoutValue(['#billing_city', '#billing-city', '[name="billing_city"]', '[autocomplete="address-level2"]', '[autocomplete="billing address-level2"]'], b.city) ||
      fillCheckoutByHint(['billing_city', 'city', 'address-level2'], b.city) || changed;

    // State — try state code first (WC classic dropdown uses codes like "KL"),
    // then full name (WC Blocks text field accepts full name)
    const stateCode = mapIndiaStateToCode(b.state || '');
    const stateRaw = b.state || '';
    const stateSelectors = ['#billing_state', '#billing-state', '[name="billing_state"]',
      '[autocomplete="address-level1"]', '[autocomplete="billing address-level1"]',
      '.wc-block-components-state-input input', '[data-id="state"]'];
    changed = fillCheckoutValue(stateSelectors, stateCode || stateRaw) ||
      fillCheckoutValue(stateSelectors, stateRaw) ||
      fillCheckoutValue(stateSelectors, stateCode) ||
      fillCheckoutByHint(['billing_state', 'state', 'address-level1', 'province', 'region'], stateCode || stateRaw) || changed;

    // Postcode / PIN
    changed = fillCheckoutValue([
      '#billing_postcode', '#billing-postcode', '[name="billing_postcode"]',
      '[autocomplete="postal-code"]', '[autocomplete="billing postal-code"]',
      '.wc-block-components-address-form__postcode input', '[data-id="postcode"]'
    ], b.postcode) ||
      fillCheckoutByHint(['billing_postcode', 'pin code', 'pincode', 'postcode', 'postal code', 'postal-code', 'zip'], b.postcode) || changed;

    // Phone
    changed = fillCheckoutValue(['#billing_phone', '#billing-phone', '[name="billing_phone"]', '[autocomplete="tel"]', '[autocomplete="billing tel"]'], b.phone) ||
      fillCheckoutByHint(['billing_phone', 'phone', 'tel', 'mobile'], b.phone) || changed;

    // Country
    changed = fillCheckoutValue(['#billing_country', '#billing-country', '[name="billing_country"]', '[autocomplete="country"]', '[autocomplete="billing country"]'], b.country || 'IN') ||
      fillCheckoutByHint(['billing_country', 'country/region', 'country'], b.country || 'IN') || changed;

    // Shipping fields (same approach)
    changed = fillCheckoutValue(['#shipping_first_name', '#shipping-first_name', '[name="shipping_first_name"]', '[autocomplete="shipping given-name"]'], s.first_name) ||
      fillCheckoutByHint(['shipping_first_name', 'shipping first name'], s.first_name) || changed;
    changed = fillCheckoutValue(['#shipping_last_name', '#shipping-last_name', '[name="shipping_last_name"]', '[autocomplete="shipping family-name"]'], s.last_name) ||
      fillCheckoutByHint(['shipping_last_name', 'shipping last name'], s.last_name) || changed;
    changed = fillCheckoutValue(['#shipping_address_1', '#shipping-address_1', '[name="shipping_address_1"]', '[autocomplete="shipping address-line1"]'], s.address_1) ||
      fillCheckoutByHint(['shipping_address_1', 'shipping address'], s.address_1) || changed;
    changed = fillCheckoutValue(['#shipping_city', '#shipping-city', '[name="shipping_city"]', '[autocomplete="shipping address-level2"]'], s.city) ||
      fillCheckoutByHint(['shipping_city', 'shipping city'], s.city) || changed;
    changed = fillCheckoutValue(['#shipping_state', '#shipping-state', '[name="shipping_state"]', '[autocomplete="shipping address-level1"]'], stateCode || stateRaw) ||
      fillCheckoutByHint(['shipping_state', 'shipping state'], stateCode || stateRaw) || changed;
    changed = fillCheckoutValue(['#shipping_postcode', '#shipping-postcode', '[name="shipping_postcode"]', '[autocomplete="shipping postal-code"]'], s.postcode) ||
      fillCheckoutByHint(['shipping_postcode', 'shipping pin', 'shipping postal'], s.postcode) || changed;
    changed = fillCheckoutValue(['#shipping_country', '#shipping-country', '[name="shipping_country"]', '[autocomplete="shipping country"]'], s.country || 'IN') ||
      fillCheckoutByHint(['shipping_country', 'shipping country'], s.country || 'IN') || changed;

    return changed;
  }

  function applyStoredCheckoutAddress() {
    const payload = readStoredCheckoutAddress();
    if (!payload) return;

    // Apply immediately (classic WC fields may already be in DOM)
    applyCheckoutAddressPayload(payload);

    // Keep retrying for 15s — WC Blocks re-renders and wipes fields, state/postcode
    // may appear late. Do NOT stop early on first success; all fields must be filled.
    let tries = 0;
    const maxTries = 50;
    const timer = setInterval(() => {
      tries += 1;
      applyCheckoutAddressPayload(payload);
      if (tries >= maxTries) clearInterval(timer);
    }, 300);
  }

  async function api(path, body) {
    const controller = new AbortController();
    // 30 s: GPT-4o-mini (3-12 s) + Google TTS (1-3 s) + WooCommerce calls can
    // easily exceed 6.5 s. The old 6.5 s limit was aborting almost every chat
    // request before the backend finished, causing silent failures.
    const TIMEOUT = path === '/chat' ? 35000 : 15000;
    const timer = setTimeout(() => controller.abort(), TIMEOUT);
    // Identify the store so the backend uses THIS tenant's installed-app token
    // (Admin token from OAuth) instead of the global env client. Without this,
    // product search hits the wrong/blank credentials and returns nothing — and in
    // production the backend rejects the call outright (no resolvable tenant).
    const shopQS = tenantQS(path.includes('?'));
    const r = await fetch(`${CFG.agent_api_url}/api/v1${path}${shopQS}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-WooAgent-Nonce': CFG.nonce || '',
        'X-WP-Nonce': CFG.wp_rest_nonce || CFG.nonce || '',
        'X-WooAgent-Session': S.sessionId
      },
      body: JSON.stringify(body),
      signal: controller.signal
    });
    clearTimeout(timer);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  }

  function normalizeChatResponse(raw) {
    if (!raw || typeof raw !== 'object') return null;

    // Direct FastAPI response — already has the right shape, return as-is
    if ('text' in raw || 'response_text' in raw || 'ui_actions' in raw || 'actions' in raw) {
      return raw;
    }

    const success = !!raw.success;
    if (!success) return null;

    const data = raw.data && typeof raw.data === 'object' ? raw.data : {};

    // WordPress bridge: success_response($result) wraps FastAPI output as { success, data: <FastAPI JSON> }
    // Sometimes nested again as data.data if PHP decode added an extra layer
    const inner = (data.data && typeof data.data === 'object') ? data.data : data;

    // Helper: pick first non-empty array from candidates
    const _arr = (...candidates) => {
      for (const c of candidates) { if (Array.isArray(c) && c.length) return c; }
      return [];
    };

    // FastAPI uses 'ui_actions'/'actions'; legacy bridge used 'actions_taken'
    const acts = _arr(inner.ui_actions, inner.actions, inner.actions_taken,
                      data.ui_actions,  data.actions,  data.actions_taken);

    const textVal = inner.text || inner.response_text || data.text || data.response_text || '';

    return {
      session_id:      inner.session_id   || data.session_id   || S.sessionId,
      text:            textVal,
      response_text:   textVal,
      speech_text:     inner.speech_text  || data.speech_text  || textVal,
      language:        inner.language     || data.language     || S.language || 'en',
      ui_actions:      acts,
      actions:         acts,
      audio_base64:    inner.audio_base64 || data.audio_base64 || null,
      audio_format:    inner.audio_format || data.audio_format || null,
      address_state:   inner.address_state  || data.address_state  || S.addressState || 'idle',
      address_data:    inner.address_data   || data.address_data   || null,
      suggested_replies: _arr(inner.suggested_replies, data.suggested_replies),
    };
  }

  async function apiChat(payload) {
    // Hard timeout so S.loading never gets stuck if fetch hangs silently
    const TIMEOUT_MS = 40000; // must exceed api('/chat') inner timeout of 35 s
    const timeoutPromise = new Promise((_, reject) =>
      setTimeout(() => reject(new Error('Agent request timed out')), TIMEOUT_MS)
    );
    try {
      const direct = await Promise.race([api('/chat', payload), timeoutPromise]);
      return normalizeChatResponse(direct) || direct;
    } catch (primaryError) {
      if (!CFG.rest_url) throw primaryError;

      const endpoint = String(CFG.rest_url).replace(/\/$/, '') + '/chat';
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 9000);

      try {
        const res = await fetch(endpoint, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-WooAgent-Nonce': CFG.nonce || '',
            'X-WP-Nonce': CFG.wp_rest_nonce || CFG.nonce || '',
            'X-WooAgent-Session': S.sessionId
          },
          body: JSON.stringify(payload),
          signal: controller.signal
        });

        clearTimeout(timer);
        const json = await res.json().catch(() => ({}));
        if (!res.ok) {
          const detail = (json && (json.error || json.message)) || `HTTP ${res.status}`;
          throw new Error(detail);
        }

        const mapped = normalizeChatResponse(json);
        if (!mapped) {
          throw new Error('Invalid fallback response');
        }
        setStatus('Online · ' + CFG.store_name + ' (bridge)');
        return mapped;
      } catch (fallbackError) {
        clearTimeout(timer);
        throw fallbackError;
      }
    }
  }

  // Seed the real cart (badge + snapshot, no chat card) on load so the first
  // message's cart_context already reflects the customer's actual cart.
  if (IS_SHOPIFY) {
    fetchCartShopify(true).catch(() => {});
  }

  if (isCheckoutPage()) {
    // Apply immediately and again after React Blocks mounts
    applyStoredCheckoutAddress();
    window.addEventListener('load', applyStoredCheckoutAddress);
    document.addEventListener('DOMContentLoaded', applyStoredCheckoutAddress);
    // WooCommerce classic checkout events
    document.body.addEventListener('updated_checkout', applyStoredCheckoutAddress);
    document.body.addEventListener('wc-blocks_added_to_cart', applyStoredCheckoutAddress);
    if (window.jQuery) {
      window.jQuery(document.body).on('updated_checkout', applyStoredCheckoutAddress);
    }

    // Keep applying aggressively for 10s after page load (WC Blocks re-renders clear fields)
    let refillCount = 0;
    const refillTimer = setInterval(() => {
      refillCount++;
      applyStoredCheckoutAddress();
      if (refillCount >= 40) clearInterval(refillTimer); // 40 * 250ms = 10s
    }, 250);

    // MutationObserver with throttle so we don't spam during React renders
    let mutThrottle = null;
    const checkoutObserver = new MutationObserver(() => {
      if (mutThrottle) return;
      mutThrottle = setTimeout(() => {
        applyStoredCheckoutAddress();
        mutThrottle = null;
      }, 400);
    });
    checkoutObserver.observe(document.body, { childList: true, subtree: true });

    // Clean up all listeners, timers, and the observer when navigating away
    window.addEventListener('pagehide', () => {
      window.removeEventListener('load', applyStoredCheckoutAddress);
      document.removeEventListener('DOMContentLoaded', applyStoredCheckoutAddress);
      document.body.removeEventListener('updated_checkout', applyStoredCheckoutAddress);
      document.body.removeEventListener('wc-blocks_added_to_cart', applyStoredCheckoutAddress);
      clearInterval(refillTimer);
      if (mutThrottle) { clearTimeout(mutThrottle); mutThrottle = null; }
      checkoutObserver.disconnect();
    }, { once: true });
  }

  // ── Live Shopping Navigator: resume after an agent-driven navigation ────────
  // The redirect handler sets _wa_reopen just before moving the page. On the new
  // page, re-open the panel via the normal reopen path — openPane() restores the
  // conversation from localStorage and (voice-first mode) auto-resumes the live
  // voice session, exactly like a manual re-open. Mic permission persists
  // per-origin, so getUserMedia succeeds without a fresh gesture in most browsers.
  try {
    if (localStorage.getItem('_wa_voice_nav_resume') === '1') {
      localStorage.removeItem('_wa_voice_nav_resume');
      setTimeout(() => {
        try {
          resumeVoiceNavMode();
        } catch (e) { }
      }, 700);
    } else if (LIVE_NAV && localStorage.getItem('_wa_reopen') === '1') {
      localStorage.removeItem('_wa_reopen');
      setTimeout(() => { try { if (!S.open) openPane(); } catch (e) { } }, 700);
    }
  } catch (e) { }
})();
