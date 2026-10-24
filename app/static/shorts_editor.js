// Shorts Factory ↔ the shared video editor (video_editor.js) for one clip at a time.
// Uses the page globals of shorts.html: cards, selectedPreset, selectedProjectId,
// selectedProjectName, selectedPlatforms(), setCardMusic().
//
// A dropped clip is uploaded to media/ at once (/api/shorts/upload): the editor works on
// media/ files, and the submit only sends settings. Each card keeps the editor's spec
// (crop, zoom, trim, sound, music, text lines); render_short renders exactly that later,
// so the editor runs in its settings mode here (no render, “Done” instead).
(function () {
	const MANUAL = [
		['motion', 'Motion in the first 2 seconds'],
		['loop', 'The end loops back to the start'],
		['ui', 'Game UI is readable on a phone'],
	];
	const ICON = { ok: '✓', info: 'ℹ', warn: '⚠', error: '✕' };
	const MAX_UPLOADS = 2;  // at a time

	const fmt = x => (x == null || isNaN(x)) ? '—' : (Math.round(x * 10) / 10).toFixed(1);
	const esc = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
	const clone = o => JSON.parse(JSON.stringify(o));

	// ---- Settings from the project (tile on /shorts) ---------------------------------
	function preset() {
		return (typeof selectedPreset !== 'undefined' && selectedPreset) || {};
	}
	function projectCrop() {
		const p = preset();
		return {
			crop_x: (p.crop_x != null ? +p.crop_x : 50) / 100,
			crop_y: (p.crop_y != null ? +p.crop_y : 50) / 100,
			zoom: Math.max(1, Math.min(4, +p.zoom || 1)),
		};
	}
	function baseSpec() {
		return Object.assign({ format: 'vertical', trim_start: null, trim_end: null, source_volume: 1,
			music: null, overlays: [] }, projectCrop());
	}
	// A card's music select → the spec's track; same track keeps its start/volume/fades.
	function musicFor(path, prev) {
		if (!path) return null;
		if (prev && prev.path === path) return prev;
		const p = preset();
		return { path, start: Math.max(0, +p.music_offset || 0), volume: prev ? prev.volume : 0.8,
			fade_in: p.fade_in != null ? +p.fade_in : 0.5, fade_out: p.fade_out != null ? +p.fade_out : 1.5 };
	}

	// ---- Per-card state ----------------------------------------------------------------
	function initCard(card) {
		card.spec = baseSpec();
		card.spec.music = musicFor(card.musicEl.value, null);
		card.cropTouched = false;
		card.manual = {};
		card.lint = null;
		card.rel = null;
		card.info = null;
		card.blobDuration = null;
		card.summaryEl = card.root.querySelector('.sf-summary');
		card.chipEl = card.root.querySelector('.sf-lint');
		card.stageEl = card.root.querySelector('.sf-stage');
		card.videoEl = card.stageEl.querySelector('video');
		card.editBtn = card.root.querySelector('.sf-edit');
		card.metaEl = card.root.querySelector('.sf-meta');
		const v = card.videoEl;
		v.addEventListener('loadedmetadata', () => {
			card.blobDuration = isFinite(v.duration) ? v.duration : null;
			layoutCard(card);
			seekCard(card);
			updateCard(card);
			scheduleLint();
		});
		// hover plays the trimmed clip, muted
		card.stageEl.addEventListener('mouseenter', () => { if (v.readyState >= 1) v.play().catch(() => {}); });
		card.stageEl.addEventListener('mouseleave', () => { v.pause(); seekCard(card); });
		v.addEventListener('timeupdate', () => {
			const end = trimEndEff(card);
			if (end != null && v.currentTime >= end - 0.05) v.currentTime = card.spec.trim_start || 0;
		});
		card.stageEl.addEventListener('click', () => open(card));
		card.editBtn.addEventListener('click', () => open(card));
		card.chipEl.addEventListener('click', () => open(card));
		card.musicEl.addEventListener('change', () => setMusic(card, card.musicEl.value));
		if (window.ResizeObserver) new ResizeObserver(() => layoutCard(card)).observe(card.stageEl);
		queueUpload(card);
		updateCard(card);
	}

	function srcDuration(card) {
		return (card.info && card.info.duration) || card.blobDuration || null;
	}
	function trimEndEff(card) {
		const d = srcDuration(card);
		const te = card.spec.trim_end;
		if (d == null) return te;
		return te != null ? Math.min(te, d) : d;
	}
	function clipDuration(card) {
		const end = trimEndEff(card);
		return end == null ? null : Math.max(0, end - (card.spec.trim_start || 0));
	}
	function liveOverlays(card) {
		return (card.spec.overlays || []).filter(o => (o.text || '').trim());
	}
	// Fields added to the card's `meta` item on submit
	function metaFor(card) {
		return { src_rel: card.rel, spec: clone(card.spec) };
	}

	// The card's picture: the source placed like the render places it (cover + zoom + crop).
	function layoutCard(card) {
		const v = card.videoEl, st = card.stageEl;
		const W = st.clientWidth, H = st.clientHeight;
		const vw = v.videoWidth || (card.info && card.info.width), vh = v.videoHeight || (card.info && card.info.height);
		if (!W || !H || !vw || !vh) return;
		const s = card.spec;
		const k = Math.max(W / vw, H / vh) * (s.zoom || 1);
		const w = vw * k, h = vh * k;
		Object.assign(v.style, { width: w + 'px', height: h + 'px',
			left: -(w - W) * s.crop_x + 'px', top: -(h - H) * s.crop_y + 'px' });
	}
	function seekCard(card) {
		const v = card.videoEl;
		if (v.readyState >= 1) { try { v.currentTime = (card.spec.trim_start || 0) + 0.05; } catch (e) {} }
	}

	function chipHtml(counts) {
		if (!counts) return '';
		let s = `<span class="ok">✓ ${counts.ok || 0}</span>`;
		if (counts.warn) s += ` · <span class="warn">⚠ ${counts.warn}</span>`;
		if (counts.error) s += ` · <span class="err">✕ ${counts.error}</span>`;
		return s;
	}
	function updateCard(card) {
		const d = clipDuration(card);
		const n = liveOverlays(card).length;
		const s = card.spec;
		const parts = [`⏱ ${fmt(d)} s${(s.trim_start || s.trim_end != null) ? ' ✂' : ''}`];
		parts.push(`${n} ${n === 1 ? 'line' : 'lines'}`);
		if (s.zoom > 1.001) parts.push(`zoom ${s.zoom.toFixed(2)}×`);
		if (s.source_volume <= 0) parts.push('game sound off');
		card.summaryEl.textContent = parts.join(' · ');
		card.chipEl.innerHTML = chipHtml(card.lint && card.lint.counts);
		card.chipEl.style.display = card.lint ? '' : 'none';
		renderUpload(card);
	}

	// ---- Upload ------------------------------------------------------------------------
	const queue = [];
	let active = 0;
	function queueUpload(card) {
		card.upload = { state: 'queued', pct: 0 };
		card.uploadDone = new Promise(res => { card._uploadResolve = res; });
		queue.push(card);
		pump();
	}
	function pump() {
		while (active < MAX_UPLOADS && queue.length) {
			const card = queue.shift();
			if (card.removed) { card._uploadResolve(); continue; }
			active++;
			startUpload(card);
		}
	}
	function startUpload(card) {
		const xhr = new XMLHttpRequest();
		card.xhr = xhr;
		card.upload = { state: 'uploading', pct: 0 };
		renderUpload(card);
		const finish = () => { active--; card.xhr = null; renderUpload(card); card._uploadResolve(); pump(); };
		xhr.upload.onprogress = e => {
			if (e.lengthComputable) { card.upload.pct = e.loaded / e.total; renderUpload(card); }
		};
		xhr.onload = () => {
			let j = {};
			try { j = JSON.parse(xhr.responseText); } catch (e) { /* not json */ }
			if (xhr.status >= 200 && xhr.status < 300 && j.rel) {
				card.rel = j.rel;
				card.info = j;
				card.upload = { state: 'done' };
				if (card.removed) dropUpload(card);
				layoutCard(card);
				updateCard(card);
				scheduleLint();
			} else {
				card.upload = { state: 'error', error: j.detail || ('HTTP ' + xhr.status) };
			}
			finish();
		};
		xhr.onerror = () => { card.upload = { state: 'error', error: 'server unreachable' }; finish(); };
		xhr.onabort = () => { card.upload = { state: 'error', error: 'canceled' }; finish(); };
		const fd = new FormData();
		fd.append('file', card.file, card.file.name);
		xhr.open('POST', '/api/shorts/upload');
		xhr.send(fd);
	}
	function retryUpload(card) {
		if (card.upload.state !== 'error') return;
		queueUpload(card);
	}
	function dropUpload(card) {
		if (!card.rel) return;
		fetch('/api/shorts/upload?rel=' + encodeURIComponent(card.rel), { method: 'DELETE' }).catch(() => {});
		card.rel = null;
	}
	function renderUpload(card) {
		const up = card.upload || {};
		const box = card.root.querySelector('.sf-up');
		const ready = up.state === 'done';
		box.hidden = ready;
		card.root.classList.toggle('sf-uploading', !ready);
		if (!ready) {
			box.querySelector('i').style.width = Math.round((up.pct || 0) * 100) + '%';
			const t = box.querySelector('.sf-up-text');
			if (up.state === 'error') {
				t.innerHTML = `✕ upload failed: ${esc(up.error)} · <a href="#" class="sf-up-retry">retry</a>`;
				t.querySelector('.sf-up-retry').onclick = e => { e.preventDefault(); e.stopPropagation(); retryUpload(card); };
			} else {
				t.textContent = up.state === 'queued' ? 'waiting to upload…' : `uploading ${Math.round((up.pct || 0) * 100)}%`;
			}
			box.classList.toggle('sf-up-err', up.state === 'error');
		}
		card.editBtn.disabled = !ready;
		card.editBtn.textContent = ready ? '✂ Editor' : '⏳ uploading…';
		if (card.metaEl) {
			const i = card.info;
			card.metaEl.textContent = i ? `${i.width}×${i.height} · ${fmt(i.duration)} s · ${(+i.size_mb).toFixed(1)} MB${i.has_audio ? '' : ' · no sound'}` : '';
		}
	}
	// Resolves when no clip is still uploading; → the cards that failed.
	async function waitUploads() {
		await Promise.all(cards.map(c => c.uploadDone));
		return cards.filter(c => !c.rel);
	}
	function pendingUploads() {
		return cards.filter(c => c.upload && (c.upload.state === 'queued' || c.upload.state === 'uploading')).length;
	}

	// ---- Music and project changes from the page ------------------------------------------
	function setMusic(card, path) {
		card.spec.music = musicFor(path, card.spec.music);
		updateCard(card);
		scheduleLint();
	}
	// A project was picked: its crop goes to every clip whose frame you didn't move.
	function applyProject() {
		const crop = projectCrop();
		cards.forEach(c => {
			if (!c.cropTouched) Object.assign(c.spec, crop);
			layoutCard(c);
			updateCard(c);
		});
		scheduleLint();
	}

	// ---- Checklist (server-side linter) ----------------------------------------------------
	let lintTimer = null, lintSeq = 0, lintPending = null;
	function scheduleLint() {
		clearTimeout(lintTimer);
		lintTimer = setTimeout(runLint, 400);
	}
	// The render fades the music in, not the game's sound: the first frame is silent only
	// when the music is all there is.
	function fadeIn(card) {
		const s = card.spec;
		const gameSound = s.source_volume > 0 && !(card.info && card.info.has_audio === false);
		return s.music && !gameSound ? (s.music.fade_in || 0) : 0;
	}
	async function runLint() {
		clearTimeout(lintTimer);
		if (!cards.length) return;
		const seq = ++lintSeq;
		const batch = cards.slice();
		const body = {
			project_id: selectedProjectId,
			goal_id: document.getElementById('g_goal').value || null,
			platforms: selectedPlatforms(),
			cards: batch.map(c => ({
				title: c.titleEl.value || '', description: c.descEl.value || '', tags: c.tagsEl.value || '',
				music_path: (c.spec.music && c.spec.music.path) || '', duration: clipDuration(c),
				overlays: liveOverlays(c), fade_in: fadeIn(c),
			})),
		};
		const p = (async () => {
			try {
				const r = await fetch('/api/shorts/lint', {
					method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
				});
				if (!r.ok) return;
				const j = await r.json();
				if (seq !== lintSeq) return;
				batch.forEach((c, i) => { c.lint = (j.cards || [])[i] || null; updateCard(c); });
				if (cur) renderFindings();
			} catch (e) { /* offline: keep the old chips */ }
		})();
		lintPending = p;
		return p;
	}
	async function lintNow() {
		await runLint();
		if (lintPending) await lintPending;
	}
	function confirmSubmit() {
		const bad = cards.filter(c => c.lint && (c.lint.counts.warn || c.lint.counts.error));
		if (!bad.length) return true;
		const w = bad.reduce((s, c) => s + (c.lint.counts.warn || 0), 0);
		const e = bad.reduce((s, c) => s + (c.lint.counts.error || 0), 0);
		const n = bad.length;
		return confirm(`${n} ${n === 1 ? 'clip has' : 'clips have'} issues (✕ ${e} · ⚠ ${w}). Send anyway?`);
	}

	// ---- The editor's right column: title, checklist, ← → between clips ------------------
	let cur = null;
	let panel = null;
	function pq(sel) { return panel.querySelector(sel); }
	function buildPanel() {
		panel = document.createElement('div');
		panel.className = 'sfx-panel';
		panel.innerHTML = `
			<div class="sfx-nav">
				<button type="button" class="btn btn-sm sfx-prev" title="Previous clip">← prev</button>
				<span class="sfx-pos muted"></span>
				<button type="button" class="btn btn-sm sfx-next" title="Next clip">next →</button>
			</div>
			<h4>Title</h4>
			<input type="text" class="sfx-title" placeholder="Hook + game name…" />
			<div class="ve-note">The only text shown in the Shorts player: one line, ~40 Latin / ~20 Japanese characters.</div>
			<h4>Checklist</h4>
			<div class="sfe-findings"><span class="muted">…</span></div>
			<a href="/guide#checklist" target="_blank" class="sfe-why">Why → Playbook</a>
			<h4>Check by hand</h4>
			<div class="sfe-manual">${MANUAL.map(([k, t]) => `<label><input type="checkbox" data-k="${k}" /> ${t}</label>`).join('')}</div>`;
		pq('.sfx-title').addEventListener('input', e => {
			if (!cur) return;
			cur.titleEl.value = e.target.value;
			cur.titleEl.dispatchEvent(new Event('input', { bubbles: true }));
		});
		pq('.sfe-manual').addEventListener('change', e => {
			if (cur && e.target.dataset.k) cur.manual[e.target.dataset.k] = e.target.checked;
		});
		pq('.sfx-prev').addEventListener('click', () => step(-1));
		pq('.sfx-next').addEventListener('click', () => step(1));
	}
	function step(d) {
		const ready = cards.filter(c => c.rel);
		const i = ready.indexOf(cur);
		const next = ready[i + d];
		if (next) open(next);
	}
	function renderPanel() {
		const ready = cards.filter(c => c.rel);
		const i = ready.indexOf(cur);
		pq('.sfx-pos').textContent = ready.length > 1 ? `clip ${i + 1} of ${ready.length}` : '';
		pq('.sfx-prev').disabled = i <= 0;
		pq('.sfx-next').disabled = i < 0 || i >= ready.length - 1;
		pq('.sfx-nav').hidden = ready.length < 2;
		pq('.sfx-title').value = cur.titleEl.value || '';
		panel.querySelectorAll('.sfe-manual input').forEach(x => { x.checked = !!cur.manual[x.dataset.k]; });
		renderFindings();
	}
	function renderFindings() {
		if (!cur || !panel) return;
		const box = pq('.sfe-findings');
		if (!cur.lint) { box.innerHTML = '<span class="muted">Checks appear in a second…</span>'; return; }
		box.innerHTML = cur.lint.findings.map(f =>
			`<div class="sfe-f sfe-f-${f.level}"><span class="sfe-fi">${ICON[f.level] || '•'}</span><span>${esc(f.message)}</span></div>`
		).join('') || '<span class="muted">Nothing to check</span>';
	}

	function editorProject() {
		const p = preset();
		const name = (typeof selectedProjectName !== 'undefined' && selectedProjectName) || '';
		if (!name && selectedProjectId == null) return null;
		return { name, crop_x: p.crop_x != null ? p.crop_x : 50, crop_y: p.crop_y != null ? p.crop_y : 50,
			crop_scale: p.zoom || 1, music_path: p.music_path || '' };
	}

	function open(card) {
		if (!card.rel || !window.VideoEditor) return;
		if (!panel) buildPanel();
		cur = card;
		renderPanel();
		const before = { x: card.spec.crop_x, y: card.spec.crop_y, z: card.spec.zoom };
		VideoEditor.open({
			mode: 'spec',
			rel: card.rel,
			spec: clone(card.spec),
			format: 'vertical',
			formats: ['vertical'],
			title: card.file.name,
			project: editorProject(),
			panel,
			onChange: s => {
				if (cur !== card) return;
				if (s.crop_x !== before.x || s.crop_y !== before.y || s.zoom !== before.z) card.cropTouched = true;
				const path = s.music ? s.music.path : '';
				card.spec = s;
				if (path !== (card.musicEl.value || '')) {
					setCardMusic(card, path);   // keeps the select in sync (and calls setMusic → same spec)
					card.spec.music = s.music;
				}
				layoutCard(card);
				seekCard(card);
				updateCard(card);
				scheduleLint();
			},
			onClose: () => { if (cur === card) cur = null; },
		});
		scheduleLint();
	}

	function forget(card) {
		card.removed = true;
		if (cur === card && window.VideoEditor) VideoEditor.close(true);
		if (card.xhr) card.xhr.abort();
		dropUpload(card);
		if (card.url) URL.revokeObjectURL(card.url);
	}

	// Checklist also depends on the global goal / platforms
	document.addEventListener('DOMContentLoaded', () => {
		const g = document.getElementById('g_goal');
		if (g) g.addEventListener('change', scheduleLint);
		const pf = document.getElementById('g_platforms');
		if (pf) pf.addEventListener('change', scheduleLint);
	});

	window.SFEditor = { initCard, open, forget, metaFor, scheduleLint, lintNow, confirmSubmit,
		applyProject, setMusic, waitUploads, pendingUploads, clipDuration };
})();
