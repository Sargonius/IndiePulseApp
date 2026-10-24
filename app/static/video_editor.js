// Video editor modal for every page that takes a video (Create Post, Create Video).
// VideoEditor.open({rel, format?, formats?, project?, platforms?, applyLabel?, onApply})
//   rel       — media/ path of the video; an edited file reopens with its source + settings
//   format    — preferred frame for a fresh edit: original | landscape | vertical | square
//   project   — {name, crop_x, crop_y, crop_scale} (crop in %, as on /projects)
//   platforms — () => ['discord', 'x', …] for the "fits the limit?" report
//   onApply   — ({out_rel, url, size_mb, duration, width, height, format, noop}) => …
// Settings mode (the Shorts Factory renders later, in its own job):
//   mode: 'spec', spec — the clip's settings, onChange(spec) on every edit,
//   panel — an element shown in the right column, onClose() — after “Done”/Esc.
// The render is one ffmpeg pass on the server (/api/video/edit, services/video_edit.py);
// the preview here is the same geometry (cover-scale + zoom + crop) and the same text PNGs.
(function () {
	const FORMATS = {
		original: { label: 'Original', w: 0, h: 0 },
		landscape: { label: '16:9', w: 1920, h: 1080 },
		vertical: { label: '9:16 · Shorts', w: 1080, h: 1920 },
		square: { label: '1:1', w: 1080, h: 1080 },
	};
	const POS = [['top', 'top'], ['upper', 'upper'], ['middle', 'middle'], ['lower', 'lower']];
	const SIZE = [['s', 'S'], ['m', 'M'], ['l', 'L']];
	const MAX_OVL = 6, MIN_CLIP = 0.5, MAX_LONG = 1920;
	const LEVEL_ICON = { ok: '✓', info: '↓', warn: '⚠', error: '✕' };

	const r2 = x => Math.round(x * 100) / 100;
	const esc = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
	const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));
	function fmtT(s) {
		if (s == null || isNaN(s)) return '—';
		s = Math.max(0, s);
		const m = Math.floor(s / 60), sec = s - m * 60;
		return `${m}:${sec < 10 ? '0' : ''}${sec.toFixed(1)}`;
	}
	const fmtMMSS = s => { s = Math.round(s || 0); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
	const mediaUrl = rel => '/media/' + String(rel).split('/').map(encodeURIComponent).join('/');

	// ---- stylesheet + track list (shared by every open) ------------------------
	(function css() {
		if (document.getElementById('ve_css')) return;
		const l = document.createElement('link');
		l.id = 've_css'; l.rel = 'stylesheet';
		const me = document.currentScript && document.currentScript.src;
		l.href = '/static/video_editor.css' + (me && me.includes('?') ? me.slice(me.indexOf('?')) : '');
		document.head.appendChild(l);
	})();

	let tracksPromise = null;
	function loadTracks(force) {
		if (!tracksPromise || force) {
			tracksPromise = fetch('/api/music/list').then(r => r.json()).catch(() => ({ tracks: [], folders: [] }));
		}
		return tracksPromise;
	}

	// ---- state ------------------------------------------------------------------
	let el = null, opts = null, src = null, spec = null, raf = 0;
	let tracks = [], wave = null, applied = '', job = null, pollT = 0, result = null;
	let specMode = false, opening = false;
	const imgs = new Map(); // overlay object → {img, key, url, timer}
	const audio = new Audio();
	audio.preload = 'auto';
	let soundOn = true, playing = false;

	const q = s => el.querySelector(s);
	const qa = s => el.querySelectorAll(s);

	function frameSize() {
		const f = FORMATS[spec.format];
		if (f.w) return [f.w, f.h];
		const w = src.width || 1920, h = src.height || 1080;
		const k = Math.min(1, MAX_LONG / Math.max(w, h));
		return [Math.max(2, Math.round(w * k / 2) * 2), Math.max(2, Math.round(h * k / 2) * 2)];
	}
	const srcDur = () => src.duration || (q('video').duration || 0);
	const tStart = () => spec.trim_start || 0;
	const tEnd = () => (spec.trim_end != null ? spec.trim_end : srcDur());
	const clipDur = () => Math.max(0, tEnd() - tStart());
	const sig = () => JSON.stringify(cleanSpec());

	function freshSpec(fmt) {
		return { format: fmt, crop_x: 0.5, crop_y: 0.5, zoom: 1, trim_start: null, trim_end: null,
			source_volume: 1, music: null, overlays: [] };
	}
	function cleanSpec() {
		const s = JSON.parse(JSON.stringify(spec));
		s.overlays = s.overlays.filter(o => (o.text || '').trim()).map(o => ({
			text: o.text.trim(), start: r2(+o.start || 0), end: r2(+o.end || 0), pos: o.pos, size: o.size }));
		if (s.trim_start != null && s.trim_start <= 0) s.trim_start = null;
		if (s.trim_end != null && s.trim_end >= srcDur() - 0.05) s.trim_end = null;
		return s;
	}

	// ---- build the modal once ----------------------------------------------------
	function build() {
		el = document.createElement('div');
		el.className = 've-backdrop';
		el.hidden = true;
		el.innerHTML = `
		<div class="ve-modal" role="dialog" aria-label="Video editor">
			<div class="ve-top">
				<b>🎬 Video editor</b><span class="ve-fname muted"></span>
				<button type="button" class="btn btn-sm ve-close" title="Esc">Close</button>
			</div>
			<div class="ve-body">
				<div class="ve-col ve-left">
					<div class="ve-formats">${Object.entries(FORMATS).map(([k, f]) =>
						`<button type="button" class="ve-fmt" data-fmt="${k}">${f.label}</button>`).join('')}</div>
					<div class="ve-stage-wrap">
						<div class="ve-stage">
							<video playsinline preload="auto"></video>
							<div class="ve-ovls"></div>
							<div class="ve-unsafe"><i class="u-top"></i><i class="u-bottom"></i><i class="u-right"></i></div>
							<div class="ve-stage-msg" hidden></div>
						</div>
					</div>
					<div class="ve-transport">
						<button type="button" class="btn btn-sm ve-play" title="Space">▶</button>
						<span class="ve-time">0:00.0</span>
						<span class="ve-frame-info muted"></span>
						<span class="ve-spacer"></span>
						<label class="ve-opt"><input type="checkbox" class="ve-sound" checked /> Sound</label>
						<label class="ve-opt ve-unsafe-opt"><input type="checkbox" class="ve-unsafe-toggle" checked /> UI zones</label>
					</div>
					<div class="ve-tl" title="Click — seek; drag the edges — trim (keys I / O)">
						<div class="ve-tl-bar">
							<div class="ve-tl-sel"></div>
							<div class="ve-tl-marks"></div>
							<div class="ve-tl-h ve-tl-hs"></div>
							<div class="ve-tl-h ve-tl-he"></div>
							<div class="ve-tl-play"></div>
						</div>
					</div>
					<div class="ve-trim">
						<label>Start <input type="number" class="ve-ts" min="0" step="0.1" /></label>
						<button type="button" class="btn btn-sm ve-ts-now" title="I">⟦ = current frame</button>
						<label>End <input type="number" class="ve-te" min="0" step="0.1" /></label>
						<button type="button" class="btn btn-sm ve-te-now" title="O">= current frame ⟧</button>
						<button type="button" class="btn btn-sm ve-trim-reset">Reset trim</button>
					</div>
					<div class="ve-dur"></div>
				</div>

				<div class="ve-col ve-mid">
					<h4>Frame</h4>
					<div class="ve-row">
						<label class="ve-grow">Zoom <span class="ve-zoom-val">1.00×</span>
							<input type="range" class="ve-zoom" min="1" max="4" step="0.01" value="1" /></label>
						<button type="button" class="btn btn-sm ve-center">Center</button>
						<button type="button" class="btn btn-sm ve-preset" hidden>Project crop</button>
					</div>
					<div class="ve-note">Drag the picture to choose what’s in frame; mouse wheel — zoom.</div>

					<h4>Sound</h4>
					<label class="ve-grow">Video sound <span class="ve-sv-val">100%</span>
						<input type="range" class="ve-sv" min="0" max="1.5" step="0.05" value="1" /></label>
					<div class="ve-row">
						<select class="ve-music ve-grow"><option value="">No music</option></select>
						<button type="button" class="btn btn-sm ve-music-refresh" title="Rescan folders">↻</button>
					</div>
					<div class="ve-music-empty ve-note" hidden></div>
					<div class="ve-music-opts" hidden>
						<canvas class="ve-wave" height="46" title="Click/drag — where in the track the music starts"></canvas>
						<div class="ve-row ve-small">
							<label>start at <input type="number" class="ve-ms" min="0" step="0.1" /> s</label>
							<label>vol. <input type="range" class="ve-mv" min="0" max="1.5" step="0.05" value="0.8" /> <span class="ve-mv-val"></span></label>
						</div>
						<div class="ve-row ve-small">
							<label>fade in <input type="number" class="ve-mfi" min="0" max="10" step="0.1" /> s</label>
							<label>fade out <input type="number" class="ve-mfo" min="0" max="10" step="0.1" /> s</label>
						</div>
					</div>

					<h4>On-screen text</h4>
					<div class="ve-quick">
						<button type="button" class="btn btn-sm" data-add="hook">＋ Hook</button>
						<button type="button" class="btn btn-sm" data-add="caption">＋ Caption</button>
						<button type="button" class="btn btn-sm" data-add="end">＋ End title</button>
					</div>
					<div class="ve-lines"></div>
				</div>

				<div class="ve-col ve-right">
					<div class="ve-render-box">
					<h4>Ready?</h4>
					<button type="button" class="btn btn-primary ve-apply">✓ Render & use in post</button>
					<button type="button" class="btn ve-render">Render to preview</button>
					<div class="ve-progress" hidden><div class="ve-bar"><i></i></div>
						<span class="ve-ptext"></span><button type="button" class="btn btn-sm ve-cancel">Stop</button></div>
					<div class="ve-err" hidden></div>
					<div class="ve-result" hidden>
						<video controls playsinline preload="metadata"></video>
						<div class="ve-res-info muted"></div>
						<div class="ve-res-stale" hidden>Settings changed after the render</div>
					</div>
					<div class="ve-fit"></div>
					<div class="ve-note">The source stays untouched: you get a new file. Reopen the editor on the result —
						the same settings come back. Network limits (Discord 10 MB etc.) are checked on the final file;
						on publish the video is re-encoded for each network.</div>
					</div>
					<div class="ve-spec-box" hidden>
						<button type="button" class="btn btn-primary ve-done">✓ Done</button>
						<div class="ve-extra"></div>
					</div>
				</div>
			</div>
		</div>`;
		document.body.appendChild(el);
		wire();
	}

	// ---- events ----------------------------------------------------------------------
	function wire() {
		const v = q('video');
		el.addEventListener('mousedown', e => { if (e.target === el) close(); });
		q('.ve-close').addEventListener('click', () => close());
		q('.ve-done').addEventListener('click', () => close(true));
		document.addEventListener('keydown', e => {
			if (!el || el.hidden) return;
			if (e.key === 'Escape') { close(); return; }
			const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target && e.target.tagName) || '') && e.target.type !== 'range' && e.target.type !== 'checkbox';
			if (typing) return;
			if (e.code === 'Space') { e.preventDefault(); togglePlay(); }
			else if (e.key === 'i' || e.key === 'I' || e.key === 'ш' || e.key === 'Ш') setTrim('start', v.currentTime);
			else if (e.key === 'o' || e.key === 'O' || e.key === 'щ' || e.key === 'Щ') setTrim('end', v.currentTime);
		});

		qa('.ve-fmt').forEach(b => b.addEventListener('click', () => setFormat(b.dataset.fmt)));
		q('.ve-play').addEventListener('click', togglePlay);
		v.addEventListener('play', () => { playing = true; q('.ve-play').textContent = '❚❚'; syncAudio(true); });
		v.addEventListener('pause', () => { playing = false; q('.ve-play').textContent = '▶'; audio.pause(); });
		v.addEventListener('seeked', () => syncAudio(true));
		v.addEventListener('loadedmetadata', () => {
			if (!src.duration && isFinite(v.duration)) src.duration = v.duration;
			if (!src.width) { src.width = v.videoWidth; src.height = v.videoHeight; }
			q('.ve-stage-msg').hidden = true;
			layout();
			renderTrim();
			v.currentTime = tStart();
		});
		v.addEventListener('error', () => {
			const m = q('.ve-stage-msg');
			m.hidden = false;
			m.textContent = 'The browser can’t play this video (codec). Trimming by numbers and rendering still work.';
		});
		q('.ve-sound').addEventListener('change', e => { soundOn = e.target.checked; applyVolumes(); syncAudio(true); });
		q('.ve-unsafe-toggle').addEventListener('change', layout);

		// crop: drag the picture, wheel = zoom, double click = center
		const stage = q('.ve-stage');
		let drag = null;
		stage.addEventListener('pointerdown', e => {
			if (e.button !== 0) return;
			drag = { x: e.clientX, y: e.clientY, cx: spec.crop_x, cy: spec.crop_y, moved: false };
			stage.setPointerCapture(e.pointerId);
		});
		stage.addEventListener('pointermove', e => {
			if (!drag) return;
			const g = geom();
			if (!g) return;
			const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
			if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
			if (g.w > g.W + 0.5) spec.crop_x = clamp(drag.cx - dx / (g.w - g.W), 0, 1);
			if (g.h > g.H + 0.5) spec.crop_y = clamp(drag.cy - dy / (g.h - g.H), 0, 1);
			layout();
		});
		stage.addEventListener('pointerup', () => {
			if (drag && !drag.moved) togglePlay();
			if (drag && drag.moved) changed();
			drag = null;
		});
		stage.addEventListener('dblclick', () => { spec.crop_x = 0.5; spec.crop_y = 0.5; layout(); changed(); });
		stage.addEventListener('wheel', e => {
			e.preventDefault();
			setZoom(spec.zoom * (e.deltaY < 0 ? 1.05 : 1 / 1.05));
		}, { passive: false });
		q('.ve-zoom').addEventListener('input', e => setZoom(+e.target.value));
		q('.ve-center').addEventListener('click', () => { spec.crop_x = 0.5; spec.crop_y = 0.5; layout(); changed(); });
		q('.ve-preset').addEventListener('click', applyProjectCrop);

		// timeline
		const bar = q('.ve-tl-bar');
		let tdrag = null;
		const timeAt = e => { const r = bar.getBoundingClientRect(); return clamp((e.clientX - r.left) / r.width, 0, 1) * srcDur(); };
		bar.addEventListener('pointerdown', e => {
			const t = e.target;
			tdrag = t.classList.contains('ve-tl-hs') ? 'start' : t.classList.contains('ve-tl-he') ? 'end' : 'seek';
			bar.setPointerCapture(e.pointerId);
			if (tdrag === 'seek') v.currentTime = timeAt(e);
		});
		bar.addEventListener('pointermove', e => {
			if (!tdrag) return;
			const t = timeAt(e);
			if (tdrag === 'seek') v.currentTime = t; else { setTrim(tdrag, t, true); v.currentTime = t; }
		});
		bar.addEventListener('pointerup', () => { if (tdrag && tdrag !== 'seek') changed(); tdrag = null; });
		q('.ve-ts').addEventListener('change', e => setTrim('start', e.target.value));
		q('.ve-te').addEventListener('change', e => setTrim('end', e.target.value === '' ? null : e.target.value));
		q('.ve-ts-now').addEventListener('click', () => setTrim('start', v.currentTime));
		q('.ve-te-now').addEventListener('click', () => setTrim('end', v.currentTime));
		q('.ve-trim-reset').addEventListener('click', () => { spec.trim_start = null; spec.trim_end = null; renderTrim(); changed(); });

		// sound + music
		q('.ve-sv').addEventListener('input', e => { spec.source_volume = +e.target.value; renderSound(); applyVolumes(); changed(); });
		q('.ve-music').addEventListener('change', e => setMusic(e.target.value));
		q('.ve-music-refresh').addEventListener('click', async () => { await fillTracks(true); });
		q('.ve-ms').addEventListener('change', e => { if (spec.music) { spec.music.start = clamp(+e.target.value || 0, 0, 3600); drawWave(); syncAudio(true); changed(); } });
		q('.ve-mv').addEventListener('input', e => { if (spec.music) { spec.music.volume = +e.target.value; renderMusic(); applyVolumes(); changed(); } });
		q('.ve-mfi').addEventListener('change', e => { if (spec.music) { spec.music.fade_in = clamp(+e.target.value || 0, 0, 10); changed(); } });
		q('.ve-mfo').addEventListener('change', e => { if (spec.music) { spec.music.fade_out = clamp(+e.target.value || 0, 0, 10); changed(); } });
		const wv = q('.ve-wave');
		let wdrag = false;
		const waveAt = e => {
			const r = wv.getBoundingClientRect();
			const d = (wave && wave.duration) || 0;
			// the click marks where the music starts (e.g. right on the drop)
			return clamp((e.clientX - r.left) / r.width * d, 0, Math.max(0, d - 0.5));
		};
		wv.addEventListener('pointerdown', e => { if (!spec.music || !wave) return; wdrag = true; wv.setPointerCapture(e.pointerId); spec.music.start = r2(waveAt(e)); renderMusic(); });
		wv.addEventListener('pointermove', e => { if (!wdrag) return; spec.music.start = r2(waveAt(e)); renderMusic(); });
		wv.addEventListener('pointerup', () => { if (wdrag) { wdrag = false; syncAudio(true); changed(); } });

		// text lines
		q('.ve-quick').addEventListener('click', e => {
			const kind = e.target.dataset && e.target.dataset.add;
			if (!kind || spec.overlays.length >= MAX_OVL) return;
			const d = clipDur() || 3;
			const rel = clamp(v.currentTime - tStart(), 0, d);
			let o;
			if (kind === 'hook') o = { text: '', start: 0, end: r2(Math.min(2.5, d)), pos: 'upper', size: 'l' };
			else if (kind === 'caption') {
				const s = r2(Math.min(rel, Math.max(0, d - 1.2)));
				o = { text: '', start: s, end: r2(Math.min(s + 2.5, d)), pos: spec.format === 'vertical' ? 'middle' : 'lower', size: 'm' };
			} else {
				const name = (opts.project && opts.project.name) || '';
				o = { text: name ? `${name} — Wishlist on Steam` : 'Wishlist on Steam', start: r2(Math.max(0, d - 3)), end: r2(d), pos: 'middle', size: 'l' };
			}
			spec.overlays.push(o);
			renderLines();
			const inputs = qa('.ve-lt');
			const last = inputs[inputs.length - 1];
			if (last) { last.focus(); last.select(); }
			changed();
		});

		// render
		q('.ve-render').addEventListener('click', () => render(false));
		q('.ve-apply').addEventListener('click', () => render(true));
		q('.ve-cancel').addEventListener('click', cancelRender);

		if (window.ResizeObserver) new ResizeObserver(() => { if (!el.hidden) layout(); }).observe(q('.ve-left'));
	}

	// ---- geometry: stage size, then the picture like ffmpeg crops it ---------------------
	function geom() {
		const v = q('video'), stage = q('.ve-stage');
		const W = stage.clientWidth, H = stage.clientHeight;
		const vw = v.videoWidth || src.width, vh = v.videoHeight || src.height;
		if (!W || !H || !vw || !vh) return null;
		const k = Math.max(W / vw, H / vh) * spec.zoom;
		return { W, H, w: vw * k, h: vh * k };
	}

	function layout() {
		if (!el || el.hidden || !spec) return;
		const [fw, fh] = frameSize();
		const wrap = q('.ve-stage-wrap'), stage = q('.ve-stage');
		const maxW = wrap.clientWidth || 480;
		const maxH = Math.min(window.innerHeight * 0.58, 560);
		let w = maxW, h = w * fh / fw;
		if (h > maxH) { h = maxH; w = h * fw / fh; }
		stage.style.width = Math.round(w) + 'px';
		stage.style.height = Math.round(h) + 'px';
		const g = geom();
		if (g) {
			Object.assign(q('video').style, {
				width: g.w + 'px', height: g.h + 'px',
				left: -(g.w - g.W) * spec.crop_x + 'px', top: -(g.h - g.H) * spec.crop_y + 'px',
			});
		}
		const vert = spec.format === 'vertical';
		q('.ve-unsafe').style.display = vert && q('.ve-unsafe-toggle').checked ? '' : 'none';
		q('.ve-unsafe-opt').style.display = vert ? '' : 'none';
		q('.ve-frame-info').textContent = `${fw}×${fh}`;
		q('.ve-stage').classList.toggle('ve-pannable', !!g && (g.w > g.W + 0.5 || g.h > g.H + 0.5));
	}

	function setFormat(fmt) {
		if (!FORMATS[fmt]) return;
		const first = spec.format !== fmt;
		spec.format = fmt;
		if (first && fmt === 'vertical' && opts.project && opts.project.crop_x != null && spec.crop_x === 0.5 && spec.zoom === 1) applyProjectCrop(true);
		qa('.ve-fmt').forEach(b => b.classList.toggle('active', b.dataset.fmt === fmt));
		layout();
		renderLines();   // overlay PNGs depend on the frame size
		renderTrim();
		changed();
	}

	function setZoom(z) {
		spec.zoom = r2(clamp(z, 1, 4));
		q('.ve-zoom').value = String(spec.zoom);
		q('.ve-zoom-val').textContent = spec.zoom.toFixed(2) + '×';
		layout();
		changed();
	}

	function applyProjectCrop(silent) {
		const p = opts.project;
		if (!p) return;
		if (p.crop_x != null) spec.crop_x = clamp(+p.crop_x / 100, 0, 1);
		if (p.crop_y != null) spec.crop_y = clamp(+p.crop_y / 100, 0, 1);
		if (p.crop_scale) { spec.zoom = r2(clamp(+p.crop_scale, 1, 4)); q('.ve-zoom').value = String(spec.zoom); q('.ve-zoom-val').textContent = spec.zoom.toFixed(2) + '×'; }
		layout();
		if (silent !== true) changed();
	}

	// ---- trim ----------------------------------------------------------------------------------
	function setTrim(which, val, live) {
		const d = srcDur();
		if (which === 'start') {
			let s = clamp(+val || 0, 0, d || 1e9);
			s = Math.min(s, Math.max(0, tEnd() - MIN_CLIP));
			spec.trim_start = s > 0.01 ? r2(s) : null;
		} else {
			let e = val == null ? null : +val;
			if (e != null && d && e >= d - 0.05) e = null;
			if (e != null) e = r2(Math.max(e, tStart() + MIN_CLIP));
			spec.trim_end = e;
		}
		renderTrim();
		if (!live) changed();
	}

	function renderTrim() {
		if (!spec) return;
		const d = srcDur() || 1;
		q('.ve-ts').value = (tStart()).toFixed(1);
		q('.ve-te').value = spec.trim_end != null ? spec.trim_end.toFixed(1) : (srcDur() ? srcDur().toFixed(1) : '');
		const a = tStart() / d * 100, b = tEnd() / d * 100;
		Object.assign(q('.ve-tl-sel').style, { left: a + '%', width: Math.max(0, b - a) + '%' });
		q('.ve-tl-hs').style.left = a + '%';
		q('.ve-tl-he').style.left = b + '%';
		q('.ve-tl-marks').innerHTML = spec.overlays.filter(o => (o.text || '').trim()).map(o => {
			const s = (tStart() + (+o.start || 0)) / d * 100, e = (tStart() + Math.min(+o.end || 0, clipDur())) / d * 100;
			return `<i style="left:${s}%;width:${Math.max(0.5, e - s)}%"></i>`;
		}).join('');
		const c = clipDur();
		let hint = '', cls = '';
		if (spec.format === 'vertical') {
			cls = c >= 12 && c <= 25 ? 'ok' : (c < 5 || c > 60 ? 'err' : 'warn');
			hint = 'Short: aim for 12–25 s';
		} else if (c > 140) { cls = 'warn'; hint = 'X accepts up to 2:20'; }
		q('.ve-dur').innerHTML = `Total: <b class="${cls}">${fmtT(c)}</b> of ${fmtT(srcDur())}` + (hint ? ` <span class="muted">(${hint})</span>` : '');
	}

	// ---- sound + music --------------------------------------------------------------------------
	function renderSound() {
		q('.ve-sv').value = String(spec.source_volume);
		q('.ve-sv-val').textContent = spec.source_volume > 0 ? Math.round(spec.source_volume * 100) + '%' : 'off';
	}

	async function fillTracks(force) {
		const j = await loadTracks(force);
		tracks = j.tracks || [];
		const sel = q('.ve-music');
		const cur = spec.music ? spec.music.path : '';
		const groups = {};
		tracks.forEach(t => { (groups[t.folder || ''] = groups[t.folder || ''] || []).push(t); });
		let html = '<option value="">No music</option>';
		const pm = opts.project && opts.project.music_path;
		if (pm) html += `<option value="${esc(pm)}">★ ${esc(pm.split(/[\\/]/).pop())} (project track)</option>`;
		Object.entries(groups).forEach(([g, list]) => {
			html += `<optgroup label="${esc(g)}">` + list.map(t =>
				`<option value="${esc(t.path)}">${esc(t.name.replace(/\.[^.]+$/, ''))} · ${fmtMMSS(t.duration_sec)}</option>`).join('') + '</optgroup>';
		});
		sel.innerHTML = html;
		if (cur && ![...sel.options].some(o => o.value === cur)) sel.add(new Option(cur.split(/[\\/]/).pop(), cur));
		sel.value = cur;
		const empty = q('.ve-music-empty');
		if (!tracks.length) {
			const dirs = (j.folders || []).map(f => f.path + (f.exists ? '' : ' (folder not found)')).join('; ');
			empty.innerHTML = (dirs ? `Music folders are empty: ${esc(dirs)}. ` : 'No music folders set. ') +
				'<a href="/accounts#section_video" target="_blank">Settings → Video & music</a>';
			empty.hidden = false;
		} else empty.hidden = true;
	}

	function setMusic(path) {
		if (!path) { spec.music = null; audio.pause(); audio.removeAttribute('src'); wave = null; }
		else {
			const prev = spec.music || {};
			spec.music = { path, start: prev.path === path ? prev.start : 0, volume: prev.volume != null ? prev.volume : 0.8,
				fade_in: prev.fade_in != null ? prev.fade_in : 0.5, fade_out: prev.fade_out != null ? prev.fade_out : 1.5 };
			loadMusic();
		}
		renderMusic();
		changed();
	}

	function loadMusic() {
		const path = spec.music && spec.music.path;
		if (!path) return;
		const url = '/api/music/file?path=' + encodeURIComponent(path);
		if (audio.dataset.path !== path) { audio.src = url; audio.dataset.path = path; }
		wave = null;
		drawWave();
		fetch('/api/music/peaks?path=' + encodeURIComponent(path)).then(r => r.ok ? r.json() : null).then(j => {
			if (j && spec.music && spec.music.path === path) { wave = j; drawWave(); }
		}).catch(() => {});
		syncAudio(true);
	}

	function renderMusic() {
		const m = spec.music;
		q('.ve-music-opts').hidden = !m;
		if (!m) return;
		q('.ve-ms').value = m.start.toFixed(1);
		q('.ve-mv').value = String(m.volume);
		q('.ve-mv-val').textContent = Math.round(m.volume * 100) + '%';
		q('.ve-mfi').value = m.fade_in;
		q('.ve-mfo').value = m.fade_out;
		drawWave();
	}

	function drawWave() {
		const c = q('.ve-wave');
		if (!c || !spec.music) return;
		const W = c.clientWidth || 300, H = c.height;
		if (c.width !== W) c.width = W;
		const g = c.getContext('2d');
		g.clearRect(0, 0, W, H);
		const d = (wave && wave.duration) || 0;
		if (!wave || !wave.peaks || !wave.peaks.length || !d) {
			g.fillStyle = '#94a3b8'; g.font = '12px sans-serif'; g.fillText('loading waveform…', 8, H / 2 + 4);
			return;
		}
		// the part of the track that plays under the clip (it loops if the clip is longer)
		const a = spec.music.start / d * W, b = Math.min(W, (spec.music.start + clipDur()) / d * W);
		g.fillStyle = 'rgba(37,99,235,.14)';
		g.fillRect(a, 0, Math.max(2, b - a), H);
		const p = wave.peaks, n = p.length;
		for (let x = 0; x < W; x++) {
			const v = p[Math.min(n - 1, Math.floor(x / W * n))] || 0;
			const h = Math.max(1, v * (H - 4));
			g.fillStyle = x >= a && x <= b ? '#2563eb' : '#94a3b8';
			g.fillRect(x, (H - h) / 2, 1, h);
		}
		g.fillStyle = '#dc2626';
		g.fillRect(a, 0, 2, H);
		if (playing) {
			const v = q('video');
			const t = (spec.music.start + Math.max(0, v.currentTime - tStart())) % d;
			g.fillStyle = '#0f172a';
			g.fillRect(t / d * W, 0, 1, H);
		}
	}

	function musicEnvelope(rel) {
		const m = spec.music;
		if (!m) return 1;
		let k = 1;
		if (m.fade_in > 0) k = Math.min(k, rel / m.fade_in);
		if (m.fade_out > 0) k = Math.min(k, (clipDur() - rel) / m.fade_out);
		return clamp(k, 0, 1);
	}

	function applyVolumes() {
		const v = q('video');
		v.muted = !soundOn || spec.source_volume <= 0;
		v.volume = clamp(spec.source_volume, 0, 1);
	}

	function syncAudio(force) {
		const v = q('video');
		if (!spec || !spec.music || !audio.src || !soundOn || !playing) { audio.pause(); return; }
		const rel = Math.max(0, v.currentTime - tStart());
		let t = spec.music.start + rel;
		const d = audio.duration || (wave && wave.duration) || 0;
		if (d > 0) t = t % d;
		if (force || Math.abs(audio.currentTime - t) > 0.3) { try { audio.currentTime = t; } catch (e) {} }
		if (audio.paused) audio.play().catch(() => {});
	}

	// ---- text lines (same model as the Shorts editor) ----------------------------------------------
	function optionList(list, val) {
		return list.map(([k, t]) => `<option value="${k}"${k === val ? ' selected' : ''}>${t}</option>`).join('');
	}

	function renderLines() {
		const host = q('.ve-lines');
		host.innerHTML = '';
		for (const [o, rec] of imgs) {
			if (!spec || !spec.overlays.includes(o)) { rec.img.remove(); if (rec.url) URL.revokeObjectURL(rec.url); imgs.delete(o); }
		}
		if (!spec) return;
		spec.overlays.forEach((o, i) => {
			const row = document.createElement('div');
			row.className = 've-line';
			row.innerHTML = `
				<div class="ve-line-top"><span class="ve-n">${i + 1}</span>
					<input type="text" class="ve-lt" maxlength="120" placeholder="Line text…" value="${esc(o.text)}" />
					<button type="button" class="btn btn-sm ve-del" title="Delete">✕</button></div>
				<div class="ve-line-bot">
					<label>from <input type="number" class="ve-ls" min="0" step="0.1" value="${(+o.start).toFixed(1)}" /></label>
					<label>to <input type="number" class="ve-le" min="0" step="0.1" value="${(+o.end).toFixed(1)}" /></label>
					<select class="ve-lp">${optionList(POS, o.pos)}</select>
					<select class="ve-lz">${optionList(SIZE, o.size)}</select>
					<button type="button" class="btn btn-sm ve-go" title="Go to line">▸</button>
				</div>`;
			row.querySelector('.ve-lt').addEventListener('input', e => { o.text = e.target.value; lineChanged(o); });
			row.querySelector('.ve-ls').addEventListener('change', e => { o.start = r2(Math.max(0, +e.target.value || 0)); lineChanged(o); });
			row.querySelector('.ve-le').addEventListener('change', e => { o.end = r2(Math.max(0, +e.target.value || 0)); lineChanged(o); });
			row.querySelector('.ve-lp').addEventListener('change', e => { o.pos = e.target.value; lineChanged(o); });
			row.querySelector('.ve-lz').addEventListener('change', e => { o.size = e.target.value; lineChanged(o); });
			row.querySelector('.ve-del').addEventListener('click', () => {
				spec.overlays.splice(spec.overlays.indexOf(o), 1);
				renderLines();
				changed();
			});
			row.querySelector('.ve-go').addEventListener('click', () => { q('video').currentTime = tStart() + (+o.start || 0) + 0.05; });
			host.appendChild(row);
			ensurePreview(o);
		});
		qa('.ve-quick button').forEach(b => { b.disabled = spec.overlays.length >= MAX_OVL; });
		renderTrim();
	}

	function lineChanged(o) { ensurePreview(o); renderTrim(); changed(); }

	function ensurePreview(o) {
		let rec = imgs.get(o);
		if (!rec) {
			const img = document.createElement('img');
			img.alt = '';
			q('.ve-ovls').appendChild(img);
			rec = { img, key: '', url: '', timer: 0 };
			imgs.set(o, rec);
		}
		const [w, h] = frameSize();
		const key = `${(o.text || '').trim()}|${o.pos}|${o.size}|${w}x${h}`;
		if (key === rec.key) return;
		rec.key = key;
		clearTimeout(rec.timer);
		if (!(o.text || '').trim()) { rec.img.removeAttribute('src'); return; }
		rec.timer = setTimeout(async () => {
			try {
				const r = await fetch('/api/video/overlay_preview', {
					method: 'POST', headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ text: o.text, pos: o.pos, size: o.size, w, h }),
				});
				if (!r.ok || rec.key !== key) return;
				const url = URL.createObjectURL(await r.blob());
				if (rec.url) URL.revokeObjectURL(rec.url);
				rec.url = url;
				rec.img.src = url;
			} catch (e) { /* keep the previous image */ }
		}, 250);
	}

	// ---- playback loop --------------------------------------------------------------------------------
	function togglePlay() {
		const v = q('video');
		if (v.paused) { if (v.currentTime >= tEnd() - 0.05 || v.currentTime < tStart()) v.currentTime = tStart(); v.play().catch(() => {}); }
		else v.pause();
	}

	function tick() {
		if (!el || el.hidden) return;
		const v = q('video');
		const ts = tStart(), te = tEnd();
		if (playing && te && v.currentTime >= te - 0.03) { v.currentTime = ts; }
		else if (playing && v.currentTime < ts - 0.05) { v.currentTime = ts; }
		const rel = v.currentTime - ts;
		for (const [o, rec] of imgs) {
			rec.img.style.display = (rec.img.getAttribute('src') && rel >= (+o.start || 0) && rel < (+o.end || 0)) ? '' : 'none';
		}
		qa('.ve-line').forEach((row, i) => {
			const o = spec.overlays[i];
			row.classList.toggle('ve-active', !!o && rel >= (+o.start || 0) && rel < (+o.end || 0));
		});
		const d = srcDur() || 1;
		q('.ve-tl-play').style.left = (v.currentTime / d * 100) + '%';
		q('.ve-time').textContent = `${fmtT(Math.max(0, rel))} / ${fmtT(clipDur())}`;
		if (spec.music && playing) {
			audio.volume = clamp(spec.music.volume * musicEnvelope(rel), 0, 1);
			syncAudio(false);
			drawWave();
		}
		raf = requestAnimationFrame(tick);
	}

	// ---- render + apply ---------------------------------------------------------------------------------
	function changed() {
		if (specMode) {
			if (!opening && opts.onChange) opts.onChange(cleanSpec());
			return;
		}
		const stale = !!result && sig() !== result.sig;
		q('.ve-res-stale').hidden = !stale;
		q('.ve-apply').textContent = result && !stale ? `✓ ${opts.applyLabel || 'Use'}` : `✓ Render & ${opts.applyShort || 'use in post'}`;
	}

	function showProgress(on, text, p) {
		q('.ve-progress').hidden = !on;
		q('.ve-render').disabled = on;
		q('.ve-apply').disabled = on;
		if (on) {
			q('.ve-bar i').style.width = Math.round((p || 0) * 100) + '%';
			q('.ve-ptext').textContent = text || '';
		}
	}

	async function render(thenApply) {
		const s = cleanSpec();
		const key = JSON.stringify(s);
		if (thenApply && result && result.sig === key) { apply(); return; }
		q('.ve-err').hidden = true;
		showProgress(true, 'Rendering…', 0);
		try {
			const r = await fetch('/api/video/edit', {
				method: 'POST', headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ src: src.rel, spec: s }),
			});
			const j = await r.json().catch(() => ({}));
			if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
			job = j;
			while (job.status === 'queued' || job.status === 'running') {
				showProgress(true, job.status === 'queued' ? 'Queued…' : `Rendering ${Math.round((job.progress || 0) * 100)}%`, job.progress);
				await new Promise(res => { pollT = setTimeout(res, 700); });
				const pr = await fetch('/api/video/edit/' + job.id);
				if (!pr.ok) throw new Error('render lost (server restarted?)');
				job = await pr.json();
			}
			if (job.status !== 'done') throw new Error(job.status === 'cancelled' ? 'Render stopped' : (job.error || 'Render failed'));
			result = { ...job, sig: key, format: s.format };
			showResult();
			showProgress(false);
			changed();
			if (thenApply) apply();
		} catch (e) {
			showProgress(false);
			const box = q('.ve-err');
			box.hidden = false;
			box.textContent = '✕ ' + e.message;
		} finally {
			job = null;
		}
	}

	function cancelRender() {
		clearTimeout(pollT);
		if (job && job.id) fetch('/api/video/edit/' + job.id + '/cancel', { method: 'POST' });
	}

	function showResult() {
		const box = q('.ve-result');
		box.hidden = false;
		const rv = box.querySelector('video');
		if (rv.dataset.rel !== result.out_rel) { rv.src = mediaUrl(result.out_rel) + '?t=' + Date.now(); rv.dataset.rel = result.out_rel; }
		q('.ve-res-info').textContent = result.noop
			? `No changes — the source file goes as is · ${(+result.size_mb).toFixed(1)} MB`
			: `${result.width}×${result.height} · ${fmtT(result.duration)} · ${(+result.size_mb).toFixed(1)} MB`;
		loadFit(result.out_rel);
	}

	async function loadFit(rel) {
		const box = q('.ve-fit');
		const plats = (opts.platforms && opts.platforms()) || [];
		if (!plats.length) { box.innerHTML = ''; return; }
		try {
			const r = await fetch(`/api/video/fit?rel=${encodeURIComponent(rel)}&platforms=${encodeURIComponent(plats.join(','))}`);
			const j = await r.json();
			box.innerHTML = '<h4>Network limits</h4>' + (j.rows || []).map(row =>
				`<div class="ve-fit-row ve-fit-${row.level}"><span class="ve-fi">${LEVEL_ICON[row.level] || '•'}</span>` +
				`<span><b>${esc(row.label)}</b>: ${esc(row.text)}</span></div>`).join('');
		} catch (e) { box.innerHTML = ''; }
	}

	function apply() {
		if (!result) return;
		applied = result.sig;
		const out = { out_rel: result.out_rel, url: result.url, size_mb: result.size_mb, duration: result.duration,
			width: result.width, height: result.height, format: result.format, noop: !!result.noop, src: src.rel };
		close(true);
		if (opts.onApply) opts.onApply(out);
	}

	// ---- open / close -------------------------------------------------------------------------------------
	async function open(o) {
		if (!el) build();
		if (!el.hidden) {  // another clip while open (the Shorts Factory's ← / →)
			cancelAnimationFrame(raf);
			q('video').pause();
			audio.pause();
		}
		opts = o || {};
		specMode = opts.mode === 'spec';
		opening = true;
		let info;
		try {
			const r = await fetch('/api/video/probe?rel=' + encodeURIComponent(opts.rel || ''));
			info = await r.json();
			if (!r.ok) throw new Error(info.detail || 'couldn’t open the video');
		} catch (e) { opening = false; alert('Video editor: ' + e.message); return; }
		if (specMode) info.edit = null;  // settings come from the page, the file is the source
		src = info.edit ? Object.assign({}, info.source)
			: { rel: info.rel, url: info.url, duration: info.duration, width: info.width, height: info.height,
				has_audio: info.has_audio, fps: info.fps, size_mb: info.size_mb };
		const prev = specMode ? opts.spec : (info.edit && info.edit.spec);
		spec = Object.assign(freshSpec(FORMATS[opts.format] ? opts.format : 'original'), prev || {});
		spec.overlays = (spec.overlays || []).map(x => Object.assign({}, x));
		if (spec.music) spec.music = Object.assign({ start: 0, volume: 0.8, fade_in: 0.5, fade_out: 1.5 }, spec.music);
		if (!prev && spec.format === 'vertical') applyProjectCrop(true);
		result = null; wave = null;
		if (info.edit) {  // reopened an edited file: it is the current result
			result = { out_rel: info.rel, url: info.url, size_mb: info.size_mb, duration: info.duration,
				width: info.width, height: info.height, format: spec.format, sig: '' };
		}

		q('.ve-fname').textContent = opts.title || (String(src.rel).split('/').pop() + (info.edit ? '  (re-edit)' : ''));
		const allowed = opts.formats && opts.formats.length ? opts.formats : Object.keys(FORMATS);
		qa('.ve-fmt').forEach(b => { b.hidden = !allowed.includes(b.dataset.fmt); b.classList.toggle('active', b.dataset.fmt === spec.format); });
		q('.ve-formats').hidden = allowed.length < 2;
		q('.ve-preset').hidden = !(opts.project && opts.project.crop_x != null);
		q('.ve-render-box').hidden = specMode;
		q('.ve-spec-box').hidden = !specMode;
		q('.ve-close').textContent = specMode ? 'Done' : 'Close';
		q('.ve-extra').replaceChildren(...(specMode && opts.panel ? [opts.panel] : []));
		q('.ve-err').hidden = true;
		q('.ve-result').hidden = true;
		q('.ve-fit').innerHTML = '';
		showProgress(false);

		el.hidden = false;
		document.body.style.overflow = 'hidden';
		const v = q('video');
		const url = mediaUrl(src.rel);
		if (v.dataset.rel !== src.rel) { v.src = url; v.dataset.rel = src.rel; } else { layout(); v.currentTime = tStart(); }
		soundOn = q('.ve-sound').checked;
		applyVolumes();
		setZoom(spec.zoom);
		renderSound();
		renderTrim();
		renderLines();
		await fillTracks(false);
		renderMusic();
		if (spec.music) loadMusic();
		if (result) {
			result.sig = sig();
			showResult();
		}
		applied = sig();
		changed();
		opening = false;
		layout();
		cancelAnimationFrame(raf);
		raf = requestAnimationFrame(tick);
	}

	function close(force) {
		if (!el || el.hidden) return;
		// settings mode: every edit already went to the page (onChange)
		if (!specMode && force !== true && sig() !== applied && !confirm('Close the editor? Your changes won’t reach the post.')) return;
		cancelRender();
		cancelAnimationFrame(raf);
		q('video').pause();
		audio.pause();
		el.hidden = true;
		document.body.style.overflow = '';
		spec && (spec.overlays = []);
		renderLines();  // drops the overlay previews
		if (specMode && opts.onClose) opts.onClose();
	}

	window.VideoEditor = { open, close, loadTracks, FORMATS, isOpen: () => !!el && !el.hidden };
})();
