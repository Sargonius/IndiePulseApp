// The ⚙️ Settings page and the setup wizard: the status badges of the sections.
// Load status indicators on page load
async function loadSettingsStatus() {
	try {
		const resp = await fetch('/api/settings/status');
		const data = await resp.json();
		
		const platforms = ['discord', 'telegram', 'vk', 'x', 'youtube', 'bluesky', 'facebook', 'reddit', 'tiktok', 'openai'];
		
		platforms.forEach(platform => {
			const info = data[platform];
			if (!info) return;
			
			const badge = document.getElementById('status_' + platform);
			const section = document.getElementById('section_' + platform);
			
			if (badge) {
				badge.textContent = info.message;
				badge.className = 'status-badge ' + info.status;
			}
			
			if (section) {
				section.classList.remove('status-configured', 'status-partial', 'status-not_configured');
				section.classList.add('status-' + info.status);
			}
		});
	} catch (err) {
		console.error('Failed to load settings status:', err);
	}
}

document.addEventListener('DOMContentLoaded', loadSettingsStatus);

// Refresh status after any form submission
document.addEventListener('htmx:afterSwap', (e) => {
	setTimeout(loadSettingsStatus, 500);
});
