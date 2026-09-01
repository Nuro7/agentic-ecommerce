/**
 * Speako Fullscreen Luxury Overlay — Clean, Minimal & High-Performance SPA
 * Supports: Curated Discovery View (Image 2), Product Detail PDP (Image 1),
 * Glassy Voice Interaction Bar, and Native Storefront Cart Integration.
 */
(function (window, document) {
  'use strict';

  // SVG Icons
  var SVG = {
    back: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>',
    close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6L6 18M6 6l12 12"/></svg>',
    cart: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 2L3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z"/><line x1="3" y1="6" x2="21" y2="6"/><path d="M16 10a4 4 0 0 1-8 0"/></svg>',
    verified: '<svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg>',
    mic: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>',
    sparkles: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 0l2.5 8.5L23 11l-8.5 2.5L12 22l-2.5-8.5L1 11l8.5-2.5z"/></svg>'
  };

  function SpeakoOverlay(cfg) {
    this.cfg = Object.assign({
      apiBase: '',
      shop: '',
      storeName: 'Maison Speako',
      currency: '$',
      overlayEnabled: true,
      platform: 'shopify'
    }, cfg || {});

    this.stack = [];
    this.products = [];
    this.currentProduct = null;
    this.currentVariant = null;
    this.isListening = false;
    this.cartCount = 0;
    this.listeners = {};

    this._init();
  }

  var proto = SpeakoOverlay.prototype;

  /* ── Event Emitter ── */
  proto.on = function (evt, fn) { (this.listeners[evt] = this.listeners[evt] || []).push(fn); };
  proto.emit = function (evt, data) { (this.listeners[evt] || []).forEach(function (f) { f(data); }); };

  /* ── Config Updates from Bridge ── */
  proto.setConfig = function (newCfg) {
    this.cfg = Object.assign(this.cfg, newCfg || {});
    if (this.els && this.els.title && this.cfg.storeName) {
      this.els.title.textContent = this.cfg.storeName;
    }
  };

  /* ── Mount Shadow DOM ── */
  proto._init = function () {
    if (document.getElementById('speako-overlay-host')) return;
    var host = document.createElement('div');
    host.id = 'speako-overlay-host';
    document.documentElement.appendChild(host);
    this.shadow = host.attachShadow({ mode: 'open' });

    // Stylesheet injection + fallback dynamic link
    var style = document.createElement('style');
    style.textContent = typeof window.__SPEAKO_OVERLAY_CSS__ === 'string' ? window.__SPEAKO_OVERLAY_CSS__ : '';
    
    var link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = (this.cfg.apiBase || '') + '/static/speako-overlay.css';

    this.root = document.createElement('div');
    this.root.className = 'speako-root';

    this.shadow.appendChild(style);
    this.shadow.appendChild(link);
    this.shadow.appendChild(this.root);

    this._buildScaffold();
    this._bindEvents();
  };

  /* ── Scaffold HTML ── */
  proto._buildScaffold = function () {
    this.root.innerHTML = [
      '<div class="speako-header">',
        '<div class="sp-header-left">',
          '<button class="sp-header-back" data-act="back" style="display:none" aria-label="Back">' + SVG.back + '</button>',
          '<div class="sp-header-avatar" data-avatar></div>',
          '<div class="sp-title-wrap">',
            '<div class="sp-title-row">',
              '<span class="sp-title" data-title>' + this.cfg.storeName + '</span>',
              '<span class="sp-verified-badge">' + SVG.verified + '</span>',
            '</div>',
            '<div class="sp-subtitle" data-subtitle>20 curated results · voice session live</div>',
          '</div>',
        '</div>',
        '<div class="sp-header-right">',
          '<button class="sp-btn speako-cart-badge" data-act="cart" aria-label="Cart">' + SVG.cart + '<span class="sp-badge" data-badge>0</span></button>',
          '<button class="sp-btn" data-act="close" aria-label="Close">' + SVG.close + '</button>',
        '</div>',
      '</div>',
      '<div class="speako-body" data-body></div>',
      '<div class="sp-voicebar">',
        '<span class="sp-voicebar-wave" data-wave><span></span><span></span><span></span><span></span><span></span></span>',
        '<input data-voice-input placeholder="Ask Aria or find your style…" aria-label="Ask Aria">',
        '<span class="sp-voicebar-label">Voice active</span>',
        '<button class="sp-mic-btn" data-act="mic" aria-label="Microphone">' + SVG.mic + '</button>',
      '</div>',
      '<div class="sp-toast" data-toast></div>'
    ].join('');

    this.els = {
      body: this.root.querySelector('[data-body]'),
      title: this.root.querySelector('[data-title]'),
      subtitle: this.root.querySelector('[data-subtitle]'),
      avatar: this.root.querySelector('[data-avatar]'),
      backBtn: this.root.querySelector('[data-act="back"]'),
      badge: this.root.querySelector('[data-badge]'),
      voiceInput: this.root.querySelector('[data-voice-input]'),
      micBtn: this.root.querySelector('[data-act="mic"]'),
      wave: this.root.querySelector('[data-wave]'),
      toast: this.root.querySelector('[data-toast]')
    };
  };

  /* ── Event Delegation ── */
  proto._bindEvents = function () {
    var _this = this;

    this.root.addEventListener('click', function (e) {
      var actEl = e.target.closest('[data-act]');
      if (!actEl) return;
      var act = actEl.getAttribute('data-act');
      if (act === 'close') _this.close();
      if (act === 'back') _this.popView();
      if (act === 'cart') _this.openCartExit();
      if (act === 'mic') _this.toggleVoice();
    });

    this.els.voiceInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        var query = _this.els.voiceInput.value.trim();
        if (query) {
          _this.els.voiceInput.value = '';
          _this.chat(query);
        }
      }
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && _this.root.classList.contains('sp-visible')) {
        _this.close();
      }
    });
  };

  /* ── View Navigation ── */
  proto.open = function (view, params) {
    this.root.classList.add('sp-visible');
    document.body.style.overflow = 'hidden';
    this.pushView(view || 'discovery', params || {});
  };

  proto.close = function () {
    this.root.classList.remove('sp-visible');
    document.body.style.overflow = '';
    this.stack = [];
  };

  proto.pushView = function (view, params) {
    this.stack.push({ view: view, params: params || {} });
    this.renderTop();
  };

  proto.popView = function () {
    if (this.stack.length > 1) {
      this.stack.pop();
      this.renderTop();
    } else {
      this.close();
    }
  };

  proto.renderTop = function () {
    var top = this.stack[this.stack.length - 1];
    if (!top) return;

    var isPdp = top.view === 'pdp';
    this.els.backBtn.style.display = this.stack.length > 1 ? 'inline-flex' : 'none';
    this.els.avatar.style.display = isPdp ? 'none' : 'block';

    if (top.view === 'discovery' || top.view === 'search' || top.view === 'home') {
      this.renderDiscovery(top.params);
    } else if (isPdp) {
      this.renderPDP(top.params);
    } else if (top.view === 'cartexit' || top.view === 'cart') {
      this.renderCartExit(top.params);
    }
  };

  /* ── 1. Curated Discovery View (Image 2) ── */
  proto.renderDiscovery = function (params) {
    var _this = this;
    params = params || {};
    this.els.title.textContent = 'Resort discovery with Aria';
    this.els.subtitle.textContent = (this.products.length || 20) + ' curated results · voice session live';

    var chips = [
      { label: 'Under $100', active: true },
      { label: '$100 – $250' },
      { label: 'In stock' },
      { label: 'New in' },
      { label: "Resort '26" },
      { label: 'Silk' }
    ];

    var chipsHtml = '<div class="sp-facets">' + chips.map(function (c) {
      return '<button class="sp-chip' + (c.active ? ' active' : '') + '">' + c.label + '</button>';
    }).join('') + '</div>';

    var heroHtml = '<div class="sp-search-hero"><h1 class="sp-hero-title">Here\'s the resort edit I\'d wear for golden-hour dinners — all under $250.</h1></div>';

    var items = (params.products && params.products.length) ? params.products : (this.products.length ? this.products : [
      { handle: 'lumiere-silk-midi-dress', title: 'Lumière Silk Midi Dress', price: 79.95, compare: 129.00, save: 38, image: 'https://images.unsplash.com/photo-1595777457583-95e059d581b8?w=800' },
      { handle: 'riviera-woven-tote', title: 'Riviera Woven Tote', price: 65.00, compare: 89.00, save: 27, image: 'https://images.unsplash.com/photo-1584917865442-de89df76afd3?w=800' },
      { handle: 'atelier-leather-slide', title: 'Atelier Leather Slide', price: 110.00, image: 'https://images.unsplash.com/photo-1543163521-1bf539c55dd2?w=800' },
      { handle: 'solene-tortoise-sunglasses', title: 'Solene Tortoise Sunglasses', price: 48.00, compare: 72.00, save: 33, waitlist: true, image: 'https://images.unsplash.com/photo-1511499767150-a48a237f0083?w=800' }
    ]);

    var gridHtml = '<div class="sp-grid">' + items.map(function (p) {
      var priceNum = typeof p.price === 'object' ? Number((p.price && p.price.amount) || 0) : Number(p.price || 0);
      var compNum = typeof p.compare === 'object' ? Number((p.compare && p.compare.amount) || 0) : Number(p.compare || p.compare_at_price || 0);
      var savePct = p.save || (compNum > priceNum ? Math.round((compNum - priceNum) / compNum * 100) : 0);

      var badges = (savePct ? '<span class="sp-badge-sale">SAVE ' + savePct + '%</span>' : '') +
                   (p.waitlist ? '<span class="sp-badge-waitlist">WAITLIST</span>' : '');
      var imgUrl = p.image || (p.images && p.images[0] && (p.images[0].src || p.images[0])) || 'https://images.unsplash.com/photo-1595777457583-95e059d581b8?w=800';

      return [
        '<div class="sp-card" data-handle="' + (p.handle || '') + '">',
          '<div class="sp-card-media"><img src="' + imgUrl + '" alt="' + p.title + '">' + badges + '</div>',
          '<div class="sp-card-body">',
            '<div class="sp-card-vendor">' + (p.vendor || p.brand || 'MAISON SPEAKO') + '</div>',
            '<div class="sp-card-title">' + p.title + '</div>',
            '<div class="sp-card-bottom">',
              '<div class="sp-card-pricerow">',
                '<span class="sp-card-price">$' + priceNum.toFixed(2) + '</span>',
                (compNum > priceNum ? '<span class="sp-card-compare">$' + compNum.toFixed(2) + '</span>' : ''),
              '</div>',
              '<button class="sp-card-action-btn" data-open-pdp="' + (p.handle || '') + '">&#43;</button>',
            '</div>',
          '</div>',
        '</div>'
      ].join('');
    }).join('') + '</div>';

    this.els.body.innerHTML = heroHtml + chipsHtml + gridHtml;

    this.els.body.querySelectorAll('[data-handle]').forEach(function (card) {
      card.addEventListener('click', function () {
        var h = card.getAttribute('data-handle');
        var prod = items.find(function (x) { return x.handle === h; }) || items[0];
        _this.pushView('pdp', { product: prod });
      });
    });
  };

  /* ── 2. Product Detail PDP View (Image 1) ── */
  proto.renderPDP = function (params) {
    var _this = this;
    params = params || {};
    var p = params.product || {
      title: 'Lumière Silk Midi Dress',
      price: 79.95,
      compare: 129.00,
      image: 'https://images.unsplash.com/photo-1595777457583-95e059d581b8?w=800'
    };

    var priceNum = typeof p.price === 'object' ? Number((p.price && p.price.amount) || 0) : Number(p.price || 79.95);
    var compNum = typeof p.compare === 'object' ? Number((p.compare && p.compare.amount) || 0) : Number(p.compare || p.compare_at_price || 129.00);
    var imgUrl = p.image || (p.images && p.images[0] && (p.images[0].src || p.images[0])) || 'https://images.unsplash.com/photo-1595777457583-95e059d581b8?w=800';

    this.els.title.textContent = p.title;
    this.els.subtitle.textContent = p.vendor || 'Maison Speako';

    this.els.body.innerHTML = [
      '<div class="sp-pdp-container">',
        '<div class="sp-pdp-grid">',
          '<div class="sp-pdp-media-hero"><img src="' + imgUrl + '" alt="' + p.title + '"></div>',
          '<div class="sp-pdp-info-col">',
            '<h1 class="sp-pdp-title-main">' + p.title + '</h1>',
            '<div class="sp-pdp-price-row">',
              '<span class="sp-pdp-price-current">$' + priceNum.toFixed(2) + '</span>',
              (compNum > priceNum ? '<span class="sp-pdp-price-original">$' + compNum.toFixed(2) + '</span>' : ''),
              (compNum > priceNum ? '<span class="sp-pdp-save-badge">Save $' + (compNum - priceNum).toFixed(2) + '</span>' : ''),
            '</div>',
            '<p class="sp-pdp-desc-text">Bias-cut mulberry silk with a fluid drape and hand-finished seams. Designed for golden-hour dinners and slow coastal evenings.</p>',
            
            '<div class="sp-pdp-section-label">COLOURWAY</div>',
            '<div class="sp-pdp-options-row">',
              '<button class="sp-pdp-pill active">Champagne</button>',
              '<button class="sp-pdp-pill">Ivory</button>',
              '<button class="sp-pdp-pill">Onyx</button>',
            '</div>',

            '<div class="sp-pdp-section-label">SIZE</div>',
            '<div class="sp-pdp-options-row">',
              '<button class="sp-pdp-pill size-pill">XS</button>',
              '<button class="sp-pdp-pill size-pill active">S</button>',
              '<button class="sp-pdp-pill size-pill">M</button>',
              '<button class="sp-pdp-pill size-pill">L</button>',
            '</div>',

            '<div class="sp-pdp-stock-alert"><span class="sp-pdp-stock-dot"></span><span>Live stock: only 4 left in Champagne</span></div>',

            '<div class="sp-pdp-actions-row">',
              '<button class="sp-btn-pdp-add" data-add-btn>Add to Cart · $' + priceNum.toFixed(2) + '</button>',
              '<button class="sp-btn-pdp-buy" data-buy-btn>Buy it now</button>',
            '</div>',

            '<div class="sp-voice-prompt-card" data-voice-prompt role="button" tabindex="0">',
              '<div class="sp-prompt-mic-orb">' + SVG.mic + '</div>',
              '<div class="sp-prompt-copy">',
                '<div class="sp-prompt-title">"Aria, does this run true to size?"</div>',
                '<div class="sp-prompt-sub">Tap to speak · answers in ~1s</div>',
              '</div>',
              '<div class="sp-voicebar-wave active"><span></span><span></span><span></span><span></span><span></span></div>',
            '</div>',
          '</div>',
        '</div>',
      '</div>'
    ].join('');

    this.els.body.querySelectorAll('.sp-pdp-pill').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var group = btn.closest('.sp-pdp-options-row');
        group.querySelectorAll('.sp-pdp-pill').forEach(function (b) { b.classList.remove('active'); });
        btn.classList.add('active');
      });
    });

    this.els.body.querySelector('[data-add-btn]').addEventListener('click', function () {
      _this.cartCount++;
      _this.els.badge.textContent = _this.cartCount;
      _this.toast('Added to cart ✓');
      _this.emit('addtocart', { product: p, quantity: 1 });
    });

    this.els.body.querySelector('[data-buy-btn]').addEventListener('click', function () {
      location.href = '/checkout';
    });

    this.els.body.querySelector('[data-voice-prompt]').addEventListener('click', function () {
      _this.toggleVoice();
    });
  };

  /* ── 3. Cart Exit Screen ── */
  proto.renderCartExit = function () {
    this.els.title.textContent = 'Your Cart';
    this.els.subtitle.textContent = 'Store checkout';
    this.els.body.innerHTML = [
      '<div class="sp-cartx">',
        '<div class="sp-cartx-orb">' + SVG.sparkles + '</div>',
        '<h2 class="sp-cartx-title">Your Bag (' + this.cartCount + ')</h2>',
        '<p class="sp-cartx-sub">Your items are synced to your store session.</p>',
        '<div class="sp-cartx-actions">',
          '<button class="sp-btn sp-cartx-view" onclick="location.href=\'/cart\'">View Cart</button>',
          '<button class="sp-btn sp-cartx-checkout" onclick="location.href=\'/checkout\'">Checkout</button>',
        '</div>',
      '</div>'
    ].join('');
  };

  /* ── Voice & Chat Integration ── */
  proto.toggleVoice = function () {
    this.isListening = !this.isListening;
    this.els.wave.classList.toggle('active', this.isListening);
    this.els.micBtn.classList.toggle('listening', this.isListening);
    this.emit(this.isListening ? 'voicestart' : 'voicestop', {});
  };

  proto.chat = function (message) {
    var _this = this;
    this.toast('Aria is searching…');
    fetch((this.cfg.apiBase || '') + '/api/v1/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: message, session_id: 'wa_' + Date.now() })
    }).then(function (r) { return r.json(); }).then(function (res) {
      if (res.products && res.products.length) {
        _this.products = res.products;
        _this.pushView('discovery', { products: res.products });
      } else if (res.text || res.response_text) {
        _this.toast(res.text || res.response_text);
      }
    }).catch(function () {
      _this.toast('Could not reach Aria right now.');
    });
  };

  proto.toast = function (msg) {
    var t = this.els.toast;
    t.textContent = msg;
    t.classList.add('show');
    setTimeout(function () { t.classList.remove('show'); }, 2800);
  };

  // Expose global instance & bridge
  window.SpeakoOverlay = SpeakoOverlay;
  window.__SPEAKO_OVERLAY__ = new SpeakoOverlay(window.SpeakoOverlayConfig || {});

})(window, document);
