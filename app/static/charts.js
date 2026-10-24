/* IndiePulse charts: stat-tile sparklines and goal charts. Plain SVG, no dependencies.
 * One accent (--primary) for the series that matters, one context gray (#8492a6,
 * validated at >= 3:1 on white) for history before a goal started. Hairline solid
 * grid; dashed lines are reserved for reference values (target, pre-start average).
 * Every value is also reachable without hovering: the tiles print their numbers and
 * the goal page has a table view. */
(function () {
	'use strict';
	const NS = 'http://www.w3.org/2000/svg';
	const root = getComputedStyle(document.documentElement);
	const tok = (name, fallback) => (root.getPropertyValue(name) || fallback).trim();
	const C = {
		accent: tok('--primary', '#2563eb'),
		context: '#8492a6',
		grid: tok('--border', '#e5e7eb'),
		ink: tok('--text', '#1e1e1e'),
		muted: tok('--muted', '#64748b'),
		good: tok('--success', '#16a34a'),
		event: '#334155',
		surface: '#ffffff',
	};
	const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

	function fmtDay(s) {
		const p = String(s).split('-').map(Number);
		return MONTHS[p[1] - 1] + ' ' + p[2];
	}
	function fmtNum(v) {
		if (v === null || v === undefined || isNaN(v)) return '—';
		const a = Math.abs(v);
		let s;
		if (a >= 10000) s = (a / 1000).toFixed(a >= 100000 ? 0 : 1).replace(/\.0$/, '') + 'K';
		else s = a.toLocaleString('en-US', { maximumFractionDigits: 1 });
		return (v < 0 ? '−' : '') + s;
	}
	function svgEl(tag, attrs, parent) {
		const e = document.createElementNS(NS, tag);
		for (const k in attrs) e.setAttribute(k, attrs[k]);
		if (parent) parent.appendChild(e);
		return e;
	}
	function text(parent, x, y, str, attrs) {
		const t = svgEl('text', Object.assign({ x: x, y: y, 'font-size': 11, fill: C.muted }, attrs || {}), parent);
		t.textContent = str;
		return t;
	}
	// A 4-tick axis whose step is 1/2/2.5/5 × 10^k, so every tick is a round number.
	const TICKS = 4;
	function niceStep(raw) {
		if (!(raw > 0)) return 1;
		const p = Math.pow(10, Math.floor(Math.log10(raw)));
		const n = raw / p;
		return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p;
	}
	function niceScale(lo, hi, integer) {
		let step = niceStep((hi - lo) / TICKS || 1);
		if (integer) step = Math.max(1, Math.round(step));
		let min = Math.floor(lo / step) * step;
		while (min + step * TICKS < hi) step = niceStep(step * 1.01 + 1e-9);
		min = Math.floor(lo / step) * step;
		return { min: min, max: min + step * TICKS };
	}
	function pathOf(points) {
		return points.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
	}

	// ── tooltip (one per page; values lead, labels follow) ─────────────────────
	let tip = null;
	function showTip(pageX, pageY, head, rows) {
		if (!tip) {
			tip = document.createElement('div');
			tip.className = 'chart-tip';
			tip.setAttribute('role', 'tooltip');
			document.body.appendChild(tip);
		}
		tip.textContent = '';
		const h = document.createElement('div');
		h.className = 'tip-head';
		h.textContent = head;
		tip.appendChild(h);
		rows.forEach(r => {
			const row = document.createElement('div');
			row.className = 'tip-row';
			if (r.key) {
				const k = document.createElement('span');
				k.className = 'tip-key';
				k.style.background = r.key;
				row.appendChild(k);
			}
			if (r.value !== undefined && r.value !== null) {
				const b = document.createElement('b');
				b.textContent = r.value;
				row.appendChild(b);
			}
			const s = document.createElement('span');
			s.textContent = r.label || '';
			row.appendChild(s);
			tip.appendChild(row);
		});
		tip.hidden = false;
		const w = tip.offsetWidth, hh = tip.offsetHeight;
		const vw = window.scrollX + document.documentElement.clientWidth;
		let left = pageX + 14, top = pageY - hh - 12;
		if (left + w > vw - 8) left = pageX - w - 14;
		if (top < window.scrollY + 8) top = pageY + 16;
		tip.style.left = left + 'px';
		tip.style.top = top + 'px';
	}
	function hideTip() { if (tip) tip.hidden = true; }

	// ── sparkline: last 7 days in the accent, the rest as context ──────────────
	function sparkline(host) {
		let vals, days;
		try {
			vals = JSON.parse(host.dataset.spark || '[]');
			days = JSON.parse(host.dataset.days || '[]');
		} catch (e) { return; }
		if (vals.length < 2) return;
		const W = Math.max(80, host.clientWidth || 140), H = Math.max(20, host.clientHeight || 36), pad = 5;
		const level = host.dataset.kind === 'level';
		const max = Math.max.apply(null, vals);
		const min = level ? Math.min.apply(null, vals) : Math.min(0, Math.min.apply(null, vals));
		const x = i => pad + i * (W - 2 * pad) / (vals.length - 1);
		const y = v => (max === min) ? H / 2 : H - pad - (v - min) / (max - min) * (H - 2 * pad);
		const pts = vals.map((v, i) => [x(i), y(v)]);
		const split = Math.max(0, vals.length - 7);
		const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, width: W, height: H, class: 'spark-svg' });
		svgEl('path', { d: pathOf(pts.slice(0, split + 1)), fill: 'none', stroke: C.context, 'stroke-width': 1.5,
			'stroke-linejoin': 'round', 'stroke-linecap': 'round' }, svg);
		svgEl('path', { d: pathOf(pts.slice(split)), fill: 'none', stroke: C.accent, 'stroke-width': 2,
			'stroke-linejoin': 'round', 'stroke-linecap': 'round' }, svg);
		const cross = svgEl('line', { y1: 0, y2: H, stroke: C.grid, 'stroke-width': 1, visibility: 'hidden' }, svg);
		const last = pts[pts.length - 1];
		svgEl('circle', { cx: last[0], cy: last[1], r: 4, fill: C.accent, stroke: C.surface, 'stroke-width': 2 }, svg);
		svgEl('rect', { x: 0, y: 0, width: W, height: H, fill: 'transparent' }, svg);
		svg.addEventListener('pointermove', e => {
			const r = svg.getBoundingClientRect();
			const i = Math.max(0, Math.min(vals.length - 1,
				Math.round((e.clientX - r.left - pad * r.width / W) / ((r.width - 2 * pad * r.width / W) / (vals.length - 1)))));
			cross.setAttribute('x1', x(i));
			cross.setAttribute('x2', x(i));
			cross.setAttribute('visibility', 'visible');
			showTip(e.pageX, e.pageY, days[i] ? fmtDay(days[i]) : '', [{ value: fmtNum(vals[i]), label: ' ' + (host.dataset.label || '') }]);
		});
		svg.addEventListener('pointerleave', () => { cross.setAttribute('visibility', 'hidden'); hideTip(); });
		host.textContent = '';
		host.appendChild(svg);
	}

	// ── goal charts ─────────────────────────────────────────────────────────────
	const M = { l: 44, r: 64, t: 16, b: 44 };  // b = x labels + the events lane

	function frame(host, height) {
		const W = Math.max(320, host.clientWidth || 640);
		const svg = svgEl('svg', { viewBox: `0 0 ${W} ${height}`, width: W, height: height, class: 'chart-svg' });
		host.textContent = '';
		host.appendChild(svg);
		return { svg, W, H: height, pw: W - M.l - M.r, ph: height - M.t - M.b };
	}

	function axes(f, n, days, yMin, yMax, yOf, xOf) {
		const g = svgEl('g', {}, f.svg);
		for (let i = 0; i <= TICKS; i++) {
			const v = yMin + (yMax - yMin) * i / TICKS;
			const yy = yOf(v);
			svgEl('line', { x1: M.l, x2: M.l + f.pw, y1: yy, y2: yy, stroke: C.grid, 'stroke-width': 1 }, g);
			text(g, M.l - 8, yy + 4, fmtNum(v), { 'text-anchor': 'end', style: 'font-variant-numeric: tabular-nums' });
		}
		const every = Math.max(1, Math.ceil(n / 6));
		for (let i = 0; i < n; i += every) {
			text(g, xOf(i), f.H - 6, fmtDay(days[i]), { 'text-anchor': 'middle' });
		}
	}

	function eventsLane(f, d, xOf, idxOf) {
		const byDay = {};
		(d.events || []).forEach(e => { (byDay[e.day] = byDay[e.day] || []).push(e); });
		const laneY = f.H - M.b + 18;
		Object.keys(byDay).forEach(day => {
			const i = idxOf[day];
			if (i === undefined) return;
			const evs = byDay[day];
			const g = svgEl('g', { class: 'ev-mark', tabindex: 0 }, f.svg);
			svgEl('circle', { cx: xOf(i), cy: laneY, r: 12, fill: 'transparent' }, g);  // hit area
			svgEl('circle', { cx: xOf(i), cy: laneY, r: 4.5, fill: C.event, stroke: C.surface, 'stroke-width': 2 }, g);
			if (evs.length > 1) text(g, xOf(i) + 7, laneY + 4, String(evs.length), { 'font-size': 10 });
			const show = e => {
				const r = f.svg.getBoundingClientRect();
				const px = r.left + window.scrollX + xOf(i) * r.width / f.W;
				const py = r.top + window.scrollY + laneY * r.height / f.H;
				showTip(e && e.pageX ? e.pageX : px, e && e.pageY ? e.pageY : py, fmtDay(day),
					evs.map(ev => ({ label: (ev.kind === 'post' ? '📣 ' : '✍️ ') + ev.title +
						(ev.platforms && ev.platforms.length ? ' · ' + ev.platforms.join(', ') : '') })));
			};
			g.addEventListener('pointerenter', show);
			g.addEventListener('focus', () => show(null));
			g.addEventListener('pointerleave', hideTip);
			g.addEventListener('blur', hideTip);
		});
		return byDay;
	}

	function refLine(f, yy, label, color) {
		svgEl('line', { x1: M.l, x2: M.l + f.pw, y1: yy, y2: yy, stroke: color, 'stroke-width': 1.5,
			'stroke-dasharray': '5 4' }, f.svg);
		text(f.svg, M.l + f.pw + 6, yy + 4, label, { fill: C.muted });
	}

	function lineChart(host, d, key) {
		const f = frame(host, 240);
		const days = d.days, vals = d[key] || [];
		const n = days.length;
		if (!n) return;
		const nums = vals.filter(v => v !== null && v !== undefined);
		const refs = [d.target].concat(d.kind === 'level' && d.baseline !== null ? [d.baseline] : []).filter(v => v !== null && v !== undefined);
		const all = nums.concat(refs);
		let sc;
		if (d.kind === 'level' && nums.length) {
			const lo = Math.min.apply(null, all), hi = Math.max.apply(null, all);
			const padV = Math.max(1, (hi - lo) * 0.1);
			sc = niceScale(Math.max(0, lo - padV), hi + padV, true);
		} else {
			sc = niceScale(0, Math.max.apply(null, all.concat([1])) * 1.05, false);
		}
		const yMin = sc.min, yMax = sc.max;
		const xOf = i => M.l + (n === 1 ? f.pw / 2 : i * f.pw / (n - 1));
		const yOf = v => M.t + f.ph - (v - yMin) / ((yMax - yMin) || 1) * f.ph;
		const idxOf = {};
		days.forEach((dd, i) => { idxOf[dd] = i; });
		axes(f, n, days, yMin, yMax, yOf, xOf);

		const si = idxOf[d.start];
		if (si !== undefined) {
			svgEl('line', { x1: xOf(si), x2: xOf(si), y1: M.t, y2: M.t + f.ph, stroke: C.muted, 'stroke-width': 1 }, f.svg);
			text(f.svg, xOf(si) + 4, M.t + 10, 'start', {});
		}
		if (d.target !== null && d.target !== undefined) refLine(f, yOf(d.target), 'goal ' + fmtNum(d.target), C.muted);
		if (d.kind === 'level' && d.baseline !== null && d.baseline !== undefined) {
			refLine(f, yOf(d.baseline), 'base ' + fmtNum(d.baseline), C.grid);
		}

		// series: split into runs of known values
		const pts = [];
		vals.forEach((v, i) => { if (v !== null && v !== undefined) pts.push([xOf(i), yOf(v), i]); });
		if (pts.length) {
			if (key === 'cum') {
				const area = pathOf(pts) + ` L${pts[pts.length - 1][0].toFixed(1)} ${yOf(yMin).toFixed(1)} L${pts[0][0].toFixed(1)} ${yOf(yMin).toFixed(1)} Z`;
				svgEl('path', { d: area, fill: C.accent, 'fill-opacity': 0.1, stroke: 'none' }, f.svg);
			}
			svgEl('path', { d: pathOf(pts), fill: 'none', stroke: C.accent, 'stroke-width': 2,
				'stroke-linejoin': 'round', 'stroke-linecap': 'round' }, f.svg);
			const last = pts[pts.length - 1];
			svgEl('circle', { cx: last[0], cy: last[1], r: 4, fill: C.accent, stroke: C.surface, 'stroke-width': 2 }, f.svg);
			text(f.svg, last[0] + 8, last[1] - 8, fmtNum(vals[last[2]]), { fill: C.ink, 'font-weight': 600 });
		}
		if (d.achieved_day && idxOf[d.achieved_day] !== undefined && vals[idxOf[d.achieved_day]] !== null) {
			const i = idxOf[d.achieved_day];
			svgEl('circle', { cx: xOf(i), cy: yOf(vals[i]), r: 5, fill: C.good, stroke: C.surface, 'stroke-width': 2 }, f.svg);
			text(f.svg, xOf(i) - 6, yOf(vals[i]) - 10, '✓ goal', { 'text-anchor': 'end', fill: C.ink });
		}
		const byDay = eventsLane(f, d, xOf, idxOf);

		// crosshair + tooltip
		const cross = svgEl('line', { y1: M.t, y2: M.t + f.ph, stroke: C.muted, 'stroke-width': 1, visibility: 'hidden' }, f.svg);
		const dot = svgEl('circle', { r: 4, fill: C.accent, stroke: C.surface, 'stroke-width': 2, visibility: 'hidden' }, f.svg);
		const hit = svgEl('rect', { x: M.l, y: M.t, width: f.pw, height: f.ph, fill: 'transparent' }, f.svg);
		hit.addEventListener('pointermove', e => {
			const r = f.svg.getBoundingClientRect();
			const px = (e.clientX - r.left) * f.W / r.width;
			const i = Math.max(0, Math.min(n - 1, Math.round((px - M.l) / (n === 1 ? 1 : f.pw / (n - 1)))));
			cross.setAttribute('x1', xOf(i));
			cross.setAttribute('x2', xOf(i));
			cross.setAttribute('visibility', 'visible');
			const v = vals[i];
			if (v !== null && v !== undefined) {
				dot.setAttribute('cx', xOf(i));
				dot.setAttribute('cy', yOf(v));
				dot.setAttribute('visibility', 'visible');
			} else dot.setAttribute('visibility', 'hidden');
			const rows = [];
			if (key === 'cum') {
				rows.push({ key: C.accent, value: fmtNum(v), label: ' so far' });
				rows.push({ value: fmtNum(d.values[i]), label: ' that day' });
			} else rows.push({ key: C.accent, value: fmtNum(v), label: ' ' + d.label });
			(byDay[days[i]] || []).forEach(ev => rows.push({ label: (ev.kind === 'post' ? '📣 ' : '✍️ ') + ev.title }));
			showTip(e.pageX, e.pageY, fmtDay(days[i]), rows);
		});
		hit.addEventListener('pointerleave', () => {
			cross.setAttribute('visibility', 'hidden');
			dot.setAttribute('visibility', 'hidden');
			hideTip();
		});
	}

	function columnChart(host, d) {
		const f = frame(host, 170);
		const days = d.days, vals = d.values || [];
		const n = days.length;
		if (!n) return;
		const nums = vals.filter(v => v !== null && v !== undefined);
		const yMax = niceScale(0, Math.max.apply(null, nums.concat([d.before_rate || 0, 1])) * 1.05, true).max;
		const band = f.pw / n;
		const bw = Math.max(2, Math.min(24, band - 2));  // 2px surface gap between neighbours
		const xOf = i => M.l + band * (i + 0.5);
		const yOf = v => M.t + f.ph - v / yMax * f.ph;
		const idxOf = {};
		days.forEach((dd, i) => { idxOf[dd] = i; });
		axes(f, n, days, 0, yMax, yOf, xOf);
		const base = yOf(0);
		vals.forEach((v, i) => {
			if (v === null || v === undefined || v <= 0) return;
			const x0 = xOf(i) - bw / 2, top = yOf(v), r = Math.min(4, bw / 2, base - top);
			const dpath = `M${x0} ${base} L${x0} ${top + r} Q${x0} ${top} ${x0 + r} ${top} L${x0 + bw - r} ${top} Q${x0 + bw} ${top} ${x0 + bw} ${top + r} L${x0 + bw} ${base} Z`;
			svgEl('path', { d: dpath, fill: days[i] < d.start ? C.context : C.accent, class: 'col' }, f.svg);
		});
		if (d.before_rate) refLine(f, yOf(d.before_rate), 'pre-start ' + fmtNum(d.before_rate) + '/d', C.muted);
		const byDay = eventsLane(f, d, xOf, idxOf);
		const hl = svgEl('rect', { y: M.t, height: f.ph, width: band, fill: C.accent, 'fill-opacity': 0.06, visibility: 'hidden' }, f.svg);
		const hit = svgEl('rect', { x: M.l, y: M.t, width: f.pw, height: f.ph, fill: 'transparent' }, f.svg);
		hit.addEventListener('pointermove', e => {
			const r = f.svg.getBoundingClientRect();
			const px = (e.clientX - r.left) * f.W / r.width;
			const i = Math.max(0, Math.min(n - 1, Math.floor((px - M.l) / band)));
			hl.setAttribute('x', M.l + band * i);
			hl.setAttribute('visibility', 'visible');
			const rows = [{ key: days[i] < d.start ? C.context : C.accent, value: fmtNum(vals[i]), label: ' ' + d.label }];
			(byDay[days[i]] || []).forEach(ev => rows.push({ label: (ev.kind === 'post' ? '📣 ' : '✍️ ') + ev.title }));
			showTip(e.pageX, e.pageY, fmtDay(days[i]) + (days[i] < d.start ? ' · before start' : ''), rows);
		});
		hit.addEventListener('pointerleave', () => { hl.setAttribute('visibility', 'hidden'); hideTip(); });
	}

	function goal(dataEl) {
		if (!dataEl) return;
		let d;
		try { d = JSON.parse(dataEl.textContent); } catch (e) { return; }
		const main = document.getElementById('goal_chart');
		const daily = document.getElementById('goal_daily');
		const hasData = (d.values || []).some(v => v !== null && v !== undefined);
		const draw = () => {
			if (!hasData) {
				main.textContent = 'No data yet — the chart appears after the first metrics run.';
				main.classList.add('chart-empty');
				if (daily) daily.remove();
				return;
			}
			if (d.kind === 'flow') {
				lineChart(main, d, 'cum');
				if (daily) columnChart(daily, d);
			} else lineChart(main, d, 'values');
		};
		draw();
		let t;
		window.addEventListener('resize', () => { clearTimeout(t); t = setTimeout(draw, 150); });
	}

	function initSparks() { document.querySelectorAll('.spark[data-spark]').forEach(sparkline); }
	document.addEventListener('DOMContentLoaded', initSparks);
	if (document.readyState !== 'loading') initSparks();

	window.IPChart = { sparkline: sparkline, goal: goal };
})();
