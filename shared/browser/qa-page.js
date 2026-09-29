// Loaded by the agents' browser MCP server onto every page object it opens
// (--init-page; CommonJS, because the server require()s it). It makes each
// browser_run_code_unsafe call one line —
//
//     async (page) => page.qa.step(() => page.locator('#add-to-cart').click())
//
// — instead of the frame loop, iframe-prefix walk, harvest and brief-screen
// recorder a model used to retype on every turn. One step is one round trip: act,
// wait until every frame stops changing (never a fixed sleep), report what is
// there, counted.
//
// Every selector returned was counted where it lives. A selector inside an iframe
// comes back as the frame chain shared/frames.py defines, and every hop of it is
// proven unique on its own: a count through two matching iframes reads 1 while the
// click then fails.
const fs = require('fs');

const ENTER = ' >> internal:control=enter-frame >> ';
const HOP = /\s*>>\s*internal:control=enter-frame\s*>>\s*/;
const short = e => String((e && e.message) || e).split('\n')[0].slice(0, 200);

// Evidence: what was measured, written straight to a file in the run's audit dir
// (QA_EVIDENCE_FILE) as it is measured. Python reads it after the run, so an
// inventory or a count reaches the flow map without the model retyping it — the
// explorer left 30 of 87 selectors unverifiable by never typing an inventory for
// their page — and without passing through the model's context at all.
const EVIDENCE = process.env.QA_EVIDENCE_FILE || '';
const note = row => {
  if (!EVIDENCE) return;
  try { fs.appendFileSync(EVIDENCE, JSON.stringify({ ts: Date.now(), ...row }) + '\n'); } catch (e) {}
};
// Locators the repo already has (QA_KNOWN_LOCATORS: [{owner, name, selector}]),
// counted live on every distinct page state: which still resolve, which broke.
let KNOWN = [];
try {
  if (process.env.QA_KNOWN_LOCATORS) KNOWN = JSON.parse(fs.readFileSync(process.env.QA_KNOWN_LOCATORS, 'utf8'));
} catch (e) { KNOWN = []; }

exports.default = async ({ page }) => {
  // The way into a frame: one stable unique selector per level, '' for the main
  // frame, null when some level has none (window.__qaLink decides).
  const prefix = async f => {
    let p = '';
    for (; f.parentFrame(); f = f.parentFrame()) {
      const s = await (await f.frameElement()).evaluate(el => window.__qaLink(el));
      if (!s) return null;
      p = s + ENTER + p;
    }
    return p;
  };

  // What qa-helpers.js reports typed, from any frame (cross-origin included),
  // straight to the evidence file: a form that submits and navigates away keeps
  // nothing in the page to read back later.
  if (EVIDENCE) {
    await page.exposeBinding('__qaTyped', async ({ frame }, t) => {
      const p = await prefix(frame).catch(() => null);
      note({ typed: { sel: t && t.sel && p !== null ? p + t.sel : null,
                      value: t ? t.value : null, password: !!(t && t.password) } });
    }).catch(() => {});
    await page.exposeBinding('__qaClicked', async ({ frame }, c) => {
      const p = await prefix(frame).catch(() => null);
      note({ clicked: { ...c, sel: c && c.sel && p !== null ? p + c.sel : null } });
    }).catch(() => {});
  }

  const everyFrame =(fn, arg) => Promise.all(page.frames().map(async f => ({
    frame: f.url().split('?')[0].slice(0, 100),
    prefix: await prefix(f).catch(() => null),
    result: await f.evaluate(fn, arg).catch(e => 'error: ' + short(e)),
  })));

  // Selectors with their frame's prefix on. Inside an iframe no stable selector
  // tells apart, nothing can be located reliably: no selector, and why.
  const located = frames => frames.map(fr => ({
    ...fr,
    result: Array.isArray(fr.result) ? fr.result.map(e => (
      fr.prefix === null
        ? { ...e, sel: null, why: 'its iframe has no stable unique selector' }
        : { ...e, sel: e.sel ? fr.prefix + e.sel : e.sel })) : fr.result,
  }));

  // A field with no unique selector of its own, anchored on what the user reads
  // beside it (rule 2b g) and counted here: `tr:has-text('Name') input`. Left
  // to the model, that took two calls, one listing the form's inputs and one
  // counting the candidates it built from them.
  const anchorOnLabels = async frames => {
    for (const fr of frames) {
      if (!Array.isArray(fr.result) || fr.prefix === null) continue;
      for (const e of fr.result) {
        // No selector, or only a shared one (`input[type='text']`, 15 matches).
        if ((e.sel && e.total === 1) || !e.near || !e.box) continue;
        const label = e.near.split(/\s{2,}|\t|\n/)[0].slice(0, 30).replace(/'/g, "\\'");
        const sel = fr.prefix + (e.within ? e.within + ' ' : '') + `${e.box}:has-text('${label}') `
          + e.tag + (e.type ? `[type='${e.type}']` : '');
        const n = (await check({ n: sel })).n;
        if (n.total === 1 && n.visible === 1) Object.assign(e, { sel, total: 1, visible: 1 });
      }
    }
  };

  const harvest = async (scope, opts) => {
    // A scope may name its frame the way every selector here does —
    // '#checkout >> internal:control=enter-frame >> form' — and then applies only
    // inside that frame. A model asked for exactly that, got "nothing matches"
    // from every frame, and fell back to writing its own frame search.
    // Ending at the hop ('#checkout >> internal:control=enter-frame') is the whole
    // frame. Unsplit, that string went to querySelector in every frame and every
    // harvest of an embedded checkout came back a SyntaxError.
    const parts = scope
      ? String(scope).replace(/\s*>>\s*internal:control=enter-frame\s*$/, ENTER).split(HOP) : [];
    const inner = parts.length ? parts[parts.length - 1] || null : null;
    const framePrefix = parts.length > 1 ? parts.slice(0, -1).join(ENTER) + ENTER : null;
    // Playwright resolves the scope, so its own syntax works (`:visible`,
    // `:has-text()`): handed to querySelector, 'div.cart:visible' was a
    // SyntaxError in every frame.
    let frames = located(await Promise.all(page.frames().map(async f => {
      const root = inner && await f.locator(inner).count().catch(() => 0)
        ? await f.locator(inner).first().elementHandle().catch(() => null) : null;
      return {
        frame: f.url().split('?')[0].slice(0, 100),
        prefix: await prefix(f).catch(() => null),
        result: inner && !root ? 'nothing matches scope ' + inner
          : await f.evaluate(([r, o]) => (window.__qa ? window.__qa.harvest(r, o)
            : 'no window.__qa here'), [root, opts || {}]).catch(e => 'error: ' + short(e)),
      };
    })));
    await anchorOnLabels(frames);
    if (framePrefix !== null) {
      const all = frames;
      frames = frames.filter(fr => fr.prefix === framePrefix);
      if (!frames.length) {
        return [{ frame: null, prefix: framePrefix, result: `error: no frame is reached by ${scope}`
          + ' — the frames here are ' + all.map(fr => JSON.stringify(fr.prefix)).join(', ') }];
      }
    }
    // A scope usually exists in one frame; the rest have nothing to say.
    const useful = frames.filter(fr => Array.isArray(fr.result) && fr.result.length);
    return useful.length ? useful : frames;
  };

  // Count each named selector — CSS or Playwright syntax, iframe chains hop by hop.
  const check = async named => {
    const out = {};
    for (const [name, sel] of Object.entries(named || {})) {
      try {
        const parts = String(sel).split(HOP);
        let scope = page, bad = null;
        for (const hop of parts.slice(0, -1)) {
          const n = await scope.locator(hop).count();
          if (n !== 1) { bad = { total: 0, visible: 0, error: `iframe hop ${hop} matches ${n}` }; break; }
          scope = scope.frameLocator(hop);
        }
        if (bad) { out[name] = bad; continue; }
        const loc = scope.locator(parts[parts.length - 1]);
        const total = await loc.count();
        let visible = 0;
        for (let i = 0; i < Math.min(total, 20); i++) if (await loc.nth(i).isVisible()) visible++;
        const result = { total, visible };
        if (total) {
          result.text = ((await loc.first().innerText({ timeout: 1000 }).catch(() => '')) || '')
            .trim().replace(/\s+/g, ' ').slice(0, 60);
          // Unique and visible is not "the field": a cart's read-only total cell
          // counted 1/1 and was reported as the amount field typed into.
          result.editable = await loc.first().isEditable({ timeout: 500 }).catch(() => false);
          // A radio or checkbox, or the label that controls one, says whether it is
          // selected: "select the promo" was confirmed with two more calls of the
          // model's own, the first on `#690`.
          const checked = await loc.first().isChecked({ timeout: 500 }).catch(() => null);
          if (checked !== null) result.checked = checked;
        }
        out[name] = result;
      } catch (e) {
        out[name] = { error: short(e) + (/(^|[\s>+~,(])#\d/.test(sel)
          ? " — an id starting with a digit is not valid CSS as #…; write [id='…']" : "") };
      }
    }
    return out;
  };

  // Requests in flight. A spinner can sit still while the next frame is still
  // loading behind it: a payment popup read as settled 0.8s after "Pay now", before
  // its 3-D Secure iframe existed. Streams never finish, and a request older than
  // a few seconds is a long poll or a beacon, not the page still loading.
  const inflight = new Map();
  const STREAMS = new Set(['websocket', 'eventsource', 'media']);
  page.on('request', r => { if (!STREAMS.has(r.resourceType())) inflight.set(r, Date.now()); });
  page.on('requestfinished', r => inflight.delete(r));
  page.on('requestfailed', r => inflight.delete(r));
  const loading = () => [...inflight.values()].some(t => Date.now() - t < 5000);

  // Every frame's size, cheaply. A ticking countdown changes digits, not the
  // text's length, so it does not keep the page "changing" forever.
  const fingerprint = async () => (await Promise.all(page.frames().map(f => f.evaluate(() =>
    document.body ? document.body.innerText.length + ':' + document.getElementsByTagName('*').length : '-')
    .catch(() => 'x')))).join('|');

  // Wait until no frame has changed for `quietMs`, at most `maxMs`. Replaces fixed
  // sleeps: a settled page returns at once, a slow one gets the time it needs.
  const settle = async (maxMs = 10000, quietMs = 700) => {
    const t0 = Date.now();
    let last = await fingerprint(), since = Date.now();
    while (Date.now() - t0 < maxMs) {
      await page.waitForTimeout(150);
      const now = await fingerprint();
      if (now !== last || loading()) { last = now; since = Date.now(); }
      else if (Date.now() - since >= quietMs) break;
    }
    return Date.now() - t0;
  };

  // Write what this page state looks like to the evidence file: every frame's
  // inventory (with its frame chain), the counts the caller asked for, and — once
  // per distinct state — a live count of every locator the repo already has.
  let lastKnown = '';
  const evidence = async (tag, checks) => {
    if (!EVIDENCE) return;
    const frames = await everyFrame(o => (window.__qa ? window.__qa.inventory(o) : []), {});
    const inventory = frames.flatMap(fr => (Array.isArray(fr.result) && fr.prefix !== null
      ? fr.result.map(e => (fr.prefix ? { ...e, frame: fr.prefix } : e)) : []));
    let known;
    const key = page.url() + '|' + await fingerprint();
    if (KNOWN.length && key !== lastKnown) {
      lastKnown = key;
      const counted = await check(Object.fromEntries(KNOWN.map((k, i) => [i, k.selector])));
      known = KNOWN.map((k, i) => ({ ...k, ...counted[i] }));
    }
    note({ page: tag || null, url: page.url(), inventory, checks, known });
  };

  // One step, one round trip: act, settle, then report — the named selectors
  // counted (`check`) and/or what is on the page now (`harvest`, default on).
  // `before` is counted before acting: the control the step clicks, when the click
  // takes it off the page. Counted after, a payment-method tab read 0/0 and was
  // never reported, so its locator was guessed and the guess failed in step 04.
  // `page` names the page the step lands on, so its evidence is filed under it.
  const step = async (action, opts = {}) => {
    const t0 = Date.now();
    const out = { ok: true };
    if (opts.before) out.before = await check(opts.before);
    if (action) {
      try { await action(); } catch (e) { out.ok = false; out.error = short(e); }
    }
    out.settledMs = await settle(opts.maxMs, opts.quietMs);
    out.url = page.url();
    if (opts.check) out.check = await check(opts.check);
    if (opts.harvest !== false) out.frames = await harvest(opts.scope, opts);
    // What the harvest counted is evidence as much as what `check` counted.
    const harvested = {};
    for (const fr of out.frames || []) {
      for (const e of Array.isArray(fr.result) ? fr.result : []) {
        if (e.sel && 'total' in e) harvested[e.sel] = { total: e.total, visible: e.visible, tag: e.tag };
      }
    }
    await evidence(opts.page, {
      ...harvested,
      ...(out.before ? selectorsOf(opts.before, out.before) : {}),
      ...(out.check ? selectorsOf(opts.check, out.check) : {}),
    });
    out.ms = Date.now() - t0;
    return out;
  };

  // Evidence is keyed by selector, not by the caller's name for it.
  const selectorsOf = (named, counted) => Object.fromEntries(
    Object.entries(named || {}).map(([name, sel]) => [sel, counted[name]]));

  // Counting on its own, for a page a step already reported.
  const checkOnly = async (named, opts = {}) => {
    const counted = await check(named);
    await evidence(opts.page, selectorsOf(named, counted));
    return counted;
  };

  // A screen that shows briefly and closes (a payment result, a toast): start
  // recording, then act — not awaited, so recording runs even if the action hangs —
  // and return every new state of every frame for `ms`, each element counted while
  // it was on screen.
  const record = async (action, ms = 20000, opts = {}) => {
    const grab = async () => located(await everyFrame(
      o => (window.__qa ? window.__qa.harvest(null, o) : []), { texts: true, limit: 80 }));
    const id = (fr, e) => fr.frame + ' ' + e.sel + ' ' + e.text;
    const before = new Set((await grab()).flatMap(fr =>
      Array.isArray(fr.result) ? fr.result.map(e => id(fr, e)) : []));
    const t0 = Date.now();
    const acted = Promise.resolve().then(action).then(() => 'ok', short);
    const states = [], seen = new Set();
    while (Date.now() - t0 < ms) {
      for (const fr of await grab()) {
        if (!Array.isArray(fr.result)) continue;
        const fresh = fr.result.filter(e => !before.has(id(fr, e)));
        // Digits ignored here only, so a ticking countdown is not a new state.
        const key = fresh.map(e => id(fr, e)).join('|').replace(/\d/g, '');
        if (fresh.length && !seen.has(key)) {
          seen.add(key);
          states.push({ ms: Date.now() - t0, frame: fr.frame, els: fresh });
        }
      }
      await page.waitForTimeout(150);
    }
    // Each element was counted while it was on screen: that is evidence too.
    note({ page: opts.page || null, url: page.url(), checks: Object.fromEntries(
      states.flatMap(s => s.els.filter(e => e.sel)
        .map(e => [e.sel, { total: e.total, visible: e.visible, text: e.text }]))) });
    return { action: await acted, states };
  };

  page.qa = { prefix, harvest, check: checkOnly, settle, step, record };
};
