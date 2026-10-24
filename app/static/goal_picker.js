/* Goal picker for the create forms (post / video / shorts): a post can be made
 * "for" a marketing goal, so the goal page can show what was done for it. */
window.IPGoals = (function () {
	'use strict';
	async function load(select, projectId) {
		if (!select) return;
		const seq = (select._ipSeq = (select._ipSeq || 0) + 1);  // only the latest call may fill it
		const q = projectId ? ('?project_id=' + encodeURIComponent(projectId)) : '';
		let goals = [];
		try {
			const r = await fetch('/api/goals' + q);
			goals = r.ok ? await r.json() : [];
		} catch (e) { goals = []; }
		if (seq !== select._ipSeq) return;
		const keep = select.value;
		select.textContent = '';
		select.appendChild(new Option('— no goal —', ''));
		goals.forEach(g => select.appendChild(new Option(g.title + (g.project_id ? '' : ' (global)'), String(g.id))));
		if ([...select.options].some(o => o.value === keep)) select.value = keep;
	}
	// "?project=3" in the URL (links from the home screen) picks that project's tile
	function preselectProject(container) {
		const pre = new URLSearchParams(location.search).get('project');
		if (!pre || !container) return;
		const tile = container.querySelector('[data-project-id="' + CSS.escape(pre) + '"]');
		if (tile) tile.click();
	}
	return { load: load, preselectProject: preselectProject };
})();
