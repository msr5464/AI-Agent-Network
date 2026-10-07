// Preloaded into every document the agents' browser opens, iframes included (the
// MCP server's --init-script, after window.__qaLink). The measuring half of step
// 02's selector rules — harvest, candidate selectors, the batch count — so a model
// calls it instead of retyping it: a 15-minute run spent a third of its output
// tokens rewriting these few functions in nearly every one of 48 calls.
//
// The bar is unchanged: every selector returned was counted in this document, and
// `visible` is the same test rule 2c always used.
window.__qa = (() => {
  // Per-load tokens (ids, hashes, timestamps) make a selector that works once.
  const volatile = v => /\d{4,}|[0-9a-f]{8,}/i.test(v);
  // CSS-in-JS class names are regenerated per build.
  const hashed = c => volatile(c) || /^(css|sc|jsx|emotion|makeStyles)-/.test(c)
    || /__[a-z0-9]{5,}$/i.test(c);
  const q = v => "'" + String(v).replace(/\\/g, '\\\\').replace(/'/g, "\\'") + "'";
  const vis = el => {
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  // A password field's value never leaves the page: every harvest goes to the
  // model and, as evidence, to disk.
  const text = el => (el.type === 'password' ? ''
    : (el.innerText || el.value || el.getAttribute('aria-label') || ''))
    .trim().replace(/\s+/g, ' ').slice(0, 60);
  // Which element something resolved to, stable while it stays in this document,
  // so Python can tell that two spellings name one element. A run kept a close
  // button's selector that was unique only while an overlay was closed, for the
  // overlay's own close button, which it had clicked through another selector.
  // The document token keeps a reloaded frame's numbers from colliding.
  const ids = new WeakMap();
  const doc = Math.random().toString(36).slice(2, 8);
  let lastId = 0;
  const uid = el => {
    if (!ids.has(el)) ids.set(el, doc + ':' + (++lastId));
    return ids.get(el);
  };
  const count = sel => {
    try {
      const m = [...document.querySelectorAll(sel)];
      return { total: m.length, visible: m.filter(vis).length };
    } catch (e) {
      return { error: 'INVALID_CSS: ' + e.message };
    }
  };

  // Rule 2b's order, stable values only.
  const candidates = el => {
    const tag = el.tagName.toLowerCase(), out = [];
    for (const a of ['data-cy', 'data-testid', 'data-test', 'data-qa']) {
      const v = el.getAttribute(a);
      if (v && !volatile(v)) out.push(`[${a}=${q(v)}]`);
    }
    if (el.id && !volatile(el.id)) out.push('#' + CSS.escape(el.id));
    for (const a of ['name', 'aria-label', 'placeholder', 'title', 'href', 'for', 'type']) {
      const v = el.getAttribute(a);
      if (v && v.length < 80 && !volatile(v)) out.push(`${tag}[${a}=${q(v)}]`);
    }
    const cls = [...el.classList].filter(c => !hashed(c));
    if (cls.length) out.push(tag + '.' + cls.map(c => CSS.escape(c)).join('.'));
    return out;
  };

  // The first candidate that is unique, else one scoped under the nearest unique
  // ancestor, else the best non-unique one with its count — never a positional one.
  const best = el => {
    const own = candidates(el);
    for (const s of own) {
      const n = count(s);
      if (n.total === 1) return { sel: s, ...n };
    }
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const scope = candidates(p).find(s => count(s).total === 1);
      if (!scope) continue;
      for (const s of own) {
        const n = count(scope + ' ' + s);
        if (n.total === 1) return { sel: scope + ' ' + s, ...n };
      }
      break;  // a broader ancestor cannot tell siblings apart either
    }
    return own.length ? { sel: own[0], ...count(own[0]) } : { sel: null };
  };

  const INTERACTIVE = 'input,button,a,select,textarea,label,summary,[role=button],'
    + '[role=link],[role=tab],[role=checkbox],[role=radio],[role=option],'
    + '[role=menuitem],[contenteditable],[onclick]';
  const IDENTITY = 'h1,h2,h3,header,nav,main,footer,form,[role=main],[role=navigation],'
    + '[role=banner],[class*=title],[class*=header],[id*=title],[id*=header]';
  const ownText = el => [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
  // A control a framework made clickable without saying so: a checkout `div`
  // with a React click handler has no role, no onclick attribute and no
  // tabindex, only the pointer cursor. The outermost one, since the cursor
  // inherits. Missed, a model spent four calls and a screenshot finding it.
  const pointer = el => getComputedStyle(el).cursor === 'pointer'
    && !(el.parentElement && getComputedStyle(el.parentElement).cursor === 'pointer');
  const control = el => el.matches(INTERACTIVE) || pointer(el);

  // Every value typed into a field, reported as it happens to page.qa's binding
  // (window.__qaTyped), so INPUT_USED is held to what was typed: a run reported
  // `amountField|20,000` for a cart total it had only read. A password field
  // reports that it was typed into, never its value.
  const typed = e => {
    const el = e.target;
    if (!window.__qaTyped || !el || !el.matches
        || !el.matches('input,textarea,select,[contenteditable]')) return;
    const password = el.type === 'password';
    const b = best(el);
    window.__qaTyped({ sel: b.sel, total: b.total, visible: b.visible, uid: uid(el), password,
      value: password ? null : String(el.isContentEditable ? el.innerText : el.value).slice(0, 200) });
  };
  document.addEventListener('input', typed, true);
  document.addEventListener('change', typed, true);

  // Every click, reported the same way (window.__qaClicked), as the control it
  // landed on: a run clicked a page's main button, never counted or reported it,
  // and step 03 guessed its locator. Python confirms a plan's locator from a
  // recorded click whose text names it.
  const clicked = e => {
    let el = e.target;
    while (el && el.nodeType === 1 && el !== document.body && !control(el)) el = el.parentElement;
    if (!window.__qaClicked || !el || el.nodeType !== 1 || el === document.body) return;
    window.__qaClicked({ ...best(el), text: text(el), uid: uid(el) });
  };
  document.addEventListener('click', clicked, true);

  return {
    vis, count, text, best, uid,

    // Rule 2a + 2b + 2c in one call: every visible interactive element — and, with
    // `texts`, every visible element holding its own text, for reading values —
    // each with its best stable selector already counted.
    harvest(scope, opts = {}) {
      const { texts = false, limit = 60 } = opts || {};
      // An element when page.qa resolved the scope itself (any Playwright syntax).
      const root = !scope ? document
        : typeof scope === 'string' ? document.querySelector(scope) : scope;
      if (!root) return { error: 'nothing matches scope ' + scope };
      return [...root.querySelectorAll('*')]
        .filter(el => (control(el) || (texts && ownText(el))) && vis(el))
        .slice(0, limit)
        .map(el => {
          const o = { tag: el.tagName.toLowerCase(), text: text(el), ...best(el), uid: uid(el) };
          const type = el.getAttribute('type');
          if (type) o.type = type;
          // No unique selector of its own: what the user reads next to it, so a
          // label-anchored one (`tr:has-text('Name') input`) can be built and checked.
          // And the nearest ancestor that is unique itself, to scope one under.
          if (o.total !== 1) {
            const box = (el.labels && el.labels[0])
              || el.closest('tr, li, label, fieldset, [class*=field], [class*=form-group]');
            const near = box ? text(box).replace(o.text, '').trim() : '';
            if (near) o.near = near.slice(0, 40);
            // The tag to anchor on: page.qa counts `tr:has-text('Name') input`.
            if (near && box) o.box = box.tagName.toLowerCase();
            for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
              const scope = candidates(p).find(s => count(s).total === 1);
              if (scope) { o.within = scope; break; }
            }
          }
          return o;
        });
    },

    // The page as an inventory, in the shape the flow map's PAGE_STATE carries:
    // every visible control plus the page's identity — headings and the
    // header/nav/main containers a page object anchors on. A run inventoried a
    // products page as its tiles and buttons only, and the page object whose
    // anchors are the container and the title matched nothing.
    inventory(opts = {}) {
      const { limit = 80 } = opts || {};
      const KEEP = /^(data-|aria-label$|placeholder$|type$|href$|for$|title$)/;
      return [...document.querySelectorAll('*')]
        .filter(el => (control(el) || el.matches(IDENTITY)) && vis(el))
        .slice(0, limit)
        .map(el => {
          const o = { tag: el.tagName.toLowerCase() };
          if (el.id) o.id = el.id;
          const cls = el.getAttribute('class');
          if (cls) o.class = cls.trim().replace(/\s+/g, ' ');
          const name = el.getAttribute('name');
          if (name) o.name = name;
          const role = el.getAttribute('role');
          if (role) o.role = role;
          const t = text(el);
          if (t) o.text = t;
          const attributes = {};
          for (const a of el.attributes) {
            if (KEEP.test(a.name)) attributes[a.name] = a.value.slice(0, 80);
          }
          if (Object.keys(attributes).length) o.attributes = attributes;
          return o;
        });
    },

    // Rule 2c: every named candidate counted at once, with the text of the first match.
    check(named) {
      const out = {};
      for (const [name, sel] of Object.entries(named || {})) {
        const n = count(sel);
        if (n.total >= 1) n.text = text(document.querySelector(sel));
        out[name] = n;
      }
      return out;
    },
  };
})();
