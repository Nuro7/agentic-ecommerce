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
    'show_cart',
    'cart_updated',
    'apply_discount_code',
    'highlight_card',
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

  function formatMoney(amount, currency) {
    var num = Number(amount || 0);
    if (isNaN(num)) num = 0;
    if (currency) return String(currency) + num.toFixed(2);
    return num.toFixed(2);
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
    this._pdpCache = {};
    this._host = null;
    this._root = null;
    this._shadow = null;
    this._scrollLockOwner = null;
    this._nativeHtmlCache = {};
    this._nativePdpOpen = false;
    this._nativeVariant = null;
    this._nativeProduct = null;
  }

  var proto = Overlay.prototype;

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
      this._root.style.setProperty('--sp-brand', this.cfg.primary_color);
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
          var handle = extractHandle(p, p.url || '');
          _this.open('pdp', { handle: handle, product: p.product || null, availability: act.type === 'show_availability' });
          if (p.product) { _this._render('pdp', { handle: handle, product: p.product }); return; }
          if (!handle) { _this._toast('Could not open that product.', true); return; }
          return _this._loadProduct(handle);
        }
        case 'show_cart':
        case 'cart_updated':
          _this.open('cart', {});
          return _this._loadCart();
        case 'apply_discount_code':
          _this.open('cart', { applyCode: p.code || p.discount_code || '' });
          return _this._loadCart();
        case 'highlight_card':
          _this._highlightCard(p);
          return;
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
      root.setAttribute('data-theme', 'light');
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
        '<div class="sp-title">' + escapeHtml(this.cfg.storeName || 'Speako') + '</div>' +
        '<button class="sp-btn speako-cart-badge" data-act="cart" aria-label="Cart">' +
          '&#128722;<span class="sp-badge" data-badge>0</span></button>' +
        '<button class="sp-btn" data-act="close" aria-label="Close">&#10005;</button>' +
      '</div>' +
      '<div class="speako-body" data-body></div>' +
      '<div class="sp-toast" data-toast></div>';

    this._els = {
      header: refs._root.querySelector('.speako-header'),
      back: refs._root.querySelector('[data-act="back"]'),
      cartBtn: refs._root.querySelector('[data-act="cart"]'),
      closeBtn: refs._root.querySelector('[data-act="close"]'),
      badge: refs._root.querySelector('[data-badge]'),
      body: refs._root.querySelector('[data-body]'),
      toast: refs._root.querySelector('[data-toast]')
    };

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
      if (_this._stack.size() > 1) {
        _this._stack.pop();
        _this._renderTop();
      } else {
        _this.close();
      }
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && _this._open) {
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
      var card = e.target && e.target.closest ? e.target.closest('[data-handle]') : null;
      if (card && _this._open) {
        var handle = card.getAttribute('data-handle');
        if (handle) {
          _this.pushView('pdp', { handle: handle });
          _this._loadProduct(handle);
        }
      }
    });

    // Ambient voice events from the widget bridge
    this.on('transcript', function (data) {
      var t = (data && (data.text || data.transcript)) || '';
      if (t && _this._els.transcript) _this._els.transcript.textContent = t;
    });
    this.on('status', function (data) { /* reserved */ });
  };

  proto._handleBack = function () {
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

  proto._loadSearch = function (query, filters) {
    var _this = this;
    if (!query) {
      this._render('search', { query: query, products: [], filters: filters || {} });
      return Promise.resolve();
    }
    this._publish('search_submitted', { search_term: query });
    this._render('search', { query: query, products: this._products, loading: true, filters: filters || {} });
    var qs = 'q=' + encodeURIComponent(query) + '&first=20';
    return this._api('/search?' + qs).then(function (products) {
      _this._products = products;
      _this._render('search', { query: query, products: products, filters: filters || {} });
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

  /* ── Cart state helpers ── */

  proto._ensureCart = function (variantId, quantity) {
    var _this = this;
    if (this._cartId) {
      return this._api('/cart/lines', {
        method: 'POST',
        body: { cart_id: this._cartId, lines: [{ merchandise_id: variantId, quantity: quantity || 1 }] }
      }).then(function (cart) {
        if (cart.errors) throw new Error('discount'); // surfaced inline by caller
        _this._cartId = cart.cart_id;
        _this._discountCodes = cart.discount_codes || [];
        return cart;
      });
    }
    return this._api('/cart', {
      method: 'POST',
      body: { lines: [{ merchandise_id: variantId, quantity: quantity || 1 }] }
    }).then(function (cart) {
      if (cart.errors) throw new Error(cart.errors[0].message || 'Could not create cart');
      _this._cartId = cart.cart_id;
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

  proto._setBadgeCartId = function (cartId) {
    this._cartId = cartId;
    try { sessionStorage.setItem('_speako_cart', cartId); } catch (e) {}
  };

  proto._restoreCartId = function () {
    if (this._cartId) return this._cartId;
    try { this._cartId = sessionStorage.getItem('_speako_cart') || null; } catch (e) {}
    return this._cartId;
  };

  /* ── Rendering ── */

  proto._render = function (view, params) {
    if (!this._els) return;
    // Native PDP lives in a fullscreen light-DOM layer; tear it down whenever
    // the rendered view is anything but a product detail.
    if (view !== 'pdp') this._closeNativePdp();
    var body = this._els.body;
    this._els.back.style.visibility = this._stack.size() > 1 ? 'visible' : 'hidden';
    if (view === 'home') this._renderHome(params);
    else if (view === 'search') this._renderSearch(params);
    else if (view === 'pdp') this._renderPdp(params);
    else if (view === 'cart') this._renderCart(params);
    this._els.transcript = body.querySelector('[data-transcript]') || null;
  };

  proto._renderHome = function (params) {
    var _this = this;
    this._els.body.innerHTML =
      '<div class="speako-home">' +
        '<div class="speako-hello">' + escapeHtml(this.cfg.storeName || 'Speako') + '</div>' +
        '<p class="speako-hint">Ask to search, or browse the store here while Aria stays on the line.</p>' +
        '<div class="sp-searchbar">' +
          '<input data-search-input placeholder="Search products…" aria-label="Search products">' +
          '<button data-gone>Search</button>' +
        '</div>' +
        '<div class="speako-ambient"><span class="sp-voice-dot"></span>' +
          '<div class="sp-transcript" data-transcript>Aria is listening…</div></div>' +
      '</div>';
    var input = this._els.body.querySelector('[data-search-input]');
    var cb = function () {
      var q = input.value.trim();
      if (q) { _this.pushView('search', { query: q }); _this._loadSearch(q); }
    };
    input.addEventListener('keydown', function (e) { if (e.key === 'Enter') cb(); });
    this._els.body.querySelector('[data-gone]').addEventListener('click', cb);
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
    if (params.loading) {
      grid = '<div class="sp-spinner"></div>';
    } else if (!products.length) {
      grid = '<div class="sp-empty">No products found' + (query ? ' for "' + escapeHtml(query) + '"' : '') + '.</div>';
    } else {
      grid = this._gridHtml(products);
    }

    this._els.body.innerHTML =
      '<div class="sp-searchbar">' +
        '<input data-search-input value="' + escapeAttr(query) + '" placeholder="Search products…" aria-label="Search products">' +
        '<button data-gone>Search</button>' +
      '</div>' +
      chips +
      '<div class="sp-grid" data-grid>' + grid + '</div>';

    var input = this._els.body.querySelector('[data-search-input]');
    var doSearch = function () {
      var q = input.value.trim();
      if (!q) return;
      if (_this._stack.size() > 1) { _this._stack.pop(); }
      _this._render('search', { query: q, products: _this._products, loading: true });
      _this._loadSearch(q);
    };
    input.addEventListener('keydown', function (e) { if (e.key === 'Enter') doSearch(); });
    this._els.body.querySelector('[data-gone]').addEventListener('click', doSearch);

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
  };

  proto._buildFacets = function (products) {
    var out = [];
    out.push({
      key: 'price1', label: 'Under ₹1,500',
      match: function (p) { return (p.price && p.price.amount || 0) < 1500; }
    });
    out.push({
      key: 'price2', label: '₹1,500 – ₹5,000',
      match: function (p) { var a = (p.price && p.price.amount || 0); return a >= 1500 && a <= 5000; }
    });
    out.push({
      key: 'price3', label: 'Above ₹5,000',
      match: function (p) { return (p.price && p.price.amount || 0) > 5000; }
    });
    var vendors = {};
    var tags = {};
    (products || []).forEach(function (p) {
      if (p.vendor) vendors[p.vendor] = (vendors[p.vendor] || 0) + 1;
      (p.tags || []).forEach(function (t) { tags[t] = (tags[t] || 0) + 1; });
    });
    Object.keys(vendors).sort().forEach(function (v) {
      out.push({ key: 'vendor:' + v, label: String(v), match: function (p) { return p.vendor === v; } });
    });
    out.push({
      key: 'avail', label: 'In stock', match: function (p) { return p.available_for_sale !== false; }
    });
    Object.keys(tags).sort().forEach(function (t) {
      out.push({ key: 'tag:' + t, label: '#' + String(t), match: function (p) { return (p.tags || []).indexOf(t) !== -1; } });
    });
    return out;
  };

  proto._gridHtml = function (products, all) {
    var _this = this;
    return (products || []).map(function (p) {
      var img = p.image
        ? '<img src="' + escapeAttr(p.image) + '" alt="' + escapeAttr(p.title || '') + '" loading="lazy">'
        : '<div class="sp-card-empty">No image</div>';
      var price = formatMoney((p.price && p.price.amount) || 0, _this.cfg.currency || p.currency_code);
      var vendor = (p.vendor && p.title) ? '<div class="sp-card-vendor">' + escapeHtml(p.vendor) + '</div>' : '';
      return '<div class="sp-card" data-handle="' + escapeAttr(p.handle || '') + '">' +
        img +
        '<div class="sp-card-body">' +
          '<div class="sp-card-title">' + escapeHtml(p.title || '') + '</div>' +
          '<div class="sp-card-price">' + escapeHtml(price) + '</div>' +
          vendor +
        '</div></div>';
    }).join('');
  };

  proto._highlightCard = function (payload) {
    var _this = this;
    var p = payload || {};
    var handle = extractHandle(p, '');
    var card = this._els && this._els.body && this._els.body.querySelector('[data-handle="' + handle + '"]');
    if (card) {
      card.classList.add('sp-highlight');
      setTimeout(function () { card.classList.remove('sp-highlight'); }, 4000);
    }
  };

  proto._renderPdp = function (params) {
    // Product detail → embed the theme's REAL product section (fetched from the
    // storefront) so the page matches the live web exactly. Falls back to the
    // in-SPA mini PDP whenever native embedding is disabled or fails.
    var nativeMode = (this.cfg.nativePdp || this.cfg.native_pdp || 'on') !== 'off';
    if (nativeMode && params && params.handle) {
      this._renderPdpNative(params);
      return;
    }
    this._renderPdpMini(params);
  };

  proto._renderPdpMini = function (params) {
    var _this = this;
    var product = params.product;
    if (params.loading || !product) {
      this._els.body.innerHTML = params.loading
        ? '<div class="sp-spinner"></div>'
        : '<div class="sp-empty">Product not available.</div>';
      return;
    }
    this._currentVariant = pickVariant(product, null);
    var images = product.images && product.images.length
      ? product.images
      : (product.image ? [product.image] : []);
    var carousel = '<div class="sp-carousel">' +
      (images.length ? images.map(function (src) {
        return '<img src="' + escapeAttr(src) + '" alt="' + escapeAttr(product.title || '') + '" loading="lazy">';
      }).join('') : '<div class="sp-card-empty">No image</div>') +
      '</div>';

    var stock = this._currentVariant && this._currentVariant.available_for_sale === false
      ? '<span class="sp-stock out">Out of stock</span>'
      : '<span class="sp-stock in">In stock</span>';

    var optionsHtml = '';
    (product.options || []).forEach(function (opt) {
      optionsHtml += '<div class="sp-option-group">' +
        '<div class="sp-option-label">' + escapeHtml(opt.name || '') + '</div>' +
        (opt.values && opt.values.length <= 5
          ? '<div class="sp-swatches" data-opt="' + escapeAttr(opt.name || '') + '">' +
              (opt.values || []).map(function (v) {
                return '<button class="sp-swatch" data-val="' + escapeAttr(v) + '">' + escapeHtml(v) + '</button>';
              }).join('') + '</div>'
          : '<select class="sp-option-select" data-opt="' + escapeAttr(opt.name || '') + '">' +
              (opt.values || []).map(function (v) {
                return '<option value="' + escapeAttr(v) + '">' + escapeHtml(v) + '</option>';
              }).join('') + '</select>') +
        '</div>';
    });

    var price = formatMoney(variantPrice(this._currentVariant), this.cfg.currency || product.currency_code || '');

    this._els.body.innerHTML =
      carousel +
      '<div class="sp-pdp-title">' + escapeHtml(product.title || '') + '</div>' +
      '<div class="sp-pdp-price" data-price>' + escapeHtml(price) + '</div>' +
      stock +
      optionsHtml +
      '<div class="sp-qty-row">' +
        '<button class="sp-qty-btn" data-qminus>&#8722;</button><span class="sp-qty-val" data-qty>1</span>' +
        '<button class="sp-qty-btn" data-qplus>&#43;</button>' +
      '</div>' +
      '<button class="sp-add" data-add>Add to cart</button>' +
      (product.description_html ? '<div class="sp-pdp-desc">' + product.description_html + '</div>' : '');

    var qty = 1;
    var updateVariant = function () {
      var selected = [];
      _this._els.body.querySelectorAll('[data-opt]').forEach(function (ctl) {
        var name = ctl.getAttribute('data-opt');
        var value = ctl.tagName === 'SELECT' ? ctl.value : (ctl.querySelector('.sp-swatch.active') || {}).getAttribute ? ctl.querySelector('.sp-swatch.active').getAttribute('data-val') : '';
        if (value) selected.push({ name: name, value: value });
      });
      var variant = pickVariant(product, selected);
      if (variant) {
        _this._currentVariant = variant;
        _this._els.body.querySelector('[data-price]').textContent = formatMoney(variantPrice(variant), _this.cfg.currency || '');
        var badge = _this._els.body.querySelector('.sp-stock');
        if (badge) {
          badge.className = variant.available_for_sale === false ? 'sp-stock out' : 'sp-stock in';
          badge.textContent = variant.available_for_sale === false ? 'Out of stock' : 'In stock';
        }
        var add = _this._els.body.querySelector('[data-add]');
        if (add) add.disabled = variant.available_for_sale === false;
      }
    };

    this._els.body.querySelectorAll('[data-opt]').forEach(function (ctl) {
      if (ctl.tagName === 'SELECT') {
        ctl.addEventListener('change', updateVariant);
      } else {
        var defaultSwatch = ctl.querySelector('.sp-swatch');
        if (defaultSwatch) defaultSwatch.classList.add('active');
        ctl.querySelectorAll('.sp-swatch').forEach(function (sw) {
          sw.addEventListener('click', function () {
            ctl.querySelectorAll('.sp-swatch').forEach(function (x) { x.classList.remove('active'); });
            sw.classList.add('active');
            updateVariant();
          });
        });
      }
    });

    updateVariant();

    this._els.body.querySelector('[data-qminus]').addEventListener('click', function () {
      qty = Math.max(1, qty - 1);
      _this._els.body.querySelector('[data-qty]').textContent = String(qty);
    });
    this._els.body.querySelector('[data-qplus]').addEventListener('click', function () {
      qty += 1;
      _this._els.body.querySelector('[data-qty]').textContent = String(qty);
    });

    var add = this._els.body.querySelector('[data-add]');
    if (this._currentVariant && this._currentVariant.available_for_sale === false) add.disabled = true;
    add.addEventListener('click', function () {
      var v = _this._currentVariant;
      if (!v) { _this._toast('Select a variant first.', true); return; }
      if (v.available_for_sale === false) { _this._toast('This variant is out of stock.', true); return; }
      _this._addLine(v.id, qty);
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
      _this._addLine(v.id, qty);
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
        if (res.errors) {
          _this._toast((res.errors[0] && res.errors[0].message) || 'Could not start checkout.', true);
          btn.disabled = false;
          btn.textContent = 'Checkout →';
          return;
        }
        // The ONLY top-level transition in the overlay flow.
        try { window.location.href = res.checkout_url; } catch (e) {}
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
    pushView: function (view, params) { instance.pushView(view, params); },
    on: function (event, cb) { instance.on(event, cb); return bridge; },
    emit: function (event, data) { instance.emit(event, data); },
    setConfig: function (cfg) { instance.setConfig(cfg); return bridge; }
  };

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function escapeAttr(s) { return escapeHtml(s); }

  // Auto-configure from the widget loader config if present.
  if (typeof window !== 'undefined' && window.wooagent_config) {
    instance.setConfig(window.wooagent_config);
  }

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
      formatMoney: formatMoney,
      cartSubtotal: cartSubtotal,
      pickVariant: pickVariant,
      variantPrice: variantPrice,
      applyDiscount: applyDiscount,
      STORE_VIEW_ACTIONS: STORE_VIEW_ACTIONS,
      escapeHtml: escapeHtml
    };
  }
})();