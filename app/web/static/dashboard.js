/* dashboard.js — all interactive behaviour for the PM web UI.
   Loaded on every page via base.html; each section guards itself with
   element-existence checks so it is safe to run on pages where the
   relevant elements are absent. */

// === Copy buttons (run_detail.html, unmatched.html) ===
document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('.copy-btn[data-path]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      navigator.clipboard.writeText(btn.dataset.path).catch(function () {
        btn.title = 'Copy failed';
      });
      var orig = btn.textContent;
      btn.textContent = '✓'; // ✓
      setTimeout(function () { btn.textContent = orig; }, 1200);
    });
  });
});

// === Log filter (logs.html) ===
function filterLogs(query) {
  var lines = document.querySelectorAll('.log-line');
  var q = query.trim().toLowerCase();
  var visible = 0;
  lines.forEach(function (el) {
    var match = !q || el.textContent.toLowerCase().includes(q);
    el.style.display = match ? '' : 'none';
    if (match) { visible++; }
  });
  var counter = document.getElementById('log-match-count');
  if (counter) {
    counter.textContent = q ? visible + ' / ' + lines.length + ' lines' : '';
  }
}

document.addEventListener('DOMContentLoaded', function () {
  // Wire the filter input (replaces oninput= inline handler)
  var filterInput = document.getElementById('log-filter');
  if (filterInput) {
    filterInput.addEventListener('input', function () {
      filterLogs(filterInput.value);
    });
  }

  // Auto-scroll to bottom on initial page load (logs page only)
  if (document.getElementById('log-panel')) {
    window.scrollTo(0, document.body.scrollHeight);
  }
});

// Strip ?msg= from URL after displaying flash messages (prevents stale alerts on refresh/bookmark)
document.addEventListener('DOMContentLoaded', function() {
  if (window.location.search.includes('msg=')) {
    var url = new URL(window.location.href);
    url.searchParams.delete('msg');
    window.history.replaceState({}, '', url.pathname + (url.search || ''));
  }
});

// Re-apply filter after HTMX refreshes the log panel
document.addEventListener('htmx:afterSwap', function (evt) {
  if (evt.detail.target && evt.detail.target.id === 'log-panel') {
    var q = document.getElementById('log-filter');
    if (q && q.value) { filterLogs(q.value); }
  }
});

// === Filename test modal (plugins.html) ===
function openFilenameTest() {
  var input = document.getElementById('filename-test-input');
  var result = document.getElementById('filename-test-result');
  if (!input || !result) { return; }
  input.value = '';
  result.innerHTML = '';
  var backdrop = document.getElementById('filename-test-backdrop');
  if (backdrop) {
    backdrop.style.display = 'flex';
    setTimeout(function () { input.focus(); }, 50);
  }
}

function closeFilenameTest() {
  var backdrop = document.getElementById('filename-test-backdrop');
  if (backdrop) { backdrop.style.display = 'none'; }
}

document.addEventListener('DOMContentLoaded', function () {
  var backdrop = document.getElementById('filename-test-backdrop');
  if (!backdrop) { return; }

  // Close on backdrop click
  backdrop.addEventListener('click', function (e) {
    if (e.target === backdrop) { closeFilenameTest(); }
  });
});

// Close on Escape (global — safe since backdrop won't exist on non-plugins pages)
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') { closeFilenameTest(); }
});
