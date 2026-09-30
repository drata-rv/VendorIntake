(function () {
  var root = document.documentElement;
  var theme = null;
  var collapsed = false;
  try {
    theme = window.localStorage.getItem('bridge-theme');
    collapsed = window.localStorage.getItem('bridge-nav-collapsed') === 'true';
  } catch (e) {
    theme = null;
  }
  if (theme !== 'light' && theme !== 'dark') {
    var dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    theme = dark ? 'dark' : 'light';
  }
  root.setAttribute('data-theme', theme);
  if (collapsed && root.hasAttribute('data-nav')) root.setAttribute('data-nav', 'collapsed');
})();
