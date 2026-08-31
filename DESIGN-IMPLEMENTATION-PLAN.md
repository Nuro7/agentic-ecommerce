# Overlay Design Implementation Plan

## Design Analysis Summary

Based on the provided images, the following design changes need to be applied across all overlay pages (PDP, Search, Cart, Compare, Chat, Home).

---

## 1. Key Design Changes Identified

### Color Scheme Updates
- **Save badges**: Change from gradient purple (`var(--sp-grad)`) to solid green/teal (`#10b981` or similar)
- **Active variant/size selectors**: Dark background with border highlight (not gradient)
- **Primary CTA (Add to Cart)**: Keep gradient but ensure consistent purple-to-blue
- **Secondary CTA (Buy it now)**: Outlined/ghost style with border

### Component Changes

#### A. Header Bar
- Back arrow (left)
- Product/collection title with optional verified icon
- Brand/subtitle below title
- Cart icon with badge (top-right)
- Close (X) button (far right)

#### B. Product Cards (Search/Grid)
- Dark card background with subtle border
- "SAVE XX%" badge → green/teal solid background
- "WAITLIST" badge → dark glass style, top-right
- Brand name uppercase above product title
- Price with strikethrough compare price
- "+" quick-add button on hover (bottom-right of card)

#### C. PDP Variant Selectors
- **COLOURWAY/Color**: Pill-shaped buttons with dark background, active state has border highlight
- **SIZE**: Pill-shaped buttons, active state has border highlight
- Remove gradient from active state, use subtle border instead

#### D. PDP Action Buttons
- "Add to Cart · $79.95" → Gradient button (primary)
- "Buy it now" → Outlined/ghost button (secondary)
- Stack vertically with gap

#### E. Voice Prompt Card (PDP)
- Glass-morphic card with microphone icon
- Suggested voice command text
- "Tap to speak · answers in ~1s" subtitle
- Waveform animation on right side

#### F. Filter Chips (Search)
- Active chip: Gradient background
- Inactive chips: Dark glass with border
- Horizontal scrollable row

#### G. Stock Indicator
- Green dot + "Live stock: only X left in [Variant]"
- Subtle, inline display

#### H. Bottom Voice Bar
- Waveform animation (left)
- Input field with placeholder "Ask Aria or find your style..."
- "Voice active" text
- Gradient microphone button

---

## 2. Files to Modify

### Primary Files
| File | Changes |
|------|---------|
| `backend/static/speako-overlay.css` | All visual style updates |
| `backend/static/speako-overlay.js` | HTML template updates for new elements |

### Secondary Files (if needed)
| File | Changes |
|------|---------|
| `backend/static/wooagent-widget.js` | Widget bridge updates (if overlay API changes) |

---

## 3. Implementation Steps

### Phase 1: CSS Variables & Base Styles
1. Add new CSS variables for save badge color (green/teal)
2. Update card styles for darker background
3. Add "waitlist" badge style
4. Update voice prompt card styles

### Phase 2: Component Style Updates
1. **Header bar**: Update layout for back arrow + title + brand + cart + close
2. **Product cards**: Update grid layout, card styles, badge positions
3. **PDP variant selectors**: Remove gradient from active, add border highlight
4. **PDP action buttons**: Swap primary/secondary styles
5. **Filter chips**: Update active state styling
6. **Voice bar**: Update layout and animations

### Phase 3: JavaScript Template Updates
1. Update `_renderSearch()` for new card HTML structure
2. Update `_renderPdpMini()` for new PDP layout
3. Add voice prompt card component
4. Update header HTML generation

### Phase 4: Testing & Polish
1. Test all overlay views (Home, Search, PDP, Cart, Compare, Chat)
2. Verify responsive behavior
3. Check dark/light theme compatibility
4. Test voice integration

---

## 4. Detailed CSS Changes

### Save Badge (Green/Teal)
```css
/* Current */
.sp-badge-sale { background: var(--sp-grad); }

/* New */
.sp-badge-sale { background: #10b981; box-shadow: 0 4px 14px rgba(16, 185, 129, 0.4); }
```

### Active Variant Selector
```css
/* Current */
.sp-swatch.active { border-color: var(--sp-brand); background: var(--sp-brand-soft); }

/* New */
.sp-swatch.active { 
  border-color: var(--sp-fg); 
  background: var(--sp-bg-2);
  box-shadow: 0 0 0 1px var(--sp-fg);
}
```

### Primary CTA (Add to Cart)
```css
/* Keep gradient but ensure consistent */
.sp-add {
  background: var(--sp-grad);
  color: #fff;
  border: 0;
  box-shadow: var(--sp-glow);
}
```

### Secondary CTA (Buy it now)
```css
/* Change to outlined */
.sp-buy-now {
  background: transparent;
  color: var(--sp-fg);
  border: 1.5px solid var(--sp-border);
}
.sp-buy-now:hover { border-color: var(--sp-brand); background: var(--sp-brand-soft); }
```

### Voice Prompt Card
```css
.sp-voice-prompt {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px;
  background: var(--sp-glass);
  border: 1px solid var(--sp-border);
  border-radius: var(--sp-radius);
  backdrop-filter: blur(12px);
}
```

### Waitlist Badge
```css
.sp-badge-waitlist {
  position: absolute;
  top: 10px;
  right: 10px;
  background: var(--sp-glass-2);
  color: var(--sp-fg);
  font-size: 10px;
  font-weight: 700;
  padding: 4px 10px;
  border-radius: var(--sp-radius-pill);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  border: 1px solid var(--sp-border);
}
```

---

## 5. JavaScript Template Changes

### Search Grid Card (`_gridHtml`)
- Add waitlist badge support
- Update "+" button position and style
- Update brand name display

### PDP (`_renderPdpMini`)
- Add voice prompt card after action buttons
- Update stock indicator text format
- Update button text and styles

---

## 6. Testing Checklist

- [ ] PDP view renders correctly with new styles
- [ ] Search results grid displays cards properly
- [ ] Filter chips work with new active state
- [ ] Voice prompt card appears on PDP
- [ ] Bottom voice bar renders correctly
- [ ] Cart view maintains consistent styling
- [ ] Compare view works with updated card styles
- [ ] Chat view bubbles still look correct
- [ ] Light theme variant works
- [ ] Mobile responsive behavior intact
- [ ] Voice integration still functional
- [ ] Add to Cart / Buy It Now buttons work

---

## 7. Rollback Plan

If issues arise:
1. Revert CSS changes to `speako-overlay.css`
2. Revert JS template changes to `speako-overlay.js`
3. Clear browser cache and test

---

## 8. Estimated Effort

| Task | Hours |
|------|-------|
| CSS variable updates | 0.5 |
| Component style updates | 2.0 |
| JS template updates | 2.0 |
| Testing & polish | 1.5 |
| **Total** | **6.0** |

---

## 9. Dependencies

- No new libraries required
- No backend API changes needed
- No migration required
- Purely frontend CSS/JS changes

---

## 10. Notes

- The overlay is a self-contained vanilla JS SPA (no build step)
- All HTML is generated in JavaScript string concatenation
- CSS is injected into the overlay shadow root
- Changes must be compatible with the existing "Midnight Concierge" design system
- Must maintain theme-leak-proof construction (shadow DOM isolation)
