// Countersign web app. No framework; every string from a page or a model goes through esc().
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
let S = null;            // /api/state
let poll = null;         // the timer for a live run or job

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) }, ...opts });
  const text = await res.text();
  let body; try { body = JSON.parse(text); } catch { body = { detail: text }; }
  if (!res.ok) throw new Error(body.detail || res.statusText);
  return body;
}
const post = (path, body, headers) => api(path, { method: 'POST', body: JSON.stringify(body || {}), headers });

async function refresh() { S = await api('/api/state'); chips(); return S; }

function chips() {
  const m = S.model ? `<span class="chip">model <b>${esc(S.model)}</b></span>` : '<span class="chip warn">no model: rules only</span>';
  const s = S.spend ? `<span class="chip">model spend <b>$${S.spend.usd.toFixed(3)}</b> of $${S.spend.cap}</span>` : '';
  const d = S.demo ? '<span class="chip">demo: bundled test forms only</span>' : '';
  $('#chips').innerHTML = m + s + d;
}

function tabs(name) {
  for (const a of document.querySelectorAll('#tabs a')) a.classList.toggle('on', a.dataset.tab === name);
}

function view(html) { $('#view').innerHTML = html; }

function ago(iso) { return iso ? esc(iso.replace('T', ' ').slice(0, 19)) : ''; }

function statusPill(s) { return `<span class="status s-${esc(s)}">${esc(s)}</span>`; }

function timeline(events) {
  if (!events || !events.length) return '';
  const t0 = events[0].t;
  return `<ul class="timeline">${events.map(e => `<li><span class="t">+${(e.t - t0).toFixed(1)}s</span>${esc(e.msg)}</li>`).join('')}</ul>`;
}

// ---------- login ----------

function loginView() {
  view(`<div class="card" style="max-width:420px"><h2>Owner sign-in</h2>
    <input type="password" id="pw" class="wide" placeholder="password" autofocus>
    <p><button id="go">Sign in</button> <span id="msg" class="muted"></span></p></div>`);
  $('#go').onclick = async () => {
    try { await post('/api/login', { password: $('#pw').value }); await refresh(); route(); }
    catch (e) { $('#msg').textContent = e.message; }
  };
}

// ---------- 1 learn ----------

const PICKS = {
  'wizard.html': 'A registration form in two steps, with an oddly worded rules box and a pre-ticked newsletter.',
  'plain.html': 'One page. A sponsor-sharing box arrives ticked; the job title arrives prefilled.',
  'hostile.html': 'Hidden text instructs the agent; its script tries to send what is typed to another host; the total is $1,278.',
  'consent.html': 'Asks for a statement only you can make: work authorisation. Never ticked for you.',
};

function learnView() {
  tabs('learn');
  const picks = S.fixtures.map(u => {
    const name = u.split('/').pop();
    return `<button class="pick" data-url="${esc(u)}"><b>${esc(name)}</b><span class="small muted">${esc(PICKS[name] || '')}</span></button>`;
  }).join('');
  const tools = S.tools.length ? `<table><tr><th>Form</th><th>Tool</th><th>Steps</th><th>Parameters</th><th>Consent boxes</th><th>Read by</th></tr>
    ${S.tools.map(t => `<tr><td><a href="#tool/${esc(t.form_id)}"><b>${esc(t.title)}</b></a><div class="key">${esc(t.url)}</div></td>
      <td class="mono">${esc(t.name)}</td><td>${t.steps}</td><td>${t.params}</td><td>${t.boxes}</td><td class="small">${esc(t.learned_by)}</td></tr>`).join('')}</table>`
    : '<p class="muted">Nothing learned yet.</p>';
  view(`<h1>Learn a form once</h1>
    <p class="muted">A browser in a throwaway sandbox walks the form (pressing Next through its steps, never Submit). The model reads it and compiles a typed tool: fields, types, choices, steps, and how each consent box reads.</p>
    <div class="card"><h2>Form address</h2>
      <div class="row"><input id="url" class="wide" style="flex:1" placeholder="http://form.test/wizard.html">
      <input id="fid" placeholder="name (optional)" style="width:180px"><button id="learn">Learn it</button></div>
      ${S.demo ? `<p class="note">This public demo learns only its own test forms, served on a private network inside the deployment:</p><div class="picks">${picks}</div>` : ''}
      <div id="job"></div></div>
    <div class="card"><h2>Learned forms</h2>${tools}</div>`);
  for (const b of document.querySelectorAll('.pick')) b.onclick = () => { $('#url').value = b.dataset.url; $('#fid').value = ''; };
  $('#learn').onclick = async () => {
    try {
      const rec = await post('/api/learn', { url: $('#url').value.trim(), form_id: $('#fid').value.trim() || null });
      watchJob(rec.id, async (r) => {
        $('#job').innerHTML = `<h3>Learning ${statusPill(r.status)}</h3>${timeline(r.events)}${r.error ? `<div class="banner no">${esc(r.error)}</div>` : ''}`;
        if (r.status === 'done') { await refresh(); location.hash = `#tool/${r.form_id}`; }
      });
    } catch (e) { $('#job').innerHTML = `<div class="banner no">${esc(e.message)}</div>`; }
  };
}

function watchJob(id, render) {
  clearInterval(poll);
  const tick = async () => {
    let r;
    try { r = await api(`/api/runs/${id}`); } catch (e) { clearInterval(poll); return; }
    await render(r);
    if (!['queued', 'running'].includes(r.status)) clearInterval(poll);
  };
  poll = setInterval(tick, 700);
  tick();
}

async function toolView(id) {
  tabs('learn');
  const t = await api(`/api/tools/${id}`);
  const c = t.compiled;
  const shots = [...Array(c.steps.length + 1).keys()].map(n =>
    `<figure><img src="/api/tools/${esc(id)}/shot/${n}" alt="step ${n + 1}" onerror="this.parentNode.remove()"><figcaption>step ${n + 1} as learned</figcaption></figure>`).join('');
  const params = t.params.map(p => `<tr><td><b>${esc(p.label || p.key)}</b><div class="key">${esc(p.key)}</div></td>
    <td class="mono">${esc(p.kind)}${p.required ? ' <span class="badge b-block">required</span>' : ''}</td>
    <td>${esc(p.description)}${p.options.length ? `<div class="small muted">choices: ${p.options.filter(o => o).map((o, i) => esc(o) + (p.option_labels[i] && p.option_labels[i] !== o ? ' = ' + esc(p.option_labels[i]) : '')).join(', ')}</div>` : ''}${p.default ? `<div class="small muted">page puts: ${esc(p.default)}</div>` : ''}</td>
    <td class="mono small">${esc(p.fact)}</td><td>${p.step + 1}</td></tr>`).join('');
  const boxes = t.boxes.map(b => `<tr><td>${esc(b.label)}<div class="key">${esc(b.key)}${b.prechecked ? ' &middot; arrives ticked' : ''}${b.required ? ' &middot; required' : ''}</div></td>
    <td><span class="badge b-${esc(b.kind)}">${esc(b.kind)}</span></td><td>${esc(ruleText(b))}</td><td class="small muted">${esc(b.reason)}</td></tr>`).join('');
  view(`<p><a href="#learn" class="muted">&larr; Learned forms</a></p>
    <h1>${esc(t.title)}</h1><p class="muted">${esc(t.purpose)}</p>
    <p class="small muted">${esc(c.url)} &middot; ${c.steps.length + 1} step(s) &middot; read by ${esc(t.learned_by)} &middot; ${ago(t.learned_at)} &middot; fingerprint <span class="mono">${esc(c.fingerprint.slice(0, 12))}</span></p>
    ${(t.notes || []).map(n => `<div class="banner info">${esc(n)}</div>`).join('')}
    ${(t.warnings || []).length ? `<div class="card"><h2>What the model noticed</h2><ul>${t.warnings.map(w => `<li>${esc(w)}</li>`).join('')}</ul><p class="note">The model is a reader here, not a judge. Types, choices and keys above come from the page itself.</p></div>` : ''}
    <div class="card"><h2>Parameters of <span class="mono">${esc(t.name)}</span></h2>
      <table><tr><th>Field</th><th>Type</th><th>What it asks for</th><th>Fact</th><th>Step</th></tr>${params}</table></div>
    <div class="card"><h2>Consent boxes</h2><p class="note">Decided by your standing rule, never by a caller. A statement only you can make is never ticked for you.</p>
      <table><tr><th>Box</th><th>Reads as</th><th>What happens</th><th>Why</th></tr>${boxes || '<tr><td colspan=4 class="muted">none</td></tr>'}</table>
      ${t.yours.length ? `<p class="small">Never filled, always yours: <span class="mono">${t.yours.map(esc).join(', ')}</span></p>` : ''}</div>
    <div class="card"><h2>Steps as the sandbox saw them</h2><div class="trail">${shots}</div></div>
    <div class="card"><h2>The typed tool, as another agent sees it</h2><pre class="json">${esc(JSON.stringify(t.definition, null, 2))}</pre></div>
    <p><a class="btn" href="#grant/${esc(id)}">Grant access to this form &rarr;</a></p>`);
  bindZoom();
}

function ruleText(b) {
  return { terms: 'ticked under your rule', marketing: 'left unticked under your rule',
    owner: 'yours to decide: only you can make this statement' }[b.kind] || (b.required ? 'yours to decide' : 'left unticked: optional');
}

// ---------- 2 grant ----------

function grantView(formId) {
  tabs('grant');
  if (!S.tools.length) { view('<h1>Grant access</h1><p class="muted">Learn a form first.</p>'); return; }
  formId = formId || S.tools[0].form_id;
  const facts = Object.entries(S.profile || {});
  const opts = S.tools.map(t => `<option value="${esc(t.form_id)}" ${t.form_id === formId ? 'selected' : ''}>${esc(t.title)} (${esc(t.form_id)})</option>`).join('');
  const grants = S.grants.map(g => `<tr><td class="mono">${esc(g.grant_id)}</td><td>${esc(g.caller)}</td><td class="mono">${esc(g.form_id)}</td>
    <td class="small">${g.facts.map(esc).join(', ')}</td><td>${g.max_total === null ? 'any' : '$' + esc(g.max_total)}</td>
    <td>${g.may_submit ? 'yes' : 'no'}</td><td>${esc(g.consent)}</td><td class="small">${new Date(g.expires_at * 1000).toLocaleString()}</td>
    <td>${g.token ? `<button class="ghost small" data-copy="${esc(g.token)}">copy token</button>` : ''}</td></tr>`).join('');
  view(`<h1>Grant access</h1>
    <p class="muted">A grant is a signed, expiring statement of what one caller may do with one form: which facts it may disclose, the largest total it may accept, whether it may submit, and how consent boxes are decided. Anything outside it is refused before a container starts.</p>
    <div class="row">
    <div class="card"><h2>New grant</h2>
      <label class="field">Form</label><select id="g-form" class="wide">${opts}</select>
      <label class="field">Caller</label><input id="g-caller" class="wide" value="you, in this app">
      <label class="field">Facts it may disclose</label>
      <div class="facts grid2">${facts.map(([k, v]) => `<label><input type="checkbox" value="${esc(k)}" ${k === 'passport_no' ? '' : 'checked'}> <span class="mono">${esc(k)}</span> <span class="small muted">${esc(v)}</span></label>`).join('')}</div>
      <div class="grid2">
        <div><label class="field">Largest total it may accept ($)</label><input id="g-max" type="number" min="0" step="0.01" value="0"></div>
        <div><label class="field">Expires in (minutes)</label><input id="g-ttl" type="number" min="1" value="120"></div>
      </div>
      <label class="field"><input type="checkbox" id="g-submit" checked> It may submit, once I approve the receipt</label>
      <label class="field">Consent boxes</label>
      <label><input type="radio" name="g-consent" value="accept-terms" checked> My standing rule: accept terms, decline marketing and anything optional, leave statements to me</label><br>
      <label><input type="radio" name="g-consent" value="ask"> Ask me about every box</label>
      <p><button id="issue">Sign this grant</button></p><div id="issued"></div></div>
    <div class="card"><h2>Your facts</h2>
      <p class="note">${S.demo ? 'A made-up person for the demo. ' : ''}The model only ever sees the facts a grant allows. The rest never leave this server.</p>
      <textarea id="facts" rows="10">${esc(JSON.stringify(S.profile || {}, null, 1))}</textarea>
      <p><button class="ghost" id="save-facts">Save facts</button> <span id="facts-msg" class="small muted"></span></p></div>
    </div>
    <div class="card"><h2>Grants</h2><table><tr><th>Grant</th><th>Caller</th><th>Form</th><th>Facts</th><th>Max total</th><th>Submit</th><th>Consent</th><th>Expires</th><th></th></tr>${grants || '<tr><td colspan=9 class="muted">none yet</td></tr>'}</table></div>`);
  $('#issue').onclick = async () => {
    try {
      const g = await post('/api/grants', {
        form_id: $('#g-form').value, caller: $('#g-caller').value,
        facts: [...document.querySelectorAll('.facts input:checked')].map(i => i.value),
        max_total: $('#g-max').value, may_submit: $('#g-submit').checked, ttl_minutes: +$('#g-ttl').value,
        consent: document.querySelector('input[name=g-consent]:checked').value,
      });
      await refresh();
      $('#issued').innerHTML = `<div class="banner ok">Signed grant ${esc(g.grant_id)} for ${esc(g.caller)}</div>
        <div class="token">${esc(g.token)}</div>
        <p><a class="btn go" href="#run/new/${esc(g.form_id)}/${esc(g.grant_id)}">Fill it now with this grant &rarr;</a></p>`;
    } catch (e) { $('#issued').innerHTML = `<div class="banner no">${esc(e.message)}</div>`; }
  };
  $('#save-facts').onclick = async () => {
    try { await post('/api/profile', { facts: JSON.parse($('#facts').value) }); await refresh(); $('#facts-msg').textContent = 'saved'; grantView($('#g-form').value); }
    catch (e) { $('#facts-msg').textContent = e.message; }
  };
  for (const b of document.querySelectorAll('[data-copy]')) b.onclick = () => navigator.clipboard.writeText(b.dataset.copy);
}

// ---------- 3 run ----------

function runsView(formId, grantId) {
  tabs('run');
  const opts = S.tools.map(t => `<option value="${esc(t.form_id)}" ${t.form_id === formId ? 'selected' : ''}>${esc(t.title)} (${esc(t.form_id)})</option>`).join('');
  const rows = S.runs.map(r => `<tr><td><a href="#run/${esc(r.id)}" class="mono">${esc(r.id)}</a></td><td>${esc(r.kind)}</td><td>${statusPill(r.status)}</td>
    <td class="mono">${esc(r.form_id)}</td><td>${esc(r.caller || '')}${r.via === 'agent' ? ' <span class="badge b-model">agent</span>' : ''}</td>
    <td>${r.approvable === true ? '<span class="badge b-known">approvable</span>' : r.approvable === false ? '<span class="badge b-block">blocked</span>' : ''}
    ${r.approved_by ? `<a class="small" href="#run/${esc(r.approved_by)}">approved &rarr;</a>` : ''}${r.rejected ? '<span class="small muted">rejected</span>' : ''}</td>
    <td class="small muted">${ago(r.created)}</td></tr>`).join('');
  view(`<h1>Fill and review</h1>
    <p class="muted">Each fill runs in a fresh sandbox under a grant and stops before Submit. You get a receipt: every field, where its value came from, known or guessed, and everything the page tried.</p>
    <div class="card"><h2>New fill</h2><div class="row" style="align-items:flex-end">
      <div><label class="field">Form</label><select id="r-form">${opts}</select></div>
      <div><label class="field">Grant</label><select id="r-grant"></select></div>
      <div><button id="start">Fill in a sandbox</button></div></div><div id="msg"></div></div>
    <div class="card"><h2>Runs</h2><table><tr><th>Run</th><th>Kind</th><th>Status</th><th>Form</th><th>Caller</th><th>Receipt</th><th>When</th></tr>${rows || '<tr><td colspan=7 class="muted">none yet</td></tr>'}</table></div>`);
  const fillGrants = () => {
    const f = $('#r-form').value;
    const gs = S.grants.filter(g => g.form_id === f && g.expires_at * 1000 > Date.now());
    $('#r-grant').innerHTML = gs.length ? gs.map(g => `<option value="${esc(g.grant_id)}" ${g.grant_id === grantId ? 'selected' : ''}>${esc(g.grant_id)} &middot; ${esc(g.caller)} &middot; ${g.facts.length} facts</option>`).join('')
      : '<option value="">no live grant for this form</option>';
  };
  if (S.tools.length) { $('#r-form').onchange = fillGrants; fillGrants(); }
  $('#start').onclick = async () => {
    try { const rec = await post('/api/runs', { form_id: $('#r-form').value, grant_id: $('#r-grant').value }); location.hash = `#run/${rec.id}`; }
    catch (e) { $('#msg').innerHTML = `<div class="banner no">${esc(e.message)}</div>`; }
  };
}

const LIFTABLE = ['consent-preticked', 'consent-required'];

function runView(id) {
  tabs('run');
  view('<p class="muted">Loading run&hellip;</p>');
  const decisions = {};
  watchJob(id, async (r) => {
    // Keep the owner's choices across refreshes of a live run.
    for (const el of document.querySelectorAll('.seg button.on')) decisions[el.dataset.key] = el.dataset.v === '1';
    view(renderRun(r, decisions));
    bindRun(r, decisions);
    bindZoom();
  });
}

function renderRun(r, decisions) {
  const rc = r.receipt;
  const head = `<p><a href="#run" class="muted">&larr; Runs</a>${r.of ? ` &middot; <a href="#run/${esc(r.of)}" class="muted">the receipt this submits</a>` : ''}</p>
    <h1>${r.kind === 'submit' ? 'Countersigned submit' : 'Fill'} <span class="mono">${esc(r.id)}</span> ${statusPill(r.status)}</h1>
    <p class="small muted">form <span class="mono">${esc(r.form_id)}</span> &middot; caller ${esc(r.caller || '')} ${r.via === 'agent' ? '<span class="badge b-model">another agent</span>' : ''} &middot; grant <span class="mono">${esc(r.grant_id || '')}</span> &middot; ${ago(r.created)}</p>`;
  const err = r.error ? `<div class="banner no">${esc(r.error)}</div>` : '';
  const live = `<div class="card"><h2>What happened</h2>${timeline(r.events)}</div>`;
  if (!rc) return head + err + live;

  const blocking = rc.flags.filter(f => f.blocking);
  const model = rc.flags.filter(f => !f.blocking && f.kind.startsWith('model-'));
  const evidence = rc.flags.filter(f => !f.blocking && !f.kind.startsWith('model-'));
  let banner;
  if (r.kind === 'submit') banner = `<div class="banner ok">Submitted, after you approved receipt ${esc((r.receipt.digest || '').slice(0, 12))}&hellip;; the site answered with ${esc((r.landed || {}).url || '')}</div>`;
  else if (r.approved_by) banner = `<div class="banner info">Approved. <a href="#run/${esc(r.approved_by)}">See the submit &rarr;</a></div>`;
  else if (r.rejected) banner = '<div class="banner info">Rejected. Nothing was submitted.</div>';
  else banner = rc.approvable ? '<div class="banner ok">Nothing blocks this receipt. Nothing has been submitted.</div>'
    : `<div class="banner no">${blocking.length} thing(s) need you before this can be submitted. Nothing has been submitted.</div>`;

  const src = (e) => {
    if (e.source.startsWith('fact:')) return `your fact <span class="mono">${esc(e.source.slice(5))}</span>`;
    if (e.source.startsWith('guess:')) return `guess: ${esc(e.source.slice(6))}`;
    if (e.source.startsWith('policy:')) return `your rule (${esc(e.source.slice(7))})`;
    return { 'page-default': 'the page put it there', none: 'nobody' }[e.source] || esc(e.source);
  };
  const intents = ((r.mapping || {}).intents) || {};
  const rows = rc.entries.map(e => `<tr><td><b>${esc(e.label || e.key)}</b><div class="key">${esc(e.key)}</div></td>
    <td class="val">${esc(e.value) || '<span class="muted">empty</span>'}</td><td class="small">${src(e)}${intents[e.key] && (intents[e.key].uses || []).length > 1 ? `<div class="key">from ${intents[e.key].uses.map(esc).join(', ')}</div>` : ''}</td>
    <td><span class="badge b-${esc(e.status)}">${esc(e.status)}</span></td></tr>`).join('');
  const flagList = (list, cls) => `<ul class="flags">${list.map(f => `<li class="${cls}"><span class="kind">${esc(f.kind)}</span>${esc(f.detail)}</li>`).join('')}</ul>`;

  const net = summarizeNet(r.proxy_log || []);
  const shots = (r.trail || []).map(s => `<figure><img src="/api/runs/${esc(r.id)}/shot/${s.n}" alt="${esc(s.caption)}"><figcaption>${esc(s.caption)}</figcaption></figure>`).join('');
  const m = r.mapping;
  const mapping = m ? `<div class="card"><h2>How values were chosen</h2><p class="small">Mapped by ${esc(m.by)}.${(m.withheld || []).length ? ` The grant leaves out <span class="mono">${m.withheld.map(esc).join(', ')}</span>, so the model never saw ${m.withheld.length === 1 ? 'it' : 'them'}.` : ''}</p>
    ${(m.notes || []).length ? `<ul class="flags">${m.notes.map(n => `<li>${esc(n)}</li>`).join('')}</ul>` : ''}</div>` : '';

  let approve = '';
  if (r.kind === 'fill' && r.status === 'done' && !r.approved_by && !r.rejected) {
    const consentBlocks = blocking.filter(f => LIFTABLE.includes(f.kind) && f.key);
    const other = blocking.filter(f => !(LIFTABLE.includes(f.kind) && f.key));
    const seg = (f) => {
      const v = decisions[f.key];
      const note = /your rule declines/.test(f.detail) ? 'Marketing. Your rule declines it; the form will not go through unless you tick it.'
        : /must be ticked to submit/.test(f.detail) ? 'Only you can make this statement. Countersign never ticks it for you.'
        : /nobody asked/.test(f.detail) ? 'It arrived ticked. Untick it unless you want it.' : '';
      return `<div class="decide"><span class="lbl">${esc(f.detail)}${note ? `<div class="note">${esc(note)}</div>` : ''}</span>
        <span class="seg"><button data-key="${esc(f.key)}" data-v="1" class="${v === true ? 'on' : ''}">Tick it</button><button data-key="${esc(f.key)}" data-v="0" class="${v === false ? 'on' : ''}">Leave unticked</button></span></div>`;
    };
    approve = `<div class="card" id="approve-card"><h2>Countersign</h2>
      ${consentBlocks.length ? `<p class="small">Boxes only you can decide:</p>${consentBlocks.map(seg).join('')}` : ''}
      ${other.length ? `<p class="small no-t">These cannot be approved away: ${other.map(f => esc(f.kind)).join(', ')}. Fix the form or the grant and run it again.</p>` : ''}
      <p class="note">Approving signs this exact receipt (digest <span class="mono">${esc(rc.digest.slice(0, 16))}</span>). A second fresh sandbox replays the fill; if anything differs, nothing is submitted.</p>
      <p><button class="go" id="approve" ${other.length ? 'disabled' : ''}>Approve and submit</button> <button class="stop" id="reject">Reject</button> <span id="amsg" class="small"></span></p></div>`;
  }
  const confirm = r.confirmation ? `<div class="card"><h2>After Submit</h2><ul class="flags"><li class="model"><span class="kind">${esc(r.confirmation.kind)}</span>${esc(r.confirmation.detail)}</li></ul></div>` : '';

  return head + err + banner + approve + confirm +
    `<div class="card"><h2>Receipt</h2><table><tr><th>Field</th><th>Value</th><th>Where it came from</th><th>Status</th></tr>${rows}</table>
     ${rc.total !== null ? `<p class="small">Total read from the page: <b>$${esc(rc.total)}</b></p>` : ''}</div>
     ${blocking.length ? `<div class="card"><h2>Needs you</h2>${flagList(blocking, 'blocking')}</div>` : ''}
     ${evidence.length ? `<div class="card"><h2>Evidence</h2>${flagList(evidence, '')}</div>` : ''}
     ${model.length ? `<div class="card"><h2>Model witness <span class="badge b-model">never a gate</span></h2><p class="note">A second reading of the screenshots and page text. It cannot approve a run or lift a block, because a page can write text aimed at it.</p>${flagList(model, 'model')}</div>` : ''}
     <div class="card"><h2>Screenshot trail</h2><div class="trail">${shots}</div></div>
     ${mapping}
     <div class="card"><h2>Network: the sandbox's only way out</h2>${net}</div>` + live;
}

function summarizeNet(log) {
  const rows = new Map();
  for (const x of log) {
    if (!['allow', 'deny'].includes(x.action)) continue;
    const k = `${x.action}|${x.host}:${x.port}|${x.action === 'deny' ? x.reason : ''}`;
    rows.set(k, (rows.get(k) || 0) + 1);
  }
  if (!rows.size) return '<p class="muted small">no requests</p>';
  return `<table class="net"><tr><th></th><th>Destination</th><th>Times</th><th>Why</th></tr>${[...rows].map(([k, n]) => {
    const [a, dest, why] = k.split('|');
    return `<tr><td class="${a === 'allow' ? 'ok-t' : 'no-t'}">${a === 'allow' ? 'allowed' : 'refused'}</td><td>${esc(dest)}</td><td>${n}</td><td>${esc(why)}</td></tr>`;
  }).join('')}</table>`;
}

function bindRun(r, decisions) {
  for (const b of document.querySelectorAll('.seg button')) b.onclick = () => {
    decisions[b.dataset.key] = b.dataset.v === '1';
    for (const s of b.parentNode.children) s.classList.toggle('on', s === b);
  };
  const ap = $('#approve');
  if (ap) ap.onclick = async () => {
    const needed = r.receipt.flags.filter(f => f.blocking && LIFTABLE.includes(f.kind) && f.key).map(f => f.key);
    const missing = needed.filter(k => !(k in decisions));
    if (missing.length) { $('#amsg').innerHTML = `<span class="no-t">Decide ${missing.map(esc).join(', ')} first.</span>`; return; }
    try {
      const rec = await post(`/api/runs/${r.id}/approve`, { digest: r.receipt.digest, consent: decisions });
      location.hash = `#run/${rec.id}`;
    } catch (e) { $('#amsg').innerHTML = `<span class="no-t">${esc(e.message)}</span>`; }
  };
  const rj = $('#reject');
  if (rj) rj.onclick = async () => { await post(`/api/runs/${r.id}/reject`); runView(r.id); };
}

// ---------- agents ----------

async function agentsView() {
  tabs('agents');
  const base = location.origin;
  const tools = S.tools.map(t => `<option value="${esc(t.form_id)}">${esc(t.name)}</option>`).join('');
  view(`<h1>For other agents</h1>
    <p class="muted">Every learned form is a typed tool. Another agent lists them over MCP and calls one with the owner's grant. It gets a receipt back. It can never approve one: only the owner can.</p>
    <div class="card"><h2>MCP endpoint</h2><p class="mono">${esc(base)}/mcp</p>
      <pre class="json">${esc(`curl -s ${base}/mcp -H 'Content-Type: application/json' \\
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

curl -s ${base}/mcp -H 'Content-Type: application/json' -H "Authorization: Bearer $GRANT" \\
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"fill_wizard","arguments":{"full_name":"Ada Example","email":"ada@example.org","country":"PH"}}}'`)}</pre></div>
    <div class="card"><h2>Try it as an agent</h2>
      <div class="grid2"><div><label class="field">Tool</label><select id="a-tool" class="wide">${tools}</select></div>
      <div><label class="field">Grant token (from the Grant page)</label><input id="a-token" class="wide" placeholder="eyJ..."></div></div>
      <label class="field">Arguments</label><textarea id="a-args" rows="6">{"full_name": "Ada Example", "email": "ada@example.org", "country": "PH"}</textarea>
      <p><button id="a-call">Call the tool</button></p><div id="a-out"></div></div>
    <div class="card"><h2>tools/list</h2><pre class="json" id="a-list">loading&hellip;</pre></div>`);
  const list = await post('/mcp', { jsonrpc: '2.0', id: 1, method: 'tools/list' });
  $('#a-list').textContent = JSON.stringify(list.result, null, 2);
  $('#a-call').onclick = async () => {
    $('#a-out').innerHTML = '<p class="muted">Calling&hellip; (a sandbox is starting if the grant allows it)</p>';
    try {
      const t = S.tools.find(x => x.form_id === $('#a-tool').value);
      const res = await post('/mcp', { jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: t.name, arguments: JSON.parse($('#a-args').value) } },
        { Authorization: 'Bearer ' + $('#a-token').value.trim() });
      const out = res.result || res.error;
      const text = out.content ? out.content[0].text : JSON.stringify(out);
      const run = out.structuredContent && out.structuredContent.run;
      $('#a-out').innerHTML = `<div class="banner ${out.isError ? 'no' : 'ok'}">${out.isError ? 'Refused or failed' : 'Receipt returned to the agent'}</div>
        <pre class="json">${esc(text)}</pre>${run ? `<p><a class="btn ghost" href="#run/${esc(run)}">Open it as the owner &rarr;</a></p>` : ''}`;
      await refresh();
    } catch (e) { $('#a-out').innerHTML = `<div class="banner no">${esc(e.message)}</div>`; }
  };
}

// ---------- shared ----------

function bindZoom() {
  for (const img of document.querySelectorAll('.trail img')) img.onclick = () => {
    const lb = $('#lightbox'); lb.querySelector('img').src = img.src; lb.hidden = false;
  };
}
$('#lightbox').onclick = () => { $('#lightbox').hidden = true; };

async function evidence() {
  try {
    const e = await api('/api/fixture-log');
    if (!e.available) { $('#evidence').hidden = true; return; }
    $('#evidence').hidden = false;
    const c = e.collector;
    $('#evidence').innerHTML = `<span>What the test sites actually received:</span>
      <span>form.test <b>${e.counts['form.test'] || 0}</b> requests, <b>${e.posts}</b> submissions</span>
      <span>collector.test (the exfiltration target) <span class="${c ? 'hot' : 'zero'}">${c}</span> requests</span>`;
  } catch { $('#evidence').hidden = true; }
}

async function route() {
  clearInterval(poll);
  if (!S) await refresh();
  if (S.needs_login) return loginView();
  const [name, a, b, c] = location.hash.replace(/^#/, '').split('/');
  try {
    if (name === 'tool' && a) return await toolView(a);
    if (name === 'grant') return grantView(a);
    if (name === 'run' && a === 'new') return runsView(b, c);
    if (name === 'run' && a) return runView(a);
    if (name === 'run') { await refresh(); return runsView(); }
    if (name === 'agents') return await agentsView();
    await refresh();
    return learnView();
  } catch (e) { view(`<div class="banner no">${esc(e.message)}</div>`); }
}

window.addEventListener('hashchange', route);
route();
evidence();
setInterval(evidence, 3000);
