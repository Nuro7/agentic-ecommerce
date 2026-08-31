/* Speako Fullscreen Overlay — self-contained SPA (vanilla JS, no build step).
 *
 * Mounts its own #speako-overlay-host with an OPEN shadow root (deliberate for
 * the testing phase — closed roots are invisible in DevTools). All Storefront
 * traffic goes through the server-side proxy /api/v1/overlay/* so no Storefront
 * token ever reaches the browser.
 *
 * The pure logic (router stack, claim() decisions, money, variant pick) lives at
 * the top in plain functions and is exported for `node --test` (see
 * backend/tests/overlay/router.test.js). DOM code only runs in a browser.
 */
(function () {
  'use strict';

  /* ════════════════════════════ PURE LOGIC ════════════════════════════ */

  var SPEAKO_MARKER = { type: 'speako-overlay' };

  var STORE_VIEW_ACTIONS = new Set([
    'show_products',
    'show_product_detail',
    'show_availability',
    'compare_products',
    'show_cart',
    'cart_updated',
    'apply_discount_code',
    'highlight_card',
    'active_product_index',
    'search'
  ]);

  function RouterStack() {
    this._stack = [];
  }
  RouterStack.prototype.push = function (view, params) {
    this._stack.push({ view: view || 'home', params: params || {} });
    return this._stack.length;
  };
  RouterStack.prototype.pop = function () {
    return this._stack.pop() || null;
  };
  RouterStack.prototype.top = function () {
    return this._stack.length ? this._stack[this._stack.length - 1] : null;
  };
  RouterStack.prototype.size = function () {
    return this._stack.length;
  };
  RouterStack.prototype.reset = function () {
    this._stack.length = 0;
  };

  function isSameOriginTarget(url, origin) {
    if (!url) return true;                 // no url → treat as store-targeted
    if (url.charAt(0) === '/') return true; // relative path → same store
    try {
      return new URL(url, origin).origin === (origin || '');
    } catch (e) {
      return false;
    }
  }

  function isRealCheckout(act) {
    if (!act) return false;
    var p = act.payload || {};
    // A redirect_checkout with an explicit store-reason is store-nav, not the
    // final checkout transition. Plain redirect_checkout (/checkout) is REAL.
    if (act.type !== 'redirect_checkout') return false;
    return !(p && (p.reason === 'search' || p.reason === 'product' || p.reason === 'cart'));
  }

  function isStoreNav(act, origin) {
    if (!act) return false;
    var p = act.payload || {};
    if (act.type !== 'redirect' && act.type !== 'redirect_checkout') return false;
    var reason = String(p.reason || '');
    if (reason !== 'search' && reason !== 'product' && reason !== 'cart') return false;
    return isSameOriginTarget(p.url, origin);
  }

  function claimAction(act, opts) {
    opts = opts || {};
    if (!opts.enabled || opts.platform !== 'shopify') return false;
    if (!act || !act.type) return false;
    if (STORE_VIEW_ACTIONS.has(act.type)) return true;
    if (act.type === 'redirect' || act.type === 'redirect_checkout') {
      if (isRealCheckout(act)) return false;       // true checkout stays a hard nav
      return isStoreNav(act, opts.origin);
    }
    return false;
  }

  function extractHandle(payload, url) {
    var p = payload || {};
    if (p.product && p.product.handle) return String(p.product.handle);
    if (p.handle) return String(p.handle);
    var target = p.url || url || '';
    var m = String(target).match(/\/products\/([^/?#]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }

  // Pull the search term out of a storefront URL like /search?q=blue+sneakers.
  // The brain/LLM often emit search redirects carrying ONLY a URL (speako:navigate
  // detail / append_live_navigation), so the overlay must derive the query from
  // the URL instead of bailing out with an empty search that shows "No products".
  function extractSearchQuery(url) {
    var s = String(url || '');
    var m = s.match(/[?&]q=([^&#]*)/);
    if (!m) return '';
    try { return decodeURIComponent(m[1].replace(/\+/g, ' ')); }
    catch (e) { return m[1]; }
  }

  // Normalize the /overlay/search response into a stable shape. The server now
  // returns an envelope {products, query(cleaned), message, match, ...}; older
  // builds (and the API test fake) may return a bare product array. Both are
  // accepted so the grid never mis-reads the payload.
  function unwrapSearch(data) {
    if (Array.isArray(data)) {
      return { products: data, query: '', message: '', match: data.length ? 'exact' : 'none' };
    }
    if (data && typeof data === 'object') {
      return {
        products: Array.isArray(data.products) ? data.products : [],
        query: data.query || '',
        message: data.message || '',
        match: data.match || ''
      };
    }
    return { products: [], query: '', message: '', match: 'none' };
  }

  // Choose the product whose title best matches a spoken/typed title. Prefers an
  // exact (case-insensitive) title equality, then substring containment either
  // way. Returns null when nothing plausibly matches — the caller must NOT open
  // a PDP in that case, so we never render a hallucinated / wrong product.
  function pickTitleMatch(products, title) {
    var list = Array.isArray(products) ? products : [];
    if (!list.length) return null;
    var want = String(title || '').trim().toLowerCase();
    if (!want) return null;
    var contains = null;
    for (var i = 0; i < list.length; i++) {
      var t = String((list[i] && list[i].title) || '').trim().toLowerCase();
      if (!t) continue;
      if (t === want) return list[i];
      if (!contains && (t.indexOf(want) !== -1 || want.indexOf(t) !== -1)) contains = list[i];
    }
    return contains;
  }

  function formatMoney(amount, currency) {
    var num = Number(amount || 0);
    if (isNaN(num)) num = 0;
    if (currency) return String(currency) + num.toFixed(2);
    return num.toFixed(2);
  }

  // Brand-shade helpers — plain sRGB channel math so merchant re-tinting works on
  // every engine (older Safari / in-app webviews lack color-mix). A non-hex input
  // returns null, and callers fall back to the stylesheet's rose/magenta defaults.
  function spHexToRgb(hex) {
    var h = String(hex || '').replace('#', '').trim();
    if (h.length === 3) h = h.charAt(0) + h.charAt(0) + h.charAt(1) + h.charAt(1) + h.charAt(2) + h.charAt(2);
    if (h.length !== 6 || /[^0-9a-fA-F]/.test(h)) return null;
    var n = parseInt(h, 16);
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
  }
  function spChan(v) { v = Math.round(v); return v < 0 ? 0 : (v > 255 ? 255 : v); }
  function spToHex(r, g, b) {
    return '#' + [r, g, b].map(function (x) {
      var s = spChan(x).toString(16);
      return s.length === 1 ? '0' + s : s;
    }).join('');
  }
  // amt 0..1: fraction of the way from `hex` toward pure white (toWhite) or black.
  function spMixToward(hex, toWhite, amt) {
    var c = spHexToRgb(hex);
    if (!c) return hex;
    var t = toWhite ? 255 : 0;
    return spToHex(c.r + (t - c.r) * amt, c.g + (t - c.g) * amt, c.b + (t - c.b) * amt);
  }
  function spAlpha(hex, a) {
    var c = spHexToRgb(hex);
    if (!c) return hex;
    return 'rgba(' + c.r + ', ' + c.g + ', ' + c.b + ', ' + a + ')';
  }

  function cartSubtotal(lines) {
    var total = 0;
    (lines || []).forEach(function (line) {
      var price = (line.unit_price && line.unit_price.amount) || 0;
      total += Number(price || 0) * (Number(line.quantity) || 0);
    });
    return Math.max(0, Math.round(total * 100) / 100);
  }

  function pickVariant(product, selected) {
    var variants = (product && product.variants) || [];
    if (!selected || !selected.length) {
      for (var i = 0; i < variants.length; i++) {
        if (variants[i].available_for_sale !== false) return variants[i];
      }
      return variants[0] || null;
    }
    var target = selected.map(function (s) { return s.name + '=' + s.value; }).sort().join('|');
    for (var j = 0; j < variants.length; j++) {
      var opts = (variants[j].selected_options || [])
        .map(function (o) { return (o.name || '') + '=' + (o.value || ''); }).sort().join('|');
      if (opts === target) return variants[j];
    }
    // No exact match → prefer the first AVAILABLE variant, never an OOS one.
    for (var k = 0; k < variants.length; k++) {
      if (variants[k].available_for_sale !== false) return variants[k];
    }
    return variants[0] || null;
  }

  function variantPrice(variant) {
    if (!variant || !variant.price) return 0;
    var n = Number(variant.price.amount || 0);
    return isNaN(n) ? 0 : n;
  }

  function applyDiscount(subtotal, percent) {
    var pct = Math.min(Math.max(Number(percent) || 0, 0), 100);
    var total = Number(subtotal || 0) * (100 - pct) / 100;
    if (isNaN(total)) total = 0;
    return Math.max(0, Math.round(total * 100) / 100);
  }

  // Percent saved when a compare-at price is present and higher than the price.
  // Returns an integer 0-100; 0 means "no genuine discount" (hide the badge).
  function savePercent(price, compareAt) {
    var p = Number(price || 0);
    var c = Number(compareAt || 0);
    if (!(c > p && p > 0)) return 0;
    return Math.round((c - p) / c * 100);
  }

  // Star fill as a 0-100 width percentage for the gold overlay layer.
  function starPercent(rating) {
    var r = Number(rating || 0);
    if (isNaN(r) || r < 0) r = 0;
    if (r > 5) r = 5;
    return Math.round(r / 5 * 1000) / 10;
  }

  // Normalize a compare list (handles or product objects) to unique handles,
  // capped at 3 per the spec (2-3 products side-by-side).
  function normalizeCompare(items) {
    var seen = {};
    var out = [];
    (items || []).forEach(function (it) {
      var h = typeof it === 'string' ? it : (it && (it.handle || (it.product && it.product.handle)));
      h = h ? String(h) : '';
      if (h && !seen[h]) { seen[h] = 1; out.push(h); }
    });
    return out.slice(0, 3);
  }

  /* ════════════════════════════ SPA ════════════════════════════ */

  function Overlay() {
    this.cfg = {};
    this._open = false;
    this._stack = new RouterStack();
    this._listeners = {};
    this._cartId = null;
    this._products = [];
    this._facets = [];
    this._discountCodes = [];
    this._currentVariant = null;
    this._currentProduct = null;   // product on view — handed to voice on Buy-It-Now
    this._pdpCache = {};
    this._compare = [];        // handles selected for side-by-side compare
    this._compareCache = {};   // handle → full product
    this._host = null;
    this._root = null;
    this._shadow = null;
    this._scrollLockOwner = null;
    this._nativeHtmlCache = {};
    this._nativePdpOpen = false;
    this._nativeVariant = null;
    this._nativeProduct = null;
    // True once a REAL checkout redirect has begun. While set, the overlay is
    // locked into a full-screen "Redirecting to secure checkout…" state: no
    // close(), back, Escape, popstate, view re-render, or voice event may reset
    // it to Home or interrupt the browser navigation to the hosted checkout.
    this._checkoutRedirecting = false;
    this._checkoutRedirectTimer = null;
  }

  var proto = Overlay.prototype;

  // Inline stroke icons (Lucide-style, currentColor) — no emoji, theme-aware.
  var SVG = {
    mic: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="22"/></svg>',
    sparkles: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3z"/><path d="M19 15l.9 2.4L22 18l-2.1.6L19 21l-.9-2.4L16 18l2.1-.6L19 15z"/></svg>',
    trending: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/></svg>',
    truck: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1 3h13v13H1z"/><path d="M14 8h4l3 3v5h-7V8z"/><circle cx="6.5" cy="18.5" r="1.8"/><circle cx="17.5" cy="18.5" r="1.8"/></svg>',
    search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>'
  };

  proto.setConfig = function (cfg) {
    cfg = cfg || {};
    this.cfg = Object.assign({}, this.cfg, cfg, {
      overlayEnabled: cfg.overlayEnabled !== undefined ? cfg.overlayEnabled : this.cfg.overlayEnabled,
      platform: cfg.platform || this.cfg.platform || 'shopify',
      apiBase: ((cfg.agent_api_url || this.cfg.apiBase || '') + '').replace(/\/$/, ''),
      shop: cfg.shop || this.cfg.shop || '',
      storeName: cfg.store_name || this.cfg.storeName || 'Speako',
      currency: cfg.currency || this.cfg.currency || '$'
    });
    console.log('[Speako Overlay] setConfig →', {
      overlayEnabled: this.cfg.overlayEnabled,
      platform: this.cfg.platform,
      shop: this.cfg.shop,
      apiBase: this.cfg.apiBase,
      storeName: this.cfg.storeName,
      hasRoot: !!this._root,
      rawOverlayMode: window.wooagent_config && window.wooagent_config.overlay_mode
    });
    if (this.cfg.primary_color && this._root) {
      // Re-tint the whole accent system to the merchant's brand colour. Shades
      // are computed with plain sRGB math (not color-mix) so custom colours
      // render identically on older Safari / in-app webviews. A non-hex value
      // leaves the stylesheet's rose/magenta defaults untouched.
      var c = this.cfg.primary_color;
      if (spHexToRgb(c)) {
        var st = this._root.style;
        st.setProperty('--sp-brand', c);
        st.setProperty('--sp-brand-2', spMixToward(c, false, 0.24));
        st.setProperty('--sp-brand-lite', spMixToward(c, true, 0.26));
        st.setProperty('--sp-brand-soft', spAlpha(c, 0.16));
        st.setProperty('--sp-brand-glow', spAlpha(c, 0.45));
        st.setProperty('--sp-grad', 'linear-gradient(135deg, ' +
          spMixToward(c, true, 0.20) + ' 0%, ' + c + ' 52%, ' +
          spMixToward(c, false, 0.24) + ' 100%)');
      }
    }
  };

  proto.on = function (event, cb) {
    (this._listeners[event] = this._listeners[event] || []).push(cb);
    return this;
  };

  proto.emit = function (event, data) {
    (this._listeners[event] || []).forEach(function (cb) {
      try { cb(data); } catch (e) {}
    });
  };

  proto.isOpen = function () { return this._open; };

  proto.open = function (view, params) {
    if (!this._ensureMounted()) {
      console.warn('[Speako Overlay] open() aborted — _ensureMounted() failed (no DOM or shadow attach error)');
      return false;
    }
    console.log('[Speako Overlay] open() → view=' + view + ' enabled=' + this._enabled() + ' wasOpen=' + this._open, params || {});
    if (!this._open) {
      this._open = true;
      this._stack.reset();
      try { history.pushState(SPEAKO_MARKER, ''); } catch (e) {}
      this._lockScroll();
      this._root.classList.add('sp-visible');
      console.log('[Speako Overlay] OPENED → sp-visible added to root', { rootVisible: this._root.classList.contains('sp-visible') });
    }
    this.pushView(view || 'home', params || {});
    return true;
  };

  proto.pushView = function (view, params) {
    if (!this._open) this.open(view, params);
    params = params || {};
    this._stack.push(view, params);
    if (this._stack.size() > 1) {
      try { history.pushState(SPEAKO_MARKER, ''); } catch (e) {}
    }
    this._render(view, params);
    return true;
  };

  proto.close = function () {
    // Locked during a real checkout redirect — never tear the overlay down (or
    // reveal Home) while the browser is navigating to the hosted checkout.
    if (this._checkoutRedirecting) {
      console.log('[Speako Overlay] close() suppressed — checkout redirect in progress');
      return;
    }
    this._closeNativePdp();
    if (!this._open) {
      console.log('[Speako Overlay] close() called but overlay was NOT open');
      return;
    }
    this._open = false;
    this._unlockScroll();
    if (this._root) this._root.classList.remove('sp-visible');
    // Unwind our synthetic history marker so the theme's back-nav is untouched.
    try { if (history.state && history.state.type === 'speako-overlay') { history.back(); } } catch (e) {}
    this.emit('close');
    console.log('[Speako Overlay] CLOSED');
  };

  // ── Real-checkout redirect lock ─────────────────────────────────────────
  // Called the instant a TRUE checkout transition begins (the overlay's own
  // express buy-now / cart checkout, OR the widget's guided prepareShopifyCheckout
  // for redirect_checkout_with_address). It shows a full-screen, non-dismissable
  // "Redirecting to secure checkout…" cover so nothing — a stray voice event, a
  // browser Back, an Escape key, a competing render — can reset the overlay to
  // Home before window.location.href actually leaves the page. Idempotent: a
  // second call only refreshes the message. If a bound checkout_url is passed it
  // performs the hard navigation itself.
  proto.beginCheckoutRedirect = function (opts) {
    opts = opts || {};
    var message = opts.message || 'Redirecting to secure checkout…';
    var _this = this;
    this._checkoutRedirecting = true;
    // Make sure there is a surface to paint the cover onto, even if the overlay
    // was never opened (e.g. a cart-page checkout with the panel closed).
    if (!this._ensureMounted()) {
      // No DOM surface — still honour the hard nav if we were handed a URL.
      if (opts.checkoutUrl) { try { window.location.href = opts.checkoutUrl; } catch (e) {} }
      return;
    }
    if (!this._open) {
      this._open = true;
      this._lockScroll();
      if (this._root) this._root.classList.add('sp-visible');
    }
    this._paintCheckoutRedirect(message);
    // Safety valve: if navigation has not taken over after 20s the bind almost
    // certainly failed. Release the lock and let the customer retry instead of
    // trapping them under a spinner forever.
    try { if (this._checkoutRedirectTimer) clearTimeout(this._checkoutRedirectTimer); } catch (e) {}
    this._checkoutRedirectTimer = setTimeout(function () {
      _this._clearCheckoutRedirect();
      try { _this._toast('Could not reach checkout. Please try again.', true); } catch (e) {}
    }, 20000);
    if (opts.checkoutUrl) {
      try { window.location.href = opts.checkoutUrl; } catch (e) {}
    }
  };

  proto._paintCheckoutRedirect = function (message) {
    if (!this._root) return;
    var existing = this._root.querySelector('.sp-checkout-redirect');
    if (existing) {
      var msgEl = existing.querySelector('[data-msg]');
      if (msgEl) msgEl.textContent = message;
      return;
    }
    var cover = document.createElement('div');
    cover.className = 'sp-checkout-redirect';
    // Inline styles so the lock renders identically regardless of the external
    // stylesheet (it must never fail open).
    cover.setAttribute('style', [
      'position:absolute', 'inset:0', 'z-index:2147483647',
      'display:flex', 'flex-direction:column', 'align-items:center',
      'justify-content:center', 'gap:18px', 'text-align:center', 'padding:24px',
      'background:rgba(255,255,255,0.97)', 'color:#111827',
      'font:600 16px/1.4 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif'
    ].join(';'));
    cover.innerHTML =
      '<div class="sp-spinner" aria-hidden="true" style="width:44px;height:44px;border:4px solid rgba(0,0,0,0.12);border-top-color:currentColor;border-radius:50%;animation:sp-spin 0.8s linear infinite"></div>' +
      '<div data-msg role="status" aria-live="assertive">' + escapeHtml(message) + '</div>' +
      '<style>@keyframes sp-spin{to{transform:rotate(360deg)}}</style>';
    this._root.appendChild(cover);
  };

  proto._clearCheckoutRedirect = function () {
    this._checkoutRedirecting = false;
    try { if (this._checkoutRedirectTimer) clearTimeout(this._checkoutRedirectTimer); } catch (e) {}
    this._checkoutRedirectTimer = null;
    if (this._root) {
      var cover = this._root.querySelector('.sp-checkout-redirect');
      if (cover && cover.parentNode) cover.parentNode.removeChild(cover);
    }
  };

  // Public: release the redirect lock after a KNOWN failed bind (called by the
  // widget) so the customer isn't bounced to a blank checkout / Home. Restores
  // the last view and surfaces a retry message instead.
  proto.endCheckoutRedirect = function (message) {
    if (!this._checkoutRedirecting) return;
    this._clearCheckoutRedirect();
    try { this._renderTop(); } catch (e) {}
    if (message) { try { this._toast(message, true); } catch (e) {} }
  };

  proto.isCheckoutRedirecting = function () { return !!this._checkoutRedirecting; };


  proto.claim = function (act) {
    var result = claimAction(act, {
      enabled: this._enabled(),
      platform: this.cfg.platform,
      origin: (typeof location !== 'undefined' ? location.origin : '')
    });
    if (act && act.type) {
      console.log('[Speako Overlay] claim(' + act.type + ') → ' + result + ' | enabled=' + this._enabled() + ' platform=' + this.cfg.platform);
    }
    return result;
  };

  proto.handle = function (act) {
    var _this = this;
    console.log('[Speako Overlay] handle() called → type=' + (act && act.type), (act && act.payload) || {});
    return Promise.resolve().then(function () {
      var p = act.payload || {};
      switch (act.type) {
        case 'search': {
          var _searchQ = p.query || extractSearchQuery(p.url);
          console.log('[Speako Overlay] search action → query="' + _searchQ + '" (raw=' + JSON.stringify(p.query) + ' url=' + (p.url || '') + ')');
          _this.open('search', { query: _searchQ || '' });
          return _this._loadSearch(_searchQ || '');
        }
        case 'show_products': {
          var _prods = p.products || [];
          var _spQ = p.query || extractSearchQuery(p.url);
          _this.open('search', { query: _spQ || '', products: _prods });
          // Products already provided in the payload → render them as-is (a
          // _loadSearch('') call would only wipe them with the empty state).
          if (_prods.length) { _this._render('search', { query: _spQ || '', products: _prods, filters: p.filters || {} }); return Promise.resolve(); }
          return _this._loadSearch(_spQ || '', p.filters);
        }
        case 'show_product_detail':
        case 'show_availability': {
          var isAvail = act.type === 'show_availability';
          var handle = extractHandle(p, p.url || '');
          // 1) Fully-specified product object with a handle → exact bind, render
          //    directly (no round-trip, no guessing).
          if (p.product && (p.product.handle || handle)) {
            var boundHandle = handle || p.product.handle;
            _this.open('pdp', { handle: boundHandle, product: p.product, availability: isAvail });
            _this._render('pdp', { handle: boundHandle, product: p.product });
            return;
          }
          // 2) Exact handle/id → load the canonical product; never fabricate.
          if (handle) {
            _this.open('pdp', { handle: handle, availability: isAvail });
            return _this._loadProduct(handle);
          }
          // 3) No handle → resolve by title against the live catalog BEFORE
          //    opening a PDP, so a hallucinated title can never render a product.
          var wantTitle = p.title || p.product_title
            || (p.product && p.product.title) || p.name || p.query || '';
          if (wantTitle) {
            _this.open('pdp', { handle: '', loading: true, availability: isAvail });
            return _this._resolveProductByTitle(wantTitle);
          }
          _this._toast('Could not open that product.', true);
          return;
        }
        case 'show_cart':
        case 'cart_updated':
          return _this._showCartExits();
        case 'compare_products': {
          // Accept handles from payload.handles, payload.products[].handle, or
          // whatever is already staged in the compare tray.
          var reqHandles = normalizeCompare(
            (p.handles && p.handles.length ? p.handles : (p.products || _this._compare)) || []
          );
          if (!reqHandles.length) { _this._toast('Pick 2-3 products to compare.', true); return; }
          _this._compare = reqHandles;
          _this.open('compare', { handles: reqHandles });
          return _this._loadCompare(reqHandles);
        }
        case 'apply_discount_code':
          return _this._showCartExits({ code: p.code || p.discount_code || '' });
        case 'highlight_card':
          _this._highlightCard(p);
          return;
        case 'active_product_index': {
          // Voice stream signalling "I'm now talking about product N" — the
          // index may arrive under any of these keys depending on the emitter.
          var _idx = (p.index != null) ? p.index
                   : (p.active_product_index != null ? p.active_product_index
                      : (p.value != null ? p.value : null));
          _this._highlightCard({ index: _idx, handle: p.handle, product: p.product });
          return;
        }
        case 'redirect_checkout_with_address': {
          // REAL checkout with a prefilled address. The widget owns the bind +
          // navigation (prepareShopifyCheckout), so the overlay normally never
          // claims this. If it ever reaches here, DO NOT switch views or reset to
          // Home — lock into the redirect state. Hard-nav only if a bound URL is
          // already present (else the widget completes the navigation).
          _this.beginCheckoutRedirect({ checkoutUrl: p.checkout_url || p.url || '' });
          return;
        }
        case 'redirect':
        case 'redirect_checkout': {
          var reason = String(p.reason || '');
          if (reason === 'search') {
            var _redQ = p.query || extractSearchQuery(p.url);
            console.log('[Speako Overlay] redirect(reason=search) → query="' + _redQ + '" (raw=' + JSON.stringify(p.query) + ' url=' + (p.url || '') + ')');
            _this.open('search', { query: _redQ || '' });
            return _this._loadSearch(_redQ || '', p.filters);
          }
          if (reason === 'product') {
            var ph = extractHandle(p, p.url || '');
            _this.open('pdp', { handle: ph, product: p.product || null });
            if (p.product) { _this._render('pdp', { handle: ph, product: p.product }); return; }
            if (!ph) { _this._toast('Could not open that product.', true); return; }
            return _this._loadProduct(ph);
          }
          if (reason === 'cart') {
            _this.open('cart', {});
            return _this._loadCart();
          }
          // No store-reason → this is the TRUE checkout transition. Never reset
          // to Home: lock the overlay and let the hard navigation proceed.
          if (isRealCheckout(act)) {
            _this.beginCheckoutRedirect({ checkoutUrl: p.checkout_url || (act.type === 'redirect_checkout' ? '' : p.url) || '' });
          }
          return;
        }
        default:
          return;
      }
    });
  };

  proto._enabled = function () {
    var cfg = this.cfg;
    if (!cfg.overlayEnabled) return false;
    if (cfg.platform !== 'shopify') return false;
    return true;
  };

  proto._ensureMounted = function () {
    if (this._host && this._root) return true;
    if (typeof document === 'undefined' || typeof window === 'undefined') return false;
    try {
      var host = document.createElement('div');
      host.id = 'speako-overlay-host';
      document.documentElement.appendChild(host);
      var shadow = host.attachShadow({ mode: 'open' });
      var style = document.createElement('style');
      style.textContent = typeof window.__SPEAKO_OVERLAY_CSS__ === 'string'
        ? window.__SPEAKO_OVERLAY_CSS__
        : '';
      var root = document.createElement('div');
      root.className = 'speako-root';
      root.setAttribute('data-theme', (this.cfg && this.cfg.theme === 'light') ? 'light' : 'dark');
      shadow.appendChild(style);
      shadow.appendChild(root);

      this._host = host;
      this._shadow = shadow;
      this._root = root;
      this._wire(cssRoot(root));
      console.log('[Speako Overlay] mounted #speako-overlay-host → host=' + !!host + ' shadow=' + !!shadow + ' cssBytes=' + (style.textContent || '').length);
      return true;
    } catch (e) {
      console.warn('[Speako Overlay] _ensureMounted() threw:', e);
      return false;
    }
  };

  function cssRoot(root) {
    var css = {};
    ['header', 'back', 'title', 'cartBtn', 'badge', 'close', 'body',
     'searchInput', 'searchGo', 'transcript', 'facets', 'grid', 'toast'].forEach(function (id) {
      css[id] = null;
    });
    css._root = root;
    return css;
  }

  proto._wire = function (refs) {
    var _this = this;
    this._refs = refs;
    refs._root.innerHTML = '' +
      '<div class="speako-header">' +
        '<button class="sp-btn" data-act="back" aria-label="Back">&#8592;</button>' +
        '<div class="sp-title-wrap">' +
          '<div class="sp-title">' + escapeHtml(this.cfg.storeName || 'Speako') + '</div>' +
          '<div class="sp-subtitle" data-header-subtitle></div>' +
        '</div>' +
        '<button class="sp-btn speako-cart-badge" data-act="cart" aria-label="Cart">' +
          '&#128722;<span class="sp-badge" data-badge>0</span></button>' +
        '<button class="sp-btn" data-act="close" aria-label="Close">&#10005;</button>' +
      '</div>' +
      '<div class="speako-body" data-body></div>' +
      '<div class="sp-voicebar">' +
        '<div class="sp-voicebar-field">' +
          '<span class="sp-wave" data-wave>' +
            '<span></span><span></span><span></span><span></span><span></span><span></span><span></span></span>' +
          '<input data-voice-input placeholder="Ask Aria or find your style…" aria-label="Ask Aria">' +
          '<span class="sp-voicebar-label">Voice active</span>' +
          '<button class="sp-mic-btn" data-act="mic" aria-label="Talk to Aria">' + SVG.mic + '</button>' +
        '</div>' +
      '</div>' +
      '<div class="sp-toast" data-toast></div>';

    this._els = {
      header: refs._root.querySelector('.speako-header'),
      back: refs._root.querySelector('[data-act="back"]'),
      cartBtn: refs._root.querySelector('[data-act="cart"]'),
      closeBtn: refs._root.querySelector('[data-act="close"]'),
      badge: refs._root.querySelector('[data-badge]'),
      subtitle: refs._root.querySelector('[data-header-subtitle]'),
      body: refs._root.querySelector('[data-body]'),
      voicebar: refs._root.querySelector('.sp-voicebar'),
      voiceInput: refs._root.querySelector('[data-voice-input]'),
      micBtn: refs._root.querySelector('[data-act="mic"]'),
      wave: refs._root.querySelector('.sp-voicebar [data-wave]'),
      toast: refs._root.querySelector('[data-toast]')
    };

    // Persistent voice bar — submit text search from any screen; toggle voice.
    var submitVoice = function () {
      var q = (_this._els.voiceInput.value || '').trim();
      if (!q) return;
      _this._els.voiceInput.value = '';
      // Typed text is a conversation with Aria (the Brain), not a bare catalog
      // lookup: it shares the live-voice memory (same session id) and handles
      // arbitrary input — greetings, questions, "no such product" — gracefully.
      _this._chat(q);
    };
    this._els.voiceInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); submitVoice(); }
    });
    this._els.micBtn.addEventListener('click', function () {
      _this._toggleVoice();
    });

    refs._root.addEventListener('click', function (e) {
      var el = e.target && e.target.closest ? e.target.closest('[data-act]') : null;
      if (!el) return;
      var act = el.getAttribute('data-act');
      if (act === 'close') _this.close();
      else if (act === 'back') _this._handleBack();
      else if (act === 'cart') { _this.open('cart', {}); _this._loadCart(); }
    });

    window.addEventListener('popstate', function () {
      if (!_this._open) return;
      // Locked during a real checkout redirect: swallow Back by re-pushing our
      // marker so the browser can't pop the overlay away mid-navigation.
      if (_this._checkoutRedirecting) {
        try { history.pushState(SPEAKO_MARKER, ''); } catch (e) {}
        return;
      }
      if (_this._stack.size() > 1) {
        _this._stack.pop();
        _this._renderTop();
      } else {
        _this.close();
      }
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && _this._open) {
        if (_this._checkoutRedirecting) { e.preventDefault(); return; }
        e.preventDefault();
        _this.close();
      }
    });

    // Single delegated click handler — product cards, header actions.
    refs._root.addEventListener('click', function (e) {
      var actEl = e.target && e.target.closest ? e.target.closest('[data-act]') : null;
      if (actEl) {
        var act = actEl.getAttribute('data-act');
        if (act === 'close') _this.close();
        else if (act === 'back') _this._handleBack();
        else if (act === 'cart') { _this.open('cart', {}); _this._loadCart(); }
        return;
      }
      // Compare toggle sits ON a card — intercept it before card navigation.
      var cmpEl = e.target && e.target.closest ? e.target.closest('[data-compare-toggle]') : null;
      if (cmpEl) {
        e.stopPropagation();
        _this._toggleCompare(cmpEl.getAttribute('data-compare-toggle'), cmpEl);
        return;
      }
      var card = e.target && e.target.closest ? e.target.closest('[data-handle]') : null;
      if (card && _this._open) {
        var handle = card.getAttribute('data-handle');
        if (handle) {
          _this.pushView('pdp', { handle: handle });
          _this._loadProduct(handle);
        }
      }
    });

    // Ambient voice events from the widget bridge → drive the transcript line
    // and the waveform visualizer without any top-level reload.
    this.on('transcript', function (data) {
      var t = (data && (data.text || data.transcript)) || '';
      // While the mic is live, mirror the recognized speech into the input.
      if (_this._listening && _this._els.voiceInput) {
        _this._els.voiceInput.value = t;
      }
      if (_this._els.transcript) _this._els.transcript.textContent = t || 'Listening…';
      _this._setWave(!!t || !!_this._listening);
    });
    this.on('status', function (data) {
      var s = (data && (data.state || data.status)) || '';
      var speaking = /listen|speak|record|think|process|active/i.test(String(s));
      _this._setWave(speaking);
    });
    this.on('suggestion', function () { /* forwarded to voice agent listeners */ });
  };

  proto._setWave = function (active) {
    if (!this._els) return;
    var on = !!active;
    if (this._els.wave) this._els.wave.classList.toggle('active', on);
    if (this._els.micBtn) this._els.micBtn.classList.toggle('listening', on);
    // The greeting orb (present only on the home view) mirrors the state.
    var orb = this._els.body && this._els.body.querySelector('[data-orb]');
    if (orb) orb.classList.toggle('listening', on);
  };

  // Toggle the live voice session. The overlay owns the UI state (wave, mic,
  // orb) and emits voicestart/voicestop so the widget bridge can drive the
  // actual WebSocket capture — no page reload, works from every screen.
  proto._toggleVoice = function () {
    this._listening = !this._listening;
    this._setWave(this._listening);
    if (this._listening) {
      this.emit('voicestart', {});
      this._publish('voice_started', {});
    } else {
      this.emit('voicestop', {});
    }
  };

  // Start the live voice session if it isn't already running (idempotent). The
  // launcher calls this right after open() so the mic is hot the instant the
  // overlay appears — pure voice-to-voice, no extra tap on the orb.
  proto.startVoice = function () {
    if (this._listening) return;
    this._toggleVoice();
  };

  proto._handleBack = function () {
    if (this._checkoutRedirecting) return;   // locked during checkout redirect
    if (this._stack.size() > 1) {
      this._stack.pop();
      this._renderTop();
    } else {
      this.close();
    }
  };

  // Re-render the view now on top of the stack, restoring live state (search
  // results / pdp product / cart) so back-nav never shows an empty screen.
  proto._renderTop = function () {
    var top = this._stack.top();
    if (!top) return;
    if (top.view === 'search') {
      this._render('search', { query: top.params.query || '', products: this._products, filters: top.params.filters });
    } else if (top.view === 'pdp') {
      var product = top.params.product || this._pdpCache[top.params.handle || ''];
      this._render('pdp', { handle: top.params.handle || '', product: product || null });
      if (!product && top.params.handle) this._loadProduct(top.params.handle);
    } else if (top.view === 'cart') {
      this._loadCart();
    } else if (top.view === 'compare') {
      var handles = top.params.handles || this._compare;
      if (handles && handles.length) { this._render('compare', { handles: handles, loading: true }); this._loadCompare(handles); }
      else this._render('compare', { handles: [] });
    } else {
      this._render(top.view, top.params);
    }
  };

  proto._lockScroll = function () {
    if (!document.body) return;
    this._scrollLockOwner = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
  };

  proto._unlockScroll = function () {
    if (!document.body) return;
    document.body.style.overflow = this._scrollLockOwner || '';
    this._scrollLockOwner = null;
  };

  /* ── API helpers ── */

  proto._api = function (path, opts) {
    opts = opts || {};
    var base = this.cfg.apiBase;
    var url = base + '/api/v1/overlay' + path;
    var sep = url.indexOf('?') === -1 ? '?' : '&';
    if (this.cfg.shop) url += sep + 'shop=' + encodeURIComponent(this.cfg.shop);
    var init = { method: opts.method || 'GET', headers: { 'Content-Type': 'application/json' } };
    if (opts.body) init.body = JSON.stringify(opts.body);
    return fetch(url, init).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (data) {
        if (!resp.ok) {
          var msg = (data.errors && data.errors[0] && data.errors[0].message) || ('Request failed (' + resp.status + ')');
          var err = new Error(msg);
          err.status = resp.status;
          throw err;
        }
        return data;
      });
    });
  };

  /* ── Conversational chat → the Brain (POST /api/v1/chat) ──
     Text typed in the overlay talks to the full agent, NOT the stateless
     /overlay/search. Memory is shared with live voice because both read the
     SAME session id from localStorage('_wa_sid_v2') — the widget bridge owns
     that key for the voice WebSocket. One id ⇒ one Redis session ⇒ Aria
     remembers across voice + text + navigation. */
  proto._sid = function () {
    var mk = function () {
      return 's_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 10);
    };
    try {
      var id = localStorage.getItem('_wa_sid_v2');
      if (!id) { id = mk(); localStorage.setItem('_wa_sid_v2', id); }
      return id;
    } catch (e) {
      if (!this.__sid) this.__sid = mk();
      return this.__sid;
    }
  };

  // Coerce a Brain product (show_products payload) into the card shape the grid
  // renderer expects, tolerating scalar prices / image arrays. Products come
  // from the same store client as /overlay/search, so this is mostly a no-op —
  // it exists so a shape drift never blanks the shelf.
  proto._normProduct = function (p) {
    if (!p || typeof p !== 'object') return p;
    var money = function (v) { return v == null ? null : (typeof v === 'object' ? v : { amount: v }); };
    var out = {};
    for (var k in p) { if (Object.prototype.hasOwnProperty.call(p, k)) out[k] = p[k]; }
    if (out.price != null) out.price = money(out.price);
    if (out.compare_at_price != null) out.compare_at_price = money(out.compare_at_price);
    if (!out.image) {
      var img = p.featured_image || p.thumbnail || '';
      if (!img && Array.isArray(p.images) && p.images.length) {
        var f = p.images[0];
        img = typeof f === 'string' ? f : (f && (f.src || f.url)) || '';
      }
      if (!img && p.image && typeof p.image === 'object') img = p.image.src || p.image.url || '';
      out.image = img || '';
    }
    return out;
  };

  proto._chat = function (message) {
    var _this = this;
    var msg = String(message || '').trim();
    if (!msg) return Promise.resolve();
    if (this._chatBusy) return Promise.resolve();
    this._chatBusy = true;

    // Optimistic: show the question + a typing shimmer the instant they send.
    this._showChat({ user: msg, thinking: true });

    var base = this.cfg.apiBase || '';
    var url = base + '/api/v1/chat';
    if (this.cfg.shop) url += '?shop=' + encodeURIComponent(this.cfg.shop);
    var payload = {
      session_id: this._sid(),
      message: msg,
      message_type: 'text',
      language: this.cfg.language || 'en',
      store_name: this.cfg.storeName || '',
      store_url: (typeof location !== 'undefined' ? location.origin : ''),
      current_page: (typeof location !== 'undefined')
        ? { url: location.href, title: (typeof document !== 'undefined' ? document.title : '') }
        : {}
    };

    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (data) {
        if (!resp.ok) {
          var e = new Error((data && data.detail) || ('Chat failed (' + resp.status + ')'));
          e.status = resp.status;
          throw e;
        }
        return data;
      });
    }).then(function (r) {
      _this._chatBusy = false;
      _this._applyChatResponse(msg, r || {});
    }).catch(function (err) {
      _this._chatBusy = false;
      _this._showChat({
        user: msg,
        aria: 'I had trouble reaching the store just now. Mind trying that again?',
        suggestions: []
      });
      _this.emit('storefailure', { message: msg, error: (err && err.message) || String(err) });
    });
  };

  // Turn a /chat response into overlay UI. A product list drives the concierge
  // shelf (search view) with Aria's line as the answer copy; product-detail /
  // cart / compare / redirect reuse the existing action dispatcher so they work
  // from text too; anything else is a plain conversational reply — so "hi",
  // "what's your return policy", or "no such product" read like a real
  // assistant instead of a jarring "0 results".
  proto._applyChatResponse = function (userMsg, r) {
    var _this = this;
    var text = (r.text || r.response_text || '').toString();
    var actions = r.ui_actions || r.actions || [];
    if (!Array.isArray(actions)) actions = [];
    var suggestions = (r.suggested_replies || r.suggestions || []);
    if (!Array.isArray(suggestions)) suggestions = [];
    suggestions = suggestions.map(function (s) {
      return typeof s === 'string' ? s : (s && (s.title || s.label || s.text || s.reply)) || '';
    }).filter(Boolean);

    var find = function (types) {
      for (var i = 0; i < actions.length; i++) {
        if (actions[i] && types.indexOf(actions[i].type) !== -1) return actions[i];
      }
      return null;
    };
    // Drop the transient "thinking" chat card before navigating to a real view,
    // so Back never lands on a stale typing bubble.
    var dropThinking = function () {
      var top = _this._stack.top();
      if (top && top.view === 'chat') _this._stack.pop();
    };

    // A) Product shelf — render directly so Aria's copy becomes the answer line.
    var listAct = find(['show_products']);
    if (listAct) {
      var payload = listAct.payload || {};
      var prods = (payload.products || []).map(function (p) { return _this._normProduct(p); });
      var q = payload.query || userMsg;
      dropThinking();
      _this.open('search', { query: q, products: prods });
      _this._render('search', { query: q, products: prods, message: text || null, filters: payload.filters || {} });
      return;
    }

    // B) A single product / availability card.
    var detailAct = find(['show_product_detail', 'show_availability']);
    if (detailAct) { dropThinking(); if (text) _this._toast(text); return _this.handle(detailAct); }

    // C) Add-to-cart from chat → forward to the NATIVE cart the customer sees
    //    (the bridge owns /cart/add.js), then confirm in-thread and let them
    //    keep browsing. We never surface the overlay's own cart page.
    var addAct = find(['add_to_cart']);
    if (addAct) {
      var ap = addAct.payload || {};
      _this.emit('addtocart', {
        variant_id: _this._nativeAddId(ap.variation_id || ap.variant_id || ''),
        quantity: ap.quantity || 1,
        product_id: ap.product_id != null ? ap.product_id : null,
        handle: ap.handle || null
      });
      return _this._showChat({
        user: userMsg,
        aria: text || 'Added to your cart. Want to keep looking, or head to checkout?',
        suggestions: suggestions.length ? suggestions : ['Show my cart', 'Keep shopping']
      });
    }

    // D) "Show my cart" / cart changed / discount → the two Shopify-native exits
    //    (View Cart + Checkout). No in-overlay cart or checkout page.
    var cartAct = find(['show_cart', 'cart_updated', 'apply_discount_code']);
    if (cartAct) {
      dropThinking();
      if (text) _this._toast(text);
      var cp = cartAct.payload || {};
      return _this._showCartExits(
        cartAct.type === 'apply_discount_code'
          ? { code: cp.code || cp.discount_code || '' }
          : {}
      );
    }

    // D/E/F) Compare / redirect / explicit search → existing dispatcher.
    var viewAct = find(['compare_products', 'redirect', 'redirect_checkout', 'redirect_checkout_with_address', 'search']);
    if (viewAct) { dropThinking(); if (text) _this._toast(text); return _this.handle(viewAct); }

    // G) Pure conversation — a proper concierge reply, in place.
    _this._showChat({
      user: userMsg,
      aria: text || 'I’m right here. Tell me what you’re after — a product, your cart, or an order.',
      suggestions: suggestions
    });
  };

  // Push (or update in place) the conversational chat view.
  proto._showChat = function (params) {
    var top = this._stack.top();
    if (top && top.view === 'chat') { top.params = params; this._render('chat', params); }
    else { this.pushView('chat', params); }
  };

  proto._renderChat = function (params) {
    var _this = this;
    params = params || {};
    var user = params.user || '';
    var aria = params.aria || '';
    var thinking = !!params.thinking;
    var suggestions = params.suggestions || [];

    var userHtml = user
      ? '<div class="sp-msg sp-msg-user"><div class="sp-bubble">' + escapeHtml(user) + '</div></div>'
      : '';

    var ariaInner = thinking
      ? '<div class="sp-typing"><span></span><span></span><span></span></div>'
      : escapeHtml(aria).replace(/\n/g, '<br>');
    var ariaHtml = (aria || thinking)
      ? '<div class="sp-msg sp-msg-aria">' +
          '<span class="sp-msg-avatar">' + SVG.sparkles + '</span>' +
          '<div class="sp-bubble">' + ariaInner + '</div>' +
        '</div>'
      : '';

    var chips = '';
    if (!thinking && suggestions.length) {
      chips = '<div class="sp-chat-suggest">' + suggestions.slice(0, 4).map(function (s) {
        return '<button class="sp-chat-chip" data-chat-suggest="' + escapeAttr(s) + '">' + escapeHtml(s) + '</button>';
      }).join('') + '</div>';
    }

    this._els.body.innerHTML = '<div class="sp-chat">' + userHtml + ariaHtml + chips + '</div>';

    this._els.body.querySelectorAll('[data-chat-suggest]').forEach(function (btn) {
      btn.addEventListener('click', function () { _this._chat(btn.getAttribute('data-chat-suggest')); });
    });
  };

  proto._loadSearch = function (query, filters) {
    var _this = this;
    if (!query) {
      this._render('search', { query: query, products: [], filters: filters || {} });
      return Promise.resolve();
    }
    this._publish('search_submitted', { search_term: query });
    this._render('search', { query: query, products: this._products, loading: true, filters: filters || {} });
    var qs = 'q=' + encodeURIComponent(query) + '&first=20';
    return this._api('/search?' + qs).then(function (data) {
      var env = unwrapSearch(data);
      _this._products = env.products;
      // Prefer the server's cleaned query ("formal shoes") over the raw spoken
      // text ("formal shoes 5000? under 5000") for the header + answer line, and
      // surface the honest server message (e.g. "no X under N, but here are…").
      _this._render('search', {
        query: env.query || query,
        products: env.products,
        message: env.message,
        match: env.match,
        filters: filters || {}
      });
    }).catch(function (err) {
      _this._toast(err.message || 'Search failed.', true);
      _this.emit('storefailure', { query: query });
    });
  };

  proto._loadProduct = function (handle) {
    var _this = this;
    this._render('pdp', { handle: handle, loading: true });
    return this._api('/product/' + encodeURIComponent(handle)).then(function (product) {
      if (product) _this._pdpCache[handle] = product;
      _this._publish('product_viewed', { product: product });
      _this._render('pdp', { handle: handle, product: product });
    }).catch(function (err) {
      _this._toast(err.message || 'Could not load product.', true);
      _this.emit('storefailure', { handle: handle });
    });
  };

  // Resolve a product by its title against the live catalog, then open its PDP
  // by the exact matched handle. This is the anti-hallucination fallback for
  // show_product_detail when the assistant emits a title but no handle/id: we
  // NEVER open a PDP for an unverified handle. Caller should have opened a
  // loading PDP first (this method fills the resolved handle into that entry).
  proto._resolveProductByTitle = function (title) {
    var _this = this;
    var t = String(title || '').trim();
    if (!t) { _this._toast('Could not open that product.', true); _this._handleBack(); return Promise.resolve(); }
    var qs = 'q=' + encodeURIComponent(t) + '&first=10';
    return _this._api('/search?' + qs).then(function (data) {
      var env = unwrapSearch(data);
      var match = pickTitleMatch(env.products, t);
      if (match && match.handle) {
        var top = _this._stack.top();
        if (top && top.view === 'pdp') top.params.handle = match.handle;
        return _this._loadProduct(match.handle);
      }
      // Honest miss — no fabricated product, back out of the loading PDP.
      _this._toast('I couldn’t find “' + t + '”. Want to search instead?', true);
      _this._handleBack();
    }).catch(function (err) {
      _this._toast(err.message || 'Could not open that product.', true);
      _this._handleBack();
    });
  };

  /* ── Cart state helpers ── */

  proto._ensureCart = function (variantId, quantity) {
    var _this = this;
    // Reuse the SAME session-backed cart across every view (search → PDP → cart →
    // checkout) and across a page navigation — restore it from sessionStorage if
    // the in-memory id was dropped (e.g. a native PDP reload). A single cart token
    // is what lets buyer-identity + delivery address bind to the cart the customer
    // actually checks out with.
    this._restoreCartId();
    if (this._cartId) {
      return this._api('/cart/lines', {
        method: 'POST',
        body: { cart_id: this._cartId, lines: [{ merchandise_id: variantId, quantity: quantity || 1 }] }
      }).then(function (cart) {
        if (cart.errors) throw new Error('discount'); // surfaced inline by caller
        _this._setBadgeCartId(cart.cart_id);
        _this._discountCodes = cart.discount_codes || [];
        return cart;
      });
    }
    return this._api('/cart', {
      method: 'POST',
      body: { lines: [{ merchandise_id: variantId, quantity: quantity || 1 }] }
    }).then(function (cart) {
      if (cart.errors) throw new Error(cart.errors[0].message || 'Could not create cart');
      _this._setBadgeCartId(cart.cart_id);
      _this._discountCodes = cart.discount_codes || [];
      return cart;
    });
  };

  proto._loadCart = function (applyCode) {
    var _this = this;
    this._render('cart', { loading: true, applyCode: applyCode || '' });
    if (!this._cartId) {
      this._render('cart', { cart: null, applyCode: applyCode || '' });
      return Promise.resolve();
    }
    return this._api('/cart/status?cart_id=' + encodeURIComponent(this._cartId)).then(function (cart) {
      _this._badge(cart.total_quantity || 0);
      _this._render('cart', { cart: cart, applyCode: applyCode || '' });
    }).catch(function (err) {
      _this._toast(err.message || 'Could not load cart.', true);
      _this._render('cart', { cart: null, applyCode: applyCode || '' });
    });
  };

  proto._addLine = function (variantId, quantity) {
    var _this = this;
    return this._ensureCart(variantId, quantity).then(function () {
      return _this._loadCart();
    }).then(function () {
      _this._toast('Added to cart.');
      _this._publish('cart_line_added', { variant_id: variantId, quantity: quantity || 1 });
    }).catch(function (err) {
      _this._toast(err.message || 'Could not add to cart.', true);
    });
  };

  proto._badge = function (count) {
    if (this._els && this._els.badge) this._els.badge.textContent = String(count || 0);
  };

  /* ── Native Shopify cart (the ONE cart the customer sees at /cart) ────────
   * The overlay never keeps its own cart page. Adds are forwarded to the
   * widget bridge, which owns the native theme cart (/cart/add.js etc.); the
   * bridge pushes the authoritative count back via nativeCartCount(). "Show
   * cart" opens a two-exit screen (View Cart + Checkout) that hard-navigates
   * to the store's own /cart and /checkout — no in-overlay cart or checkout. */

  // Storefront variant GIDs carry the numeric theme/AJAX variant id as their
  // suffix (gid://shopify/ProductVariant/123 → 123), which is exactly what
  // /cart/add.js needs; a bare numeric id passes through unchanged.
  proto._nativeAddId = function (variantId) {
    var s = String(variantId == null ? '' : variantId);
    var tail = (s.indexOf('/') !== -1 ? s.split('/').pop() : s).split('?')[0];
    var m = tail.match(/\d+/);
    return m ? m[0] : '';
  };

  // Forward an add to the widget bridge (native cart). Optimistic toast now;
  // the bridge confirms with the real count via nativeCartCount().
  proto._nativeAdd = function (variantId, quantity, extra) {
    var id = this._nativeAddId(variantId);
    extra = extra || {};
    if (!id) { this._toast('Choose a variant first.', true); return; }
    this._pendingAdd = true;
    this._toast('Adding to cart…');
    this.emit('addtocart', {
      variant_id: id,
      quantity: Math.max(1, parseInt(quantity, 10) || 1),
      product_id: extra.product_id != null ? extra.product_id : null,
      handle: extra.handle || null,
      permalink: extra.permalink || null
    });
  };

  // Two-exit "show cart" screen. Opens the overlay on the cartexit view and
  // reads the live native cart for a summary; both buttons leave to the store.
  proto._showCartExits = function (params) {
    params = params || {};
    var top = this._stack && this._stack.top && this._stack.top();
    if (this._open && top && top.view === 'cartexit') {
      top.params = params;
      this._render('cartexit', params);
    } else {
      this.open('cartexit', params);
    }
    return this._loadNativeCart();
  };

  // Read the native theme cart same-origin (/cart.js) for the exit-screen
  // summary + header badge. Never throws into the UI.
  proto._loadNativeCart = function () {
    var _this = this;
    if (typeof fetch === 'undefined') { return Promise.resolve(); }
    return fetch('/cart.js', { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r && r.ok ? r.json() : null; })
      .catch(function () { return null; })
      .then(function (cart) {
        _this._nativeCart = cart || null;
        if (cart && cart.item_count != null) _this._badge(cart.item_count);
        var top = _this._stack && _this._stack.top && _this._stack.top();
        if (top && top.view === 'cartexit') _this._render('cartexit', { cart: cart });
      });
  };

  proto._renderCartExits = function (params) {
    var _this = this;
    params = params || {};
    var cart = params.cart || this._nativeCart || null;
    var origin = (typeof location !== 'undefined') ? location.origin : '';
    var count = (cart && cart.item_count != null) ? cart.item_count : null;
    var money = function (cents) {
      var cur = (cart && cart.currency) || (_this.cfg && _this.cfg.currency) || '';
      return (cur ? cur + ' ' : '') + ((Number(cents) || 0) / 100).toFixed(2);
    };
    var summary;
    if (count === 0) {
      summary = '<p class="sp-cartx-empty">Your cart is empty — add something you love and it’ll show up here.</p>';
    } else if (count == null) {
      summary = '<p class="sp-cartx-sub">Your cart is ready on the store.</p>';
    } else {
      var items = (cart.items || []).slice(0, 4).map(function (it) {
        var img = it.image
          ? '<img class="sp-cartx-thumb" src="' + escapeAttr(it.image) + '" alt="" loading="lazy">'
          : '<span class="sp-cartx-thumb sp-cartx-thumb-ph"></span>';
        return '<li class="sp-cartx-item">' + img +
          '<span class="sp-cartx-name">' + escapeHtml(it.product_title || it.title || 'Item') + '</span>' +
          '<span class="sp-cartx-qty">×' + (it.quantity || 1) + '</span></li>';
      }).join('');
      var more = (cart.items && cart.items.length > 4)
        ? '<li class="sp-cartx-more">+' + (cart.items.length - 4) + ' more</li>' : '';
      summary = '<p class="sp-cartx-sub">' + count + (count === 1 ? ' item' : ' items') +
        ' · <strong>' + money(cart.total_price) + '</strong></p>' +
        '<ul class="sp-cartx-items">' + items + more + '</ul>';
    }
    var note = params.code
      ? '<p class="sp-cartx-note">Enter code <strong>' + escapeHtml(params.code) + '</strong> at checkout to apply it.</p>'
      : '<p class="sp-cartx-note">You’ll continue on the store’s own secure pages.</p>';
    this._els.body.innerHTML =
      '<div class="sp-cartx">' +
        '<div class="sp-cartx-orb">' + SVG.sparkles + '</div>' +
        '<h2 class="sp-cartx-title">Your cart</h2>' +
        summary +
        '<div class="sp-cartx-actions">' +
          '<button class="sp-btn sp-cartx-view" data-cartx="cart">View Cart</button>' +
          '<button class="sp-btn sp-cartx-checkout" data-cartx="checkout">Checkout</button>' +
        '</div>' +
        note +
      '</div>';
    var go = function (path) {
      if (!origin) return;
      try { _this.beginCheckoutRedirect({ message: 'Taking you to the store…' }); } catch (e) {}
      try { location.href = origin + path; } catch (e) {}
    };
    var vc = this._els.body.querySelector('[data-cartx="cart"]');
    var co = this._els.body.querySelector('[data-cartx="checkout"]');
    if (vc) vc.addEventListener('click', function () { go('/cart'); });
    if (co) co.addEventListener('click', function () { go('/checkout'); });
  };

  // Pushed IN from the widget bridge after a native add settles: authoritative
  // count → header badge, and confirm the optimistic add.
  proto.nativeCartCount = function (n) {
    this._badge(n || 0);
    if (this._pendingAdd) { this._pendingAdd = false; this._toast('Added to cart ✓'); this._popCart(); }
    var top = this._stack && this._stack.top && this._stack.top();
    if (top && top.view === 'cartexit') this._loadNativeCart();
  };

  proto.nativeCartError = function (msg) {
    this._pendingAdd = false;
    this._toast(msg || 'Could not add to cart.', true);
  };

  // The visible PDP hero image, used as the source of the fly-to-cart clone.
  proto._pdpHeroImg = function () {
    if (!this._els || !this._els.body) return null;
    return this._els.body.querySelector(
      '[data-gallery] img, .sp-gallery-main img, .sp-pdp-media img, .sp-card-media img, img'
    );
  };

  // A short pop on the header cart button — the landing beat of fly-to-cart and
  // a standalone confirmation when motion is reduced.
  proto._popCart = function () {
    var btn = this._els && this._els.cartBtn;
    if (!btn) return;
    btn.classList.add('sp-cart-pop');
    setTimeout(function () { btn.classList.remove('sp-cart-pop'); }, 420);
  };

  // Fly-to-cart: a floating clone of the product image arcs from `sourceEl`
  // into the header cart badge, then the badge pops. Purely cosmetic — the real
  // cart mutation + authoritative badge count are owned by _addLine/_loadCart;
  // this only animates on a confirmed add. Robust across the shadow boundary:
  // the clone lives in the light DOM with fully inline styles and positions via
  // viewport rects, so no shadow-scope or transformed-ancestor surprises.
  proto._flyToCart = function (sourceEl) {
    try {
      if (typeof document === 'undefined' || !document.body) return;
      if (typeof window !== 'undefined' && window.matchMedia &&
          window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
        this._popCart();
        return;
      }
      var cartBtn = this._els && this._els.cartBtn;
      if (!sourceEl || !cartBtn) { this._popCart(); return; }
      var s = sourceEl.getBoundingClientRect();
      var t = cartBtn.getBoundingClientRect();
      if (!s.width || !t.width) { this._popCart(); return; }

      var imgUrl = (sourceEl.tagName === 'IMG' ? sourceEl.getAttribute('src') : '') ||
        (sourceEl.querySelector && sourceEl.querySelector('img') &&
         sourceEl.querySelector('img').getAttribute('src')) || '';
      var size = Math.min(110, Math.max(52, s.width * 0.6));
      var clone = document.createElement('div');
      clone.setAttribute('aria-hidden', 'true');
      clone.style.cssText =
        'position:fixed;z-index:2147483647;pointer-events:none;border-radius:14px;' +
        'background:#fff center/cover no-repeat;box-shadow:0 10px 30px rgba(139,92,255,0.5);' +
        'transition:transform 0.72s cubic-bezier(0.22,0.68,0.3,1),opacity 0.72s ease;' +
        'left:' + (s.left + s.width / 2 - size / 2) + 'px;' +
        'top:' + (s.top + s.height / 2 - size / 2) + 'px;' +
        'width:' + size + 'px;height:' + size + 'px;';
      if (imgUrl) clone.style.backgroundImage = 'url("' + imgUrl.replace(/"/g, '\\"') + '")';
      else clone.style.background = 'var(--sp-brand, #8b5cff)';
      document.body.appendChild(clone);

      var dx = (t.left + t.width / 2) - (s.left + s.width / 2);
      var dy = (t.top + t.height / 2) - (s.top + s.height / 2);
      var _this = this;
      // Paint the start frame first, then kick the transition on the next frame.
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          clone.style.transform = 'translate(' + dx + 'px,' + dy + 'px) scale(0.12)';
          clone.style.opacity = '0.25';
        });
      });
      var done = function () {
        if (clone && clone.parentNode) clone.parentNode.removeChild(clone);
        _this._popCart();
      };
      clone.addEventListener('transitionend', done, { once: true });
      setTimeout(done, 950); // safety net if transitionend never fires
    } catch (e) {}
  };

  proto._setBadgeCartId = function (cartId) {
    this._cartId = cartId || null;
    try {
      if (cartId) sessionStorage.setItem('_speako_cart', cartId);
      else sessionStorage.removeItem('_speako_cart');
    } catch (e) {}
  };

  proto._restoreCartId = function () {
    if (this._cartId) return this._cartId;
    try { this._cartId = sessionStorage.getItem('_speako_cart') || null; } catch (e) {}
    return this._cartId;
  };

  /* ── Rendering ── */

  proto._render = function (view, params) {
    if (!this._els) return;
    // Locked during a real checkout redirect: never repaint the body (which
    // could flash Home / a stale view) over the "Redirecting…" cover.
    if (this._checkoutRedirecting) return;
    // Native PDP lives in a fullscreen light-DOM layer; tear it down whenever
    // the rendered view is anything but a product detail.
    if (view !== 'pdp') this._closeNativePdp();
    var body = this._els.body;
    this._els.back.style.visibility = this._stack.size() > 1 ? 'visible' : 'hidden';
    var titleEl = this._refs && this._refs._root.querySelector('.sp-title');
    if (titleEl) {
      var t = this.cfg.storeName || 'Speako';
      if (view === 'search') t = params.query ? ('Results: ' + params.query) : 'Search';
      else if (view === 'pdp') t = (params.product && params.product.title) || 'Product';
      else if (view === 'cart') t = 'Your cart';
      else if (view === 'compare') t = 'Compare';
      else if (view === 'chat') t = 'Aria';
      else if (view === 'cartexit') t = 'Your cart';
      titleEl.textContent = t;
    }
    if (view === 'home') this._renderHome(params);
    else if (view === 'search') this._renderSearch(params);
    else if (view === 'pdp') this._renderPdp(params);
    else if (view === 'cart') this._renderCart(params);
    else if (view === 'compare') this._renderCompare(params);
    else if (view === 'chat') this._renderChat(params);
    else if (view === 'cartexit') this._renderCartExits(params);
    /* View transition — trigger fade-in on body content. */
    body.classList.remove('sp-view-enter');
    void body.offsetWidth;
    body.classList.add('sp-view-enter');
    this._els.transcript = body.querySelector('[data-transcript]') || null;
  };

  proto._renderHome = function (params) {
    var _this = this;
    var suggestions = [
      { icon: SVG.sparkles, label: 'Help me choose', act: 'assist' },
      { icon: SVG.trending, label: 'Show bestsellers', act: 'search', q: 'best sellers' },
      { icon: SVG.truck, label: 'Track my order', act: 'track' }
    ];
    var chips = suggestions.map(function (s) {
      return '<button class="sp-suggest" data-suggest="' + s.act + '"' +
        (s.q ? ' data-q="' + escapeAttr(s.q) + '"' : '') + '>' +
        s.icon + '<span>' + escapeHtml(s.label) + '</span></button>';
    }).join('');

    this._els.body.innerHTML =
      '<div class="speako-home">' +
        '<button class="sp-orb" data-orb aria-label="Talk to Speako">' + SVG.mic + '</button>' +
        '<h1 class="speako-hello">Hi, how can I help you?</h1>' +
        '<p class="speako-hint">Ask me to find products, compare options, or check out — I\'ll stay right here with you.</p>' +
        '<div class="sp-suggestions">' + chips + '</div>' +
      '</div>';

    var orb = this._els.body.querySelector('[data-orb]');
    if (orb) orb.addEventListener('click', function () { _this._toggleVoice(); });
    if (this._listening) this._setWave(true);

    this._els.body.querySelectorAll('[data-suggest]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var act = btn.getAttribute('data-suggest');
        if (act === 'search') { var q = btn.getAttribute('data-q') || ''; _this.pushView('search', { query: q }); _this._loadSearch(q); }
        else if (act === 'assist') { _this.emit('suggestion', { intent: 'assist' }); _this._toast('Tell me what you’re looking for.'); }
        else if (act === 'track') { _this.emit('suggestion', { intent: 'track_order' }); _this._toast('Ask me to track your order.'); }
      });
    });
    this._badge0Banner();
  };

  proto._renderSearch = function (params) {
    var _this = this;
    var products = params.products || [];
    var query = params.query || '';
    var facets = this._buildFacets(products);
    this._products = products;
    this._facets = facets.map(function (f) { return Object.assign({}, f, { active: false }); });

    var chips = '<div class="sp-facets">';
    facets.forEach(function (f) {
      chips += '<button class="sp-chip" data-facet="' + f.key + '">' + escapeHtml(f.label) + '</button>';
    });
    chips += '</div>';

    var grid;
    var answer;
    if (params.loading) {
      grid = '<div class="sp-spinner"></div>';
      answer = 'Searching' + (query ? ' for “' + escapeHtml(query) + '”' : '') + '…';
    } else if (!products.length) {
      grid = '<div class="sp-empty">No products found' + (query ? ' for "' + escapeHtml(query) + '"' : '') + '.<br><span>Try a different search, or ask me below.</span></div>';
      // Trust the server's honest no-match copy when present (it explains the
      // "none" vs "price_relaxed" reason); never dump unrelated products.
      answer = params.message
        ? escapeHtml(params.message)
        : ('I couldn’t find a match' + (query ? ' for “' + escapeHtml(query) + '”' : '') + '. Want me to try something broader?');
    } else {
      grid = this._gridHtml(products);
      // A relaxed-price / partial match carries an explicit caveat from the
      // server ("no X under N, but here are the X we do have") — show it verbatim
      // instead of the generic "Here are N results" line so we never imply the
      // budget filter succeeded when it didn't.
      answer = params.message
        ? escapeHtml(params.message)
        : ('Here ' + (products.length === 1 ? 'is' : 'are') + ' <strong>' + products.length + '</strong> ' +
          (products.length === 1 ? 'result' : 'results') + (query ? ' for “' + escapeHtml(query) + '”' : '') + '.');
    }

    this._els.body.innerHTML =
      '<div class="sp-answer"><span class="sp-answer-avatar">' + SVG.sparkles + '</span>' +
        '<div>' + answer + '</div></div>' +
      chips +
      '<div class="sp-grid" data-grid>' + grid + '</div>';

    var gridEl = this._els.body.querySelector('[data-grid]');
    var applyFacets = function () {
      var active = _this._facets.filter(function (f) { return f.active; });
      var filtered = products.filter(function (p) {
        return active.every(function (f) { return f.match(p); });
      });
      gridEl.innerHTML = filtered.length ? _this._gridHtml(filtered, products) : '<div class="sp-empty">No matches with current filters.</div>';
    };

    this._els.body.querySelectorAll('[data-facet]').forEach(function (chip) {
      chip.addEventListener('click', function () {
        var key = chip.getAttribute('data-facet');
        var f = _this._facets.find(function (x) { return x.key === key; });
        if (f) f.active = !f.active;
        chip.classList.toggle('active', !!(f && f.active));
        applyFacets();
      });
    });

    this._syncCompareBar();
  };

  proto._buildFacets = function (products) {
    var _this = this;
    var cur = this.cfg.currency || '';
    var out = [];
    var prices = (products || [])
      .map(function (p) { return Number((p.price && p.price.amount) || 0); })
      .filter(function (n) { return n > 0; })
      .sort(function (a, b) { return a - b; });

    // Data-driven price buckets from the observed distribution (tertiles), so
    // the ranges are meaningful for THIS result set and the store's currency.
    if (prices.length >= 3 && prices[prices.length - 1] > prices[0]) {
      var lo = prices[Math.floor(prices.length / 3)];
      var hi = prices[Math.floor(prices.length * 2 / 3)];
      var round = function (n) { return n >= 100 ? Math.round(n / 10) * 10 : Math.round(n); };
      lo = round(lo); hi = round(hi);
      if (hi <= lo) hi = lo + 1;
      out.push({ key: 'price:lo', label: 'Under ' + formatMoney(lo, cur), kind: 'Price',
        match: function (p) { return ((p.price && p.price.amount) || 0) < lo; } });
      out.push({ key: 'price:mid', label: formatMoney(lo, cur) + ' – ' + formatMoney(hi, cur), kind: 'Price',
        match: function (p) { var a = (p.price && p.price.amount) || 0; return a >= lo && a <= hi; } });
      out.push({ key: 'price:hi', label: 'Above ' + formatMoney(hi, cur), kind: 'Price',
        match: function (p) { return ((p.price && p.price.amount) || 0) > hi; } });
    }

    // Availability + on-sale.
    out.push({ key: 'avail', label: 'In stock', kind: 'Availability',
      match: function (p) { return p.available_for_sale !== false; } });
    var anySale = (products || []).some(function (p) { return p.on_sale || savePercent((p.price && p.price.amount) || 0, (p.compare_at_price && p.compare_at_price.amount) || 0) > 0; });
    if (anySale) {
      out.push({ key: 'sale', label: 'On sale', kind: 'Availability',
        match: function (p) { return p.on_sale || savePercent((p.price && p.price.amount) || 0, (p.compare_at_price && p.compare_at_price.amount) || 0) > 0; } });
    }

    // Vendors + tags (from the observed set).
    var vendors = {}, tags = {};
    (products || []).forEach(function (p) {
      if (p.vendor) vendors[p.vendor] = (vendors[p.vendor] || 0) + 1;
      (p.tags || []).forEach(function (t) { tags[t] = (tags[t] || 0) + 1; });
    });
    Object.keys(vendors).sort().forEach(function (v) {
      out.push({ key: 'vendor:' + v, label: String(v), kind: 'Brand', match: function (p) { return p.vendor === v; } });
    });
    Object.keys(tags).sort().slice(0, 12).forEach(function (t) {
      out.push({ key: 'tag:' + t, label: '#' + String(t), kind: 'Tags', match: function (p) { return (p.tags || []).indexOf(t) !== -1; } });
    });
    return out;
  };

  proto._gridHtml = function (products, all) {
    var _this = this;
    var cur = this.cfg.currency || '';
    return (products || []).map(function (p) {
      var priceAmt = (p.price && p.price.amount) || 0;
      var compareAmt = (p.compare_at_price && Number(p.compare_at_price.amount)) || 0;
      var pct = savePercent(priceAmt, compareAmt);
      var tags = Array.isArray(p.tags) ? p.tags : [];
      var isNew = tags.some(function (t) { var s = String(t).toLowerCase().trim(); return s === 'new' || s === 'new in' || s === 'new-in'; });
      var soldOut = p.available_for_sale === false;
      // A single label occupies the top-left slot — sold-out wins over a sale,
      // a sale wins over "new in". All three are read from live product data.
      var badge = soldOut
        ? '<span class="sp-card-tag sold">Sold out</span>'
        : (pct ? '<span class="sp-badge-sale">' + pct + '% OFF</span>'
               : (isNew ? '<span class="sp-card-tag">New in</span>' : ''));
      var handle = p.handle || '';
      var inCompare = _this._compare.indexOf(handle) !== -1;
      var img = p.image
        ? '<img src="' + escapeAttr(p.image) + '" alt="' + escapeAttr(p.title || '') + '" loading="lazy">'
        : '<div class="sp-card-empty">No image</div>';
      var price = formatMoney(priceAmt, cur || p.currency_code);
      var vendor = p.vendor ? '<div class="sp-card-vendor">' + escapeHtml(p.vendor) + '</div>' : '';
      var rating = (p.rating || p.review_count)
        ? '<div class="sp-card-rating">' + _this._starsHtml(p.rating) +
            '<span>' + (p.rating ? Number(p.rating).toFixed(1) : '') +
            ' (' + (Number(p.review_count) || 0) + ')</span></div>'
        : '';
      return '<div class="sp-card" data-handle="' + escapeAttr(handle) + '">' +
        '<div class="sp-card-media">' + img +
          badge +
          '<button class="sp-card-compare-toggle' + (inCompare ? ' active' : '') + '" data-compare-toggle="' + escapeAttr(handle) + '" title="Compare" aria-label="Add to compare">&#8646;</button>' +
        '</div>' +
        '<div class="sp-card-body">' +
          '<div class="sp-card-title">' + escapeHtml(p.title || '') + '</div>' +
          vendor + rating +
          '<div class="sp-card-pricerow">' +
            '<span class="sp-card-price">' + escapeHtml(price) + '</span>' +
            (pct ? '<span class="sp-card-compare">' + escapeHtml(formatMoney(compareAmt, cur || p.currency_code)) + '</span>' : '') +
          '</div>' +
        '</div></div>';
    }).join('');
  };

  // Synchronize a purple glow with the product the assistant is narrating.
  // Voice streams emit either a `highlight_card` (by handle/product) or an
  // `active_product_index` (by position in the current grid); both land here.
  // Only one card glows at a time, and it scrolls into view so the customer
  // always sees what Aria is talking about.
  proto._highlightCard = function (payload) {
    var _this = this;
    var p = payload || {};
    var body = this._els && this._els.body;
    if (!body) return;

    // Clear any previous glow — a single active card at a time.
    var prev = body.querySelectorAll('.sp-card-glowing');
    for (var i = 0; i < prev.length; i++) prev[i].classList.remove('sp-card-glowing');

    var card = null;
    var idx = (p.active_product_index != null) ? p.active_product_index
            : (p.index != null ? p.index : null);
    if (idx != null && !isNaN(Number(idx))) {
      var cards = body.querySelectorAll('.sp-card');
      card = cards[Number(idx)] || null;
    }
    if (!card) {
      var handle = extractHandle(p, '');
      if (handle) card = body.querySelector('[data-handle="' + cssEsc(handle) + '"]');
    }
    if (!card) return;

    card.classList.add('sp-card-glowing');
    try { card.scrollIntoView({ behavior: 'smooth', block: 'center' }); } catch (e) {}

    // Auto-clear after a spoken beat so a stale highlight doesn't outlive the
    // narration; a fresh highlight cancels the pending clear.
    clearTimeout(this._glowTimer);
    this._glowTimer = setTimeout(function () {
      if (card) card.classList.remove('sp-card-glowing');
      _this._glowTimer = null;
    }, 6000);
  };

  proto._renderPdp = function (params) {
    // Rich, high-converting custom PDP is the DEFAULT (gallery, reviews, save %,
    // variant engine, Add-to-Cart + Buy-It-Now, accordions). The theme-embed
    // native page is opt-in only via native_pdp="embed" for merchants who
    // require pixel-parity with their live product template.
    var nativeMode = (this.cfg.nativePdp || this.cfg.native_pdp || '') === 'embed';
    if (nativeMode && params && params.handle) {
      this._renderPdpNative(params);
      return;
    }
    this._renderPdpMini(params);
  };

  // Rich, conversion-focused custom PDP — the default product view.
  proto._renderPdpMini = function (params) {
    var _this = this;
    var product = params.product;
    if (params.loading || !product) {
      this._els.body.innerHTML = params.loading
        ? '<div class="sp-spinner"></div>'
        : '<div class="sp-empty">Product not available.</div>';
      return;
    }
    var cur = this.cfg.currency || product.currency_code || '';
    this._currentVariant = pickVariant(product, null);
    // Remember the product on view so Buy-It-Now can hand its identity to the
    // voice pipeline (the brain resolves the buy-now product from page_context).
    this._currentProduct = product;

    // ── Gallery images (tolerate string[] or {url}[] shapes) ──
    var imgs = [];
    (product.images || []).forEach(function (im) { imgs.push(typeof im === 'string' ? im : (im && im.url)); });
    if (!imgs.length && product.image) imgs = [product.image];
    imgs = imgs.filter(Boolean);

    var galleryMain = imgs.length
      ? imgs.map(function (src, i) {
          return '<img src="' + escapeAttr(src) + '" data-gimg="' + i + '" alt="' + escapeAttr(product.title || '') + '" loading="lazy">';
        }).join('')
      : '<div class="sp-card-empty">No image</div>';
    var dots = imgs.length > 1
      ? '<div class="sp-gallery-dots" data-dots>' + imgs.map(function (_, i) {
          return '<span class="sp-dot' + (i === 0 ? ' active' : '') + '" data-dot="' + i + '"></span>';
        }).join('') + '</div>'
      : '';
    var thumbs = imgs.length > 1
      ? '<div class="sp-gallery-thumbs" data-thumbs>' + imgs.map(function (src, i) {
          return '<img src="' + escapeAttr(src) + '" data-thumb="' + i + '" class="' + (i === 0 ? 'active' : '') + '" alt="">';
        }).join('') + '</div>'
      : '';
    var gallery =
      '<div class="sp-gallery">' +
        '<div class="sp-gallery-main" data-gallery>' + galleryMain + '</div>' +
        dots + thumbs +
      '</div>';

    // ── Rating block ──
    var ratingHtml = '';
    if (product.rating || product.review_count) {
      ratingHtml =
        '<div class="sp-rating">' +
          this._starsHtml(product.rating) +
          (product.rating ? '<span class="sp-rating-score">' + Number(product.rating).toFixed(1) + '</span>' : '') +
          '<span class="sp-rating-count">(' + (Number(product.review_count) || 0) + ' review' + ((Number(product.review_count) || 0) === 1 ? '' : 's') + ')</span>' +
        '</div>';
    }

    // ── Options (color swatches / size pills / dropdown) ──
    // Shopify models a no-variant product as one option {name:"Title",
    // values:["Default Title"]}; drop it so it never leaks into the UI.
    var realOptions = (product.options || []).filter(function (opt) {
      var ov = opt.values || [];
      if (ov.length === 1 && String(ov[0]).trim().toLowerCase() === 'default title') return false;
      return ov.length > 0;
    });
    var optionsHtml = realOptions.map(function (opt) {
      var name = opt.name || '';
      var lname = name.toLowerCase();
      var vals = opt.values || [];
      var isColor = /colou?r/.test(lname);
      var isSize = /size/.test(lname);
      var body;
      if (vals.length > 6 && !isColor) {
        body = '<select class="sp-option-select" data-opt="' + escapeAttr(name) + '">' +
          vals.map(function (v) { return '<option value="' + escapeAttr(v) + '">' + escapeHtml(v) + '</option>'; }).join('') +
          '</select>';
      } else {
        var swClass = 'sp-swatch' + (isColor ? ' sp-swatch-color' : (isSize ? ' sp-swatch-size' : ''));
        body = '<div class="sp-swatches" data-opt="' + escapeAttr(name) + '">' +
          vals.map(function (v) {
            var style = isColor ? ' style="background:' + escapeAttr(cssColor(v)) + '"' : '';
            return '<button class="' + swClass + '" data-val="' + escapeAttr(v) + '"' + style + '>' + escapeHtml(v) + '</button>';
          }).join('') + '</div>';
      }
      return '<div class="sp-option-group">' +
        '<div class="sp-option-label">' + escapeHtml(name) + '<span class="sp-option-value" data-optval="' + escapeAttr(name) + '"></span></div>' +
        body + '</div>';
    }).join('');

    // ── Accordions ──
    var descBody = product.description_html || (product.description ? escapeHtml(product.description) : 'No description available.');
    var specsRows = '';
    realOptions.forEach(function (opt) {
      if (opt.values && opt.values.length) specsRows += '<tr><td>' + escapeHtml(opt.name || '') + '</td><td>' + escapeHtml(opt.values.join(', ')) + '</td></tr>';
    });
    if (product.vendor) specsRows = '<tr><td>Brand</td><td>' + escapeHtml(product.vendor) + '</td></tr>' + specsRows;
    if (this._currentVariant && this._currentVariant.sku) specsRows += '<tr><td>SKU</td><td>' + escapeHtml(this._currentVariant.sku) + '</td></tr>';
    var accordions =
      '<div class="sp-accordions">' +
        this._accordion('Description', '<div class="sp-pdp-desc">' + descBody + '</div>', true) +
        this._accordion('Shipping', 'Free standard shipping on eligible orders. Most orders dispatch within 1-2 business days; delivery estimates are shown at checkout.', false) +
        this._accordion('Returns', 'Easy 30-day returns. Items must be unused and in original packaging. Start a return from your order confirmation email.', false) +
        (specsRows ? this._accordion('Specifications', '<table class="sp-specs">' + specsRows + '</table>', false) : '') +
      '</div>';

    this._els.body.innerHTML =
      '<div class="sp-pdp">' +
        gallery +
        (product.vendor ? '<div class="sp-pdp-vendor">' + escapeHtml(product.vendor) + '</div>' : '') +
        '<div class="sp-pdp-title">' + escapeHtml(product.title || '') + '</div>' +
        ratingHtml +
        '<div class="sp-pdp-pricing" data-pricing></div>' +
        '<div data-stockwrap></div>' +
        optionsHtml +
        '<div class="sp-qty-row">' +
          '<span class="sp-qty-label">Quantity</span>' +
          '<div class="sp-qty-stepper">' +
            '<button class="sp-qty-btn" data-qminus aria-label="Decrease">&#8722;</button>' +
            '<span class="sp-qty-val" data-qty>1</span>' +
            '<button class="sp-qty-btn" data-qplus aria-label="Increase">&#43;</button>' +
          '</div>' +
        '</div>' +
        '<div class="sp-actions">' +
          '<button class="sp-add" id="sp-btn-add-cart" data-add>Add to Cart</button>' +
          '<button class="sp-buy-now" id="sp-btn-buy-now" data-buynow>Buy It Now</button>' +
        '</div>' +
        '<button class="sp-voice-prompt" data-voice-prompt type="button">' +
          '<span class="sp-voice-prompt-orb">' + SVG.mic + '</span>' +
          '<span class="sp-voice-prompt-copy"><strong>"Aria, does this run true to size?"</strong>' +
            '<span>Tap to speak · answers in ~1s</span></span>' +
          '<span class="sp-voice-prompt-wave sp-wave" data-voice-wave>' +
            '<span></span><span></span><span></span><span></span><span></span>' +
          '</span>' +
        '</button>' +
        accordions +
      '</div>' +
      '<div class="sp-zoom" data-zoom><button class="sp-zoom-close" data-zoomclose aria-label="Close">&#10005;</button><img data-zoomimg alt=""></div>';

    var qty = 1;

    // "Ask Aria about this" reuses the existing voice pipeline (same as the
    // greeting orb) so the concierge card is always a real, working control.
    var askAria = _this._els.body.querySelector('[data-ask-aria]');
    if (askAria) askAria.addEventListener('click', function () { _this._toggleVoice(); });

    // Voice prompt card — suggested voice command on PDP.
    var voicePrompt = _this._els.body.querySelector('[data-voice-prompt]');
    if (voicePrompt) voicePrompt.addEventListener('click', function () { _this._toggleVoice(); });

    // ── Pricing + stock repaint for the active variant ──
    var paint = function () {
      var v = _this._currentVariant;
      var price = variantPrice(v);
      var compareAmt = (v && v.compare_at_price && Number(v.compare_at_price.amount)) ||
        (product.compare_at_price && Number(product.compare_at_price.amount)) || 0;
      var pct = savePercent(price, compareAmt);
      var priceEl = _this._els.body.querySelector('[data-pricing]');
      if (priceEl) {
        priceEl.innerHTML =
          '<span class="sp-pdp-price">' + escapeHtml(formatMoney(price, cur)) + '</span>' +
          (pct ? '<span class="sp-pdp-compare">' + escapeHtml(formatMoney(compareAmt, cur)) + '</span>' +
                 '<span class="sp-save-badge">Save ' + pct + '%</span>' : '');
      }
      var oos = v && v.available_for_sale === false;
      var qa = v && typeof v.quantity_available === 'number' ? v.quantity_available : null;
      var stockCls = oos ? 'out' : (qa !== null && qa > 0 && qa <= 5 ? 'low' : 'in');
      var variantName = '';
      if (v && v.selected_options) {
        var colorOpt = v.selected_options.find(function(o) { return /colou?r/i.test(o.name); });
        if (colorOpt) variantName = colorOpt.value;
      }
      var stockTxt = oos ? 'Out of stock' : (stockCls === 'low' ? ('Live stock: only ' + qa + ' left' + (variantName ? ' in ' + variantName : '')) : 'In stock');
      var sw = _this._els.body.querySelector('[data-stockwrap]');
      if (sw) sw.innerHTML = '<span class="sp-stock ' + stockCls + '">' + stockTxt + '</span>';
      var addBtn = _this._els.body.querySelector('[data-add]');
      var buyBtn = _this._els.body.querySelector('[data-buynow]');
      if (addBtn) { addBtn.disabled = !!oos; addBtn.textContent = oos ? 'Out of stock' : ('Add to Cart · ' + formatMoney(price, cur)); }
      if (buyBtn) buyBtn.disabled = !!oos;
    };

    // ── Variant selection engine ──
    var readSelected = function () {
      var selected = [];
      _this._els.body.querySelectorAll('[data-opt]').forEach(function (ctl) {
        var name = ctl.getAttribute('data-opt');
        var value = '';
        if (ctl.tagName === 'SELECT') value = ctl.value;
        else { var act = ctl.querySelector('.sp-swatch.active'); value = act ? act.getAttribute('data-val') : ''; }
        if (value) selected.push({ name: name, value: value });
      });
      return selected;
    };
    var refreshDisabled = function (selected) {
      // For each option, disable values that yield no available variant given
      // the OTHER currently-selected options.
      _this._els.body.querySelectorAll('.sp-swatches[data-opt]').forEach(function (grp) {
        var name = grp.getAttribute('data-opt');
        var others = selected.filter(function (s) { return s.name !== name; });
        grp.querySelectorAll('.sp-swatch').forEach(function (sw) {
          var trial = others.concat([{ name: name, value: sw.getAttribute('data-val') }]);
          var v = pickVariant(product, trial);
          var ok = v && matchesAll(v, trial) && v.available_for_sale !== false;
          sw.classList.toggle('disabled', !ok);
        });
      });
    };
    var matchesAll = function (variant, sel) {
      var opts = variant.selected_options || [];
      return sel.every(function (s) {
        return opts.some(function (o) { return o.name === s.name && o.value === s.value; });
      });
    };
    var updateVariant = function () {
      var selected = readSelected();
      selected.forEach(function (s) {
        var lbl = _this._els.body.querySelector('[data-optval="' + cssEsc(s.name) + '"]');
        if (lbl) lbl.textContent = s.value;
      });
      var variant = pickVariant(product, selected);
      if (variant) _this._currentVariant = variant;
      refreshDisabled(selected);
      paint();
    };

    // Bind option controls.
    this._els.body.querySelectorAll('[data-opt]').forEach(function (ctl) {
      if (ctl.tagName === 'SELECT') {
        ctl.addEventListener('change', updateVariant);
      } else {
        var first = ctl.querySelector('.sp-swatch');
        if (first) first.classList.add('active');
        ctl.querySelectorAll('.sp-swatch').forEach(function (sw) {
          sw.addEventListener('click', function () {
            if (sw.classList.contains('disabled')) return;
            ctl.querySelectorAll('.sp-swatch').forEach(function (x) { x.classList.remove('active'); });
            sw.classList.add('active');
            updateVariant();
          });
        });
      }
    });
    updateVariant();

    // ── Gallery interactions (dots, thumbs, swipe sync, zoom) ──
    this._bindGallery();

    // ── Quantity stepper ──
    var qtyEl = this._els.body.querySelector('[data-qty]');
    this._els.body.querySelector('[data-qminus]').addEventListener('click', function () {
      qty = Math.max(1, qty - 1); qtyEl.textContent = String(qty);
    });
    this._els.body.querySelector('[data-qplus]').addEventListener('click', function () {
      qty += 1; qtyEl.textContent = String(qty);
    });

    // ── Accordions ──
    this._els.body.querySelectorAll('.sp-accordion-head').forEach(function (head) {
      head.addEventListener('click', function () { head.parentNode.classList.toggle('open'); });
    });

    // ── Add to Cart ──
    this._els.body.querySelector('[data-add]').addEventListener('click', function () {
      var v = _this._currentVariant;
      if (!v) { _this._toast('Select a variant first.', true); return; }
      if (v.available_for_sale === false) { _this._toast('This variant is out of stock.', true); return; }
      _this._flyToCart(_this._pdpHeroImg());
      _this._nativeAdd(v.id, qty, {
        product_id: (_this._currentProduct && (_this._currentProduct.id || _this._currentProduct.product_id)) || null,
        handle: (_this._currentProduct && _this._currentProduct.handle) || null
      });
    });

    // ── Buy It Now → deferred single-transition checkout ──
    this._els.body.querySelector('[data-buynow]').addEventListener('click', function () {
      var v = _this._currentVariant;
      if (!v) { _this._toast('Select a variant first.', true); return; }
      if (v.available_for_sale === false) { _this._toast('This variant is out of stock.', true); return; }
      _this._buyNow(v.id, qty);
    });
  };

  // Star row: grey background + gold foreground clipped to the rating width.
  proto._starsHtml = function (rating) {
    var pct = starPercent(rating);
    return '<span class="sp-stars" aria-label="' + (Number(rating) || 0).toFixed(1) + ' out of 5">' +
      '<span class="sp-stars-bg">★★★★★</span>' +
      '<span class="sp-stars-fg" style="width:' + pct + '%">★★★★★</span>' +
    '</span>';
  };

  proto._accordion = function (title, bodyHtml, open) {
    return '<div class="sp-accordion' + (open ? ' open' : '') + '">' +
      '<button class="sp-accordion-head" type="button">' + escapeHtml(title) +
        '<span class="sp-acc-icon">&#43;</span></button>' +
      '<div class="sp-accordion-body"><div class="sp-accordion-body-inner">' + bodyHtml + '</div></div>' +
    '</div>';
  };

  proto._bindGallery = function () {
    var _this = this;
    var main = this._els.body.querySelector('[data-gallery]');
    if (!main) return;
    var setActive = function (i) {
      _this._els.body.querySelectorAll('[data-dot]').forEach(function (d) {
        d.classList.toggle('active', +d.getAttribute('data-dot') === i);
      });
      _this._els.body.querySelectorAll('[data-thumb]').forEach(function (th) {
        th.classList.toggle('active', +th.getAttribute('data-thumb') === i);
      });
    };
    var scrollTo = function (i) {
      var img = main.querySelector('[data-gimg="' + i + '"]');
      if (img) main.scrollTo({ left: img.offsetLeft - main.offsetLeft, behavior: 'smooth' });
      setActive(i);
    };
    this._els.body.querySelectorAll('[data-dot]').forEach(function (d) {
      d.addEventListener('click', function () { scrollTo(+d.getAttribute('data-dot')); });
    });
    this._els.body.querySelectorAll('[data-thumb]').forEach(function (th) {
      th.addEventListener('click', function () { scrollTo(+th.getAttribute('data-thumb')); });
    });
    // Sync dots to manual swipe.
    var syncTimer;
    main.addEventListener('scroll', function () {
      clearTimeout(syncTimer);
      syncTimer = setTimeout(function () {
        var i = Math.round(main.scrollLeft / Math.max(1, main.clientWidth));
        setActive(i);
      }, 80);
    });
    // Zoom lightbox.
    var zoom = this._els.body.querySelector('[data-zoom]');
    var zoomImg = this._els.body.querySelector('[data-zoomimg]');
    main.querySelectorAll('[data-gimg]').forEach(function (img) {
      img.addEventListener('click', function () {
        if (!zoom || !zoomImg) return;
        zoomImg.src = img.getAttribute('src');
        zoom.classList.add('show');
      });
    });
    if (zoom) {
      zoom.addEventListener('click', function (e) {
        if (e.target === zoom || (e.target.closest && e.target.closest('[data-zoomclose]'))) zoom.classList.remove('show');
      });
    }
  };

  // Buy It Now — first ensure the variant is in the persistent cart (cartLinesAdd
  // against the SAME _cartId). Then either hand off to the guided voice journey
  // (phone → saved-address lookup → confirm → address-prefilled checkout) when the
  // widget has wired a 'buynow' listener, or fall back to the express address-less
  // checkout redirect (the single deferred top-level navigation) when it hasn't.
  proto._buyNow = function (variantId, quantity) {
    var _this = this;
    var btn = this._els.body.querySelector('[data-buynow]');
    if (btn) { btn.disabled = true; btn.textContent = 'Preparing checkout…'; }

    // A wired 'buynow' listener means the widget's voice pipeline is available.
    var voiceReady = !!(this._listeners['buynow'] && this._listeners['buynow'].length);

    return this._ensureCart(variantId, quantity).then(function () {
      if (voiceReady) {
        // Guided path: light up the live voice session and let the FSM collect the
        // phone number, look up the saved address, confirm, and drive the prefilled
        // checkout. No forced reload here — the widget owns the final navigation.
        _this._publish('buy_now', { variant_id: variantId, quantity: quantity || 1 });
        try { _this.startVoice(); } catch (e) {}
        _this.emit('buynow', {
          variant_id: variantId,
          quantity: quantity || 1,
          product_id: (_this._currentProduct && (_this._currentProduct.id || _this._currentProduct.product_id)) || null,
          handle: (_this._currentProduct && _this._currentProduct.handle) || null,
          cart_id: _this._cartId || null
        });
        _this._toast('What phone number should I use for shipping updates?');
        if (btn) { btn.disabled = false; btn.textContent = 'Buy It Now'; }
        return;
      }
      // Express fallback (no voice pipeline): jump straight to checkout as before.
      return _this._api('/cart/checkout', {
        method: 'POST',
        body: { cart_id: _this._cartId, discount_codes: _this._discountCodes || [] }
      }).then(function (res) {
        if (res.errors || !res.checkout_url) {
          _this._toast((res.errors && res.errors[0] && res.errors[0].message) || 'Could not start checkout.', true);
          if (btn) { btn.disabled = false; btn.textContent = 'Buy It Now'; }
          return;
        }
        _this._publish('buy_now', { variant_id: variantId, quantity: quantity || 1 });
        // Lock the overlay + hard-nav — never fall back to Home on an express
        // checkout: the customer explicitly asked to buy.
        _this.beginCheckoutRedirect({ checkoutUrl: res.checkout_url });
      });
    }).catch(function (err) {
      _this._toast(err.message || 'Could not start checkout.', true);
      if (btn) { btn.disabled = false; btn.textContent = 'Buy It Now'; }
    });
  };

  /* ── Native product page (theme section embed) ─────────────────────────── */
  // The REAL product page lives at {shop}/products/{handle} (Shopify Liquid +
  // theme). We fetch it same-origin, extract the product section, and render it
  // in a fullscreen light-DOM layer so the theme's own CSS styles it exactly as
  // the live web page — unlike the hand-built mini PDP above.

  proto._renderPdpNative = function (params) {
    var _this = this;
    if (params.loading) { this._els.body.innerHTML = '<div class="sp-spinner"></div>'; return; }
    var handle = params.handle || '';
    if (!handle) { this._renderPdpMini(params); return; }
    this._els.body.innerHTML = '<div class="sp-spinner"></div>';
    if (this._nativeHtmlCache[handle]) {
      var cached = this._nativeHtmlFromString(this._nativeHtmlCache[handle]);
      if (cached) { this._openNativePdp(cached, handle, params.product || this._pdpCache[handle] || null); return; }
    }
    var got = params.product
      ? Promise.resolve(params.product)
      : (this._pdpCache[handle]
          ? Promise.resolve(this._pdpCache[handle])
          : this._api('/product/' + encodeURIComponent(handle)).then(function (p) {
              _this._pdpCache[handle] = p;
              return p;
            }));
    got.then(function (prod) {
      var url = (typeof location !== 'undefined' ? location.origin : '') + '/products/' + encodeURIComponent(handle);
      return fetch(url, { headers: { 'Accept': 'text/html,application/xhtml+xml' } }).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.text();
      }).then(function (html) {
        var section = _this._extractNativeSection(html);
        if (!section) throw new Error('no-product-section');
        _this._nativeHtmlCache[handle] = section.outerHTML;
        _this._openNativePdp(section, handle, prod || null);
      }).catch(function () {
        _this._toast('Could not load the product page.', true);
        _this.emit('storefailure', { handle: handle });
        _this._renderPdpMini(Object.assign({}, params, { product: prod || params.product }));
      });
    }).catch(function () {
      _this._toast('Could not load the product.', true);
      _this.emit('storefailure', { handle: handle });
      _this._renderPdpMini(params);
    });
  };

  proto._extractNativeSection = function (html) {
    if (typeof document === 'undefined') return null;
    try {
      var t = document.createElement('template');
      t.innerHTML = html;
      var root = t.content;
      var section = root.querySelector('[data-section-type="product"], [data-section-type="main-product"], [id^="ProductSection-"]');
      if (!section) {
        var form = root.querySelector('form[action*="/cart/add"]');
        if (form) section = form.closest('[id^="shopify-section-"]') || form.closest('div') || form.parentElement;
      }
      if (!section) section = root.querySelector('main, [role="main"]');
      if (!section) section = root.body || root;
      // Strip global chrome only when we had to fall back to <main>/<body>.
      ['[data-section-type="header"]', 'header', 'footer', '.shopify-section-header'].forEach(function (sel) {
        var n;
        while ((n = section.querySelector(sel))) n.parentNode.removeChild(n);
      });
      // Hydrate lazy images (themes swap data-src/data-srcset via JS that won't
      // run on the injected markup).
      section.querySelectorAll('img').forEach(function (img) {
        if (!img.getAttribute('src') && img.getAttribute('data-src')) img.setAttribute('src', img.getAttribute('data-src'));
        if (!img.getAttribute('srcset') && img.getAttribute('data-srcset')) img.setAttribute('srcset', img.getAttribute('data-srcset'));
      });
      return section;
    } catch (e) {
      return null;
    }
  };

  proto._nativeHtmlFromString = function (html) {
    try {
      var host = document.createElement('div');
      host.innerHTML = html;
      return host.firstElementChild;
    } catch (e) {
      return null;
    }
  };

  proto._closeNativePdp = function () {
    if (!this._nativePdpOpen) return;
    this._nativePdpOpen = false;
    this._nativeVariant = null;
    this._nativeProduct = null;
    var layer = typeof document !== 'undefined' ? document.getElementById('speako-native-pdp') : null;
    if (layer && layer.parentNode) layer.parentNode.removeChild(layer);
  };

  proto._openNativePdp = function (section, handle, product) {
    var _this = this;
    this._closeNativePdp();
    if (typeof document === 'undefined' || !section) return;
    var layer = document.createElement('div');
    layer.id = 'speako-native-pdp';
    var bar = document.createElement('div');
    bar.className = 'spnp-bar';
    var badgeCount = (this._els && this._els.badge) ? this._els.badge.textContent : '0';
    bar.innerHTML =
      '<button data-np="back" aria-label="Back">&#8592;</button>' +
      '<span class="spnp-title">' + escapeHtml((product && product.title) || 'Product') + '</span>' +
      '<button data-np="cart" aria-label="Cart">&#128722;<span class="sp-badge" data-np-badge>' + String(badgeCount) + '</span></button>' +
      '<button data-np="close" aria-label="Close">&#10005;</button>';
    var scroll = document.createElement('div');
    scroll.className = 'spnp-scroll';
    scroll.appendChild(section);
    layer.appendChild(bar);
    layer.appendChild(scroll);
    document.body.appendChild(layer);
    this._nativePdpOpen = true;
    this._nativeVariant = null;
    this._nativeProduct = product || this._pdpCache[handle] || null;

    bar.addEventListener('click', function (e) {
      var b = e.target && e.target.closest ? e.target.closest('[data-np]') : null;
      if (!b) return;
      var act = b.getAttribute('data-np');
      if (act === 'close') _this.close();
      else if (act === 'back') _this._handleBack();
      else if (act === 'cart') { _this.open('cart', {}); _this._loadCart(); }
    });

    // Same-origin links: product/collection/search stay inside the overlay SPA.
    scroll.addEventListener('click', function (e) {
      var a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
      if (!a || typeof URL === 'undefined') return;
      var href = a.getAttribute('href') || '';
      if (!href || /^(javascript:|mailto:|tel:)/i.test(href)) return;
      var u;
      try { u = new URL(href, location.origin); } catch (err) { return; }
      if (u.origin !== location.origin) return;
      e.preventDefault();
      var m = u.pathname.match(/^\/products\/([^/?#]+)/);
      if (m) { _this.handle({ type: 'show_product_detail', payload: { handle: m[1], url: u.pathname } }); return; }
      if (u.pathname.match(/^\/collections\//)) {
        _this.handle({ type: 'redirect', payload: { reason: 'search', query: '' } });
        return;
      }
      if (u.searchParams && u.searchParams.get('q')) {
        var q = u.searchParams.get('q') || '';
        _this.pushView('search', { query: q });
        _this._loadSearch(q);
        return;
      }
      _this._toast('This page is not available in the shopping view.', true);
    });

    // Native Add-to-cart form → route through the overlay cart (badge/discounts
    // stay in sync) instead of a full-page POST to /cart/add.
    scroll.addEventListener('submit', function (e) {
      var f = e.target && e.target.closest ? e.target.closest('form') : null;
      if (!f) return;
      if (!/\/cart\/add/i.test(f.getAttribute('action') || '')) return;
      e.preventDefault();
      var qtyEl = f.querySelector('input[name="quantity"]');
      var qty = parseInt((qtyEl && qtyEl.value) || '1', 10) || 1;
      var v = _this._nativeVariant;
      if (!v) {
        var idIn = f.querySelector('input[name="id"], input[name="variant_id"]');
        if (idIn && idIn.value) v = { id: idIn.value };
      }
      if (!v || !v.id) { _this._toast('Choose a variant first.', true); return; }
      _this._flyToCart(_this._pdpHeroImg());
      // Native theme form: input[name="id"] is already the numeric variant id;
      // the bridge resolves the handle from the URL when needed.
      _this._nativeAdd(v.id, qty, { handle: (handle || null) });
    });

    this._bindNativeOptions(section, handle, product);
  };

  proto._bindNativeOptions = function (section, handle, product) {
    var _this = this;
    var controls = [];
    section.querySelectorAll(
      'select[id*="option" i], select[class*="option" i], select[name^="options["], ' +
      'input[name^="options["], .product-form__input input[type="radio"]'
    ).forEach(function (el) { controls.push(el); });

    var form = section.querySelector('form[action*="/cart/add"]');

    if (!controls.length) {
      if (form && product && product.variants && product.variants.length === 1) {
        var h = form.querySelector('input[name="id"], input[name="variant_id"]');
        if (h) h.value = product.variants[0].id;
        this._nativeVariant = product.variants[0];
      }
      return;
    }

    var readSelected = function () {
      var sel = [];
      controls.forEach(function (el) {
        var name = el.getAttribute('data-option') || el.name.replace(/^options\[/, '').replace(/\]$/, '') || 'Option';
        if (el.tagName === 'SELECT') {
          if (el.value) sel.push({ name: name, value: el.value });
        } else if (el.type === 'radio' && el.checked) {
          sel.push({ name: name, value: el.value });
        } else if (el.type === 'checkbox' && el.checked) {
          sel.push({ name: name, value: el.value });
        }
      });
      return sel;
    };

    var apply = function () {
      var prod = product || _this._nativeProduct;
      if (!prod) return;
      var v = pickVariant(prod, readSelected());
      if (!v) return;
      _this._nativeVariant = v;
      if (form) {
        var idIn = form.querySelector('input[name="id"], input[name="variant_id"]');
        if (idIn) idIn.value = v.id;
      }
      var priceEl = section.querySelector('[data-price], .price, .product-price, [id*="Price-"]');
      if (priceEl) priceEl.textContent = formatMoney(variantPrice(v), _this.cfg.currency || '');
      var btn = section.querySelector('.product-form__submit, [type="submit"]');
      if (btn) btn.disabled = v.available_for_sale === false;
    };

    controls.forEach(function (el) {
      el.addEventListener('change', apply);
      el.addEventListener('click', apply);
    });
    apply();
  };

  /* ── Compare view ─────────────────────────────────────────────────────── */

  // Toggle a product into/out of the compare tray (max 3). Updates the toggle
  // button and the floating compare bar in the search view.
  proto._toggleCompare = function (handle, btnEl) {
    if (!handle) return;
    var i = this._compare.indexOf(handle);
    if (i !== -1) {
      this._compare.splice(i, 1);
      if (btnEl) btnEl.classList.remove('active');
    } else {
      if (this._compare.length >= 3) { this._toast('Compare up to 3 products.', true); return; }
      this._compare.push(handle);
      if (btnEl) btnEl.classList.add('active');
    }
    // Keep any duplicate toggles (grid + hrow) in sync.
    if (this._els && this._els.body) {
      var active = this._compare;
      this._els.body.querySelectorAll('[data-compare-toggle]').forEach(function (b) {
        b.classList.toggle('active', active.indexOf(b.getAttribute('data-compare-toggle')) !== -1);
      });
    }
    this._syncCompareBar();
  };

  proto._syncCompareBar = function () {
    if (!this._els || !this._els.body) return;
    var _this = this;
    var host = this._els.body.querySelector('[data-compare-bar]');
    if (!this._compare.length) { if (host) host.remove(); return; }
    var chips = this._compare.map(function (h) {
      var prod = _this._compareCache[h] || _this._pdpCache[h];
      var label = (prod && prod.title) ? prod.title : h;
      if (label.length > 22) label = label.slice(0, 21) + '…';
      return '<span class="sp-cmp-chip">' + escapeHtml(label) +
        '<button data-cmp-remove="' + escapeAttr(h) + '" aria-label="Remove">&#10005;</button></span>';
    }).join('');
    var html = chips +
      '<button class="sp-cmp-go" data-cmp-go>Compare (' + this._compare.length + ')</button>';
    if (!host) {
      host = document.createElement('div');
      host.className = 'sp-compare-bar';
      host.setAttribute('data-compare-bar', '');
      // Insert just under the facets / search bar.
      this._els.body.insertBefore(host, this._els.body.firstChild);
    }
    host.innerHTML = html;
    host.querySelector('[data-cmp-go]').addEventListener('click', function () {
      if (_this._compare.length < 2) { _this._toast('Pick at least 2 products.', true); return; }
      _this.pushView('compare', { handles: _this._compare.slice() });
      _this._loadCompare(_this._compare.slice());
    });
    host.querySelectorAll('[data-cmp-remove]').forEach(function (b) {
      b.addEventListener('click', function () { _this._toggleCompare(b.getAttribute('data-cmp-remove'), null); });
    });
  };

  proto._loadCompare = function (handles) {
    var _this = this;
    handles = normalizeCompare(handles);
    this._render('compare', { handles: handles, loading: true });
    var jobs = handles.map(function (h) {
      if (_this._compareCache[h]) return Promise.resolve(_this._compareCache[h]);
      if (_this._pdpCache[h]) { _this._compareCache[h] = _this._pdpCache[h]; return Promise.resolve(_this._pdpCache[h]); }
      return _this._api('/product/' + encodeURIComponent(h)).then(function (p) {
        if (p) { _this._compareCache[h] = p; _this._pdpCache[h] = p; }
        return p;
      }).catch(function () { return null; });
    });
    return Promise.all(jobs).then(function (prods) {
      var products = prods.filter(Boolean);
      if (!products.length) { _this._toast('Could not load products to compare.', true); }
      _this._render('compare', { handles: handles, products: products });
    });
  };

  proto._renderCompare = function (params) {
    var _this = this;
    if (params.loading) { this._els.body.innerHTML = '<div class="sp-spinner"></div>'; return; }
    var products = params.products || [];
    if (!products.length) {
      this._els.body.innerHTML = '<div class="sp-empty">Nothing to compare yet. Add 2-3 products from search.</div>';
      return;
    }
    var cur = this.cfg.currency || '';
    // Union of option names across products for aligned attribute rows.
    var optNames = [];
    products.forEach(function (p) {
      (p.options || []).forEach(function (o) { if (o.name && optNames.indexOf(o.name) === -1) optNames.push(o.name); });
    });

    var cols = products.map(function (p) {
      var priceAmt = (p.price && p.price.amount) || 0;
      var compareAmt = (p.compare_at_price && Number(p.compare_at_price.amount)) || 0;
      var pct = savePercent(priceAmt, compareAmt);
      var img = p.image ? '<img src="' + escapeAttr(p.image) + '" alt="' + escapeAttr(p.title || '') + '">' : '<div class="sp-card-empty">No image</div>';
      var attrs = optNames.map(function (name) {
        var opt = (p.options || []).find(function (o) { return o.name === name; });
        var val = opt ? (opt.values || []).join(', ') : '—';
        return '<div class="sp-cmp-attr"><span>' + escapeHtml(name) + '</span><b>' + escapeHtml(val) + '</b></div>';
      }).join('');
      var ratingAttr = '<div class="sp-cmp-attr"><span>Rating</span><b>' +
        (p.rating ? Number(p.rating).toFixed(1) + '★ (' + (Number(p.review_count) || 0) + ')' : '—') + '</b></div>';
      var availAttr = '<div class="sp-cmp-attr"><span>Availability</span><b>' +
        (p.available_for_sale === false ? 'Out of stock' : 'In stock') + '</b></div>';
      var vendorAttr = p.vendor ? '<div class="sp-cmp-attr"><span>Brand</span><b>' + escapeHtml(p.vendor) + '</b></div>' : '';
      return '<div class="sp-compare-col">' + img +
        '<div class="sp-cmp-body">' +
          '<div class="sp-cmp-title">' + escapeHtml(p.title || '') + '</div>' +
          '<div class="sp-cmp-price">' + escapeHtml(formatMoney(priceAmt, cur)) +
            (pct ? '<span class="sp-card-compare">' + escapeHtml(formatMoney(compareAmt, cur)) + '</span>' : '') + '</div>' +
          vendorAttr + ratingAttr + availAttr + attrs +
          '<button class="sp-cmp-view" data-cmp-open="' + escapeAttr(p.handle || '') + '">View product</button>' +
        '</div></div>';
    }).join('');

    this._els.body.innerHTML = '<div class="sp-compare-wrap"><div class="sp-compare">' + cols + '</div></div>';
    this._els.body.querySelectorAll('[data-cmp-open]').forEach(function (b) {
      b.addEventListener('click', function () {
        var h = b.getAttribute('data-cmp-open');
        if (h) { _this.pushView('pdp', { handle: h }); _this._loadProduct(h); }
      });
    });
  };

  proto._renderCart = function (params) {
    var _this = this;
    var cart = params.cart;
    if (params.loading || !cart) {
      if (!cart) {
        this._els.body.innerHTML =
          '<div class="sp-empty">Your cart is empty.</div>' +
          this._discountRow('', params.applyCode || '');
        this._attachDiscount(params.applyCode || '');
        this._attachCheckout(null);
      } else {
        this._els.body.innerHTML = '<div class="sp-spinner"></div>';
      }
      return;
    }
    var lines = cart.lines || [];
    var rows = lines.map(function (line) {
      var img = line.image
        ? '<img src="' + escapeAttr(line.image) + '" alt="' + escapeAttr(line.product_title || line.variant_title || '') + '">'
        : '<img alt="" style="background:#eee">';
      return '<div class="sp-cart-line" data-line="' + escapeAttr(line.id || '') + '">' +
        img +
        '<div class="sp-cl-body">' +
          '<div class="sp-cl-title">' + escapeHtml(line.product_title || line.variant_title || 'Product') + '</div>' +
          '<div class="sp-cl-meta">' + escapeHtml(line.variant_title || '') + '</div>' +
          '<div class="sp-cl-qty">' +
            '<span>Qty <strong>' + String(line.quantity) + '</strong></span>' +
            '<button data-inc="' + escapeAttr(line.id || '') + '" aria-label="Add one">+</button>' +
          '</div>' +
        '</div>' +
        '<div class="sp-cl-price">' + escapeHtml(formatMoney((line.line_total && line.line_total.amount) || 0, _this.cfg.currency || '')) + '</div>' +
        '<button class="sp-cl-remove" data-rm="' + escapeAttr(line.id || '') + '" aria-label="Remove line">&#10005;</button>' +
      '</div>';
    }).join('');

    var summary =
      '<div class="sp-cart-summary">' +
        '<div class="sp-row"><span>Subtotal</span><span>' + escapeHtml(formatMoney(cart.subtotal && cart.subtotal.amount, _this.cfg.currency || '')) + '</span></div>' +
        (cart.discount_codes && cart.discount_codes.length
          ? cart.discount_codes.map(function (d) {
              return '<div class="sp-row"><span>Discount (' + escapeHtml(d.code) + ')</span><span>&#8722;' + escapeHtml(formatMoney((cart.total && cart.total.amount || 0) - (cart.subtotal && cart.subtotal.amount || 0), _this.cfg.currency || '')) + '</span></div>';
            }).join('')
          : '') +
        '<div class="sp-row total"><span>Total</span><span>' + escapeHtml(formatMoney(cart.total && cart.total.amount, _this.cfg.currency || '')) + '</span></div>' +
      '</div>' +
      this._discountRow('', '') +
      '<div class="sp-email-row"><input data-email type="email" placeholder="Email for checkout (optional)" inputmode="email"></div>' +
      '<button class="sp-checkout" data-checkout>Checkout &#8594;</button>';

    this._els.body.innerHTML = rows || '<div class="sp-empty">Your cart is empty.</div>';
    if (rows) {
      var wrap = document.createElement('div');
      wrap.innerHTML = summary;
      while (wrap.firstChild) this._els.body.appendChild(wrap.firstChild);
    }

    this._attachDiscount('');
    this._attachCheckout(cart);
    this._attachLineActions(cart);
  };

  proto._discountRow = function (value, applyCode) {
    return '<div class="sp-discount-row">' +
      '<input data-code placeholder="Discount code" value="' + escapeAttr(applyCode || value || '') + '">' +
      '<button data-apply>Apply</button></div>';
  };

  proto._attachDiscount = function (applyCode) {
    var _this = this;
    var row = this._els.body.querySelector('.sp-discount-row');
    if (!row) return;
    var input = row.querySelector('[data-code]');
    var go = function () {
      var code = (input.value || '').trim();
      if (!code) { _this._toast('Enter a discount code.', true); return; }
      _this._render('cart', { cart: { _loading: true }, applyCode: code });
      _this._api('/cart/discount', {
        method: 'POST',
        body: { cart_id: _this._cartId, codes: [code] }
      }).then(function (res) {
        if (res.errors) {
          _this._toast((res.errors[0] && res.errors[0].message) || 'Code not valid.', true);
          return _this._loadCart();
        }
        _this._toast('Discount applied.');
        _this._loadCart();
      }).catch(function (err) {
        _this._toast(err.message || 'Could not apply code.', true);
      });
    };
    row.querySelector('[data-apply]').addEventListener('click', go);
    input.addEventListener('keydown', function (e) { if (e.key === 'Enter') go(); });
  };

  proto._attachCheckout = function (cart) {
    var _this = this;
    var btn = this._els.body.querySelector('[data-checkout]');
    if (!btn) return;
    var emailInput = this._els.body.querySelector('[data-email]');
    var hasLines = !!(cart && cart.lines && cart.lines.length);
    btn.disabled = !hasLines;
    if (emailInput && hasLines) {
      emailInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') btn.click();
      });
    }
    btn.addEventListener('click', function () {
      if (!hasLines) { _this._toast('Your cart is empty.', true); return; }
      btn.disabled = true;
      btn.textContent = 'Preparing checkout…';
      var body = { cart_id: _this._cartId, discount_codes: _this._discountCodes || [] };
      var email = emailInput ? emailInput.value.trim() : '';
      if (email) body.email = email;
      _this._api('/cart/checkout', { method: 'POST', body: body }).then(function (res) {
        if (res.errors || !res.checkout_url) {
          _this._toast((res.errors && res.errors[0] && res.errors[0].message) || 'Could not start checkout.', true);
          btn.disabled = false;
          btn.textContent = 'Checkout →';
          return;
        }
        // The ONLY top-level transition in the overlay flow — lock + hard-nav so
        // no stray render/back can bounce the customer to Home first.
        _this.beginCheckoutRedirect({ checkoutUrl: res.checkout_url });
      }).catch(function (err) {
        _this._toast(err.message || 'Could not start checkout.', true);
        btn.disabled = false;
        btn.textContent = 'Checkout →';
      });
    });
  };

  proto._attachLineActions = function (cart) {
    var _this = this;
    this._els.body.querySelectorAll('[data-inc]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var lineId = btn.getAttribute('data-inc');
        var line = (cart.lines || []).find(function (l) { return l.id === lineId; });
        if (!line || !line.variant_id) return;
        _this._api('/cart/lines', {
          method: 'POST',
          body: { cart_id: _this._cartId, lines: [{ merchandise_id: line.variant_id, quantity: 1 }] }
        }).then(function () { return _this._loadCart(); }).catch(function (err) {
          _this._toast(err.message || 'Could not update cart.', true);
        });
      });
    });
    this._els.body.querySelectorAll('[data-rm]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var lineId = btn.getAttribute('data-rm');
        var remaining = (cart.lines || []).filter(function (l) { return l.id !== lineId; })
          .map(function (l) { return { merchandise_id: l.variant_id, quantity: l.quantity }; })
          .filter(function (l) { return l.merchandise_id; });
        // Re-create the cart without the removed line (no update/remove mutation needed).
        _this._api('/cart', {
          method: 'POST',
          body: { lines: remaining }
        }).then(function (fresh) {
          _this._setBadgeCartId(fresh.cart_id);
          return _this._loadCart();
        }).catch(function (err) {
          _this._toast(err.message || 'Could not update cart.', true);
        });
      });
    });
  };

  proto._badge0Banner = function () {
    var cart = this._restoreCartId();
    if (cart) { var _this = this; this._api('/cart/status?cart_id=' + encodeURIComponent(cart)).then(function (c) {
      _this._badge(c.total_quantity || 0);
    }).catch(function () {}); }
  };

  proto._toast = function (msg, isError) {
    if (!this._els || !this._els.toast) return;
    var t = this._els.toast;
    t.textContent = msg;
    t.classList.add('show');
    if (isError) t.classList.add('err'); else t.classList.remove('err');
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(function () {
      t.classList.remove('show');
    }, 2600);
  };

  proto._publish = function (event, data) {
    try {
      if (window.Shopify && window.Shopify.analytics && window.Shopify.analytics.publish) {
        window.Shopify.analytics.publish(event, data || {});
      }
    } catch (e) {}
  };

  /* ── Bootstrap ── */

  var instance = new Overlay();

  // Bridge consumed by the widget's eager claim/handle hook in processAction().
  var bridge = {
    open: function (view, params) { instance.open(view, params); },
    close: function () { instance.close(); },
    isOpen: function () { return instance.isOpen(); },
    claim: function (act) { return instance.claim(act); },
    handle: function (act) { return instance.handle(act); },
    beginCheckoutRedirect: function (opts) { instance.beginCheckoutRedirect(opts); return bridge; },
    endCheckoutRedirect: function (message) { instance.endCheckoutRedirect(message); return bridge; },
    isCheckoutRedirecting: function () { return instance.isCheckoutRedirecting(); },
    pushView: function (view, params) { instance.pushView(view, params); },
    startVoice: function () { instance.startVoice(); return bridge; },
    on: function (event, cb) { instance.on(event, cb); return bridge; },
    emit: function (event, data) { instance.emit(event, data); },
    nativeCartCount: function (n) { instance.nativeCartCount(n); return bridge; },
    nativeCartError: function (msg) { instance.nativeCartError(msg); return bridge; },
    setConfig: function (cfg) { instance.setConfig(cfg); return bridge; }
  };

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function escapeAttr(s) { return escapeHtml(s); }

  // Escape a string for safe use inside an [attr="…"] selector.
  function cssEsc(s) { return String(s == null ? '' : s).replace(/["\\]/g, '\\$&'); }

  // Best-effort CSS color for a swatch from an option value. Named CSS colors
  // ("Red", "Navy Blue") pass through; multi-word or unknown values fall back to
  // the brand tint so the swatch never renders invisible/transparent.
  function cssColor(v) {
    var raw = String(v == null ? '' : v).trim().toLowerCase().replace(/\s+/g, '');
    if (/^#([0-9a-f]{3}|[0-9a-f]{6})$/.test(raw)) return raw;
    var known = {
      black: '#111827', white: '#ffffff', grey: '#9ca3af', gray: '#9ca3af',
      silver: '#c0c0c0', red: '#dc2626', maroon: '#7f1d1d', orange: '#f97316',
      yellow: '#facc15', gold: '#d4af37', green: '#16a34a', olive: '#65733a',
      teal: '#14b8a6', blue: '#2563eb', navy: '#1e3a8a', navyblue: '#1e3a8a',
      purple: '#7c3aed', violet: '#8b5cf6', pink: '#ec4899', brown: '#92400e',
      beige: '#e8dcc4', cream: '#f5f0e1', tan: '#d2b48c', khaki: '#c3b091'
    };
    return known[raw] || (typeof CSS !== 'undefined' && CSS.supports && CSS.supports('color', v) ? v : 'var(--sp-brand-lite)');
  }

  // Auto-configure from the widget loader config if present.
  if (typeof window !== 'undefined' && window.wooagent_config) {
    instance.setConfig(window.wooagent_config);
  }

  // Recover the persistent cart token immediately so a page navigation (e.g. a
  // native PDP reload) keeps the SAME cart the customer has been building.
  try { instance._restoreCartId(); } catch (e) {}

  if (typeof window !== 'undefined') {
    window.__SPEAKO_OVERLAY__ = bridge;
    console.log('[Speako Overlay] bootstrapped → window.__SPEAKO_OVERLAY__ set', {
      enabled: instance._enabled(),
      overlayMode: window.wooagent_config && window.wooagent_config.overlay_mode,
      platform: window.wooagent_config && window.wooagent_config.platform
    });
  }

  /* ════════════════════════════ NODE EXPORT ════════════════════════════ */
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      RouterStack: RouterStack,
      claimAction: claimAction,
      isRealCheckout: isRealCheckout,
      isStoreNav: isStoreNav,
      isSameOriginTarget: isSameOriginTarget,
      extractHandle: extractHandle,
      extractSearchQuery: extractSearchQuery,
      unwrapSearch: unwrapSearch,
      pickTitleMatch: pickTitleMatch,
      formatMoney: formatMoney,
      cartSubtotal: cartSubtotal,
      pickVariant: pickVariant,
      variantPrice: variantPrice,
      applyDiscount: applyDiscount,
      savePercent: savePercent,
      starPercent: starPercent,
      normalizeCompare: normalizeCompare,
      STORE_VIEW_ACTIONS: STORE_VIEW_ACTIONS,
      escapeHtml: escapeHtml
    };
  }
})();