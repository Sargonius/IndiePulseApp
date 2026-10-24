// Steamworks drop-downs (<details class="sw-menu">): one open at a time, close on
// a click elsewhere or Esc.
(function () {
  function closeAll(except) {
    document.querySelectorAll('details.sw-menu[open]').forEach(function (d) {
      if (d !== except) d.removeAttribute('open');
    });
  }
  document.addEventListener('click', function (e) {
    closeAll(e.target.closest ? e.target.closest('details.sw-menu') : null);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeAll(null);
  });
})();
