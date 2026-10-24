// /tiktok — the TikTok pipeline page. Data: window.TT_CTX (app/services/tiktok_pipeline.py
// page_context), refreshed from /api/tiktok/state?sync=1. Item cards update in place:
// the caption fields and the video are never redrawn under the cursor.
(function () {
	'use strict';
	let S = window.TT_CTX || {};
	const cards = new Map();   // item id → {el, item, sigVid, sigFlow}
	const dirty = new Map();   // item id → timer of an unsaved caption edit
	const LANG_LABEL = { ja: 'JA', en: 'EN', ru: 'RU' };
	const STAGE_LABEL = { rendering: 'Rendering', ready: 'Ready', sending: 'Uploading', inbox: 'In TikTok drafts',
		live: 'Live', render_failed: 'Render failed', send_failed: 'Not sent' };
	const GROUPS = [['prep', 'Before sending'], ['app', 'In the TikTok app'], ['after', 'After posting']];
	const OPEN_GROUP = { rendering: 'prep', ready: 'prep', render_failed: 'prep', send_failed: 'prep',
		sending: 'app', inbox: 'app', live: 'after' };

	const $ = sel => document.querySelector(sel);
	const esc = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
	function fmtNum(v) {
		if (v === null || v === undefined || isNaN(v)) return '—';
		const a = Math.abs(v);
		if (a >= 10000) return (v / 1000).toFixed(a >= 100000 ? 0 : 1).replace(/\.0$/, '') + 'K';
		return Number(v).toLocaleString('en-US', { maximumFractionDigits: 1 });
	}
	function fmtHours(h) {
		if (h < 1) return Math.round(h * 60) + ' min';
		if (h < 48) return (h < 10 ? h.toFixed(1).replace(/\.0$/, '') : Math.round(h)) + ' h';
		return Math.round(h / 24) + ' d';
	}
	function toast(msg, err) {
		const t = document.createElement('div');
		t.className = 'tt-toast' + (err ? ' err' : '');
		t.textContent = msg;
		document.body.appendChild(t);
		setTimeout(() => t.remove(), err ? 7000 : 3500);
	}
	async function api(method, url, body) {
		const r = await fetch(url, { method, headers: body ? { 'Content-Type': 'application/json' } : {},
			body: body ? JSON.stringify(body) : undefined });
		let j = null;
		try { j = await r.json(); } catch (e) { /* not JSON */ }
		if (!r.ok) throw new Error((j && (typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail))) || ('HTTP ' + r.status));
		return j;
	}
	const lang = () => { try { return localStorage.getItem('tt_lang') || 'ja'; } catch (e) { return 'ja'; } };

	// ── Japan clock ───────────────────────────────────────────────────────────
	function jstParts(d) {
		const h = (d.getUTCHours() + 9) % 24;
		return { h, m: d.getUTCMinutes() };
	}
	function localAtJst(hour) {  // today's hour:00 in Japan, in this browser's local time
		const d = new Date();
		const t = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), hour - 9, 0));
		return t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
	}
	function renderClock() {
		const [lo, hi] = S.jp_evening || [19, 23];
		const now = new Date();
		const { h, m } = jstParts(now);
		const prime = h >= lo && h < hi;
		let next = '';
		if (!prime) {
			let mins = ((lo - h + 24) % 24) * 60 - m;
			if (mins <= 0) mins += 24 * 60;
			next = `Japan’s evening starts in ${Math.floor(mins / 60)} h ${mins % 60} min.`;
		}
		$('#tt_clock').innerHTML =
			`<span class="tt-lbl">Time in Japan</span>` +
			`<span class="tt-jst">${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}</span>` +
			(prime ? `<span class="tt-prime">✓ Evening in Japan: a good time to post</span>` : `<span>${esc(next)}</span>`) +
			`<span class="muted tt-small">Best: ${lo}:00–${hi}:00 in Japan = ${localAtJst(lo)}–${localAtJst(hi)} your time.</span>`;
	}

	// ── account, connection, summary ─────────────────────────────────────────
	function renderAccount() {
		const c = S.connection || {}, a = S.account || {};
		const box = $('#tt_account');
		if (!c.connected) { box.innerHTML = ''; return; }
		const chips = [['video.upload', 'drafts'], ['video.list', 'views'], ['user.info.stats', 'followers']]
			.map(([s, l]) => `<span class="tt-chip ${c.scopes.includes(s) ? 'on' : 'off'}" title="${esc(s)}">${c.scopes.includes(s) ? '✓' : '✕'} ${l}</span>`).join(' ');
		box.innerHTML =
			(a.avatar_url ? `<img src="${esc(a.avatar_url)}" alt="" referrerpolicy="no-referrer">` : '') +
			`<div><div class="tt-acc-name">${esc(a.display_name || 'TikTok account')}${a.username ? ` <span class="muted">@${esc(a.username)}</span>` : ''}</div>` +
			`<div class="muted tt-small">${a.follower_count != null ? fmtNum(a.follower_count) + ' followers · ' : ''}${chips}</div>` +
			(a.error ? `<div class="tt-err">${esc(a.error)}</div>` : '') + `</div>`;
	}

	function renderConnect() {
		const c = S.connection || {};
		const box = $('#tt_connect');
		const statsBox = `<label class="tt-inline"><input type="checkbox" id="tt_want_stats" ${c.want_stats ? 'checked' : ''}>
			Also ask for <code>video.list</code>, <code>user.info.stats</code>, <code>user.info.profile</code> (views, followers, @handle)</label>`;
		if (!c.configured) {
			box.innerHTML = `<div class="tt-connect"><b>TikTok isn’t set up.</b>
				<span>Put the TikTok app’s Client Key and Secret into <a href="/accounts#section_tiktok">⚙️ Settings → TikTok</a>, then come back here to connect the account.</span></div>`;
			return;
		}
		if (!c.connected) {
			box.innerHTML = `<div class="tt-connect"><b>Connect the new TikTok account</b>
				<ol>
					<li>In <a href="https://developers.tiktok.com/apps" target="_blank" rel="noopener">developers.tiktok.com</a> → your app → <b>Sandbox</b> → Target users: add the new account.</li>
					<li>For views and followers on this page, add the scopes <code>video.list</code>, <code>user.info.stats</code> and <code>user.info.profile</code> to the app, then tick the box below. Leave it off if they aren’t added: TikTok’s consent page fails on a scope the app doesn’t have.</li>
					<li>Connect, sign in with the <b>new</b> account and allow everything.</li>
				</ol>
				<div class="tt-row">${statsBox}<button type="button" class="btn btn-primary" id="tt_connect_btn">Connect TikTok</button></div></div>`;
		} else if (/access_token_invalid|invalid_grant|token/i.test((S.account || {}).error || '')) {
			box.innerHTML = `<div class="tt-connect"><span><b>TikTok refused the stored token</b> (expired or revoked). Drafts and status checks stop until you reconnect.</span>
				<div class="tt-row">${statsBox}<button type="button" class="btn btn-primary" id="tt_connect_btn">Reconnect</button></div></div>`;
		} else if (!c.views || !c.followers) {
			box.innerHTML = `<div class="tt-connect ok"><span><b>Drafts work.</b> Views${c.followers ? '' : ' and followers'} stay empty: this connection has no <code>${c.views ? 'user.info.stats' : 'video.list'}</code>.
				Add the scopes to the app in the developer portal, tick the box and reconnect.</span>
				<div class="tt-row">${statsBox}<button type="button" class="btn" id="tt_connect_btn">Reconnect</button></div></div>`;
		} else {
			box.innerHTML = '';
			return;
		}
		$('#tt_want_stats').addEventListener('change', async e => {
			try { S.connection = await api('POST', '/api/tiktok/settings', { want_stats: e.target.checked }); }
			catch (err) { toast(err.message, true); }
		});
		$('#tt_connect_btn').addEventListener('click', async () => {
			try { location.href = (await api('GET', '/api/tiktok/connect_url')).url; }
			catch (err) { toast(err.message, true); }
		});
	}

	function renderSummary() {
		const s = S.summary || {}, f = S.followers || {}, a = S.account || {};
		const tiles = [];
		tiles.push(`<div class="tt-tile"><span class="tt-k">Live clips</span><span class="tt-v">${s.live || 0}</span>
			<span class="tt-s">${s.inbox || 0} in TikTok drafts · ${s.ready || 0} ready</span></div>`);
		tiles.push(`<div class="tt-tile"><span class="tt-k">Views</span><span class="tt-v">${fmtNum(s.views_total)}</span>
			<span class="tt-s">${s.views_median != null ? 'median ' + fmtNum(s.views_median) + ' per clip' : (S.connection && S.connection.views ? 'none yet' : 'needs the video.list scope')}</span></div>`);
		let verdict = '', cls = '';
		if (s.jp_n >= s.jp_needed) {
			const good = s.jp_avg >= s.target_share;
			cls = good ? 'good' : 'bad';
			verdict = good ? 'Japanese pulls Japan: keep going' : `Under ${s.target_share}%: the region wins`;
		} else if (s.jp_n >= 3) {
			verdict = `Early: ${s.jp_n} of ${s.jp_needed} clips`;
		} else {
			verdict = `Type it in for ${s.jp_needed} clips (TikTok Studio)`;
		}
		tiles.push(`<div class="tt-tile"><span class="tt-k">Japan share of viewers</span><span class="tt-v">${s.jp_avg != null ? s.jp_avg + '%' : '—'}</span>
			<span class="tt-verdict ${cls}">${esc(verdict)}</span><span class="tt-s">target ≥ ${s.target_share}% · ${s.jp_n || 0} clip${s.jp_n === 1 ? '' : 's'} typed in</span></div>`);
		const fc = a.follower_count != null ? a.follower_count : (f.values && f.values.length ? f.values[f.values.length - 1] : null);
		tiles.push(`<div class="tt-tile"><span class="tt-k">Followers</span><span class="tt-v">${fmtNum(fc)}</span>
			<span class="tt-s">${s.followers_gained != null ? (s.followers_gained >= 0 ? '+' : '') + fmtNum(s.followers_gained) + ' since the first clip' : (S.connection && S.connection.followers ? 'read every 2 hours' : 'needs user.info.stats')}</span>
			${f.values && f.values.length > 1 ? `<div class="spark" data-spark='${esc(JSON.stringify(f.values))}' data-days='${esc(JSON.stringify(f.days))}' data-kind="level" data-label="followers"></div>` : ''}</div>`);
		$('#tt_summary').innerHTML = tiles.join('');
		document.querySelectorAll('#tt_summary .spark').forEach(el => window.IPChart && IPChart.sparkline(el));
	}

	// ── candidates and drops ─────────────────────────────────────────────────
	function renderCands() {
		const box = $('#tt_cands');
		const list = S.candidates || [];
		if (!list.length) { box.innerHTML = '<span class="muted tt-small">No rendered Shorts yet: make some in 🎞️ Shorts, or drop a video below.</span>'; return; }
		box.innerHTML = list.map(c => `<div class="tt-cand${c.item_id ? ' in' : ''}" data-brief="${c.brief_id}">
			${c.poster_url ? `<img class="tt-cand-img" src="${esc(c.poster_url)}" alt="" loading="lazy">` : `<div class="tt-cand-img"></div>`}
			<div class="tt-cand-body">
				<span class="tt-cand-title" title="${esc(c.title)}">${esc(c.title || 'Untitled')}</span>
				<span class="tt-cand-meta">${c.views != null ? '▶ ' + fmtNum(c.views) + ' on YouTube' : 'not on YouTube'}</span>
				<span class="tt-cand-meta">${esc(c.project || '')}${c.has_music ? ' · ♪ music comes off' : ''}</span>
				${c.item_id ? `<button type="button" class="btn btn-sm" data-goto="${c.item_id}">In the list ↓</button>`
					: `<button type="button" class="btn btn-sm btn-primary" data-add="${c.brief_id}">＋ Add</button>`}
			</div></div>`).join('');
	}
	async function addBrief(btn, briefId) {
		btn.disabled = true;
		btn.textContent = 'Preparing…';
		try {
			const res = await api('POST', '/api/tiktok/items', { brief_id: briefId, lang: lang() });
			upsert(res.item, true);
			const c = (S.candidates || []).find(x => x.brief_id === briefId);
			if (c) c.item_id = res.item.id;
			renderCands();
			(res.notes || []).forEach(n => toast(n, true));
			focusItem(res.item.id);
		} catch (e) {
			toast(e.message, true);
			btn.disabled = false;
			btn.textContent = '＋ Add';
		}
	}
	function focusItem(id) {
		const c = cards.get(id);
		if (!c) return;
		c.el.scrollIntoView({ behavior: 'smooth', block: 'start' });
		c.el.classList.add('flash');
		setTimeout(() => c.el.classList.remove('flash'), 1600);
	}
	function uploadFile(file) {
		const msg = $('#tt_drop_msg');
		const fd = new FormData();
		fd.append('file', file);
		const xhr = new XMLHttpRequest();
		xhr.open('POST', '/api/shorts/upload');
		xhr.upload.onprogress = e => { if (e.lengthComputable) msg.textContent = `Uploading ${file.name}… ${Math.round(e.loaded / e.total * 100)}%`; };
		xhr.onerror = () => { msg.textContent = ''; toast('Upload failed', true); };
		xhr.onload = async () => {
			let j = {};
			try { j = JSON.parse(xhr.responseText); } catch (e) { /* */ }
			if (xhr.status >= 400) { msg.textContent = ''; toast(j.detail || 'Upload failed', true); return; }
			msg.textContent = 'Preparing…';
			try {
				const pid = $('#tt_project').value;
				const res = await api('POST', '/api/tiktok/items', { rel: j.rel, project_id: pid ? +pid : null, lang: lang() });
				msg.textContent = '';
				upsert(res.item, true);
				focusItem(res.item.id);
			} catch (e) { msg.textContent = ''; toast(e.message, true); }
		};
		xhr.send(fd);
	}

	// ── item cards ───────────────────────────────────────────────────────────
	function sigVid(it) { return [it.video_url, it.stage === 'rendering' ? Math.round((it.render && it.render.progress || 0) * 20) : 0, it.stage, it.poster_url].join('|'); }
	function sigFlow(it) { const x = Object.assign({}, it); delete x.caption; delete x.tags; delete x.full_caption; return JSON.stringify(x); }

	function vidHtml(it) {
		let frame;
		if (it.stage === 'rendering') {
			const p = Math.round(((it.render && it.render.progress) || 0) * 100);
			frame = `<div class="tt-frame">${it.poster_url ? `<img src="${esc(it.poster_url)}" alt="" style="width:100%;height:100%;object-fit:cover;opacity:.35">` : ''}
				<span style="position:absolute">Rendering the TikTok version… ${p}%</span><div class="tt-prog"><i style="width:${p}%"></i></div></div>`;
		} else if (it.video_url) {
			frame = `<div class="tt-frame"><video src="${esc(it.video_url)}" ${it.poster_url ? `poster="${esc(it.poster_url)}"` : ''} controls playsinline preload="metadata"></video></div>`;
		} else {
			frame = `<div class="tt-frame">no video file</div>`;
		}
		const locked = ['sending', 'inbox', 'live'].includes(it.stage);
		const btns = [];
		if (it.video_url && !locked) btns.push(`<button type="button" class="btn btn-sm" data-act="edit" title="Crop, trim, frame text: the shared video editor">✂ Edit</button>`);
		if (it.video_url) btns.push(`<a class="btn btn-sm" href="${esc(it.video_url)}" download>⬇ mp4</a>`);
		const meta = it.duration ? `<span class="muted tt-small">${Math.round(it.duration)} s · ${it.has_audio ? 'game audio' : 'silent'}</span>` : '';
		return frame + btns.join('') + meta;
	}

	function captionHtml(it) {
		const langs = (S.langs || []).map(l => `<button type="button" data-lang="${l.key}" class="${l.key === it.lang ? 'on' : ''}" title="${esc(l.name)}: translates the caption and the frame text">${LANG_LABEL[l.key] || l.key}</button>`).join('');
		return `<div class="tt-row" style="display:flex;align-items:center;gap:8px;"><span class="tt-lbl">Caption</span><span class="tt-langs">${langs}</span><span class="muted tt-small tt-capmsg"></span></div>
			<textarea class="tt-cap" maxlength="${it.caption_limit || 4000}" placeholder="The hook in words, then the game’s name and “on Steam”.">${esc(it.caption)}</textarea>
			<span class="tt-lbl">Hashtags</span>
			<input type="text" class="tt-tags" value="${esc(it.tags)}" placeholder="#インディーゲーム #個人開発">
			<div class="tt-capfoot"><span class="tt-capcount"></span><span class="tt-sp"></span>
				<button type="button" class="btn btn-sm" data-act="copy">📋 Copy caption</button></div>`;
	}

	function checksHtml(it, opened) {
		const open = OPEN_GROUP[it.stage] || 'prep';
		return GROUPS.map(([g, title]) => {
			const rows = (it.checks || []).filter(c => c.stage === g);
			if (!rows.length) return '';
			const done = rows.filter(c => c.state === 'ok').length;
			const counted = rows.filter(c => c.state !== 'na').length;
			const isOpen = opened && g in opened ? opened[g] : g === open;
			return `<details data-group="${g}" ${isOpen ? 'open' : ''}><summary>${title}<span class="tt-count">${done}/${counted}</span></summary>` +
				rows.map(c => {
					const ic = { ok: '✓', warn: '⚠', todo: '○', na: '–' }[c.state] || '○';
					const fix = c.action === 'screen_text' ? `<button type="button" class="btn btn-sm" data-act="screen_text">Translate to ${LANG_LABEL[it.lang] || it.lang}</button>`
						: c.action === 'drop_music' ? `<button type="button" class="btn btn-sm" data-act="drop_music">Render without music</button>` : '';
					const tick = (!c.auto || (c.state !== 'ok' && c.state !== 'na') || c.ticked)
						? `<input type="checkbox" data-tick="${c.key}" ${c.ticked ? 'checked' : ''} title="${c.auto ? 'Checked it myself' : 'Done'}">` : '';
					return `<div class="tt-ck ${c.state}"><span class="tt-ic">${c.auto ? ic : (tick || ic)}</span>
						<span>${esc(c.label)}${c.note ? `<span class="tt-note">${esc(c.note)}</span>` : ''}${c.state !== 'ok' && c.hint ? `<span class="tt-hint">${esc(c.hint)}</span>` : ''}</span>
						<span>${fix}${c.auto && tick ? tick : ''}</span></div>`;
				}).join('') + `</details>`;
		}).join('');
	}

	function linkRow(label) {
		return `<div class="tt-row"><input type="url" class="tt-link" placeholder="https://www.tiktok.com/@you/video/…"><button type="button" class="btn btn-sm" data-act="link">${label}</button></div>`;
	}

	function linkBox(summary, opened) {
		return `<details data-group="link" ${opened && opened.link ? 'open' : ''}><summary class="muted tt-small" style="cursor:pointer">${esc(summary)}</summary>${linkRow('Link it')}</details>`;
	}

	function viewsChart(points) {
		if (!points || points.length < 2) return '';
		return `<div class="tt-chart" data-points='${esc(JSON.stringify(points))}'></div>`;
	}

	function stageHtml(it, opened) {
		const c = S.connection || {};
		const p = it.post || {};
		const err = msg => msg ? `<div class="tt-err">${esc(msg)}</div>` : '';
		switch (it.stage) {
			case 'rendering':
				return `<div class="tt-box">Rendering the TikTok version: the same cut, without the baked-in music${it.lang !== 'en' ? ' and with the frame text translated' : ''}. You can edit the caption meanwhile.</div>`;
			case 'render_failed':
				return `<div class="tt-box err"><b>The TikTok version didn’t render.</b>${err(it.render_error)}
					<div class="tt-row"><button type="button" class="btn btn-sm" data-act="rerender">Render again</button></div></div>`;
			case 'ready':
				return `<div class="tt-box"><div class="tt-row">
					${c.connected ? `<button type="button" class="btn btn-primary" data-act="send">Send to TikTok drafts →</button>`
						: `<span>Connect TikTok (top of the page) to send it, or upload the mp4 by hand.</span>`}</div>
					<span class="muted tt-small">It lands in the app as a draft: you add the sound, paste the caption and post it there.</span>
					${linkBox('Posted it by hand already?', opened)}</div>`;
			case 'sending':
				return `<div class="tt-box"><b>Uploading to TikTok…</b><span class="muted tt-small">${p.sent ? 'queued ' + esc(p.sent) : ''}</span></div>`;
			case 'send_failed':
				return `<div class="tt-box err"><b>${/tiktok_failed/.test(p.error || '') ? 'TikTok dropped the draft.' : 'Didn’t reach TikTok.'}</b>${err(p.error)}
					<div class="tt-row">${c.connected ? `<button type="button" class="btn btn-sm btn-primary" data-act="send">Send again</button>` : ''}</div>
					${linkBox('Posted it by hand?', opened)}</div>`;
			case 'inbox':
				if (it.exported) {
					return `<div class="tt-box inbox"><b>Saved for a manual upload</b> (TikTok wasn’t connected): the file is in <code>render/</code> and under ⬇ mp4.
						Upload it in the app, then paste the link here.${linkRow('Link it')}</div>`;
				}
				return `<div class="tt-box inbox"><b>${it.tt_status === 'PROCESSING_UPLOAD' ? 'TikTok is still processing the upload' : 'In your TikTok drafts'}</b>
					<ol class="tt-steps"><li>Open TikTok on the phone: the notification, or Profile → Drafts / Inbox.</li>
					<li>Add the sound (see the checklist), set the volumes.</li>
					<li>Paste the caption (📋 on the left) and post.</li></ol>
					<span class="muted tt-small">IndiePulse checks every 10 minutes whether it went live${it.status_checked ? ' · last check ' + esc(it.status_checked) : ''}.</span>
					${err(it.tt_error)}
					<div class="tt-row"><button type="button" class="btn btn-sm" data-act="refresh">Check now</button></div>
					${linkBox('Posted, but it still says draft?', opened)}</div>`;
			case 'live': {
				const m = it.metrics || {};
				const eng = (m.views && (m.likes != null)) ? ((m.likes + (m.comments || 0) + (m.shares || 0)) / m.views * 100) : null;
				const nums = c.views || m.views != null
					? `<div class="tt-metrics">
						<div class="tt-m"><b>${fmtNum(m.views)}</b><span>views</span></div>
						<div class="tt-m"><b>${fmtNum(m.likes)}</b><span>likes</span></div>
						<div class="tt-m"><b>${fmtNum(m.comments)}</b><span>comments</span></div>
						<div class="tt-m"><b>${fmtNum(m.shares)}</b><span>shares</span></div>
						<div class="tt-m"><b>${eng != null ? eng.toFixed(1) + '%' : '—'}</b><span>engagement</span></div></div>`
					: `<span class="muted tt-small">Views need the <code>video.list</code> scope (see the top of the page).</span>`;
				const extra = [];
				if (it.views_1h != null) extra.push('1 h: ' + fmtNum(it.views_1h));
				if (it.views_24h != null) extra.push('24 h: ' + fmtNum(it.views_24h));
				if (it.yt_views != null) extra.push('YouTube: ' + fmtNum(it.yt_views));
				if (m.checked) extra.push('read ' + m.checked);
				return `<div class="tt-box live"><div class="tt-row"><b>Live</b>${p.published ? `<span class="muted tt-small">since ${esc(p.published)}</span>` : ''}
						<span style="flex:1"></span>${p.url ? `<a href="${esc(p.url)}" target="_blank" rel="noopener">open on TikTok ↗</a>` : ''}
						<button type="button" class="btn btn-sm" data-act="refresh">Refresh</button></div>
					${!it.video_id ? `<span class="muted tt-small">TikTok says it’s posted; its id (and so its numbers) comes once it’s public and through moderation. Or paste the link:</span>${linkRow('Link it')}` : ''}
					${nums}${err(m.error && m.error !== 'None' ? 'Last read failed: ' + m.error : '')}
					${viewsChart(it.history)}
					${extra.length ? `<span class="muted tt-small">${esc(extra.join(' · '))}</span>` : ''}
					<div class="tt-row"><label class="tt-inline">Viewers from ${it.lang === 'ja' ? 'Japan' : 'the top country'}
						<input type="number" class="tt-share" min="0" max="100" step="0.1" value="${it.jp_share != null ? it.jp_share : ''}"> %</label>
						<span class="muted tt-small">TikTok Studio → the video → Viewers → Locations</span></div></div>`;
			}
		}
		return '';
	}

	function flowHtml(it, opened) {
		return stageHtml(it, opened) + `<div class="tt-checks">${checksHtml(it, opened)}</div>`;
	}

	function headHtml(it) {
		const src = { short: 'Short', upload: 'Dropped file', factory: 'From the factory' }[it.source] || it.source;
		return `<span class="tt-stage ${it.stage}">${STAGE_LABEL[it.stage] || it.stage}</span>
			<span class="tt-title">${esc(it.title || 'Untitled')}</span>
			<span class="muted tt-small">${esc([it.project, src].filter(Boolean).join(' · '))}</span>
			<span class="tt-sp"></span>
			<button type="button" class="btn btn-sm" data-act="archive" title="Remove from this list (a TikTok draft or post stays where it is)">✕</button>`;
	}

	function mount(it) {
		const el = document.createElement('article');
		el.className = 'tt-item';
		el.id = 'tt_item_' + it.id;
		el.innerHTML = `<div class="tt-vid"></div><div class="tt-body"><div class="tt-head"></div>
			<div class="tt-cols"><div class="tt-caption"></div><div class="tt-flow"></div></div></div>`;
		const c = { el, item: it, sigVid: null, sigFlow: null };
		cards.set(it.id, c);
		el.querySelector('.tt-caption').innerHTML = captionHtml(it);
		wireCard(c);
		return c;
	}

	function paint(c, it, full) {
		const el = c.el;
		const active = document.activeElement;
		c.item = it;
		const sv = sigVid(it);
		if (full || sv !== c.sigVid) {
			const v = el.querySelector('.tt-vid video');
			const playing = v && !v.paused;
			if (!(playing && v.getAttribute('src') === it.video_url)) { el.querySelector('.tt-vid').innerHTML = vidHtml(it); }
			c.sigVid = sv;
		}
		el.querySelector('.tt-head').innerHTML = headHtml(it);
		const flow = el.querySelector('.tt-flow');
		const sf = sigFlow(it);
		if (full || (sf !== c.sigFlow && !flow.contains(active))) {
			if (c.lastStage !== it.stage) c.opened = {};  // a new stage opens its own group
			flow.innerHTML = flowHtml(it, c.opened);
			flow.querySelectorAll('.tt-chart').forEach(drawChart);
			c.sigFlow = sf;
			c.lastStage = it.stage;
		}
		const cap = el.querySelector('.tt-caption');
		if (full) {
			cap.innerHTML = captionHtml(it);
		} else if (!cap.contains(active) && !dirty.has(it.id)) {
			const ta = cap.querySelector('.tt-cap'), tg = cap.querySelector('.tt-tags');
			if (ta.value !== it.caption) ta.value = it.caption;
			if (tg.value !== it.tags) tg.value = it.tags;
			cap.querySelectorAll('.tt-langs button').forEach(b => b.classList.toggle('on', b.dataset.lang === it.lang));
		}
		countCaption(c);
	}

	function upsert(it, full) {
		let c = cards.get(it.id);
		const isNew = !c;
		if (!c) c = mount(it);
		paint(c, it, full || isNew);
		const list = $('#tt_items');
		if (isNew) list.prepend(c.el);
		const i = (S.items || []).findIndex(x => x.id === it.id);
		if (i >= 0) S.items[i] = it; else (S.items = S.items || []).unshift(it);
		$('#tt_empty').hidden = cards.size > 0;
		schedule();
	}

	function captionText(c) {
		const cap = c.el.querySelector('.tt-cap').value.trim(), tags = c.el.querySelector('.tt-tags').value.trim();
		return [cap, tags].filter(Boolean).join('\n\n');
	}
	function countCaption(c) {
		const n = [...captionText(c)].length, lim = c.item.caption_limit || 4000;
		const tags = (c.el.querySelector('.tt-tags').value.match(/#[^\s#,]+/g) || []).length;
		c.el.querySelector('.tt-capcount').textContent = `${n.toLocaleString('en-US')} / ${lim.toLocaleString('en-US')} · ${tags} hashtag${tags === 1 ? '' : 's'}`;
	}

	async function save(c, data) {
		const res = await api('PATCH', `/api/tiktok/items/${c.item.id}`, data);
		paint(c, res.item, false);
		return res.item;
	}

	async function action(c, name, body, btn) {
		const label = btn && btn.textContent;
		if (btn) { btn.disabled = true; btn.textContent = '…'; }
		try {
			if (name === 'refresh') {
				await refresh(true);
				return;
			}
			const res = await api('POST', `/api/tiktok/items/${c.item.id}/${name}`, body || {});
			if (name === 'archive') {
				cards.delete(c.item.id);
				c.el.remove();
				S.items = (S.items || []).filter(x => x.id !== c.item.id);
				const cand = (S.candidates || []).find(x => x.item_id === c.item.id);
				if (cand) cand.item_id = null;
				renderCands();
				$('#tt_empty').hidden = cards.size > 0;
				return;
			}
			paint(c, res.item, name === 'lang');
			(res.notes || []).forEach(n => toast(n, true));
			if (name === 'send') toast('Queued: the clip goes to your TikTok drafts in a moment.');
			if (name === 'link') toast('Linked.');
			S.summary && refresh(false);
		} catch (e) {
			toast(e.message, true);
		} finally {
			if (btn && btn.isConnected) { btn.disabled = false; btn.textContent = label; }
		}
	}

	function wireCard(c) {
		const el = c.el;
		c.opened = {};
		// 'toggle' doesn't bubble: listen in the capture phase
		el.addEventListener('toggle', e => {
			const g = e.target.dataset && e.target.dataset.group;
			if (g) c.opened[g] = e.target.open;
		}, true);
		el.addEventListener('input', e => {
			if (!e.target.matches('.tt-cap, .tt-tags')) return;
			countCaption(c);
			clearTimeout(dirty.get(c.item.id));
			dirty.set(c.item.id, setTimeout(async () => {
				try {
					await save(c, { caption: el.querySelector('.tt-cap').value, tags: el.querySelector('.tt-tags').value });
				} catch (err) { toast('Caption not saved: ' + err.message, true); }
				dirty.delete(c.item.id);
			}, 700));
		});
		el.addEventListener('change', async e => {
			const t = e.target;
			try {
				if (t.matches('[data-tick]')) await save(c, { checks: { [t.dataset.tick]: t.checked } });
				else if (t.matches('.tt-share')) await save(c, { jp_share: t.value === '' ? null : +t.value });
			} catch (err) { toast(err.message, true); }
		});
		el.addEventListener('click', async e => {
			const b = e.target.closest('button[data-act], .tt-langs button[data-lang]');
			if (!b) return;
			if (b.dataset.lang) {
				if (b.dataset.lang === c.item.lang) return;
				const msg = el.querySelector('.tt-capmsg');
				el.querySelectorAll('.tt-langs button').forEach(x => { x.disabled = true; });
				msg.textContent = 'Translating…';
				await action(c, 'lang', { lang: b.dataset.lang });
				el.querySelectorAll('.tt-langs button').forEach(x => { x.disabled = false; });
				const m2 = el.querySelector('.tt-capmsg');
				if (m2) m2.textContent = '';
				return;
			}
			const act = b.dataset.act;
			if (act === 'copy') {
				try {
					await navigator.clipboard.writeText(captionText(c));
					b.textContent = '✓ Copied';
					setTimeout(() => { b.textContent = '📋 Copy caption'; }, 1500);
				} catch (err) { toast('Couldn’t copy: ' + err.message, true); }
			} else if (act === 'edit') {
				if (!window.VideoEditor) return;
				VideoEditor.open({
					rel: c.item.video_rel, format: 'vertical', formats: ['vertical'], platforms: () => ['tiktok'],
					title: (c.item.title || 'Clip') + ' · TikTok version', applyLabel: 'Use for TikTok',
					onApply: async out => {
						try { await save(c, { video_rel: out.out_rel }); paint(c, c.item, true); }
						catch (err) { toast(err.message, true); }
					},
				});
			} else if (act === 'link') {
				const url = (b.parentElement.querySelector('.tt-link') || {}).value || '';
				if (!url.trim()) { toast('Paste the TikTok link first', true); return; }
				await action(c, 'link', { url }, b);
			} else if (act === 'archive') {
				const live = ['sending', 'inbox', 'live'].includes(c.item.stage);
				if (!confirm(live ? 'Remove this clip from the list? The TikTok draft or post stays as it is; its numbers stop updating here.' : 'Remove this clip from the list?')) return;
				await action(c, 'archive', {}, b);
			} else if (act === 'send') {
				const warns = (c.item.checks || []).filter(x => x.stage === 'prep' && x.state === 'warn').length;
				if (warns && !confirm(`${warns} check${warns > 1 ? 's' : ''} before sending ${warns > 1 ? 'aren’t' : 'isn’t'} green. Send anyway?`)) return;
				await action(c, 'send', {}, b);
			} else {
				await action(c, act, {}, b);
			}
		});
	}

	// ── the views-over-time line: one series, 2px, end marker, hover readout ─
	function drawChart(host) {
		let pts;
		try { pts = JSON.parse(host.dataset.points || '[]'); } catch (e) { return; }
		if (pts.length < 2) return;
		const W = Math.max(200, host.clientWidth || 300), H = 64, padL = 4, padR = 46, padT = 8, padB = 14;
		const maxX = pts[pts.length - 1][0] || 1, maxY = Math.max(1, ...pts.map(p => p[1]));
		const x = h => padL + h / maxX * (W - padL - padR), y = v => H - padB - v / maxY * (H - padT - padB);
		const root = getComputedStyle(document.documentElement);
		const accent = (root.getPropertyValue('--primary') || '#2563eb').trim();
		const grid = (root.getPropertyValue('--border') || '#e5e7eb').trim();
		const d = pts.map((p, i) => (i ? 'L' : 'M') + x(p[0]).toFixed(1) + ' ' + y(p[1]).toFixed(1)).join(' ');
		const last = pts[pts.length - 1];
		host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Views over time: ${fmtNum(last[1])} after ${fmtHours(last[0])}">
			<line x1="${padL}" x2="${W - padR}" y1="${H - padB}" y2="${H - padB}" stroke="${grid}" stroke-width="1"/>
			<path d="${d}" fill="none" stroke="${accent}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
			<line class="tt-cross" y1="${padT}" y2="${H - padB}" stroke="${grid}" stroke-width="1" visibility="hidden"/>
			<circle cx="${x(last[0])}" cy="${y(last[1])}" r="4" fill="${accent}" stroke="#fff" stroke-width="2"/>
			<text x="${W - padR + 6}" y="${y(last[1]) + 4}" font-size="11" fill="#334155">${fmtNum(last[1])}</text>
			<text x="${padL}" y="${H - 2}" font-size="10" fill="#64748b">0</text>
			<text x="${W - padR}" y="${H - 2}" font-size="10" fill="#64748b" text-anchor="end">${fmtHours(last[0])}</text>
			<rect x="0" y="0" width="${W}" height="${H}" fill="transparent"/></svg>`;
		const svg = host.querySelector('svg'), cross = host.querySelector('.tt-cross'), tip = $('#tt_tip');
		svg.addEventListener('pointermove', e => {
			const r = svg.getBoundingClientRect();
			const h = Math.max(0, Math.min(maxX, ((e.clientX - r.left) * W / r.width - padL) / (W - padL - padR) * maxX));
			let best = pts[0];
			pts.forEach(p => { if (Math.abs(p[0] - h) < Math.abs(best[0] - h)) best = p; });
			cross.setAttribute('x1', x(best[0]));
			cross.setAttribute('x2', x(best[0]));
			cross.setAttribute('visibility', 'visible');
			tip.textContent = `${fmtNum(best[1])} views after ${fmtHours(best[0])}`;
			tip.hidden = false;
			tip.style.left = (e.pageX + 12) + 'px';
			tip.style.top = (e.pageY - 30) + 'px';
		});
		svg.addEventListener('pointerleave', () => { cross.setAttribute('visibility', 'hidden'); tip.hidden = true; });
	}

	// ── refresh loop ─────────────────────────────────────────────────────────
	let timer = null, busy = false, due = 0;
	function schedule() {  // (re)arm the poll at the pace the list needs now
		const at = Date.now() + nextDelay();
		if (timer && due && due <= at) return;
		clearTimeout(timer);
		due = at;
		timer = setTimeout(() => { due = 0; if (!document.hidden) refresh(false); else schedule(); }, at - Date.now());
	}
	function nextDelay() {
		const st = (S.items || []).map(i => i.stage);
		if (st.includes('rendering') || st.includes('sending')) return 5000;
		if (st.includes('inbox')) return 60000;
		return 180000;
	}
	async function refresh(force) {
		if (busy) return;
		busy = true;
		clearTimeout(timer);
		timer = null;
		try {
			const j = await api('GET', `/api/tiktok/state?sync=1${force ? '&force=1' : ''}`);
			S = Object.assign(S, { connection: j.connection, account: j.account, followers: j.followers, summary: j.summary });
			const ids = new Set();
			(j.items || []).forEach(it => { ids.add(it.id); upsert(it, false); });
			for (const [id, c] of cards) if (!ids.has(id)) { c.el.remove(); cards.delete(id); }
			S.items = j.items || [];
			renderAccount();
			renderConnect();
			renderSummary();
			if (j.sync && j.sync.error) toast('TikTok check failed: ' + j.sync.error, true);
			else if (force) {
				const s = j.sync || {};
				toast(s.live ? `${s.live} clip${s.live > 1 ? 's' : ''} went live.` : 'Up to date.');
			}
		} catch (e) {
			if (force) toast(e.message, true);
		} finally {
			busy = false;
			due = 0;
			schedule();
		}
	}
	document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(false); });

	// ── boot ─────────────────────────────────────────────────────────────────
	function boot() {
		const sel = $('#tt_lang');
		sel.innerHTML = (S.langs || []).map(l => `<option value="${l.key}" ${l.key === lang() ? 'selected' : ''}>${esc(l.name)}</option>`).join('');
		sel.addEventListener('change', () => { try { localStorage.setItem('tt_lang', sel.value); } catch (e) { /* */ } });
		const proj = $('#tt_project');
		(S.projects || []).forEach(p => { const o = document.createElement('option'); o.value = p.id; o.textContent = p.name; proj.appendChild(o); });
		try { const last = localStorage.getItem('tt_project'); if (last) proj.value = last; } catch (e) { /* */ }
		proj.addEventListener('change', () => { try { localStorage.setItem('tt_project', proj.value); } catch (e) { /* */ } });

		$('#tt_cands').addEventListener('click', e => {
			const add = e.target.closest('[data-add]'), go = e.target.closest('[data-goto]');
			if (add) addBrief(add, +add.dataset.add);
			else if (go) focusItem(+go.dataset.goto);
		});
		const drop = $('#tt_drop'), file = $('#tt_file');
		drop.addEventListener('click', () => file.click());
		drop.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); file.click(); } });
		file.addEventListener('change', () => { if (file.files[0]) uploadFile(file.files[0]); file.value = ''; });
		drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('over'); });
		drop.addEventListener('dragleave', () => drop.classList.remove('over'));
		drop.addEventListener('drop', e => {
			e.preventDefault();
			drop.classList.remove('over');
			const f = e.dataTransfer.files && e.dataTransfer.files[0];
			if (f) uploadFile(f);
		});

		renderAccount();
		renderConnect();
		renderSummary();
		renderCands();
		(S.items || []).slice().reverse().forEach(it => upsert(it, true));
		$('#tt_empty').hidden = cards.size > 0;
		renderClock();
		setInterval(renderClock, 30000);
		if (window.TT_JUST_CONNECTED) {
			toast('TikTok connected.');
			history.replaceState(null, '', location.pathname);
		}
		refresh(!!window.TT_JUST_CONNECTED);
	}
	boot();
})();
