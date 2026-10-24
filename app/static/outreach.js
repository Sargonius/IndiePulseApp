// /outreach — similar games, background runs, the creators table and the letter dialog.
(function () {
  const dataEl = document.getElementById('or_data');
  if (!dataEl) return;
  const DATA = JSON.parse(dataEl.textContent);
  const PID = DATA.project_id;
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
    {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const fmt = (n) => n == null ? '—' : n >= 1e6 ? (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + 'M'
    : n >= 1e3 ? (n / 1e3).toFixed(n >= 1e5 ? 0 : 1).replace(/\.0$/, '') + 'K' : String(n);
  const SENT = ['sent', 'replied', 'covered', 'declined'];

  async function post(url, fields) {
    const fd = new FormData();
    Object.entries(fields || {}).forEach(([k, v]) => { if (v !== undefined && v !== null) fd.append(k, v); });
    let res, data;
    try {
      res = await fetch(url, {method: 'POST', body: fd});
      data = await res.json();
    } catch (e) {
      data = {ok: false, error: res ? 'HTTP ' + res.status : 'the server didn’t respond'};
    }
    if (data && data.ok === false) alert(data.error || 'Something went wrong');
    return data || {ok: false};
  }

  async function copy(text, btn) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
    }
    if (btn) {
      const old = btn.textContent;
      btn.textContent = '✓ Copied';
      setTimeout(() => { btn.textContent = old; }, 1200);
    }
  }

  // ── Background runs ───────────────────────────────────────────────────────
  const job = {box: $('or_job'), timer: null, watching: false};

  function showJob(st) {
    if (!job.box) return;
    const recent = st.finished_at && (Date.now() / 1000 - st.finished_at) < 300;
    if (!st.running && !(recent && st.project_id === PID)) { job.box.hidden = true; return; }
    job.box.hidden = false;
    $('or_job_label').textContent = (st.label || 'Task') + (st.running ? '…' : '');
    $('or_job_msg').textContent = st.running ? (st.message || '') : '';
    const pct = st.total ? Math.round(100 * st.done / st.total) : (st.running ? 5 : 100);
    $('or_job_meter').firstElementChild.style.width = pct + '%';
    $('or_job_meter').classList.toggle('meter--done', !st.running);
    const out = $('or_job_result');
    out.className = 'or-job-result' + (st.error ? ' or-job-result--err' : '');
    out.innerHTML = st.running ? '' : esc(st.error || st.result || '') +
      (st.running ? '' : ' <button type="button" class="btn btn-sm" data-reload>Show results</button>');
    if ($('or_calls') && st.search_calls != null) $('or_calls').textContent = st.search_calls;
  }

  async function pollJob() {
    let st;
    try { st = await (await fetch('/api/outreach/job')).json(); } catch (e) { return; }
    showJob(st);
    clearTimeout(job.timer);
    if (st.running) {
      job.watching = true;
      job.timer = setTimeout(pollJob, 1500);
    } else if (job.watching && !st.error && st.project_id === PID) {
      job.watching = false;
      setTimeout(() => location.reload(), 1200);  // fresh tables with the summary on top
    }
  }

  document.addEventListener('click', async (ev) => {
    const runBtn = ev.target.closest('[data-run]');
    if (runBtn) {
      runBtn.disabled = true;
      const res = await post(`/api/outreach/${PID}/run`, {kind: runBtn.dataset.run, force: runBtn.dataset.force || 0});
      runBtn.disabled = false;
      if (res.ok) { job.watching = true; pollJob(); }
      return;
    }
    if (ev.target.closest('[data-reload]')) { location.reload(); return; }
    const bulk = ev.target.closest('[data-bulk]');
    if (bulk) {
      const res = await post(`/api/outreach/${PID}/games/bulk`, {action: bulk.dataset.bulk, top: 10});
      if (res.ok) location.reload();
      return;
    }
    const restore = ev.target.closest('[data-restore]');
    if (restore) {
      const res = await post(`/api/outreach/games/${restore.dataset.restore}`, {status: 'suggested'});
      if (res.ok) location.reload();
      return;
    }
    const reject = ev.target.closest('[data-reject]');
    if (reject) {
      const tr = reject.closest('[data-game]');
      const res = await post(`/api/outreach/games/${tr.dataset.game}`, {status: 'rejected'});
      if (res.ok) tr.remove();
    }
  });

  // ── Similar games table ───────────────────────────────────────────────────
  document.querySelectorAll('[data-game]').forEach((tr) => {
    const id = tr.dataset.game;
    const use = tr.querySelector('[data-use]');
    use && use.addEventListener('change', async () => {
      const res = await post(`/api/outreach/games/${id}`, {status: use.checked ? 'active' : 'suggested'});
      if (res.ok) tr.className = 'or-game--' + res.status;
      else use.checked = !use.checked;
    });
    const q = tr.querySelector('[data-query]');
    if (q) {
      let saved = q.value;
      const save = async () => {
        if (q.value === saved) return;
        const res = await post(`/api/outreach/games/${id}`, {search_query: q.value});
        if (res.ok) { saved = q.value; q.classList.add('or-saved'); setTimeout(() => q.classList.remove('or-saved'), 900); }
      };
      q.addEventListener('blur', save);
      q.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); save(); } });
    }
  });

  const addGame = $('or_add_game');
  addGame && addGame.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const btn = addGame.querySelector('button');
    btn.disabled = true;
    btn.textContent = 'Adding…';
    const res = await post(`/api/outreach/${PID}/games`, {text: addGame.elements.text.value});
    btn.disabled = false;
    btn.textContent = '+ Add';
    if (res.ok) location.reload();
  });

  // ── Creators table filters ────────────────────────────────────────────────
  const table = $('or_creators');
  const rows = table ? [...table.querySelectorAll('tbody tr')] : [];
  const filt = {status: 'open'};
  const OPEN = ['found', 'shortlist', 'drafted'];

  function applyFilters() {
    const withEmail = $('or_f_email') && $('or_f_email').checked;
    const lang = $('or_f_lang') ? $('or_f_lang').value : '';
    const subs = $('or_f_subs') ? +$('or_f_subs').value : 0;
    const text = $('or_f_text') ? $('or_f_text').value.trim().toLowerCase() : '';
    let n = 0;
    rows.forEach((tr) => {
      const d = tr.dataset;
      let ok = filt.status === 'all' ? true
        : filt.status === 'open' ? OPEN.includes(d.status)
        : filt.status === 'followup' ? d.followup === '1'
        : d.status === filt.status;
      if (ok && withEmail) ok = d.email === '1';
      if (ok && lang) ok = d.lang === lang;
      if (ok && subs) ok = +d.subs >= subs;
      if (ok && text) ok = d.text.includes(text);
      tr.hidden = !ok;
      n += ok ? 1 : 0;
    });
    if ($('or_f_count')) $('or_f_count').textContent = n + ' shown';
  }

  function setStatusFilter(status) {
    filt.status = status;
    document.querySelectorAll('#or_status_chips [data-status]').forEach((c) =>
      c.classList.toggle('active', c.dataset.status === status));
    applyFilters();
  }

  document.querySelectorAll('#or_status_chips [data-status]').forEach((c) =>
    c.addEventListener('click', () => setStatusFilter(c.dataset.status)));
  ['or_f_email', 'or_f_lang', 'or_f_subs'].forEach((id) => $(id) && $(id).addEventListener('change', applyFilters));
  $('or_f_text') && $('or_f_text').addEventListener('input', applyFilters);
  document.querySelectorAll('.or-step[data-filter]').forEach((a) =>
    a.addEventListener('click', () => setStatusFilter(a.dataset.filter)));
  applyFilters();

  rows.forEach((tr) => {
    const open = (ev) => {
      if (ev.target.closest('a')) return;  // video links open YouTube
      openContact(+tr.dataset.contact);
    };
    tr.addEventListener('click', open);
    tr.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') open(ev); });
  });

  function updateRow(c) {
    const tr = table && table.querySelector(`tr[data-contact="${c.id}"]`);
    if (!tr) return;
    tr.dataset.status = c.status;
    tr.dataset.email = c.email ? '1' : '0';
    tr.dataset.followup = c.followup_due ? '1' : '0';
    tr.cells[0].querySelector('.or-score').textContent = c.score;
    tr.cells[4].innerHTML = c.email ? `<span title="${esc(c.email)}">✉ ${esc(c.email.length > 26 ? c.email.slice(0, 25) + '…' : c.email)}</span>`
      : '<span class="muted">—</span>';
    tr.cells[5].innerHTML = `<span class="or-status or-status--${esc(c.status)}">${esc(DATA.statuses[c.status] || c.status)}</span>` +
      (c.followup_due ? ' <span class="or-status or-status--due">follow-up</span>' : '');
    if (c.creator.ignored) tr.remove();
  }

  // ── Contact dialog ────────────────────────────────────────────────────────
  const dlg = $('or_dialog');
  const body = $('or_dlg_body');
  let cur = null;  // the contact shown

  async function openContact(id) {
    body.innerHTML = '<p class="muted">Loading…</p>';
    if (!dlg.open) dlg.showModal();
    let c;
    try { c = await (await fetch(`/api/outreach/contacts/${id}`)).json(); } catch (e) { c = null; }
    if (!c || c.ok === false) { body.innerHTML = '<p>Couldn’t load this creator.</p>'; return; }
    render(c);
  }

  function visibleIds() {
    return rows.filter((tr) => !tr.hidden && tr.isConnected).map((tr) => +tr.dataset.contact);
  }

  function render(c, warnings) {
    cur = c;
    updateRow(c);
    const cr = c.creator;
    const meta = [cr.subscribers != null ? fmt(cr.subscribers) + ' subs' : null, cr.language, cr.country,
                  cr.last_upload ? 'last upload ' + cr.last_upload : null].filter(Boolean).join(' · ');
    const emails = c.emails.length ? c.emails.map((e) => `
        <label class="or-em ${e.shared ? 'or-em--shared' : ''}">
          <input type="radio" name="or_pick" value="${esc(e.email)}" ${e.email === c.email ? 'checked' : ''}>
          <span><b>${esc(e.email)}</b> <span class="muted">${esc(sourceLabel(e.source))} · ${esc(e.found_at || '')}</span>
          ${e.shared ? '<span class="or-flag" title="Seen on several unrelated channels — probably a sponsor’s address">sponsor?</span>' : ''}</span>
        </label>`).join('')
      : `<p class="muted or-small">No public email yet.${cr.about_url ? ` On YouTube, open the <a href="${esc(cr.about_url)}" target="_blank" rel="noopener">About page ↗</a> → “View email address” (it asks for a captcha), then paste it below.` : cr.platform === 'twitch' ? ' On Twitch, business emails are usually in the panels under the stream.' : ''}</p>`;
    const videos = c.videos.length ? c.videos.map((v) => `
        <li class="${v.this_project ? '' : 'muted'}"><span class="or-vgame">${esc(v.game)}</span>
          <a href="${esc(v.url)}" target="_blank" rel="noopener">${esc(v.title)}</a>
          <span class="muted">${v.views != null ? fmt(v.views) + ' views · ' : ''}${esc(v.published || '')}</span></li>`).join('')
      : '<li class="muted">Added by hand — no videos found by search.</li>';
    const links = cr.links.length ? cr.links.map((u) => `<li><a href="${esc(u)}" target="_blank" rel="noopener">${esc(u.replace(/^https?:\/\/(www\.)?/, '').slice(0, 60))}</a></li>`).join('') : '<li class="muted">none</li>';
    const sent = SENT.includes(c.status);
    const ids = visibleIds();
    const pos = ids.indexOf(c.id);
    const statusBtns = ['found', 'shortlist', 'drafted', 'sent', 'replied', 'covered', 'declined', 'skipped']
      .map((s) => `<button type="button" class="chip ${s === c.status ? 'active' : ''}" data-set-status="${s}">${esc(DATA.statuses[s])}</button>`).join('');
    body.innerHTML = `
      <div class="or-dlg-head">
        ${cr.avatar_url ? `<img src="${esc(cr.avatar_url)}" alt="" referrerpolicy="no-referrer">` : `<span class="or-avatar-ph">${esc(cr.name.slice(0, 1))}</span>`}
        <div class="or-dlg-title">
          <div><b>${esc(cr.name)}</b> <span class="or-platform or-platform--${esc(cr.platform)}">${esc(cr.platform)}</span>
            ${cr.url ? `<a href="${esc(cr.url)}" target="_blank" rel="noopener">channel ↗</a>` : ''}
            ${cr.about_url ? ` · <a href="${esc(cr.about_url)}" target="_blank" rel="noopener">about ↗</a>` : ''}</div>
          <div class="muted">${esc(meta)}</div>
        </div>
        <div class="or-dlg-nav">
          <button type="button" class="btn btn-sm" data-nav="-1" ${pos > 0 ? '' : 'disabled'} title="Previous in the list">←</button>
          <button type="button" class="btn btn-sm" data-nav="1" ${pos >= 0 && pos < ids.length - 1 ? '' : 'disabled'} title="Next in the list">→</button>
          <button type="button" class="btn btn-sm" data-close title="Close (Esc)">✕</button>
        </div>
      </div>
      <div class="or-dlg-grid">
        <div class="or-dlg-side">
          <div class="or-score-big" title="${esc(c.score_parts.join('\n'))}"><span class="or-score" style="--s:${c.score}%">${c.score}</span>
            <ul class="or-parts">${c.score_parts.map((p) => `<li>${esc(p)}</li>`).join('')}</ul></div>
          <h4>Videos</h4><ul class="or-videos">${videos}</ul>
          <h4>Emails</h4><div class="or-emails">${emails}</div>
          <div class="or-inline"><input type="text" id="or_add_email" placeholder="paste an email"><button type="button" class="btn btn-sm" data-add-email>Add</button></div>
          <h4>Links ${cr.links_scanned ? `<span class="muted">· checked ${esc(cr.links_scanned)}</span>` : ''}</h4>
          <ul class="or-links">${links}</ul>
          ${cr.links.length ? '<button type="button" class="btn btn-sm" data-scan>🔗 Look for emails on these sites</button>' : ''}
          ${cr.description ? `<details class="or-desc"><summary class="muted">Channel description</summary><div>${esc(cr.description)}</div></details>` : ''}
          <h4>About this creator <span class="muted">(all projects)</span></h4>
          <textarea id="or_cr_notes" rows="2" placeholder="streams on weekends, likes colony sims…">${esc(cr.notes)}</textarea>
          <div class="or-flags">
            <label><input type="checkbox" data-flag="do_not_contact" ${cr.do_not_contact ? 'checked' : ''}> Don’t contact (asked not to)</label>
            <label><input type="checkbox" data-flag="ignored" ${cr.ignored ? 'checked' : ''}> Hide this channel everywhere</label>
          </div>
        </div>
        <div class="or-dlg-main">
          <div class="chip-row or-status-row">${statusBtns}</div>
          ${c.sent_at ? `<p class="muted or-small">Sent ${esc(c.sent_at)}${c.followup_at ? ' · follow-up due ' + esc(c.followup_at) : ''}${c.followups ? ' · follow-up sent' : ''}${c.replied_at ? ' · replied ' + esc(c.replied_at) : ''}</p>` : ''}
          ${cr.do_not_contact ? '<p class="or-warn">This creator asked not to be contacted.</p>' : ''}
          <div class="or-letter-head"><h4>Letter <span class="muted">· in ${esc(c.letter_language)}</span></h4>
            <button type="button" class="btn ${c.body ? '' : 'btn-primary'}" data-draft ${cr.do_not_contact ? 'disabled' : ''}>${c.body ? '↻ Rewrite draft' : '✨ Write draft'}</button></div>
          ${(warnings || []).map((w) => `<p class="or-warn">${esc(w)}</p>`).join('')}
          <label class="or-field">To <input type="text" id="or_to" value="${esc(c.email || '')}" placeholder="no email yet"></label>
          <label class="or-field">Subject <input type="text" id="or_subject" value="${esc(c.subject)}" maxlength="300"></label>
          <textarea id="or_body" rows="13" placeholder="Click “Write draft” — or write it yourself.">${esc(c.body)}</textarea>
          <p class="muted or-small">${c.key ? `Steam key <code>${esc(c.key.key)}</code> · ${esc(c.key.status)}${c.key.batch ? ' · ' + esc(c.key.batch) : ''}` : 'No key attached yet — writing the draft takes one from the pool.'}</p>
          <div class="or-actions">
            <button type="button" class="btn" data-save>💾 Save</button>
            <button type="button" class="btn" data-copy-field="or_to">📋 Address</button>
            <button type="button" class="btn" data-copy-field="or_subject">📋 Subject</button>
            <button type="button" class="btn" data-copy-field="or_body">📋 Text</button>
            <button type="button" class="btn" data-gmail title="Opens a Gmail draft with the address, subject and text filled in">Open in Gmail ↗</button>
            ${sent ? '' : '<button type="button" class="btn btn-primary" data-set-status="sent" title="After you’ve pressed Send in Gmail">✓ Mark as sent</button>'}
          </div>
          ${sent && c.status === 'sent' ? `
          <div class="or-followup">
            <h4>No answer?</h4>
            <p class="muted or-small">One short follow-up after ${c.followup_at ? 'about a week' : 'a week'} is fine; more is spam.</p>
            <button type="button" class="btn btn-sm" data-followup ${c.followups ? 'disabled' : ''}>Follow-up text</button>
            <div id="or_fu"></div>
          </div>` : ''}
          ${c.status === 'covered' || sent ? `<label class="or-field">Coverage link <input type="url" id="or_cov" value="${esc(c.coverage_url)}" placeholder="https://www.youtube.com/watch?v=…"></label>
            <button type="button" class="btn btn-sm" data-covered>🎉 Mark as covered</button>` : ''}
          <h4>Notes for this game</h4>
          <textarea id="or_notes" rows="2" placeholder="replied: will stream on Friday">${esc(c.notes)}</textarea>
        </div>
      </div>`;
  }

  function sourceLabel(src) {
    const [kind, rest] = String(src || '').split(/:(.*)/s);
    return {channel: 'channel description', video: 'video description', link: 'site ' + (rest || ''),
            manual: 'added by hand', csv: 'CSV ' + (rest || '')}[kind] || src;
  }

  function letterFields() {
    return {email: $('or_to').value, subject: $('or_subject').value, body: $('or_body').value,
            notes: $('or_notes') ? $('or_notes').value : undefined};
  }

  async function saveLetter(extra) {
    const res = await post(`/api/outreach/contacts/${cur.id}`, {...letterFields(), ...(extra || {})});
    if (res.ok) render(res);
    return res;
  }

  body.addEventListener('click', async (ev) => {
    if (!cur) return;
    const t = ev.target;
    if (t.closest('[data-close]')) { dlg.close(); return; }
    const nav = t.closest('[data-nav]');
    if (nav) {
      const ids = visibleIds();
      const next = ids[ids.indexOf(cur.id) + +nav.dataset.nav];
      if (next) { await saveQuiet(); openContact(next); }
      return;
    }
    const st = t.closest('[data-set-status]');
    if (st) {
      if (st.dataset.setStatus === 'sent' && !confirm('Mark as sent? Do it after pressing Send in Gmail.')) return;
      await saveLetter({status: st.dataset.setStatus});
      return;
    }
    if (t.closest('[data-draft]')) {
      if (cur.body && !confirm('Replace the current text with a new draft?')) return;
      const btn = t.closest('[data-draft]');
      btn.disabled = true;
      btn.textContent = '✨ Writing…';
      await post(`/api/outreach/contacts/${cur.id}`, {email: $('or_to').value});
      const res = await post(`/api/outreach/contacts/${cur.id}/draft`, {});
      if (res.ok) render(res, res.warnings);
      else { btn.disabled = false; btn.textContent = '✨ Write draft'; }
      return;
    }
    if (t.closest('[data-save]')) { await saveLetter(); return; }
    const cf = t.closest('[data-copy-field]');
    if (cf) { copy($(cf.dataset.copyField).value, cf); return; }
    if (t.closest('[data-gmail]')) {
      await saveLetter();
      const f = letterFields();
      if (!f.email) { alert('No address yet — add the creator’s email first.'); return; }
      window.open('https://mail.google.com/mail/?view=cm&fs=1&to=' + encodeURIComponent(f.email) +
                  '&su=' + encodeURIComponent(f.subject) + '&body=' + encodeURIComponent(f.body), '_blank', 'noopener');
      return;
    }
    if (t.closest('[data-add-email]')) {
      const v = $('or_add_email').value.trim();
      if (!v) return;
      const res = await post(`/api/outreach/creators/${cur.creator.id}`, {add_email: v, project_id: PID});
      if (res.ok) {
        if (!$('or_to').value) await post(`/api/outreach/contacts/${cur.id}`, {email: v});
        openContact(cur.id);
      }
      return;
    }
    if (t.closest('[data-scan]')) {
      const btn = t.closest('[data-scan]');
      btn.disabled = true;
      btn.textContent = 'Checking…';
      const res = await post(`/api/outreach/creators/${cur.creator.id}/scan`, {project_id: PID});
      if (res.ok) {
        alert(res.found ? `Found ${res.found} new email${res.found > 1 ? 's' : ''}.`
          : `No emails on ${res.checked && res.checked.length ? res.checked.join(', ') : 'those sites'}.`);
        openContact(cur.id);
      }
      return;
    }
    if (t.closest('[data-followup]')) {
      const res = await post(`/api/outreach/contacts/${cur.id}/followup`, {});
      if (!res.ok) return;
      $('or_fu').innerHTML = `
        <label class="or-field">Subject <input type="text" id="or_fu_subject" value="${esc(res.subject)}"></label>
        <textarea id="or_fu_body" rows="5">${esc(res.body)}</textarea>
        <div class="or-actions">
          <button type="button" class="btn btn-sm" data-copy-field="or_fu_subject">📋 Subject</button>
          <button type="button" class="btn btn-sm" data-copy-field="or_fu_body">📋 Text</button>
          ${res.gmail_url ? `<a class="btn btn-sm" href="${esc(res.gmail_url)}" target="_blank" rel="noopener">Open in Gmail ↗</a>` : ''}
          <button type="button" class="btn btn-sm btn-primary" data-fu-sent>✓ Follow-up sent</button>
        </div>
        <p class="muted or-small">Best: reply in the same Gmail thread so they see the first letter (and the key) below.</p>`;
      return;
    }
    if (t.closest('[data-fu-sent]')) {
      const res = await post(`/api/outreach/contacts/${cur.id}/followup`, {mark_sent: 1});
      if (res.ok) render(res);
      return;
    }
    if (t.closest('[data-covered]')) {
      await saveLetter({status: 'covered', coverage_url: $('or_cov').value});
    }
  });

  body.addEventListener('change', async (ev) => {
    if (!cur) return;
    const t = ev.target;
    if (t.name === 'or_pick') {
      $('or_to').value = t.value;
      await saveLetter();
      return;
    }
    if (t.dataset.flag) {
      if (t.dataset.flag === 'ignored' && t.checked && !confirm('Hide this channel in every project?')) { t.checked = false; return; }
      const res = await post(`/api/outreach/creators/${cur.creator.id}`, {[t.dataset.flag]: t.checked ? 1 : 0, project_id: PID});
      if (res.ok) openContact(cur.id);
      return;
    }
    if (t.id === 'or_cr_notes') {
      await post(`/api/outreach/creators/${cur.creator.id}`, {notes: t.value});
    }
  });

  async function saveQuiet() {
    if (!cur || !$('or_body')) return;
    const f = letterFields();
    if (f.body === (cur.body || '') && f.subject === (cur.subject || '') && f.email === (cur.email || '')
        && (f.notes || '') === (cur.notes || '')) return;
    await post(`/api/outreach/contacts/${cur.id}`, f);
  }

  dlg.addEventListener('close', () => { saveQuiet(); cur = null; });
  dlg.addEventListener('click', (ev) => { if (ev.target === dlg) dlg.close(); });  // backdrop

  pollJob();
})();
