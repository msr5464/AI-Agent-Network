// Element fingerprinting — the single source of truth for BOTH the Python
// engine and the Java framework.
//
// Multi-mode on purpose. Called with no argument it returns the page snapshot;
// called with an element it returns that element's index in the very same walk;
// called with an array of elements it returns the snapshot AND their indices,
// resolved in that one walk.
// Two separate implementations of the walk would be free to disagree about what
// "element #27" means, and every score downstream would be quietly wrong.
//
//   evaluate(JS)            -> { url, title, viewport, landmarks, elements[] }
//   evaluate(JS, element)   -> index into that elements[] array, or -1
//   evaluate(JS, [el, ...]) -> { ...snapshot, indices: [i, ...] }
//
// The array form exists because ONE walk per element is not enough: a caller that
// snapshots the page and then asks for each index separately re-walks a DOM that
// has moved on, and `elements[index]` then names a different element entirely.
// That silently recorded a page's logo as its edit button.
//
// Keep this file identical in QA-Agent-Network and the automation framework's
// src/main/resources. capture_parity_test asserts the two agree on a live page.

(target) => {
  const IGNORED_TAGS = new Set(['HTML','HEAD','META','SCRIPT','STYLE','LINK','TITLE','BASE','NOSCRIPT']);
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();

  // ---- implicit ARIA role, the subset that matters for test locators -------
  const roleOf = el => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit.split(/\s+/)[0];
    const t = el.tagName.toLowerCase();
    if (t === 'a') return el.hasAttribute('href') ? 'link' : 'generic';
    if (t === 'button') return 'button';
    if (t === 'select') return el.multiple || el.size > 1 ? 'listbox' : 'combobox';
    if (t === 'textarea') return 'textbox';
    if (t === 'img') return el.getAttribute('alt') === '' ? 'presentation' : 'img';
    if (/^h[1-6]$/.test(t)) return 'heading';
    if (t === 'input') {
      const ty = (el.type || 'text').toLowerCase();
      return {checkbox:'checkbox', radio:'radio', range:'slider', number:'spinbutton',
              submit:'button', reset:'button', button:'button', image:'button',
              search:'searchbox', email:'textbox', tel:'textbox', url:'textbox',
              password:'textbox', text:'textbox'}[ty] || 'textbox';
    }
    return {nav:'navigation', main:'main', header:'banner', footer:'contentinfo',
            aside:'complementary', form:'form', section:'region', ul:'list',
            ol:'list', li:'listitem', table:'table', option:'option',
            dialog:'dialog', summary:'button'}[t] || 'generic';
  };

  // ---- accessible name, approximating the accname algorithm ---------------
  const accName = el => {
    const al = norm(el.getAttribute('aria-label'));
    if (al) return al;
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const parts = lb.split(/\s+/).map(id => {
        const n = document.getElementById(id); return n ? norm(n.textContent) : '';
      }).filter(Boolean);
      if (parts.length) return parts.join(' ');
    }
    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab) return norm(lab.textContent);
    }
    const wrapping = el.closest('label');
    if (wrapping && wrapping !== el) return norm(wrapping.textContent);
    const t = el.tagName.toLowerCase();
    if (t === 'input' && ['submit','reset','button'].includes((el.type||'').toLowerCase()))
      return norm(el.value);
    if (t === 'img') return norm(el.getAttribute('alt'));
    // Text content, but only for elements that name themselves from content.
    if (['button','a','h1','h2','h3','h4','h5','h6','td','th','option','summary','label'].includes(t))
      return norm(el.textContent).slice(0, 120);
    const ph = norm(el.getAttribute('placeholder'));
    if (ph) return ph;
    return norm(el.getAttribute('title'));
  };

  const xpathOf = el => {
    const parts = [];
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      let i = 1;
      for (let sib = n.previousElementSibling; sib; sib = sib.previousElementSibling)
        if (sib.tagName === n.tagName) i++;
      parts.unshift(`${n.tagName.toLowerCase()}[${i}]`);
      if (n.parentElement === null) break;
    }
    return '/' + parts.join('/');
  };

  // XPath anchored at the nearest ancestor carrying an id — survives changes
  // above that ancestor, which absolute XPath does not.
  const idXpathOf = el => {
    const anchor = el.closest('[id]');
    if (!anchor) return xpathOf(el);
    if (anchor === el) return `//*[@id='${anchor.id}']`;
    const parts = [];
    for (let n = el; n && n !== anchor; n = n.parentElement) {
      let i = 1;
      for (let sib = n.previousElementSibling; sib; sib = sib.previousElementSibling)
        if (sib.tagName === n.tagName) i++;
      parts.unshift(`${n.tagName.toLowerCase()}[${i}]`);
    }
    return `//*[@id='${anchor.id}']/` + parts.join('/');
  };

  const INTERACTIVE = new Set(['A','BUTTON','INPUT','SELECT','TEXTAREA','SUMMARY','OPTION','LABEL']);
  const isInteractive = el =>
    INTERACTIVE.has(el.tagName) ||
    el.hasAttribute('onclick') || el.hasAttribute('tabindex') ||
    ['button','link','checkbox','radio','tab','menuitem','combobox','textbox','option']
      .includes(roleOf(el));

  const vw = window.innerWidth || 1, vh = window.innerHeight || 1;
  // Walk light DOM and open shadow roots together. querySelectorAll('*') stops
  // at the shadow boundary, which makes every element inside a web component
  // invisible to scoring -- and web components are exactly where locators rot.
  const walkAll = (root, out) => {
    const kids = root.querySelectorAll ? root.querySelectorAll('*') : [];
    for (const el of kids) {
      if (IGNORED_TAGS.has(el.tagName)) continue;
      out.push(el);
      if (el.shadowRoot) walkAll(el.shadowRoot, out);
    }
    return out;
  };
  const all = walkAll(document, []);

  // Text-bearing leaves, gathered once, reused for every element's neighbours.
  const textNodes = all.filter(el => {
    if (el.children.length) return false;
    const t = norm(el.textContent);
    return t && t.length <= 80;
  }).map(el => ({text: norm(el.textContent), rect: el.getBoundingClientRect()}));

  // Edge-to-edge gap, NOT centre-to-centre. A full-width <label> sitting
  // directly above an input has a huge centre-distance but a tiny gap — and it
  // is the single most meaningful piece of context that input has.
  const gap = (a, b) => {
    const dx = Math.max(0, a.left - b.right, b.left - a.right);
    const dy = Math.max(0, a.top - b.bottom, b.top - a.bottom);
    return Math.hypot(dx, dy);
  };

  const describe = el => {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const visible = r.width > 0 && r.height > 0 &&
                    cs.visibility !== 'hidden' && cs.display !== 'none' &&
                    parseFloat(cs.opacity || '1') > 0.01;

    const attrs = {};
    for (const a of el.attributes) {
      if (a.name.startsWith('data-gt')) continue;   // ground truth: not for the healer
      if (a.name === 'style') continue;
      attrs[a.name] = a.value;
    }

    const self = norm(el.textContent);

    // Structural relations first: these are facts about what labels this
    // element, not guesses from geometry, so they outrank any distance.
    const related = [];
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) related.push(norm(l.textContent));
    }
    const lb = el.getAttribute('aria-labelledby');
    if (lb) lb.split(/\s+/).forEach(id => {
      const n = document.getElementById(id); if (n) related.push(norm(n.textContent));
    });
    const box = el.closest('section,form,fieldset,article,[role="dialog"],[role="region"]');
    if (box) {
      const h = box.querySelector('h1,h2,h3,h4,h5,h6,legend');
      if (h) related.push(norm(h.textContent));
    }

    const near = textNodes
      .filter(t => t.text !== self)
      .map(t => ({text: t.text, d: gap(r, t.rect)}))
      .filter(t => t.d < 260)
      .sort((a, b) => a.d - b.d).map(t => t.text);

    const neighbors = Array.from(new Set(related.filter(Boolean).concat(near)))
      .filter(t => t && t !== self).slice(0, 6);

    const ancestors = [];
    for (let n = el.parentElement, i = 0; n && i < 5; n = n.parentElement, i++) {
      ancestors.push({
        tag: n.tagName.toLowerCase(), id: n.id || null,
        classes: Array.from(n.classList),
        role: n.getAttribute('role') || roleOf(n),
        testid: n.getAttribute('data-testid') || n.getAttribute('data-test') || null,
      });
    }

    let sibIndex = 0, sibCount = 0;
    if (el.parentElement) {
      const sibs = Array.from(el.parentElement.children);
      sibCount = sibs.length; sibIndex = sibs.indexOf(el);
    }

    return {
      tag: el.tagName.toLowerCase(),
      id: el.id || null,
      name: el.getAttribute('name'),
      type: el.getAttribute('type'),
      class_list: Array.from(el.classList),
      role: roleOf(el),
      accessible_name: accName(el),
      aria_label: el.getAttribute('aria-label'),
      placeholder: el.getAttribute('placeholder'),
      alt: el.getAttribute('alt'),
      href: el.getAttribute('href'),
      title: el.getAttribute('title'),
      testid: el.getAttribute('data-testid') || el.getAttribute('data-test-id') ||
              el.getAttribute('data-test') || el.getAttribute('data-qa') ||
              el.getAttribute('data-cy') || null,
      text: self.slice(0, 200),
      attrs,
      is_interactive: isInteractive(el),
      is_visible: visible,
      is_enabled: !el.disabled && el.getAttribute('aria-disabled') !== 'true',
      abs_xpath: xpathOf(el),
      id_xpath: idXpathOf(el),
      ancestor_chain: ancestors,
      sibling_index: sibIndex,
      sibling_count: sibCount,
      bbox_norm: {x: r.left/vw, y: r.top/vh, w: r.width/vw, h: r.height/vh},
      area_norm: (r.width * r.height) / (vw * vh),
      aspect: r.height > 0 ? r.width / r.height : 0,
      neighbor_texts: neighbors,
      _gt: el.getAttribute('data-gt') || null,   // eval only; stripped before scoring
    };
  };

  // Page identity: landmarks + headings. Cheap, and far more stable than URL
  // for answering "are we even on the right screen?".
  const landmarks = all
    .filter(el => /^(h[1-6]|nav|main|header|footer|aside|form|dialog)$/i.test(el.tagName) ||
                  el.hasAttribute('role'))
    .map(el => `${roleOf(el)}:${accName(el).slice(0, 60)}`)
    .filter(s => !s.endsWith(':'));

  // Element given: answer with its position in the walk above and nothing else.
  if (target && target.nodeType === 1) return all.indexOf(target);

  const snapshot = {
    url: location.href,
    title: document.title,
    viewport: {w: vw, h: vh},
    landmarks: Array.from(new Set(landmarks)),
    elements: all.map((el, i) => Object.assign({index: i}, describe(el))),
  };

  // Elements given: their indices come from the walk that just built `elements`,
  // so the two cannot disagree however much the page moves afterwards.
  if (Array.isArray(target)) {
    snapshot.indices = target.map(
      el => (el && el.nodeType === 1) ? all.indexOf(el) : -1);
  }
  return snapshot;
}
