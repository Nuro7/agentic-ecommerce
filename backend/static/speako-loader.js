/**
 * speako-loader.js — plug-and-play Speako integration for custom platforms.
 *
 * Paste before </body> on every store page:
 *
 *   <script>
 *     window.SpeakoConfig = {
 *       backendUrl: "https://speako.app",
 *       storeName:  "My Store",
 *       email:      "admin@mystore.com",
 *       apiUrl:     "https://mystore.com/api",   // your store's product API
 *       apiKey:     "my_secret_key",             // your Speako API key
 *     };
 *   </script>
 *   <script src="https://speako.app/static/speako-loader.js" async></script>
 */
(async function () {
  var cfg = window.SpeakoConfig;
  if (!cfg || !cfg.backendUrl || !cfg.apiKey) return;

  var backend  = cfg.backendUrl.replace(/\/$/, '');
  var cacheKey = 'speako_tid_' + cfg.apiKey;

  // ── Step 1: resolve tenant_id ────────────────────────────────────────────
  var tenantId = localStorage.getItem(cacheKey);

  if (!tenantId) {
    try {
      var regRes = await fetch(backend + '/api/v1/onboard/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          store_name:          cfg.storeName  || 'My Store',
          email:               cfg.email      || '',
          platform:            'custom_api',
          custom_api_base_url: cfg.apiUrl     || '',
          custom_api_key:      cfg.apiKey,
        }),
      });

      if (regRes.status === 201) {
        var regData = await regRes.json();
        tenantId = regData.tenant_id;
      } else if (regRes.status === 409) {
        // Already registered — recover tenant_id by API key
        var luRes = await fetch(
          backend + '/api/v1/onboard/lookup?api_key=' + encodeURIComponent(cfg.apiKey)
        );
        if (luRes.ok) {
          var luData = await luRes.json();
          tenantId = luData.tenant_id;
        }
      }

      if (tenantId) {
        localStorage.setItem(cacheKey, tenantId);
      }
    } catch (_) {
      // Never break the merchant's page
    }
  }

  if (!tenantId) return;

  // ── Step 2: bootstrap Aria widget ────────────────────────────────────────
  window.wooagent_config = {
    backend_url:     backend,
    agent_api_url:   backend,
    tenant_id:       tenantId,
    store_name:      cfg.storeName      || '',
    primary_color:   cfg.primaryColor   || '#6366f1',
    widget_position: cfg.widgetPosition || 'bottom-right',
    enable_voice:    cfg.enableVoice    !== false,
    enable_text:     cfg.enableText     !== false,
    language:        cfg.language       || 'en',
    platform:        'custom_api',
  };

  var s    = document.createElement('script');
  s.src    = backend + '/static/wooagent-widget.js';
  s.async  = true;
  document.head.appendChild(s);

  // ── Step 3: background product sync (once per browser session) ───────────
  var syncFlag = 'speako_synced_' + tenantId;
  if (!sessionStorage.getItem(syncFlag) && cfg.apiUrl) {
    sessionStorage.setItem(syncFlag, '1');
    try {
      var prRes = await fetch(cfg.apiUrl.replace(/\/$/, '') + '/products?all=true');
      if (prRes.ok) {
        var products = await prRes.json();
        if (!Array.isArray(products)) products = [products];
        if (products.length > 0) {
          await fetch(backend + '/api/v1/ingest/products', {
            method:  'POST',
            headers: {
              'Content-Type':  'application/json',
              'Authorization': 'Bearer ' + cfg.apiKey,
            },
            body: JSON.stringify(products),
          });
        }
      }
    } catch (_) {
      // Sync failure is silent — widget still works, products may be stale
    }
  }
})();
