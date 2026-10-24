/* "IndiePulse ← Steam" bookmarklet. steam_traffic.bookmarklet() fills the
   __PLACEHOLDERS__ and turns this file into a javascript: URL.

   Clicked on any partner.steamgames.com page, it downloads the Traffic Breakdown
   CSV of every game for each of the last N GMT days (the page's own "Download as
   CSV" link, one custom range per day), then opens IndiePulse with the files in the
   URL fragment. It runs in the user's own logged-in tab: IndiePulse never sees
   Steam cookies. TOKEN proves the data came from this bookmarklet.
   A link from IndiePulse may add "#indiepulse:days=90:apps=1,2" to the Steamworks
   URL to ask for more days or a newer list of games. */
(async () => {
  const IP = __ORIGIN__, TOKEN = __TOKEN__, DEFAULT_APPS = __APPS__, DEFAULT_DAYS = __DAYS__;
  if (location.hostname !== 'partner.steamgames.com') {
    alert('IndiePulse: open any partner.steamgames.com page (logged in) and click the bookmarklet again.');
    return;
  }
  if (window.__indiepulseSync) return;
  window.__indiepulseSync = true;
  const hint = decodeURIComponent(location.hash);
  const m = hint.match(/indiepulse:days=(\d+)/), a = hint.match(/apps=([\d,]+)/);
  const DAYS = Math.max(1, Math.min(400, m ? +m[1] : DEFAULT_DAYS));
  const APPS = a ? a[1].split(',').filter(Boolean).map(Number) : DEFAULT_APPS;
  const win = window.open('about:blank', '_blank');
  const box = document.createElement('div');
  box.style.cssText = 'position:fixed;z-index:2147483647;right:16px;bottom:16px;max-width:380px;padding:12px 16px;'
    + 'background:#1b2838;color:#fff;border:1px solid #66c0f4;border-radius:8px;font:14px/1.45 Arial,sans-serif;'
    + 'box-shadow:0 6px 24px rgba(0,0,0,.5)';
  document.body.appendChild(box);
  const say = (text) => {
    box.textContent = text;
    try { win.document.title = 'IndiePulse'; win.document.body.textContent = text; } catch (e) {}
  };
  const pad = (n) => String(n).padStart(2, '0');
  const now = new Date();
  const jobs = [];
  for (const app of APPS) {
    for (let i = 1; i <= DAYS; i++) {
      const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - i));
      const us = pad(d.getUTCMonth() + 1) + '/' + pad(d.getUTCDate()) + '/' + d.getUTCFullYear();
      jobs.push({ app: app, day: d.toISOString().slice(0, 10), us: us });
    }
  }
  const rows = [], failed = [], queue = jobs.slice();
  let done = 0;
  say('IndiePulse: downloading Steam traffic… 0 / ' + jobs.length);
  const worker = async () => {
    while (queue.length) {
      const j = queue.shift();
      try {
        const r = await fetch('/apps/navtrafficstats/' + j.app + '?format=csv&attribution_filter=all'
          + '&preset_date_range=custom&start_date=' + j.us + '&end_date=' + j.us, { credentials: 'include' });
        const text = r.ok ? await r.text() : '';
        if (text.indexOf('"Page / Category"') === 0) rows.push({ appid: j.app, day: j.day, csv: text });
        else failed.push(j.app + ' ' + j.day + ' HTTP ' + r.status);
      } catch (e) {
        failed.push(j.app + ' ' + j.day + ' ' + e);
      }
      done += 1;
      say('IndiePulse: downloading Steam traffic… ' + done + ' / ' + jobs.length);
    }
  };
  await Promise.all([worker(), worker(), worker(), worker()]);
  if (!rows.length) {
    say('IndiePulse: Steam returned no CSV (' + (failed[0] || 'no data') + '). Are you logged in to Steamworks?');
    window.__indiepulseSync = false;
    return;
  }
  let bytes = new TextEncoder().encode(JSON.stringify({ v: 1, token: TOKEN, days: DAYS, failed: failed.slice(0, 20), rows: rows }));
  if (window.CompressionStream) {
    const gz = new Blob([bytes]).stream().pipeThrough(new CompressionStream('gzip'));
    bytes = new Uint8Array(await new Response(gz).arrayBuffer());
  }
  let bin = '';
  for (let i = 0; i < bytes.length; i += 32768) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 32768));
  const url = IP + '/steam/traffic/import#' + btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  window.__indiepulseSync = false;
  if (win && !win.closed) {
    win.location.href = url;
    say('IndiePulse: done — ' + rows.length + ' days sent, see the result in the new tab.');
  } else {
    location.href = url;  // popup blocked: go to IndiePulse in this tab (Back returns here)
  }
})();
