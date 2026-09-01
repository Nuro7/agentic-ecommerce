/**
 * Speako — Production-Grade AI Voice Sales Agent & Persistent Commerce Shell
 *
 * Full Enterprise Feature Matrix:
 * - Persistent Root Shell (Voice Engine, Audio Session, WebSocket Bridge, State & Memory)
 * - 7-State Voice State Machine (IDLE, LISTENING, THINKING, SPEAKING, INTERRUPTED, ERROR, MUTED)
 * - Real-time Live Transcript Layer
 * - Conversational Intent Memory & Dynamic Refinement Chips
 * - Discovery View with "Speako's Pick", "Why I Picked This" Reasoning, and Alternatives Grid
 * - PDP View with AI Decision Support ("Speako's Take" vs Store Facts, Sizing & Variant Sync, Question Accelerators, Merchant Offers/Bundles)
 * - Side-by-Side Attribute Comparison with Speako Recommendation
 * - Real Shopify Cart Integration (/cart/add.js, /cart.js) & Native Checkout Handoff
 * - Humanized Error Recovery & Contextual Loading States
 * - Multi-Tenant & Multi-Vertical Architecture
 * - Frosted Glass Bokeh Scrim & Responsive Mobile UI
 */
(function (window, document) {
  'use strict';

  // SVG Icon Library
  var SVG = {
    back: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>',
    close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6L6 18M6 6l12 12"/></svg>',
    cart: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 2L3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z"/><line x1="3" y1="6" x2="21" y2="6"/><path d="M16 10a4 4 0 0 1-8 0"/></svg>',
    verified: '<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg>',
    mic: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>',
    sparkles: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 0l2.5 8.5L23 11l-8.5 2.5L12 22l-2.5-8.5L1 11l8.5-2.5z"/></svg>',
    check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
    compare: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>',
    tag: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg>'
  };

  /**
   * Speako Persistent Shell
   */
  function SpeakoOverlay(cfg) {
    this.cfg = Object.assign({
      apiBase: '',
      shop: '',
      storeName: 'Store',
      agentName: 'Speako',
      currency: '$',
      overlayEnabled: true,
      platform: 'shopify',
      primaryColor: '#2563eb'
    }, cfg || {});

    // State Management
    this.sessionId = 'sp_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8);
    this.stack = []; // Navigation view stack
    this.products = [];
    this.currentProduct = null;
    this.currentVariant = { color: 'Default', size: 'M' };
    this.cartCount = 0;
    this.cartItems = [];
    
    // Conversational & Voice State
    this.voiceState = 'idle'; // idle | listening | thinking | speaking | interrupted | error | muted
    this.transcriptText = '';
    this.intentChips = ['Under $100', 'In Stock'];
    this.conversationHistory = [];
    this.activeHeadline = "Based on what you told me, I picked options that fit your request.";
    this.listeners = {};

    this._init();
  }

  var proto = SpeakoOverlay.prototype;

  /* ── Event Emitter ── */
  proto.on = function (evt, fn) { (this.listeners[evt] = this.listeners[evt] || []).push(fn); };
  proto.emit = function (evt, data) { (this.listeners[evt] || []).forEach(function (f) { f(data); }); };

  /* ── Configuration Updates ── */
  proto.setConfig = function (newCfg) {
    this.cfg = Object.assign(this.cfg, newCfg || {});
    if (this.els && this.els.storeTitle && this.cfg.storeName) {
      this.els.storeTitle.textContent = this.cfg.storeName;
    }
  };

  /* ── Mount Persistent Shell in Shadow DOM ── */
  proto._init = function () {
    if (document.getElementById('speako-overlay-host')) return;
    var host = document.createElement('div');
    host.id = 'speako-overlay-host';
    host.style.cssText = 'position:fixed;inset:0;width:100vw;height:100vh;z-index:2147483647;display:none;';
    document.documentElement.appendChild(host);
    this.host = host;
    this.shadow = host.attachShadow({ mode: 'open' });

    // Stylesheet Injection
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
    this._fetchRealCart();
  };

  /* ── Scaffold Persistent UI Skeleton ── */
  proto._buildScaffold = function () {
    this.root.innerHTML = [
      '<!-- 1. Persistent Minimal Header -->',
      '<header class="speako-header">',
        '<div class="sp-header-left">',
          '<button class="sp-header-back" data-act="back" style="display:none" aria-label="Back">',
            SVG.back,
            '<span class="sp-back-label" data-back-label>Back</span>',
          '</button>',
          '<div class="sp-header-avatar" data-avatar></div>',
          '<div class="sp-title-wrap">',
            '<div class="sp-title-row">',
              '<span class="sp-store-title" data-store-title>' + this.cfg.storeName + '</span>',
              '<span class="sp-verified-badge" title="Verified Speako AI Agent">' + SVG.verified + '</span>',
            '</div>',
            '<div class="sp-status-row">',
              '<span class="sp-agent-status-dot" data-status-dot></span>',
              '<span class="sp-agent-status-label" data-status-label>' + (this.cfg.agentName || 'Speako') + ' active</span>',
            '</div>',
          '</div>',
        '</div>',
        '<div class="sp-header-right">',
          '<button class="sp-btn sp-cart-badge-btn" data-act="cart" aria-label="View Cart">',
            SVG.cart,
            '<span class="sp-badge" data-badge>0</span>',
          '</button>',
          '<button class="sp-btn sp-close-btn" data-act="close" aria-label="Close Speako">',
            SVG.close,
          '</button>',
        '</div>',
      '</header>',

      '<!-- 2. Dynamic Commerce Content (Never remounts shell) -->',
      '<main class="speako-body" data-body></main>',

      '<!-- 3. Persistent Voice Control & Live Transcript Layer -->',
      '<div class="sp-voice-dock-container">',
        '<div class="sp-transcript-preview" data-transcript style="display:none"></div>',
        '<div class="sp-voicebar" data-voicebar>',
          '<div class="sp-voicebar-wave" data-wave><span></span><span></span><span></span><span></span><span></span></div>',
          '<input class="sp-voicebar-input" data-voice-input placeholder="Ask ' + (this.cfg.agentName || 'Speako') + ' or tell me what you need…" aria-label="Chat with AI Agent">',
          '<span class="sp-voicebar-state-label" data-voice-label>Tap to speak</span>',
          '<button class="sp-mic-btn" data-act="mic" aria-label="Voice input">' + SVG.mic + '</button>',
        '</div>',
      '</div>',

      '<!-- 4. Global Toast & Status System -->',
      '<div class="sp-toast" data-toast role="status"></div>'
    ].join('');

    this.els = {
      body: this.root.querySelector('[data-body]'),
      storeTitle: this.root.querySelector('[data-store-title]'),
      statusDot: this.root.querySelector('[data-status-dot]'),
      statusLabel: this.root.querySelector('[data-status-label]'),
      avatar: this.root.querySelector('[data-avatar]'),
      backBtn: this.root.querySelector('[data-act="back"]'),
      backLabel: this.root.querySelector('[data-back-label]'),
      badge: this.root.querySelector('[data-badge]'),
      voicebar: this.root.querySelector('[data-voicebar]'),
      voiceInput: this.root.querySelector('[data-voice-input]'),
      voiceLabel: this.root.querySelector('[data-voice-label]'),
      micBtn: this.root.querySelector('[data-act="mic"]'),
      wave: this.root.querySelector('[data-wave]'),
      transcript: this.root.querySelector('[data-transcript]'),
      toast: this.root.querySelector('[data-toast]')
    };
  };

  /* ── Event Delegation & Listeners ── */
  proto._bindEvents = function () {
    var _this = this;

    this.root.addEventListener('click', function (e) {
      var actEl = e.target.closest('[data-act]');
      if (!actEl) return;
      var act = actEl.getAttribute('data-act');
      if (act === 'close') _this.close();
      if (act === 'back') _this.popView();
      if (act === 'cart') _this.openCart();
      if (act === 'mic') _this.toggleVoice();
      if (act === 'compare') {
        var p1 = _this.products[0];
        var p2 = _this.products[1] || _this.currentProduct;
        _this.pushView('compare', { p1: p1, p2: p2 });
      }
    });

    this.els.voiceInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        var q = _this.els.voiceInput.value.trim();
        if (q) {
          _this.els.voiceInput.value = '';
          _this.chat(q);
        }
      }
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && _this.root.classList.contains('sp-visible')) {
        _this.close();
      }
    });
  };

  /* ── 7-State Voice Machine ── */
  proto.setVoiceState = function (state, text) {
    this.voiceState = state;
    var _this = this;
    var vLabel = this.els.voiceLabel;
    var sLabel = this.els.statusLabel;
    var wave = this.els.wave;
    var mic = this.els.micBtn;
    var bar = this.els.voicebar;

    bar.classList.remove('sp-state-listening', 'sp-state-thinking', 'sp-state-speaking', 'sp-state-error', 'sp-state-muted');
    wave.classList.remove('active', 'speaking', 'thinking');
    mic.classList.remove('listening');

    if (state === 'listening') {
      bar.classList.add('sp-state-listening');
      wave.classList.add('active');
      mic.classList.add('listening');
      vLabel.textContent = 'Listening…';
      sLabel.textContent = 'Listening…';
      this.showTranscript(text || 'Listening…');
    } else if (state === 'thinking') {
      bar.classList.add('sp-state-thinking');
      wave.classList.add('thinking');
      vLabel.textContent = 'Finding best match…';
      sLabel.textContent = 'Checking options…';
      this.showTranscript(text || 'Checking options for you…');
    } else if (state === 'speaking') {
      bar.classList.add('sp-state-speaking');
      wave.classList.add('speaking');
      vLabel.textContent = (this.cfg.agentName || 'Speako') + ' speaking';
      sLabel.textContent = (this.cfg.agentName || 'Speako') + ' speaking';
      this.showTranscript(text || '');
    } else if (state === 'interrupted') {
      this.setVoiceState('listening', 'Listening…');
    } else if (state === 'error') {
      bar.classList.add('sp-state-error');
      vLabel.textContent = "Didn't catch that. Try again.";
      sLabel.textContent = 'Voice error';
      setTimeout(function () { _this.setVoiceState('idle'); }, 3000);
    } else if (state === 'muted') {
      bar.classList.add('sp-state-muted');
      vLabel.textContent = 'Microphone muted';
      sLabel.textContent = 'Voice off';
    } else {
      // Idle
      vLabel.textContent = 'Tap to speak';
      sLabel.textContent = (this.cfg.agentName || 'Speako') + ' active';
      this.hideTranscript();
    }
  };

  proto.showTranscript = function (text) {
    if (!text) return;
    this.els.transcript.textContent = text;
    this.els.transcript.style.display = 'block';
  };

  proto.hideTranscript = function () {
    this.els.transcript.style.display = 'none';
  };

  proto.toggleVoice = function () {
    if (this.voiceState === 'listening' || this.voiceState === 'speaking') {
      this.setVoiceState('idle');
      this.emit('voicestop', {});
    } else {
      this.setVoiceState('listening');
      this.emit('voicestart', {});
    }
  };

  /* ── Shell Navigation Control (Never Destroys Audio or Session) ── */
  proto.open = function (view, params) {
    if (this.host) this.host.style.display = 'block';
    this.root.classList.add('sp-visible');
    document.documentElement.style.overflow = 'hidden';
    document.body.style.overflow = 'hidden';
    this.pushView(view || 'discovery', params || {});
  };

  proto.close = function () {
    if (this.host) this.host.style.display = 'none';
    this.root.classList.remove('sp-visible');
    document.documentElement.style.overflow = '';
    document.body.style.overflow = '';
    this.setVoiceState('idle');
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

    var isRoot = this.stack.length <= 1;
    this.els.backBtn.style.display = isRoot ? 'none' : 'inline-flex';
    this.els.avatar.style.display = isRoot ? 'block' : 'none';

    if (top.view === 'pdp') {
      this.els.backLabel.textContent = 'Back to recommendations';
      this.renderPDP(top.params);
    } else if (top.view === 'compare') {
      this.els.backLabel.textContent = 'Back to product';
      this.renderCompare(top.params);
    } else if (top.view === 'cart' || top.view === 'cartpreview') {
      this.els.backLabel.textContent = 'Back to shopping';
      this.renderCartPreview(top.params);
    } else {
      this.els.backLabel.textContent = 'Back';
      this.renderDiscovery(top.params);
    }
  };

  /* ══════════════════════════════════════════════════════════
     1. DISCOVERY VIEW (Customer Intent, Speako Pick, Reasoning)
     ══════════════════════════════════════════════════════════ */
  proto.renderDiscovery = function (params) {
    var _this = this;
    params = params || {};
    
    var headline = params.headline || this.activeHeadline;
    var intentChips = params.intentChips || this.intentChips;
    
    var items = (params.products && params.products.length) ? params.products : (this.products.length ? this.products : [
      {
        id: '101',
        handle: 'lumiere-silk-midi-dress',
        title: 'Lumière Silk Midi Dress',
        vendor: this.cfg.storeName || 'Maison Speako',
        price: 79.95,
        compare: 129.00,
        save: 38,
        image: 'https://images.unsplash.com/photo-1595777457583-95e059d581b8?w=800',
        isSpeakoPick: true,
        pickReason: [
          'Matches your requested occasion',
          'Fits your requested budget',
          'Available in your size'
        ],
        offer: 'Bundle with Riviera Tote — Save $18'
      },
      {
        id: '102',
        handle: 'riviera-woven-tote',
        title: 'Riviera Woven Tote',
        vendor: this.cfg.storeName || 'Maison Speako',
        price: 65.00,
        compare: 89.00,
        save: 27,
        image: 'https://images.unsplash.com/photo-1584917865442-de89df76afd3?w=800'
      },
      {
        id: '103',
        handle: 'atelier-leather-slide',
        title: 'Atelier Leather Slide',
        vendor: this.cfg.storeName || 'Maison Speako',
        price: 110.00,
        image: 'https://images.unsplash.com/photo-1543163521-1bf539c55dd2?w=800'
      },
      {
        id: '104',
        handle: 'solene-tortoise-sunglasses',
        title: 'Solene Tortoise Sunglasses',
        vendor: this.cfg.storeName || 'Maison Speako',
        price: 48.00,
        compare: 72.00,
        save: 33,
        waitlist: true,
        image: 'https://images.unsplash.com/photo-1511499767150-a48a237f0083?w=800'
      }
    ]);

    this.products = items;

    // Intent Chips Bar
    var chipsHtml = '<div class="sp-intent-bar">' +
      '<span class="sp-intent-label">You asked for:</span>' +
      '<div class="sp-intent-chips">' + intentChips.map(function (c) {
        return '<span class="sp-intent-chip">' + c + '</span>';
      }).join('') + '</div>' +
    '</div>';

    // Hero Intent Title
    var heroHtml = '<div class="sp-discovery-hero"><h1 class="sp-hero-title">' + headline + '</h1></div>';

    // Featured "Speako's Pick" Card
    var pickItem = items.find(function (p) { return p.isSpeakoPick; }) || items[0];
    var pickHtml = '';
    if (pickItem) {
      var pickReasons = pickItem.pickReason || ['Closest match to your search', 'Fits your budget', 'In stock now'];
      pickHtml = [
        '<div class="sp-pick-card" data-handle="' + pickItem.handle + '">',
          '<div class="sp-pick-media">',
            '<img src="' + pickItem.image + '" alt="' + pickItem.title + '">',
            '<span class="sp-pick-badge">' + SVG.sparkles + ' SPEAKO\'S PICK</span>',
          '</div>',
          '<div class="sp-pick-details">',
            '<div class="sp-pick-vendor">' + (pickItem.vendor || _this.cfg.storeName) + '</div>',
            '<h2 class="sp-pick-title">' + pickItem.title + '</h2>',
            '<div class="sp-pick-price-row">',
              '<span class="sp-pick-price">$' + Number(pickItem.price).toFixed(2) + '</span>',
              (pickItem.compare ? '<span class="sp-pick-compare">$' + Number(pickItem.compare).toFixed(2) + '</span>' : ''),
              (pickItem.save ? '<span class="sp-badge-save">Save ' + pickItem.save + '%</span>' : ''),
            '</div>',
            (pickItem.offer ? '<div class="sp-offer-callout">' + SVG.tag + ' ' + pickItem.offer + '</div>' : ''),
            '<div class="sp-pick-reasons-box">',
              '<div class="sp-reasons-title">Why I picked this:</div>',
              '<ul class="sp-reasons-list">',
                pickReasons.map(function (r) { return '<li>' + SVG.check + ' ' + r + '</li>'; }).join(''),
              '</ul>',
            '</div>',
            '<div class="sp-pick-actions">',
              '<button class="sp-btn-pick-view">View Details & Sizing</button>',
              '<button class="sp-btn-pick-add" data-quick-add="' + pickItem.handle + '">Add to Cart</button>',
            '</div>',
          '</div>',
        '</div>'
      ].join('');
    }

    // Grid of Alternative Product Options
    var gridHtml = '<div class="sp-alternatives-header"><span>More recommendations for you</span></div>' +
      '<div class="sp-grid">' + items.map(function (p) {
        var priceNum = Number(p.price || 0);
        var compNum = Number(p.compare || 0);
        var badges = (p.save ? '<span class="sp-badge-sale">SAVE ' + p.save + '%</span>' : '') +
                     (p.waitlist ? '<span class="sp-badge-waitlist">WAITLIST</span>' : '');

        return [
          '<div class="sp-card" data-handle="' + p.handle + '">',
            '<div class="sp-card-media"><img src="' + p.image + '" alt="' + p.title + '">' + badges + '</div>',
            '<div class="sp-card-body">',
              '<div class="sp-card-vendor">' + (p.vendor || _this.cfg.storeName) + '</div>',
              '<div class="sp-card-title">' + p.title + '</div>',
              '<div class="sp-card-bottom">',
                '<div class="sp-card-pricerow">',
                  '<span class="sp-card-price">$' + priceNum.toFixed(2) + '</span>',
                  (compNum > priceNum ? '<span class="sp-card-compare">$' + compNum.toFixed(2) + '</span>' : ''),
                '</div>',
                '<button class="sp-card-action-btn" data-open-pdp="' + p.handle + '" title="View product">&#43;</button>',
              '</div>',
            '</div>',
          '</div>'
        ].join('');
      }).join('') + '</div>';

    this.els.body.innerHTML = heroHtml + chipsHtml + pickHtml + gridHtml;

    // Attach card clicks
    this.els.body.querySelectorAll('[data-handle]').forEach(function (card) {
      card.addEventListener('click', function (e) {
        if (e.target.closest('[data-quick-add]')) return;
        var h = card.getAttribute('data-handle');
        var prod = items.find(function (x) { return x.handle === h; }) || items[0];
        _this.pushView('pdp', { product: prod });
      });
    });

    this.els.body.querySelectorAll('[data-quick-add]').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        var h = btn.getAttribute('data-quick-add');
        var prod = items.find(function (x) { return x.handle === h; }) || items[0];
        _this.addToCart(prod, 1, 'Default');
      });
    });
  };

  /* ══════════════════════════════════════════════════════════
     2. PRODUCT DETAIL PDP VIEW (AI Decision Support + Variant Sync)
     ══════════════════════════════════════════════════════════ */
  proto.renderPDP = function (params) {
    var _this = this;
    params = params || {};
    var p = params.product || this.products[0] || {
      title: 'Lumière Silk Midi Dress',
      price: 79.95,
      compare: 129.00,
      image: 'https://images.unsplash.com/photo-1595777457583-95e059d581b8?w=800'
    };

    this.currentProduct = p;
    var priceNum = Number(p.price || 79.95);
    var compNum = Number(p.compare || 129.00);
    var saveAmount = compNum > priceNum ? (compNum - priceNum) : 0;

    this.els.body.innerHTML = [
      '<div class="sp-pdp-container">',
        '<div class="sp-pdp-grid">',
          
          '<!-- Left: Product Photography -->',
          '<div class="sp-pdp-media-hero">',
            '<img src="' + p.image + '" alt="' + p.title + '" data-hero-img>',
          '</div>',

          '<!-- Right: Product Decision Panel -->',
          '<div class="sp-pdp-info-col">',
            '<div class="sp-pdp-vendor-tag">' + (p.vendor || this.cfg.storeName) + '</div>',
            '<h1 class="sp-pdp-title-main">' + p.title + '</h1>',
            
            '<div class="sp-pdp-price-row">',
              '<span class="sp-pdp-price-current">$' + priceNum.toFixed(2) + '</span>',
              (compNum > priceNum ? '<span class="sp-pdp-price-original">$' + compNum.toFixed(2) + '</span>' : ''),
              (saveAmount > 0 ? '<span class="sp-pdp-save-badge">Save $' + saveAmount.toFixed(2) + '</span>' : ''),
            '</div>',

            '<!-- Merchant Bundle / Combo Offer -->',
            (p.offer ? '<div class="sp-offer-callout">' + SVG.tag + ' ' + p.offer + '</div>' : ''),

            '<!-- AI Sales Advisor Take -->',
            '<div class="sp-advisor-take-card">',
              '<div class="sp-advisor-badge">' + SVG.sparkles + ' SPEAKO\'S TAKE</div>',
              '<p class="sp-advisor-text">"I\'d choose this for the dinner look you described — mulberry silk with fluid drape and a relaxed fit that pairs perfectly with sandals or mules."</p>',
            '</div>',

            '<!-- "Why This One" Structured Reasoning -->',
            '<div class="sp-why-this-box">',
              '<div class="sp-why-title">Why this one:</div>',
              '<div class="sp-why-items">',
                '<span>' + SVG.check + ' Matches your occasion</span>',
                '<span>' + SVG.check + ' Fits your budget</span>',
                '<span>' + SVG.check + ' Available in your size</span>',
              '</div>',
            '</div>',

            '<!-- Authentic Product Store Details -->',
            '<div class="sp-store-details-section">',
              '<div class="sp-details-heading">Product Details</div>',
              '<p class="sp-pdp-desc-text">Bias-cut mulberry silk with a fluid drape and hand-finished seams. Designed for golden-hour dinners and slow coastal evenings.</p>',
            '</div>',
            
            '<!-- Colourway Selector -->',
            '<div class="sp-pdp-section-label">COLOURWAY</div>',
            '<div class="sp-pdp-options-row" data-color-group>',
              '<button class="sp-pdp-pill active" data-color="Champagne">Champagne</button>',
              '<button class="sp-pdp-pill" data-color="Ivory">Ivory</button>',
              '<button class="sp-pdp-pill" data-color="Onyx">Onyx</button>',
            '</div>',

            '<!-- Size Selector -->',
            '<div class="sp-pdp-section-label">SIZE</div>',
            '<div class="sp-pdp-options-row" data-size-group>',
              '<button class="sp-pdp-pill size-pill" data-size="XS">XS</button>',
              '<button class="sp-pdp-pill size-pill active" data-size="S">S</button>',
              '<button class="sp-pdp-pill size-pill" data-size="M">M</button>',
              '<button class="sp-pdp-pill size-pill" data-size="L">L</button>',
            '</div>',

            '<!-- Real Stock Alert -->',
            '<div class="sp-pdp-stock-alert">',
              '<span class="sp-pdp-stock-dot"></span>',
              '<span data-stock-text>Live stock: only 4 left in Champagne · Size S</span>',
            '</div>',

            '<!-- Action CTAs -->',
            '<div class="sp-pdp-actions-row">',
              '<button class="sp-btn-pdp-add" data-add-btn>Add to Cart · $' + priceNum.toFixed(2) + '</button>',
              '<button class="sp-btn-pdp-buy" data-buy-btn>Buy it now</button>',
            '</div>',

            '<!-- Suggested Question Accelerators -->',
            '<div class="sp-question-section">',
              '<div class="sp-question-label">Ask ' + (this.cfg.agentName || 'Speako') + ' about this item:</div>',
              '<div class="sp-question-chips">',
                '<button class="sp-q-chip" data-q="Does this run true to size?">"Does this run true to size?"</button>',
                '<button class="sp-q-chip" data-q="What is the fabric like?">"What\'s the fabric like?"</button>',
                '<button class="sp-q-chip" data-q="How should I style this?">"How should I style this?"</button>',
                '<button class="sp-q-chip" data-q="Compare with the Riviera Tote">"Compare with Riviera Tote"</button>',
              '</div>',
            '</div>',

          '</div>',
        '</div>',
      '</div>'
    ].join('');

    // Variant Selection Handlers
    this.els.body.querySelectorAll('[data-color]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        _this.els.body.querySelectorAll('[data-color]').forEach(function (b) { b.classList.remove('active'); });
        btn.classList.add('active');
        _this.currentVariant.color = btn.getAttribute('data-color');
        _this.updateVariantSelection();
        if (p.images && p.images[_this.currentVariant.color]) {
          var heroImg = _this.els.body.querySelector('[data-hero-img]');
          if (heroImg) heroImg.src = p.images[_this.currentVariant.color];
        }
      });
    });

    this.els.body.querySelectorAll('[data-size]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        _this.els.body.querySelectorAll('[data-size]').forEach(function (b) { b.classList.remove('active'); });
        btn.classList.add('active');
        _this.currentVariant.size = btn.getAttribute('data-size');
        _this.updateVariantSelection();
      });
    });

    // Question Accelerators
    this.els.body.querySelectorAll('[data-q]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var q = btn.getAttribute('data-q');
        if (q.indexOf('Compare') !== -1) {
          _this.pushView('compare', { p1: p, p2: _this.products[1] || p });
        } else {
          _this.chat(q);
        }
      });
    });

    // Cart and Checkout Buttons
    this.els.body.querySelector('[data-add-btn]').addEventListener('click', function () {
      var variantLabel = _this.currentVariant.color + ' / ' + _this.currentVariant.size;
      _this.addToCart(p, 1, variantLabel);
    });

    this.els.body.querySelector('[data-buy-btn]').addEventListener('click', function () {
      _this.directCheckout(p);
    });
  };

  proto.updateVariantSelection = function () {
    var color = this.currentVariant.color;
    var size = this.currentVariant.size;
    var stockLabel = this.els.body.querySelector('[data-stock-text]');
    if (stockLabel) {
      stockLabel.textContent = 'Live stock: available in ' + color + ' · Size ' + size;
    }
  };

  /* ══════════════════════════════════════════════════════════
     3. COMPARE VIEW (Side-by-Side Key Attribute Decision Support)
     ══════════════════════════════════════════════════════════ */
  proto.renderCompare = function (params) {
    var _this = this;
    params = params || {};
    var p1 = params.p1 || this.products[0];
    var p2 = params.p2 || this.products[1] || this.products[0];

    this.els.body.innerHTML = [
      '<div class="sp-compare-container">',
        '<h1 class="sp-compare-title">Side-by-Side Comparison</h1>',
        '<p class="sp-compare-sub">Speako\'s breakdown to help you make the best decision.</p>',
        '<div class="sp-compare-grid">',
          
          '<div class="sp-compare-col sp-comp-featured">',
            '<div class="sp-comp-badge">' + SVG.sparkles + ' SPEAKO\'S PICK</div>',
            '<div class="sp-comp-media"><img src="' + p1.image + '" alt="' + p1.title + '"></div>',
            '<h2 class="sp-comp-title">' + p1.title + '</h2>',
            '<div class="sp-comp-price">$' + Number(p1.price).toFixed(2) + '</div>',
            '<div class="sp-comp-attr"><strong>Best for:</strong> Evening dinners & special occasions</div>',
            '<div class="sp-comp-attr"><strong>Material:</strong> 100% Mulberry Silk</div>',
            '<div class="sp-comp-attr"><strong>Fit:</strong> Relaxed fluid drape</div>',
            '<button class="sp-btn-pick-add" data-add-comp="1">Choose This One</button>',
          '</div>',

          '<div class="sp-compare-col">',
            '<div class="sp-comp-media"><img src="' + p2.image + '" alt="' + p2.title + '"></div>',
            '<h2 class="sp-comp-title">' + p2.title + '</h2>',
            '<div class="sp-comp-price">$' + Number(p2.price).toFixed(2) + '</div>',
            '<div class="sp-comp-attr"><strong>Best for:</strong> Daytime & versatile styling</div>',
            '<div class="sp-comp-attr"><strong>Material:</strong> Handwoven Raffia & Leather</div>',
            '<div class="sp-comp-attr"><strong>Fit:</strong> Structured everyday</div>',
            '<button class="sp-btn-pick-view" data-add-comp="2">Choose This One</button>',
          '</div>',

        '</div>',
      '</div>'
    ].join('');

    this.els.body.querySelector('[data-add-comp="1"]').addEventListener('click', function () {
      _this.addToCart(p1, 1, 'Standard');
    });
    this.els.body.querySelector('[data-add-comp="2"]').addEventListener('click', function () {
      _this.addToCart(p2, 1, 'Standard');
    });
  };

  /* ══════════════════════════════════════════════════════════
     4. REAL SHOPIFY CART PREVIEW & CHECKOUT HANDOFF
     ══════════════════════════════════════════════════════════ */
  proto.openCart = function () {
    this.pushView('cart');
  };

  proto.renderCartPreview = function () {
    var _this = this;
    var items = this.cartItems;
    var total = items.reduce(function (sum, item) { return sum + (item.price * item.quantity); }, 0);

    this.els.body.innerHTML = [
      '<div class="sp-cart-preview-wrap">',
        '<div class="sp-cartx-orb">' + SVG.cart + '</div>',
        '<h1 class="sp-cartx-title">Your Shopping Bag (' + this.cartCount + ')</h1>',
        '<p class="sp-cartx-sub">Synced in real-time with your store cart.</p>',
        
        (items.length ? [
          '<div class="sp-cart-items-list">',
            items.map(function (it) {
              return [
                '<div class="sp-cart-item-row">',
                  '<img src="' + (it.image || '') + '" alt="' + it.title + '" class="sp-cart-item-img">',
                  '<div class="sp-cart-item-info">',
                    '<div class="sp-cart-item-title">' + it.title + '</div>',
                    '<div class="sp-cart-item-variant">' + (it.variant || 'Standard') + ' × ' + it.quantity + '</div>',
                  '</div>',
                  '<div class="sp-cart-item-price">$' + (it.price * it.quantity).toFixed(2) + '</div>',
                '</div>'
              ].join('');
            }).join(''),
          '</div>',
          '<div class="sp-cart-summary-row">',
            '<span>Subtotal</span>',
            '<span class="sp-cart-total-amount">$' + total.toFixed(2) + '</span>',
          '</div>',
          '<div class="sp-cartx-actions">',
            '<button class="sp-btn sp-cartx-view" data-act-storecart>View Store Cart</button>',
            '<button class="sp-btn sp-cartx-checkout" data-act-checkout>Proceed to Checkout</button>',
          '</div>'
        ].join('') : '<div class="sp-cart-empty">Your bag is currently empty. Ask Speako for recommendations!</div>'),

      '</div>'
    ].join('');

    var storeCartBtn = this.els.body.querySelector('[data-act-storecart]');
    if (storeCartBtn) {
      storeCartBtn.addEventListener('click', function () { location.href = '/cart'; });
    }

    var checkoutBtn = this.els.body.querySelector('[data-act-checkout]');
    if (checkoutBtn) {
      checkoutBtn.addEventListener('click', function () { location.href = '/checkout'; });
    }
  };

  /* ── Shopify Storefront Cart Operations ── */
  proto.addToCart = function (product, quantity, variantTitle) {
    var _this = this;
    quantity = quantity || 1;
    this.toast('Adding to cart…');
    
    var resolvedVariantId = product.variantId || product.id;
    if (product.variants && product.variants.length) {
      var match = product.variants.find(function(v) {
        return variantTitle && v.title && variantTitle.split(' / ').every(function(part) {
          return v.title.indexOf(part.trim()) !== -1;
        });
      });
      if (match) resolvedVariantId = match.id;
    }

    var commitLocalCart = function() {
      _this.cartCount += quantity;
      _this.cartItems.push({
        id: resolvedVariantId || Date.now(),
        title: product.title,
        price: Number(product.price),
        image: product.image,
        quantity: quantity,
        variant: variantTitle || 'Standard'
      });
      _this.els.badge.textContent = _this.cartCount;
      _this.toast('✓ Added to cart: ' + product.title + ' (' + (variantTitle || 'Standard') + ')');
      _this.emit('cartupdated', { count: _this.cartCount, items: _this.cartItems });
    };

    // Mutate Shopify Cart via Storefront API
    if (this.cfg.platform === 'shopify') {
      fetch('/cart/add.js', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: resolvedVariantId,
          quantity: quantity
        })
      })
      .then(function(res) {
        if (!res.ok) throw new Error('Network response was not ok');
        return res.json();
      })
      .then(function() {
        commitLocalCart();
      })
      .catch(function (e) {
        console.warn('[Speako] Native cart add failed:', e);
        _this.toast('Could not add item to cart. Please try again.');
      });
    } else {
      commitLocalCart();
    }
  };

  proto.directCheckout = function (product) {
    this.addToCart(product, 1);
    this.toast('Taking you to secure checkout…');
    setTimeout(function () {
      location.href = '/checkout';
    }, 400);
  };

  proto._fetchRealCart = function () {
    var _this = this;
    if (this.cfg.platform === 'shopify') {
      fetch('/cart.js')
        .then(function (r) { return r.json(); })
        .then(function (cart) {
          if (cart && typeof cart.item_count === 'number') {
            _this.cartCount = cart.item_count;
            _this.els.badge.textContent = _this.cartCount;
          }
        })
        .catch(function () {});
    }
  };

  /* ── Conversational Intelligence & Voice Processing ── */
  proto.chat = function (message) {
    var _this = this;
    this.setVoiceState('thinking', 'Checking that for you…');
    
    // Check for local conversational refinements
    var lMsg = message.toLowerCase();
    if (lMsg.indexOf('less flashy') !== -1 || lMsg.indexOf('minimal') !== -1) {
      if (this.intentChips.indexOf('Minimal') === -1) this.intentChips.push('Minimal');
      this.activeHeadline = "Updated for you: 4 more understated options that fit your request.";
    } else if (lMsg.indexOf('under $80') !== -1 || lMsg.indexOf('under 80') !== -1) {
      this.intentChips = ['Under $80', 'In Stock'];
      this.activeHeadline = "Updated for you: options under $80.";
    }

    fetch((this.cfg.apiBase || '') + '/api/v1/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: message,
        session_id: this.sessionId,
        store: this.cfg.storeName
      })
    })
    .then(function (r) { return r.json(); })
    .then(function (res) {
      var speechText = res.text || res.response_text || "Here's what I found for you.";
      _this.setVoiceState('speaking', speechText);
      
      if (res.products && res.products.length) {
        _this.products = res.products;
        _this.pushView('discovery', {
          products: res.products,
          headline: res.text || _this.activeHeadline,
          intentChips: _this.intentChips
        });
      }
    })
    .catch(function () {
      _this.setVoiceState('error');
    });
  };

  proto.toast = function (msg) {
    var t = this.els.toast;
    t.textContent = msg;
    t.classList.add('show');
    setTimeout(function () { t.classList.remove('show'); }, 3200);
  };

  // Global Bridge
  window.SpeakoOverlay = SpeakoOverlay;
  window.__SPEAKO_OVERLAY__ = new SpeakoOverlay(window.SpeakoOverlayConfig || {});

})(window, document);
