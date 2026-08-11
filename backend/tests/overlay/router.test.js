// Pure-logic tests for the Speako Fullscreen Overlay (node --test).
// The overlay module self-exports its pure helpers when required under Node.
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const OV = require('../../static/speako-overlay.js');

const ORIGIN = 'https://demo.myshopify.com';
const SHOPIFY = { enabled: true, platform: 'shopify', origin: ORIGIN };

// ── RouterStack ──────────────────────────────────────────────────────────────
test('RouterStack push/top/size/pop/reset', () => {
  const s = new OV.RouterStack();
  assert.equal(s.size(), 0);
  assert.equal(s.top(), null);
  assert.equal(s.pop(), null);

  s.push('home', {});
  s.push('search', { query: 'shirt' });
  s.push('pdp', { handle: 'x' });
  assert.equal(s.size(), 3);
  assert.deepEqual(s.top(), { view: 'pdp', params: { handle: 'x' } });

  const popped = s.pop();
  assert.equal(popped.view, 'pdp');
  assert.equal(s.size(), 2);
  assert.equal(s.top().view, 'search');

  s.reset();
  assert.equal(s.size(), 0);
});

// ── claimAction ──────────────────────────────────────────────────────────────
test('claimAction: disabled or non-shopify platform never claims', () => {
  const act = { type: 'show_products', payload: {} };
  assert.equal(OV.claimAction(act, { enabled: false, platform: 'shopify' }), false);
  assert.equal(OV.claimAction(act, { enabled: true, platform: 'woocommerce' }), false);
  assert.equal(OV.claimAction(null, SHOPIFY), false);
  assert.equal(OV.claimAction({}, SHOPIFY), false);
});

test('claimAction: store view actions claim when overlay on', () => {
  ['show_products', 'show_product_detail', 'show_availability', 'show_cart',
   'cart_updated', 'apply_discount_code', 'highlight_card', 'search'].forEach((type) => {
    assert.equal(OV.claimAction({ type, payload: {} }, SHOPIFY), true, type);
  });
  assert.equal(OV.claimAction({ type: 'add_to_cart', payload: {} }, SHOPIFY), false);
  assert.equal(OV.claimAction({ type: 'show_orders', payload: {} }, SHOPIFY), false);
});

test('claimAction: store-nav redirects claim; real checkout never claims', () => {
  assert.equal(OV.claimAction(
    { type: 'redirect_checkout', payload: { reason: 'cart', url: '/cart' } }, SHOPIFY), true);
  assert.equal(OV.claimAction(
    { type: 'redirect', payload: { reason: 'product', url: '/products/shoe' } }, SHOPIFY), true);
  assert.equal(OV.claimAction(
    { type: 'redirect', payload: { reason: 'search', url: '/search?q=x' } }, SHOPIFY), true);

  // Plain checkout (no store-reason) → must stay a hard top-level navigation.
  assert.equal(OV.claimAction({ type: 'redirect_checkout', payload: {} }, SHOPIFY), false);
  assert.equal(OV.claimAction(
    { type: 'redirect_checkout', payload: { url: '/checkout' } }, SHOPIFY), false);
  assert.equal(OV.claimAction(
    { type: 'redirect_checkout', payload: { reason: 'product' } }, SHOPIFY), true);
});

test('claimAction: cross-origin store-nav redirects are NOT claimed', () => {
  const act = { type: 'redirect', payload: { reason: 'product', url: 'https://evil.example.com/product' } };
  assert.equal(OV.claimAction(act, SHOPIFY), false);
});

// ── isRealCheckout ───────────────────────────────────────────────────────────
test('isRealCheckout distinguishes final checkout from store-nav', () => {
  assert.equal(OV.isRealCheckout({ type: 'redirect', payload: {} }), false);
  assert.equal(OV.isRealCheckout({ type: 'redirect_checkout', payload: {} }), true);
  assert.equal(OV.isRealCheckout({ type: 'redirect_checkout', payload: { url: '/checkout' } }), true);
  assert.equal(OV.isRealCheckout({ type: 'redirect_checkout', payload: { reason: 'cart' } }), false);
  assert.equal(OV.isRealCheckout({ type: 'redirect_checkout', payload: { reason: 'search' } }), false);
});

// ── isSameOriginTarget ───────────────────────────────────────────────────────
test('isSameOriginTarget: relative/no url ok, cross-origin rejected', () => {
  assert.equal(OV.isSameOriginTarget('', ORIGIN), true);
  assert.equal(OV.isSameOriginTarget('/products/shoe', ORIGIN), true);
  assert.equal(OV.isSameOriginTarget(ORIGIN + '/cart', ORIGIN), true);
  assert.equal(OV.isSameOriginTarget('https://other.shop.com/x', ORIGIN), false);
});

// ── extractHandle ────────────────────────────────────────────────────────────
test('extractHandle: product object, handle key, or /products/ url', () => {
  assert.equal(OV.extractHandle({ product: { handle: 'runner' } }), 'runner');
  assert.equal(OV.extractHandle({ handle: 'slip-on' }), 'slip-on');
  assert.equal(OV.extractHandle({ url: '/products/men-shirt?size=L' }), 'men-shirt');
  assert.equal(OV.extractHandle({}, 'https://x/products/decode%20me'), 'decode me');
  assert.equal(OV.extractHandle({}, 'https://x/collections/all'), '');
});

// ── Money / discount math ───────────────────────────────────────────────────
test('formatMoney: currency prefix, 2 decimals', () => {
  assert.equal(OV.formatMoney(49.99, '₹'), '₹49.99');
  assert.equal(OV.formatMoney(0, '$'), '$0.00');
  assert.equal(OV.formatMoney('12.5', '$'), '$12.50');
  assert.equal(OV.formatMoney('abc', '$'), '$0.00');
});

test('cartSubtotal sums unit price × quantity', () => {
  const lines = [
    { quantity: 2, unit_price: { amount: 49.99 } },
    { quantity: 1, unit_price: { amount: 10.25 } },
    { quantity: 0, unit_price: { amount: 999 } },
  ];
  assert.equal(OV.cartSubtotal(lines), 110.23);
  assert.equal(OV.cartSubtotal([]), 0);
});

test('applyDiscount: percent stacked math with clamping', () => {
  assert.equal(OV.applyDiscount(100, 10), 90);
  assert.equal(OV.applyDiscount(100, 0), 100);
  assert.equal(OV.applyDiscount(50, 200), 0);
  assert.equal(OV.applyDiscount(100, -5), 100);
});

// ── Variant price / selection ───────────────────────────────────────────────
test('variantPrice reads amount numeric value', () => {
  assert.equal(OV.variantPrice({ price: { amount: '49.99' } }), 49.99);
  assert.equal(OV.variantPrice(null), 0);
  assert.equal(OV.variantPrice({ price: { amount: 'abc' } }), 0);
});

test('pickVariant: defaults to first available, honours selected options', () => {
  const product = {
    variants: [
      { id: 'v1', title: 'Red / S', available_for_sale: false,
        selected_options: [{ name: 'Color', value: 'Red' }, { name: 'Size', value: 'S' }] },
      { id: 'v2', title: 'Red / M', available_for_sale: true,
        selected_options: [{ name: 'Color', value: 'Red' }, { name: 'Size', value: 'M' }] },
    ],
  };
  assert.equal(OV.pickVariant(product, null).id, 'v2'); // first available
  const exact = OV.pickVariant(product, [{ name: 'Size', value: 'S' }, { name: 'Color', value: 'Red' }]);
  assert.equal(exact.id, 'v1'); // ordered compare is order-independent
  assert.equal(OV.pickVariant(product, [{ name: 'Size', value: 'XL' }]).id, 'v2'); // falls back
  assert.equal(OV.pickVariant({ variants: [] }, null), null);
});

test('escapeHtml neutralises markup', () => {
  assert.equal(OV.escapeHtml('<script>alert(1)</script>'),
    '&lt;script&gt;alert(1)&lt;/script&gt;');
  assert.equal(OV.escapeHtml(null), '');
});