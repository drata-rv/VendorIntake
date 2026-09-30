(function () {
  'use strict';

  const NAV_KEY = 'bridge-nav-collapsed';

  const $ = (selector, root) => (root || document).querySelector(selector);
  const $$ = (selector, root) => Array.from((root || document).querySelectorAll(selector));

  function readCollapsed() {
    try {
      return window.localStorage.getItem(NAV_KEY) === 'true';
    } catch (error) {
      return false;
    }
  }

  function writeCollapsed(value) {
    try {
      window.localStorage.setItem(NAV_KEY, String(value));
    } catch (error) {
      return;
    }
  }

  function initNav() {
    const nav = $('#shell-nav');
    const toggles = $$('[data-nav-toggle]');
    if (!nav || !toggles.length) return;
    const root = document.documentElement;
    const main = $('#main');
    const scrim = $('[data-nav-scrim]');
    const mobile = window.matchMedia('(max-width: 767px)');
    let collapsed = readCollapsed();
    let overlay = false;

    function visibleToggle() {
      return toggles.find((toggle) => toggle.offsetParent !== null) || toggles[0];
    }

    function render() {
      const overlayOpen = mobile.matches && overlay;
      const expanded = mobile.matches ? overlay : !collapsed;
      root.dataset.nav = collapsed ? 'collapsed' : 'expanded';
      if (overlayOpen) root.dataset.navOpen = 'true';
      else delete root.dataset.navOpen;
      if (scrim) scrim.hidden = !overlayOpen;
      if (main) main.inert = overlayOpen;
      toggles.forEach((toggle) => {
        toggle.setAttribute('aria-expanded', String(expanded));
        if (toggle.hasAttribute('data-nav-dynamic')) {
          toggle.setAttribute('aria-label', expanded ? 'Collapse navigation' : 'Expand navigation');
        }
      });
    }

    function closeOverlay() {
      overlay = false;
      render();
      visibleToggle().focus();
    }

    toggles.forEach((toggle) => {
      toggle.addEventListener('click', () => {
        if (mobile.matches) {
          overlay = !overlay;
        } else {
          collapsed = !collapsed;
          writeCollapsed(collapsed);
        }
        render();
        if (mobile.matches && overlay) $('.shell-nav-item', nav).focus();
        else visibleToggle().focus();
      });
    });

    if (scrim) scrim.addEventListener('click', closeOverlay);

    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') return;
      if (mobile.matches && overlay) closeOverlay();
      else if (nav.matches(':hover, :focus-within')) nav.classList.add('is-tip-off');
    });
    nav.addEventListener('pointerleave', () => nav.classList.remove('is-tip-off'));
    nav.addEventListener('focusout', () => nav.classList.remove('is-tip-off'));

    mobile.addEventListener('change', () => {
      overlay = false;
      render();
    });

    render();
  }

  function initUserMenu() {
    const toggle = $('[data-user-toggle]');
    const menu = toggle && document.getElementById(toggle.getAttribute('aria-controls'));
    if (!menu) return;
    const root = toggle.closest('.shell-user');

    function setOpen(open, restoreFocus) {
      menu.hidden = !open;
      toggle.setAttribute('aria-expanded', String(open));
      if (!open && restoreFocus) toggle.focus();
    }

    toggle.addEventListener('click', () => setOpen(menu.hidden));
    document.addEventListener('pointerdown', (event) => {
      if (!menu.hidden && !root.contains(event.target)) setOpen(false);
    });
    root.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !menu.hidden) setOpen(false, true);
    });
    root.addEventListener('focusout', (event) => {
      if (!menu.hidden && event.relatedTarget && !root.contains(event.relatedTarget)) setOpen(false);
    });
    menu.addEventListener('click', (event) => {
      if (event.target.closest('[data-theme-toggle]')) setOpen(false, true);
    });
  }

  initNav();
  initUserMenu();
})();
