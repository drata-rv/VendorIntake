(function () {
  var theme = null;
  try {
    theme = window.localStorage.getItem('bridge-theme');
  } catch (e) {
    theme = null;
  }
  if (theme !== 'light' && theme !== 'dark') {
    var dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    theme = dark ? 'dark' : 'light';
  }
  document.documentElement.setAttribute('data-theme', theme);
})();
