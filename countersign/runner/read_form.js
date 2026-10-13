// Evaluated in the page. Reports the form's controls, text a person cannot see, and the
// line that states a total. It reads; it changes nothing.
() => {
  const MAX_HIDDEN = 20, MAX_TEXT = 600, MAX_FIELDS = 2000, MAX_VALUE = 5000, MAX_OPTIONS = 500;

  const shown = (el) => {
    for (let e = el; e && e.nodeType === 1; e = e.parentElement) {
      const s = getComputedStyle(e);
      if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity) < 0.05) return false;
    }
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    if (parseFloat(s.fontSize) < 2) return false;
    if (r.width <= 1 || r.height <= 1) return false;
    if (r.right < 0 || r.bottom < 0 || r.left > 20000 || r.top > 200000) return false;
    return true;
  };

  const labelOf = (el) => {
    const own = el.labels && el.labels.length ? el.labels[0].innerText : '';
    return (own || el.getAttribute('aria-label') || el.placeholder || '').trim().replace(/\s+/g, ' ').slice(0, 200);
  };

  const fields = [], radios = new Map();
  for (const el of document.querySelectorAll('input, select, textarea, button')) {
    const tag = el.tagName.toLowerCase();
    let type = tag === 'select' ? 'select' : tag === 'textarea' ? 'textarea'
      : tag === 'button' ? (el.getAttribute('type') || 'submit').toLowerCase()
      : (el.getAttribute('type') || 'text').toLowerCase();
    if (fields.length >= MAX_FIELDS) break;
    const by = el.name ? 'name' : el.id ? 'id' : null;
    if (!by) continue;  // a control with no name and no id cannot be addressed or submitted
    const key = el.name || el.id;
    if (type === 'radio') {
      let g = radios.get(key);
      if (!g) {
        g = { key, by: 'name', tag, type, label: labelOf(el), required: false, options: [], value: '', checked: false, visible: false, autocomplete: '' };
        radios.set(key, g); fields.push(g);
      }
      g.options.push(el.value); g.required = g.required || el.required; g.visible = g.visible || shown(el);
      if (el.checked) { g.value = el.value; g.checked = true; }
      continue;
    }
    fields.push({
      key, by, tag, type,
      label: tag === 'button' || type === 'submit' ? (el.innerText || el.value || '').trim().slice(0, 200) : labelOf(el),
      required: !!el.required,
      options: tag === 'select' ? [...el.options].slice(0, MAX_OPTIONS).map(o => o.value.slice(0, 200)) : [],
      value: type === 'checkbox' || type === 'file' ? '' : (el.value || '').slice(0, MAX_VALUE),
      checked: type === 'checkbox' ? el.checked : false,
      visible: type !== 'hidden' && shown(el),
      autocomplete: el.getAttribute('autocomplete') || '',
    });
  }

  const hidden = [], seen = new Set();
  const push = (t) => {
    t = t.replace(/\s+/g, ' ').trim();
    if (t.length >= 20 && !seen.has(t) && hidden.length < MAX_HIDDEN) { seen.add(t); hidden.push(t.slice(0, MAX_TEXT)); }
  };
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'OPTION', 'SELECT', 'TITLE', 'HEAD', 'META', 'LINK']);
  const walker = document.createTreeWalker(document.documentElement, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_COMMENT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (n.nodeType === 8) { push(n.nodeValue || ''); continue; }
    const p = n.parentElement;
    if (!p || SKIP.has(p.tagName) || p.closest('script,style,noscript,template,select,head')) continue;
    if ((n.nodeValue || '').trim().length >= 20 && !shown(p)) push(n.nodeValue);
  }
  for (const el of document.querySelectorAll('[aria-label], [alt], [title]')) {
    if (!shown(el)) for (const a of ['aria-label', 'alt', 'title']) push(el.getAttribute(a) || '');
  }

  let total = null;
  for (const line of (document.body ? document.body.innerText : '').split('\n')) {
    if (/\btotal\b/i.test(line) && !/sub-?total/i.test(line) && /\d/.test(line)) total = line.trim().slice(0, 200);
  }

  return { fields, hidden_text: hidden, total_text: total };
}
