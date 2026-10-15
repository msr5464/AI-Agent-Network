"""Elements inside iframes: one way to write them down, for every agent.

A locator for an element inside an iframe is the iframe's selector, then the
element's, joined the way Playwright joins them itself:

    #checkout >> internal:control=enter-frame >> iframe[title='3ds'] >> internal:control=enter-frame >> #amount

That string was chosen, not invented, because it is already everywhere:
`page.frameLocator(a).locator(b)` and `page.locator(a).contentFrame().locator(b)`
both compile to it, Playwright Java's `Locator.toString()` prints it into every
failure message ("Locator@" + it), a trace records it, and `page.locator()`
accepts it back unchanged. So a failed selector read out of a log, a field read
out of a page object and a selector reported by step 02 are the same string, and
the agents' own browser can resolve all three.

What was measured before this was written (nested cross-origin iframes, a
timestamped popup name, two identical widget iframes):

  * A count through an AMBIGUOUS hop lies. With two iframes matching the hop,
    `page.locator(chain).count()` answered 1 (it enters the first) while
    `click()` failed with a strict mode violation on the iframe. So every hop has
    to be proved unique on its own; a count of the whole chain proves nothing.
  * Frame names and URLs carry per-load tokens (`popup_1790441440177`,
    `?token=...`), and hosts differ between environments. LINK_JS never uses a
    volatile value, and matches a src by its path, never its host.
  * Where no attribute identifies an iframe (identical widgets), there is no
    honest selector. LINK_JS returns null rather than a positional one; the
    element is then reported as unmeasurable instead of guessed.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

ENTER = "internal:control=enter-frame"
SEP = f" >> {ENTER} >> "

# Runs in the PARENT document with the <iframe>/<frame> element as its argument
# (Playwright: `frame.frameElement().evaluate(LINK_JS)`), so it works however
# cross-origin the child is. Returns the first stable selector that matches this
# element and nothing else in that document, or null.
LINK_JS = r"""el => {
  const doc = el.ownerDocument, tag = el.tagName.toLowerCase();
  const volatile = v => /\d{4,}|[0-9a-f]{8,}/i.test(v);
  const q = v => "'" + v.replace(/\\/g, '\\\\').replace(/'/g, "\\'") + "'";
  const unique = s => { try { return doc.querySelectorAll(s).length === 1; } catch (e) { return false; } };
  const out = [];
  if (el.id && !volatile(el.id)) out.push('#' + CSS.escape(el.id));
  const name = el.getAttribute('name') || '', stem = (name.match(/^\D{3,}/) || [''])[0];
  if (name && !volatile(name)) out.push(`${tag}[name=${q(name)}]`);
  else if (stem) out.push(`${tag}[name^=${q(stem)}]`);
  const title = el.getAttribute('title') || '';
  if (title && !volatile(title)) out.push(`${tag}[title=${q(title)}]`);
  const src = el.getAttribute('src') || '';
  if (src) {
    const path = new URL(src, doc.baseURI).pathname;
    if (path.length > 1 && !volatile(path)) out.push(`${tag}[src*=${q(path)}]`);
  }
  for (const s of out) if (unique(s)) return s;
  for (let p = el.parentElement; p && p !== doc.body; p = p.parentElement) {
    if (!p.id || volatile(p.id)) continue;
    const scope = '#' + CSS.escape(p.id);
    for (const s of out) if (unique(scope + ' ' + s)) return scope + ' ' + s;
  }
  return null;
}"""

# Preloaded into every document the agents' browser opens (the MCP server's
# --init-script), cross-origin iframes included, so the prompts call it instead
# of carrying it. A model asked to paste LINK_JS into its own code retyped it
# from memory instead, without the title rule, and then found no selector for
# a checkout's 3-D Secure iframe that has nothing else stable on it.
INIT_JS = "window.__qaLink = " + LINK_JS + ";\n"
# The chain prefix of a Playwright Frame — '' for the main frame, null when some
# hop has no stable unique selector — is computed by page.qa.prefix
# (shared/browser/qa-page.js), which every prompt calls instead of carrying it.


def split(selector: str) -> Tuple[List[str], str]:
    """(iframe selectors outermost first, the element's own selector)."""
    text = (selector or "").strip()
    if text.startswith("Locator@"):
        text = text[len("Locator@"):].strip()
    parts = [p.strip() for p in text.split(f">> {ENTER} >>")]
    return parts[:-1], parts[-1]


def join(path: List[str], inner: str) -> str:
    return SEP.join(list(path or []) + [inner])


def scoped(selector: str) -> bool:
    return ENTER in (selector or "")


def prefix_path(prefix: str) -> List[str]:
    """The iframe hops in a prefix (`a >> internal:control=enter-frame >> `), with
    or without its trailing separator."""
    return [hop.strip() for hop in re.split(rf">>\s*{re.escape(ENTER)}\s*(?:>>|$)", prefix or "")
            if hop.strip()]
