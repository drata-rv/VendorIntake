(function () {
  'use strict';

  const REQUEST_TIMEOUT_MS = 70000;
  const APPROVAL_MS = 300000;
  const FLASH_KEY = 'bridge-flash';
  const UNCONFIRMED = 'Outcome not confirmed. Check submission status before trying again.';

  const $ = (selector, root) => (root || document).querySelector(selector);
  const $$ = (selector, root) => Array.from((root || document).querySelectorAll(selector));

  function el(tag, props, children) {
    const node = document.createElement(tag);
    Object.keys(props || {}).forEach((key) => {
      const value = props[key];
      if (value === null || value === undefined || value === false) return;
      if (key === 'text') node.textContent = value;
      else if (key === 'class') node.className = value;
      else if (key === 'on') Object.keys(value).forEach((name) => node.addEventListener(name, value[name]));
      else node.setAttribute(key, value === true ? '' : String(value));
    });
    (children || []).forEach((child) => {
      if (child === null || child === undefined || child === false) return;
      node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    });
    return node;
  }

  function humanize(value) {
    const text = String(value || '').replace(/_/g, ' ').toLowerCase();
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  function formatValue(value) {
    if (value === null || value === undefined || value === '') return 'Empty';
    if (typeof value === 'boolean') return value ? 'Yes' : 'No';
    if (Array.isArray(value)) return value.length ? value.join(', ') : 'Empty';
    return String(value);
  }

  function setText(node, text) {
    if (!node) return;
    node.textContent = text || '';
    node.hidden = !text;
  }

  function setBusy(button, busy) {
    button.disabled = busy;
    button.setAttribute('aria-busy', String(busy));
    const spinner = $('.spinner', button);
    if (spinner) spinner.hidden = !busy;
  }

  function csrfToken() {
    const meta = $('meta[name="csrf-token"]');
    return meta ? meta.getAttribute('content') : '';
  }

  class ApiError extends Error {
    constructor(status, body, extra) {
      const detail = body && body.error ? body.error : {};
      super(detail.message || 'The request failed.');
      this.status = status;
      this.code = detail.code || null;
      this.fieldErrors = detail.fieldErrors || {};
      this.submissionId = detail.submissionId || null;
      this.detail = detail;
      this.network = Boolean(extra && extra.network);
      this.retryAfter = extra && extra.retryAfter ? extra.retryAfter : null;
    }
  }

  function describe(err) {
    if (err.network) return 'The request did not complete. Check your network and try again.';
    return err.message;
  }

  async function send(path, body, options) {
    const opts = options || {};
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    const headers = Object.assign({
      'Content-Type': 'application/json',
      Accept: 'application/json',
      'X-CSRF-Token': csrfToken(),
    }, opts.headers || {});
    let response;
    let data = null;
    try {
      response = await fetch(path, {
        method: 'POST',
        credentials: 'same-origin',
        headers,
        body: JSON.stringify(body === undefined ? {} : body),
        signal: controller.signal,
      });
      data = await response.json().catch(() => null);
    } catch (error) {
      throw new ApiError(0, null, { network: true });
    } finally {
      clearTimeout(timer);
    }
    if (!response.ok) throw new ApiError(response.status, data, { retryAfter: response.headers.get('Retry-After') });
    return { status: response.status, data };
  }

  async function api(path, body, options) {
    try {
      return await send(path, body, options);
    } catch (err) {
      if (err.code !== 'REAUTH_REQUIRED') throw err;
      await promptReauth();
      return send(path, body, options);
    }
  }

  let reauthPending = null;

  function promptReauth() {
    const dialog = $('#reauth-dialog');
    if (!dialog) return Promise.reject(new ApiError(403, { error: { code: 'REAUTH_REQUIRED', message: 'Confirm your password to continue.' } }));
    if (reauthPending) return reauthPending.promise;
    const pending = {};
    pending.promise = new Promise((resolve, reject) => {
      pending.resolve = resolve;
      pending.reject = reject;
    });
    reauthPending = pending;
    $('#reauth-password').value = '';
    setText($('#reauth-error'), '');
    dialog.showModal();
    $('#reauth-password').focus();
    return pending.promise;
  }

  function initReauth() {
    const dialog = $('#reauth-dialog');
    if (!dialog) return;
    const form = $('#reauth-form');
    const input = $('#reauth-password');
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const button = $('[type="submit"]', form);
      if (!input.value) {
        setText($('#reauth-error'), 'Enter your password.');
        input.focus();
        return;
      }
      button.disabled = true;
      try {
        const { data } = await send('/api/account/reauth', { password: input.value });
        const meta = $('meta[name="csrf-token"]');
        if (meta && data.csrfToken) meta.setAttribute('content', data.csrfToken);
        input.value = '';
        const pending = reauthPending;
        reauthPending = null;
        dialog.close('ok');
        pending.resolve();
      } catch (err) {
        input.value = '';
        setText($('#reauth-error'), describe(err));
        input.focus();
      } finally {
        button.disabled = false;
      }
    });
    $('[data-dialog-cancel]', dialog).addEventListener('click', () => dialog.close('cancel'));
    dialog.addEventListener('close', () => {
      if (!reauthPending) return;
      const pending = reauthPending;
      reauthPending = null;
      pending.reject(new ApiError(403, { error: { code: 'REAUTH_CANCELLED', message: 'Password confirmation was cancelled.' } }));
    });
  }

  let confirmResolve = null;

  function confirmDialog(options) {
    const dialog = $('#confirm-dialog');
    if (!dialog) return Promise.resolve(false);
    $('#confirm-title').textContent = options.title;
    $('#confirm-body').replaceChildren(...(options.body || []).map((line) => el('p', { text: line })));
    const ok = $('#confirm-ok');
    ok.textContent = options.confirmLabel;
    ok.className = 'btn ' + (options.danger ? 'btn-danger-strong' : 'btn-primary');
    dialog.returnValue = '';
    return new Promise((resolve) => {
      confirmResolve = resolve;
      dialog.showModal();
      $('[data-dialog-cancel]', dialog).focus();
    });
  }

  function initConfirm() {
    const dialog = $('#confirm-dialog');
    if (!dialog) return;
    $('#confirm-form').addEventListener('submit', (event) => {
      event.preventDefault();
      dialog.close('ok');
    });
    $('[data-dialog-cancel]', dialog).addEventListener('click', () => dialog.close('cancel'));
    dialog.addEventListener('close', () => {
      if (!confirmResolve) return;
      const resolve = confirmResolve;
      confirmResolve = null;
      resolve(dialog.returnValue === 'ok');
    });
  }

  function initTheme() {
    const button = $('[data-theme-toggle]');
    if (!button) return;
    const root = document.documentElement;
    const sync = () => button.setAttribute('aria-pressed', String(root.getAttribute('data-theme') === 'dark'));
    sync();
    button.addEventListener('click', () => {
      const next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      try {
        window.localStorage.setItem('bridge-theme', next);
      } catch (error) {
        button.dataset.unsaved = 'true';
      }
      sync();
    });
  }

  function localizeTimes() {
    $$('time[data-local]').forEach((node) => {
      const date = new Date(node.getAttribute('datetime'));
      if (!Number.isNaN(date.getTime())) node.textContent = date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
    });
  }

  function banner(host, tone, title, message, extra) {
    const node = el('div', { class: 'banner', 'data-tone': tone, role: tone === 'critical' ? 'alert' : 'status', tabindex: '-1' }, [
      title ? el('p', { class: 'banner-title', text: title }) : null,
      message ? el('p', { text: message }) : null,
    ].concat(extra || []));
    host.replaceChildren(node);
    node.focus();
    return node;
  }

  function saveFlash(tone, title, message) {
    try {
      window.sessionStorage.setItem(FLASH_KEY, JSON.stringify({ tone, title, message }));
    } catch (error) {
      return false;
    }
    return true;
  }

  function showFlash() {
    const host = $('#page-feedback');
    if (!host) return;
    let flash = null;
    try {
      flash = JSON.parse(window.sessionStorage.getItem(FLASH_KEY) || 'null');
      window.sessionStorage.removeItem(FLASH_KEY);
    } catch (error) {
      flash = null;
    }
    if (!flash) return;
    const node = banner(host, flash.tone, flash.title, flash.message);
    window.addEventListener('load', () => setTimeout(() => node.scrollIntoView({ block: 'nearest' }), 50));
  }

  function controlOf(wrapper) {
    return wrapper.matches('fieldset') ? wrapper : wrapper.querySelector('input, select, textarea');
  }

  function markError(wrapper, message) {
    const control = controlOf(wrapper);
    if (control) control.setAttribute('aria-invalid', 'true');
    setText($('.field-error', wrapper), message);
  }

  function clearErrors(root) {
    $$('.field-error', root).forEach((node) => setText(node, ''));
    $$('[aria-invalid]', root).forEach((node) => node.removeAttribute('aria-invalid'));
  }

  function focusIn(wrapper) {
    const target = wrapper.matches('fieldset') ? $('input', wrapper) : controlOf(wrapper);
    if (target) target.focus();
  }

  function renderSummary(summary, options) {
    $('.banner-title', summary).textContent = options.title;
    setText($('[data-summary-message]', summary), options.message);
    const list = $('[data-summary-list]', summary);
    list.replaceChildren();
    (options.items || []).forEach((item) => {
      const link = el('a', { href: '#', text: item.label + ': ' + item.message });
      link.addEventListener('click', (event) => {
        event.preventDefault();
        item.focus();
      });
      list.appendChild(el('li', null, [link]));
    });
    list.hidden = !(options.items && options.items.length);
    const extra = $('[data-summary-extra]', summary);
    if (extra) {
      extra.replaceChildren();
      if (options.link) extra.appendChild(el('a', { href: options.link.href, text: options.link.text }));
      extra.hidden = !options.link;
    }
    summary.hidden = false;
    summary.focus();
  }

  function initLogin() {
    const error = $('#login-error');
    if (error) error.focus();
    const form = $('[data-login-form]');
    if (!form) return;
    const host = $('#login-feedback');
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const button = $('[type="submit"]', form);
      button.disabled = true;
      if (error) error.remove();
      try {
        const response = await fetch(form.action, { method: 'POST', credentials: 'same-origin', body: new URLSearchParams(new FormData(form)) });
        if (response.ok && response.redirected) {
          window.location.assign(response.url);
          return;
        }
        const page = new DOMParser().parseFromString(await response.text(), 'text/html');
        const token = $('input[name="csrf_token"]', page);
        const reason = $('#login-error p:last-child', page);
        if (response.status === 401 && token && reason) {
          $('input[name="csrf_token"]', form).value = token.value;
          banner(host, 'critical', 'Could not sign in', reason.textContent);
        } else {
          banner(host, 'critical', 'Could not sign in', 'The sign-in form expired. Reload the page and try again.');
        }
        form.elements.password.value = '';
      } catch (err) {
        banner(host, 'critical', 'Could not sign in', 'The request did not complete. Check your network and try again.');
      } finally {
        button.disabled = false;
      }
    });
  }

  // Native form posts carry "Origin: null" under Referrer-Policy no-referrer; fetch sends the real origin the server checks.
  function initLogout() {
    $$('[data-logout-form]').forEach((form) => {
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        try {
          await fetch(form.action, { method: 'POST', credentials: 'same-origin', body: new URLSearchParams(new FormData(form)) });
        } catch (err) {
          return;
        }
        window.location.assign('/login');
      });
    });
  }

  function initIntake() {
    const form = $('[data-intake-form]');
    if (!form) return;
    const version = Number(form.dataset.formVersion);
    const summary = $('#error-summary');
    const button = $('#submit-button');
    const status = $('#submit-status');
    const attempt = { key: null, serialized: null };

    $$('[data-clear-choice]', form).forEach((clear) => {
      clear.addEventListener('click', () => {
        $$('input[type="radio"]', clear.closest('.field')).forEach((radio) => { radio.checked = false; });
      });
    });

    function collect() {
      const answers = {};
      $$('[data-field-id]', form).forEach((wrapper) => {
        const id = wrapper.dataset.fieldId;
        const type = wrapper.dataset.type;
        if (type === 'boolean') {
          const chosen = $('input[type="radio"]:checked', wrapper);
          if (chosen) answers[id] = chosen.value === 'true';
        } else if (type === 'multiselect') {
          const values = $$('input[type="checkbox"]:checked', wrapper).map((box) => box.value);
          if (values.length) answers[id] = values;
        } else {
          const value = controlOf(wrapper).value.trim();
          if (value) answers[id] = value;
        }
      });
      return answers;
    }

    function labelOf(wrapper) {
      const label = $('.field-label', wrapper).cloneNode(true);
      $$('.optional', label).forEach((node) => node.remove());
      return label.textContent.trim();
    }

    function showFieldErrors(fieldErrors) {
      const items = [];
      const known = new Set();
      $$('[data-field-id]', form).forEach((wrapper) => {
        const id = wrapper.dataset.fieldId;
        if (!Object.prototype.hasOwnProperty.call(fieldErrors, id)) return;
        known.add(id);
        markError(wrapper, fieldErrors[id]);
        items.push({ label: labelOf(wrapper), message: fieldErrors[id], focus: () => focusIn(wrapper) });
      });
      const general = Object.keys(fieldErrors).filter((id) => !known.has(id)).map((id) => fieldErrors[id]);
      renderSummary(summary, {
        title: 'Fix the highlighted fields',
        message: general.join(' ') || 'Nothing was sent to Drata. Correct these fields and submit again.',
        items,
      });
    }

    function showProblem(title, message, link) {
      renderSummary(summary, { title, message, items: [], link });
    }

    function unconfirmed() {
      showProblem('The result could not be confirmed', UNCONFIRMED, { href: '/history', text: 'Open my submissions' });
    }

    function handleError(err) {
      if (err.network || !err.code) return unconfirmed();
      if (err.code === 'VALIDATION_FAILED') return showFieldErrors(err.fieldErrors);
      if (err.code === 'FORM_CHANGED') {
        return showProblem('The form changed',
          'Reload the page to review the current fields. What you entered stays visible here until you reload, so copy anything you need first.',
          { href: window.location.href, text: 'Reload form' });
      }
      if (err.code === 'BRIDGE_BUSY') {
        const wait = err.retryAfter ? ' Try again in about ' + err.retryAfter + ' seconds.' : '';
        return showProblem('The bridge is busy', 'Another write is in progress. Your answers and submission key are kept.' + wait + ' Select Create prospective vendor again when ready.');
      }
      if (err.status === 401) {
        return showProblem('Your session ended', 'Sign in again in a new tab, then come back and submit. Your answers are still on this page.', { href: '/login', text: 'Open sign in' });
      }
      if (err.status === 403) {
        return showProblem('This page is out of date', 'Your security token no longer matches. Copy your answers, reload the page, and submit again.');
      }
      if (err.status === 503 || (err.status >= 400 && err.status < 500)) return showProblem('Submission not accepted', err.message);
      return unconfirmed();
    }

    function navigate(id) {
      window.location.assign('/result/' + encodeURIComponent(id));
    }

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (button.disabled) return;
      clearErrors(form);
      summary.hidden = true;
      if (!window.crypto || typeof window.crypto.randomUUID !== 'function') {
        showProblem('This browser cannot submit', 'A secure submission key could not be created. Use a current browser over HTTPS.');
        return;
      }
      const payload = { formVersion: version, answers: collect() };
      if (form.dataset.originSubmissionId) payload.originSubmissionId = form.dataset.originSubmissionId;
      const serialized = JSON.stringify(payload);
      // Reusing the key on identical answers lets the server replay a stored outcome instead of creating a second vendor after a lost response.
      if (serialized !== attempt.serialized) {
        attempt.key = window.crypto.randomUUID();
        attempt.serialized = serialized;
      }
      setBusy(button, true);
      status.textContent = 'Sending your submission.';
      let leaving = false;
      try {
        const { data } = await send('/api/submissions', payload, { headers: { 'Idempotency-Key': attempt.key } });
        if (data && typeof data.submissionId === 'string') {
          leaving = true;
          navigate(data.submissionId);
          return;
        }
        unconfirmed();
      } catch (err) {
        if (err.code === 'DRATA_REJECTED' && err.submissionId) {
          leaving = true;
          navigate(err.submissionId);
          return;
        }
        handleError(err);
      } finally {
        if (!leaving) {
          setBusy(button, false);
          status.textContent = '';
        }
      }
    });
  }

  function initHistory() {
    const toggle = $('[data-only-mine]');
    if (!toggle) return;
    const rows = $$('#history-table tbody tr');
    const empty = $('#filter-empty');
    function apply() {
      let shown = 0;
      rows.forEach((row) => {
        const hide = toggle.checked && row.dataset.mine !== 'true';
        row.hidden = hide;
        if (!hide) shown += 1;
      });
      if (empty) empty.hidden = shown > 0 || rows.length === 0;
    }
    toggle.addEventListener('change', apply);
    apply();
  }

  function initPassword() {
    const form = $('[data-password-form]');
    if (!form) return;
    const summary = $('#error-summary');
    const button = $('#password-button');
    const current = $('#current-password');
    const next = $('#new-password');
    const confirm = $('#confirm-password');
    const fields = { currentPassword: current, password: next, confirmPassword: confirm };

    function fail(errors) {
      const items = [];
      Object.keys(errors).forEach((key) => {
        const input = fields[key];
        if (!input) return;
        const wrapper = input.closest('.field');
        markError(wrapper, errors[key]);
        items.push({ label: $('.field-label', wrapper).textContent, message: errors[key], focus: () => input.focus() });
      });
      renderSummary(summary, { title: 'Password not changed', message: items.length ? '' : 'The password could not be changed.', items });
    }

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      clearErrors(form);
      summary.hidden = true;
      const errors = {};
      if (!current.value) errors.currentPassword = 'Enter your current password.';
      if (next.value.length < 15 || next.value.length > 128) errors.password = 'Use 15 to 128 characters.';
      if (confirm.value !== next.value) errors.confirmPassword = 'Passwords do not match.';
      if (Object.keys(errors).length) return fail(errors);
      setBusy(button, true);
      try {
        await api('/api/account/password', { currentPassword: current.value, newPassword: next.value });
        window.location.assign('/');
      } catch (err) {
        setBusy(button, false);
        const mapped = {};
        Object.keys(err.fieldErrors).forEach((key) => { mapped[key] = err.code === 'CURRENT_PASSWORD_INVALID' ? err.message : err.fieldErrors[key]; });
        if (!Object.keys(mapped).length) mapped.currentPassword = describe(err);
        fail(mapped);
      }
    });
  }

  function initConnection() {
    const root = $('[data-connection-page]');
    if (!root) return;
    showFlash();
    const view = JSON.parse($('#connection-data').textContent);
    const form = $('#connect-form');
    const keyInput = $('#api-key');
    const stored = form.elements.useStored || null;
    const feedback = $('#page-feedback');
    const testButton = $('#test-button');
    const saveButton = $('#save-button');
    const confirmBox = $('#confirm-account');
    const confirmLabel = $('#confirm-account-label');
    let tested = null;

    const useStored = () => Boolean(stored && stored.checked);
    const candidate = () => tested || (useStored() ? view.account : null);

    function refreshConfirm() {
      const account = candidate();
      confirmBox.checked = false;
      confirmLabel.textContent = account && account.id
        ? 'Confirm this is the correct Drata account: ' + (account.name || 'unnamed tenant') + (account.domain ? ' (' + account.domain + ')' : '') + ', account ID ' + account.id + '.'
        : 'Confirm the Drata account shown after testing.';
    }

    function keyReady() {
      if (useStored() || keyInput.value.trim()) return true;
      markError(keyInput.closest('.field'), 'Enter the API key, or use the stored credential.');
      keyInput.focus();
      return false;
    }

    function takeBody() {
      const body = { customFieldsEnabled: form.elements.customFieldsEnabled.checked };
      if (useStored()) {
        body.useStored = true;
      } else {
        body.apiKey = keyInput.value;
        keyInput.value = '';
      }
      return body;
    }

    function stepRow(name, outcome, detail) {
      const labels = { pass: 'Passed', fail: 'Failed', skip: 'Not run', note: 'Not exercised', off: 'Not requested' };
      const tones = { pass: 'success', fail: 'critical', skip: 'neutral', note: 'warning', off: 'neutral' };
      return el('li', { class: 'step' }, [
        el('span', { class: 'step-name', text: name }),
        el('span', { class: 'pill', 'data-tone': tones[outcome], text: labels[outcome] }),
        detail ? el('span', { class: 'muted', text: detail }) : null,
      ]);
    }

    function renderTest(result, customRequested) {
      const failure = result.failure;
      const failedAt = failure ? failure.step : null;
      const wrongAccount = Boolean(failure && failure.code === 'WRONG_ACCOUNT');
      const account = result.account;
      const rows = [
        stepRow('Identify account', account ? (wrongAccount ? 'fail' : 'pass') : failedAt === 'company' ? 'fail' : 'skip',
          account ? (account.name || 'Unnamed') + (account.domain ? ', ' + account.domain : '') + ', account ID ' + account.id : ''),
        stepRow('List vendors', result.readVerified ? 'pass' : failedAt === 'vendors' ? 'fail' : 'skip', ''),
        stepRow('Read one vendor', result.getVendor === 'verified' ? 'pass' : failedAt === 'vendor' ? 'fail' : result.readVerified ? 'note' : 'skip',
          result.readVerified && result.getVendor !== 'verified' ? 'Get Vendor permission not yet exercised.' : ''),
        stepRow('Custom field definitions', result.customFields ? 'pass' : failedAt === 'definitions' ? 'fail' : customRequested ? 'skip' : 'off',
          result.customFields ? result.customFields.length + ' vendor definitions found.' : ''),
      ];
      if (failure) {
        rows.push(el('li', { class: 'step' }, [
          el('span', { class: 'step-name', text: 'Result' }),
          el('span', { class: 'pill', 'data-tone': 'critical', text: 'Failed' }),
          el('span', { text: failure.message + (failure.httpStatus ? ' HTTP ' + failure.httpStatus + '.' : '') }),
        ]));
      }
      $('#test-steps').replaceChildren(...rows);
      tested = result.ok && account ? account : null;
      refreshConfirm();
      const panel = $('#test-result');
      panel.hidden = false;
      panel.focus();
    }

    function reportFailure(err) {
      banner(feedback, 'critical', 'Request failed', describe(err));
      if (err.fieldErrors.apiKey) markError(keyInput.closest('.field'), err.fieldErrors.apiKey);
      if (err.detail && err.detail.test) renderTest(err.detail.test, form.elements.customFieldsEnabled.checked);
    }

    if (stored) {
      stored.addEventListener('change', () => {
        keyInput.disabled = stored.checked;
        tested = null;
        refreshConfirm();
      });
    }

    testButton.addEventListener('click', async () => {
      clearErrors(form);
      if (!keyReady()) return;
      const body = takeBody();
      setBusy(testButton, true);
      try {
        const { data } = await api('/api/admin/connection/test', body);
        renderTest(data, body.customFieldsEnabled);
      } catch (err) {
        reportFailure(err);
      } finally {
        body.apiKey = null;
        setBusy(testButton, false);
      }
    });

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      clearErrors(form);
      const account = candidate();
      const attestError = $('#attest-error');
      const updates = form.elements.updatesEnabled.checked;
      if (!keyReady()) return;
      let problem = null;
      let target = null;
      if (!account) {
        problem = 'Run Test connection first so the account can be confirmed.';
        target = testButton;
      } else if (!confirmBox.checked) {
        problem = 'Confirm the Drata account.';
        target = confirmBox;
      } else if (!form.elements.attestCreate.checked) {
        problem = 'Attest the Create Vendor scope to continue.';
        target = form.elements.attestCreate;
      } else if (updates && !form.elements.attestUpdate.checked) {
        problem = 'Attest the Update Vendor scope to allow updates.';
        target = form.elements.attestUpdate;
      }
      if (problem) {
        setText(attestError, problem);
        target.focus();
        return;
      }
      const body = takeBody();
      Object.assign(body, {
        confirmAccountId: account.id,
        attestCreate: true,
        updatesEnabled: updates,
        attestUpdate: form.elements.attestUpdate.checked,
      });
      setBusy(saveButton, true);
      try {
        const { data } = await api('/api/admin/connection', body);
        saveFlash('success', 'Connection saved',
          'Credential version ' + data.credentialVersion + ' is active for ' + (data.account.name || 'the pinned tenant') + '. Write access is attested, not yet observed.');
        window.location.reload();
      } catch (err) {
        setBusy(saveButton, false);
        reportFailure(err);
      } finally {
        body.apiKey = null;
      }
    });

    $('#retention-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const retentionForm = event.currentTarget;
      const button = $('#retention-button');
      clearErrors(retentionForm);
      const body = {};
      ['terminalDays', 'unresolvedDays', 'ledgerDays'].forEach((name) => {
        const raw = retentionForm.elements[name].value.trim();
        body[name] = /^\d+$/.test(raw) ? Number(raw) : null;
      });
      setBusy(button, true);
      try {
        const { data } = await api('/api/admin/retention', body);
        retentionForm.elements.terminalDays.value = data.retention.terminalDays;
        retentionForm.elements.unresolvedDays.value = data.retention.unresolvedDays;
        retentionForm.elements.ledgerDays.value = data.retention.ledgerDays;
        banner(feedback, 'success', 'Retention saved', 'New limits apply at the next cleanup.');
      } catch (err) {
        Object.keys(err.fieldErrors).forEach((name) => {
          const input = retentionForm.elements[name];
          if (input) markError(input.closest('.field'), err.fieldErrors[name]);
        });
        banner(feedback, 'critical', 'Retention not saved', describe(err));
      } finally {
        setBusy(button, false);
      }
    });

    const retestForm = $('#retest-form');
    if (retestForm) {
      retestForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        const button = $('#retest-button');
        const problem = $('#retest-error');
        setText(problem, '');
        const attestCreate = retestForm.elements.attestCreate.checked;
        const attestUpdate = retestForm.elements.attestUpdate ? retestForm.elements.attestUpdate.checked : false;
        if (!attestCreate || (view.updatesEnabled && !attestUpdate)) {
          setText(problem, 'Attest each scope in use to retest the credential.');
          (attestCreate ? retestForm.elements.attestUpdate : retestForm.elements.attestCreate).focus();
          return;
        }
        setBusy(button, true);
        try {
          const { data } = await api('/api/admin/connection', {
            useStored: true,
            confirmAccountId: view.account.id,
            attestCreate,
            customFieldsEnabled: view.customFieldsEnabled,
            updatesEnabled: view.updatesEnabled,
            attestUpdate,
          });
          saveFlash('success', 'Credential retested', 'The connection is ' + humanize(data.connectionState).toLowerCase() + ' for ' + (data.account.name || 'the pinned tenant') + '.');
          window.location.reload();
        } catch (err) {
          setBusy(button, false);
          reportFailure(err);
        }
      });
    }

    const disconnect = $('#disconnect-button');
    if (disconnect) {
      disconnect.addEventListener('click', async () => {
        const go = await confirmDialog({
          title: 'Disconnect from Drata',
          body: [
            'Writes stop, intake closes, and the stored key is removed from this application.',
            'The key stays valid in Drata until you revoke it in Drata Settings, under API keys.',
          ],
          confirmLabel: 'Disconnect',
          danger: true,
        });
        if (!go) return;
        setBusy(disconnect, true);
        try {
          await api('/api/admin/connection/disconnect', {});
          saveFlash('warning', 'Disconnected', 'Revoke the key in Drata Settings, under API keys. Disconnecting here does not revoke it.');
          window.location.reload();
        } catch (err) {
          setBusy(disconnect, false);
          banner(feedback, 'critical', 'Disconnect failed', describe(err));
        }
      });
    }

    refreshConfirm();
  }

  function initEditor() {
    const root = $('[data-editor]');
    if (!root) return;
    const data = JSON.parse($('#editor-data').textContent);
    const feedback = $('#editor-feedback');
    const catalog = {};
    data.native.forEach((entry) => { catalog[entry.name] = entry; });
    const TEXT_TYPES = ['text', 'textarea'];
    const CHOICE_TYPES = ['select', 'multiselect'];
    let counter = 0;

    function hydrate(field) {
      const dest = field.destination || {};
      counter += 1;
      return {
        uid: counter,
        id: field.id || '',
        label: field.label || '',
        type: field.type || 'text',
        required: Boolean(field.required),
        helpText: field.helpText || '',
        maxLength: field.maxLength === undefined || field.maxLength === null ? '' : String(field.maxLength),
        optionsText: (field.options || []).map((option) => option.value + ' | ' + option.label).join('\n'),
        kind: dest.kind || 'retained_only',
        nativeField: dest.field || '',
        customFieldId: dest.customFieldId === undefined || dest.customFieldId === null ? '' : String(dest.customFieldId),
        customType: dest.customType || 'TEXT',
        reason: dest.reason || '',
      };
    }

    const state = {
      version: data.version,
      active: data.active,
      currency: data.schema.currency || 'USD',
      fields: data.schema.fields.map(hydrate),
      open: new Set(),
      dirty: false,
      errors: [],
    };

    function parseOptions(text) {
      return text.split('\n').map((line) => line.trim()).filter(Boolean).map((line) => {
        const cut = line.indexOf('|');
        const value = (cut === -1 ? line : line.slice(0, cut)).trim();
        const label = cut === -1 ? value : line.slice(cut + 1).trim();
        return { value, label: label || value };
      });
    }

    function toSchemaField(f) {
      const out = { id: f.id, label: f.label, type: f.type, required: f.required, helpText: f.helpText, options: [] };
      if (CHOICE_TYPES.indexOf(f.type) !== -1) out.options = parseOptions(f.optionsText);
      const max = f.maxLength.trim();
      if (max !== '' && TEXT_TYPES.indexOf(f.type) !== -1) out.maxLength = /^\d+$/.test(max) ? Number(max) : max;
      if (f.kind === 'native') {
        out.destination = { kind: 'native', field: f.nativeField };
      } else if (f.kind === 'custom') {
        const raw = f.customFieldId.trim();
        out.destination = { kind: 'custom', customFieldId: /^\d+$/.test(raw) ? Number(raw) : raw, customType: f.customType };
      } else {
        out.destination = { kind: 'retained_only', reason: f.reason };
      }
      return out;
    }

    const buildSchema = () => ({ currency: state.currency, fields: state.fields.map(toSchemaField) });

    function markDirty() {
      state.dirty = true;
      $('#editor-dirty').hidden = false;
    }

    function summaryOf(f) {
      if (f.kind === 'native') return (f.nativeField || 'no field') + ' (native)';
      if (f.kind === 'custom') return 'custom ' + (f.customFieldId || 'no ID');
      return 'retained only';
    }

    function labelled(id, text, control, help) {
      const helpId = help ? id + '-help' : null;
      if (helpId) control.setAttribute('aria-describedby', helpId);
      return el('div', { class: 'field' }, [
        el('label', { class: 'field-label', for: id, text }),
        help ? el('p', { class: 'field-help', id: helpId, text: help }) : null,
        control,
      ]);
    }

    function textInput(id, value, onInput) {
      const node = el('input', { class: 'input', id, type: 'text', autocomplete: 'off', spellcheck: 'false' });
      node.value = value;
      node.addEventListener('input', () => onInput(node.value));
      return node;
    }

    function selectInput(id, choices, value, onChange) {
      const node = el('select', { class: 'select', id }, choices.map((choice) => el('option', { value: choice.value, text: choice.label, disabled: choice.disabled })));
      node.value = value;
      node.addEventListener('change', () => onChange(node.value));
      return node;
    }

    function checkbox(id, text, checked, onChange) {
      const node = el('input', { type: 'checkbox', id });
      node.checked = checked;
      node.addEventListener('change', () => onChange(node.checked));
      return el('label', { class: 'choice', for: id }, [node, text]);
    }

    function textArea(id, value, rows, onInput) {
      const node = el('textarea', { class: 'textarea textarea-short', id, rows: String(rows), spellcheck: 'false' });
      node.value = value;
      node.addEventListener('input', () => onInput(node.value));
      return node;
    }

    function destinationControls(f, ids, rerender) {
      const kinds = [{ value: 'native', label: 'Drata native field' }];
      if (data.customEnabled || f.kind === 'custom') kinds.push({ value: 'custom', label: 'Drata custom field' });
      kinds.push({ value: 'retained_only', label: 'Kept locally only' });
      const nodes = [labelled(ids('kind'), 'Destination', selectInput(ids('kind'), kinds, f.kind, (value) => {
        f.kind = value;
        if (value === 'retained_only' && TEXT_TYPES.indexOf(f.type) !== -1 && f.maxLength.trim() === '') f.maxLength = '191';
        markDirty();
        rerender(ids('kind'));
      }))];
      if (f.kind === 'native') {
        const names = data.native.map((entry) => ({ value: entry.name, label: entry.name + (entry.createOnly ? ' (create only)' : '') }));
        if (!f.nativeField) names.unshift({ value: '', label: 'Select a field' });
        const entry = catalog[f.nativeField];
        nodes.push(labelled(ids('native'), 'Drata field', selectInput(ids('native'), names, f.nativeField, (value) => {
          f.nativeField = value;
          const spec = catalog[value];
          if (spec && spec.inputs.indexOf(f.type) === -1) f.type = spec.inputs[0];
          if (spec && spec.enum) f.optionsText = '';
          markDirty();
          rerender(ids('native'));
        }), entry ? 'Accepts input type: ' + entry.inputs.join(', ') + '.' : null));
      } else if (f.kind === 'custom') {
        if (Array.isArray(data.customDefs)) {
          const choices = [{ value: '', label: 'Select a custom field' }].concat(data.customDefs.map((def) => {
            const unsupported = def.readOnly || data.customTypes.indexOf(def.type) === -1;
            return {
              value: def.customFieldId + '|' + def.type,
              label: def.name + ' (ID ' + def.customFieldId + ', ' + def.type + (def.isRequired ? ', required' : '') + (unsupported ? ', not supported' : '') + ')',
              disabled: unsupported,
            };
          }));
          const current = f.customFieldId ? f.customFieldId + '|' + f.customType : '';
          if (current && !choices.some((choice) => choice.value === current)) choices.push({ value: current, label: 'ID ' + f.customFieldId + ' (not found in this tenant)' });
          nodes.push(labelled(ids('custom'), 'Custom field', selectInput(ids('custom'), choices, current, (value) => {
            const parts = value.split('|');
            f.customFieldId = parts[0] || '';
            f.customType = parts[1] || 'TEXT';
            markDirty();
            rerender(ids('custom'));
          })));
        } else {
          nodes.push(labelled(ids('customid'), 'Custom field ID', textInput(ids('customid'), f.customFieldId, (value) => { f.customFieldId = value; markDirty(); }),
            data.customEnabled ? 'Definitions could not be loaded. Enter the numeric ID from Drata.' : 'Custom fields are not enabled on the connection.'));
          nodes.push(labelled(ids('customtype'), 'Custom field type', selectInput(ids('customtype'), data.customTypes.map((type) => ({ value: type, label: type })), f.customType, (value) => {
            f.customType = value;
            markDirty();
          })));
        }
      } else {
        nodes.push(labelled(ids('reason'), 'Reason this answer is kept locally', textArea(ids('reason'), f.reason, 2, (value) => { f.reason = value; markDirty(); }),
          'Shown to administrators. Requesters see that the answer is kept with the request and not sent to Drata.'));
      }
      return nodes;
    }

    function renderItem(f, index) {
      const ids = (name) => 'ed-' + f.uid + '-' + name;
      const bodyId = ids('body');
      const open = state.open.has(f.uid);
      const errors = state.errors.filter((entry) => entry.fieldId === f.id);
      const name = f.label || f.id || 'Untitled field';
      const rerender = (focusId) => render(focusId);
      const labelNode = el('span', { class: 'editor-item-label', text: name });
      const typeTag = el('span', { class: 'tag', text: f.type });
      const destTag = el('span', { class: 'tag', text: summaryOf(f) });
      const toggle = el('button', { type: 'button', class: 'editor-toggle', 'aria-expanded': String(open), 'aria-controls': bodyId }, [
        el('span', { text: String(index + 1) + '.' }), labelNode, typeTag, destTag,
      ]);
      const body = el('div', { class: 'editor-item-body stack', id: bodyId });
      body.hidden = !open;
      toggle.addEventListener('click', () => {
        const expanded = !state.open.has(f.uid);
        if (expanded) state.open.add(f.uid); else state.open.delete(f.uid);
        toggle.setAttribute('aria-expanded', String(expanded));
        body.hidden = !expanded;
      });
      const refreshHead = () => {
        labelNode.textContent = f.label || f.id || 'Untitled field';
        typeTag.textContent = f.type;
        destTag.textContent = summaryOf(f);
      };

      const move = (delta) => {
        const target = index + delta;
        const moved = state.fields.splice(index, 1)[0];
        state.fields.splice(target, 0, moved);
        markDirty();
        render(null, moved.uid, delta < 0 ? 'up' : 'down');
      };
      const actions = el('div', { class: 'editor-item-actions' }, [
        el('button', { type: 'button', class: 'btn btn-secondary btn-sm', 'data-move': 'up', disabled: index === 0, 'aria-label': 'Move up: ' + name, text: 'Move up', on: { click: () => move(-1) } }),
        el('button', { type: 'button', class: 'btn btn-secondary btn-sm', 'data-move': 'down', disabled: index === state.fields.length - 1, 'aria-label': 'Move down: ' + name, text: 'Move down', on: { click: () => move(1) } }),
        el('button', {
          type: 'button', class: 'btn btn-danger btn-sm', 'aria-label': 'Remove: ' + name, text: 'Remove',
          on: {
            click: async () => {
              const go = await confirmDialog({ title: 'Remove field', body: ['Remove "' + name + '" from the form? This applies to the next version you save.'], confirmLabel: 'Remove field', danger: true });
              if (!go) return;
              state.fields.splice(state.fields.indexOf(f), 1);
              markDirty();
              render('add-field');
            },
          },
        }),
      ]);

      body.appendChild(el('div', { class: 'form-grid' }, [
        labelled(ids('id'), 'Field ID', textInput(ids('id'), f.id, (value) => { f.id = value; markDirty(); refreshHead(); }), 'Lowercase letters, digits and underscores. Answers are stored under this ID.'),
        labelled(ids('label'), 'Label', textInput(ids('label'), f.label, (value) => { f.label = value; markDirty(); refreshHead(); })),
      ]));
      const typeChoices = data.inputTypes.map((type) => ({ value: type, label: type }));
      const typeSelect = selectInput(ids('type'), typeChoices, f.type, (value) => {
        f.type = value;
        if (TEXT_TYPES.indexOf(value) === -1) f.maxLength = '';
        else if (f.kind === 'retained_only' && f.maxLength.trim() === '') f.maxLength = '191';
        markDirty();
        rerender(ids('type'));
      });
      const row = [labelled(ids('type'), 'Input type', typeSelect)];
      if (TEXT_TYPES.indexOf(f.type) !== -1) {
        row.push(labelled(ids('max'), 'Maximum length', textInput(ids('max'), f.maxLength, (value) => { f.maxLength = value; markDirty(); }), 'Leave empty to use the Drata limit for native fields.'));
      }
      body.appendChild(el('div', { class: 'form-grid' }, row));
      body.appendChild(checkbox(ids('required'), 'Required', f.required, (checked) => { f.required = checked; markDirty(); }));
      body.appendChild(labelled(ids('help'), 'Help text', textArea(ids('help'), f.helpText, 2, (value) => { f.helpText = value; markDirty(); })));
      if (CHOICE_TYPES.indexOf(f.type) !== -1) {
        const enumBound = f.kind === 'native' && catalog[f.nativeField] && catalog[f.nativeField].enum;
        body.appendChild(labelled(ids('options'), 'Options, one per line as value | label', textArea(ids('options'), f.optionsText, 5, (value) => { f.optionsText = value; markDirty(); }),
          enumBound ? 'Leave empty to offer every Drata value for this field. Values must come from the Drata list.' : 'Each option needs a distinct value.'));
      }
      destinationControls(f, ids, rerender).forEach((node) => body.appendChild(node));

      const item = el('li', { class: 'editor-item', 'data-uid': String(f.uid), 'data-invalid': errors.length ? 'true' : null }, [
        el('div', { class: 'editor-item-head' }, [toggle, actions]),
        errors.length ? el('div', { class: 'editor-item-errors' }, errors.map((entry) => el('p', { text: entry.message }))) : null,
        body,
      ]);
      return item;
    }

    function focusWhere(focusId, uid, dir) {
      if (focusId) {
        const target = document.getElementById(focusId);
        if (target) target.focus();
      } else if (uid) {
        const button = $('[data-uid="' + uid + '"] [data-move="' + dir + '"]');
        const fallback = $('[data-uid="' + uid + '"] .editor-toggle');
        (button && !button.disabled ? button : fallback).focus();
      }
    }

    function fieldName(id) {
      const match = state.fields.filter((f) => f.id === id)[0];
      return match ? (match.label || match.id) : id;
    }

    function openAndFocus(id) {
      const match = state.fields.filter((f) => f.id === id)[0];
      if (!match) return;
      state.open.add(match.uid);
      render(null);
      const toggle = $('[data-uid="' + match.uid + '"] .editor-toggle');
      if (toggle) toggle.focus();
    }

    function renderErrors(focus) {
      const box = $('#editor-errors');
      const list = $('#editor-error-list');
      list.replaceChildren();
      state.errors.forEach((entry) => {
        if (entry.fieldId && state.fields.some((f) => f.id === entry.fieldId)) {
          const link = el('a', { href: '#', text: fieldName(entry.fieldId) + ': ' + entry.message });
          link.addEventListener('click', (event) => {
            event.preventDefault();
            openAndFocus(entry.fieldId);
          });
          list.appendChild(el('li', null, [link]));
        } else {
          list.appendChild(el('li', { text: entry.message }));
        }
      });
      box.hidden = state.errors.length === 0;
      if (focus && state.errors.length) box.focus();
    }

    function render(focusId, movedUid, dir) {
      const list = $('#editor-fields');
      list.replaceChildren(...state.fields.map(renderItem));
      $('#form-currency').value = state.currency;
      $('#editor-version').textContent = String(state.version);
      $('#publish-label').textContent = 'Publish version ' + state.version;
      $('#editor-dirty').hidden = !state.dirty;
      const active = state.active;
      $('#editor-active').textContent = active.version
        ? 'Version ' + active.version + ', ' + (active.enabled ? 'accepting submissions' : 'closed to new submissions')
        : 'Nothing published yet';
      $('#disable-button').hidden = !active.enabled;
      renderErrors(false);
      focusWhere(focusId, movedUid, dir);
    }

    function errorsFrom(entries) {
      return (entries || []).map((entry) => ({ fieldId: entry.fieldId || null, message: entry.message }));
    }

    function errorsFromMap(map) {
      return Object.keys(map || {}).map((key) => ({ fieldId: key === '_form' ? null : key, message: map[key] }));
    }

    function adopt(schema) {
      const openIndexes = state.fields.map((f, index) => (state.open.has(f.uid) ? index : -1)).filter((index) => index >= 0);
      state.fields = schema.fields.map(hydrate);
      state.currency = schema.currency || state.currency;
      state.open = new Set(openIndexes.map((index) => state.fields[index]).filter(Boolean).map((f) => f.uid));
    }

    function nextFieldId() {
      let n = state.fields.length + 1;
      while (state.fields.some((f) => f.id === 'new_field_' + n)) n += 1;
      return 'new_field_' + n;
    }

    $('#add-field').addEventListener('click', () => {
      const f = hydrate({ id: nextFieldId(), label: 'New field', type: 'text', required: false, maxLength: 191, destination: { kind: 'retained_only', reason: '' } });
      state.fields.push(f);
      state.open.add(f.uid);
      markDirty();
      render(ids(f, 'label'));
      document.getElementById(ids(f, 'label')).select();
    });

    function ids(f, name) {
      return 'ed-' + f.uid + '-' + name;
    }

    $('#form-currency').addEventListener('input', (event) => {
      state.currency = event.target.value.toUpperCase();
      event.target.value = state.currency;
      markDirty();
    });

    async function guarded(button, task) {
      setBusy(button, true);
      try {
        await task();
      } catch (err) {
        banner(feedback, 'critical', 'Request failed', describe(err));
      } finally {
        setBusy(button, false);
      }
    }

    $('#save-form').addEventListener('click', (event) => guarded(event.currentTarget, async () => {
      try {
        const { data: saved } = await api('/api/admin/form', { schema: buildSchema() });
        state.version = saved.version;
        adopt(saved.schema);
        state.dirty = false;
        state.errors = errorsFrom(saved.errors);
        render(null);
        if (state.errors.length) {
          banner(feedback, 'warning', 'Saved with errors', 'Version ' + saved.version + ' was saved, but it cannot be published until the errors below are fixed and saved again.');
          $('#editor-errors').focus();
        } else {
          banner(feedback, 'success', 'Version ' + saved.version + ' saved', 'Preview it, then publish it when ready.');
        }
      } catch (err) {
        state.errors = errorsFromMap(err.fieldErrors);
        renderErrors(true);
        throw err;
      }
    }));

    $('#publish-form').addEventListener('click', (event) => {
      const button = event.currentTarget;
      if (state.dirty) {
        banner(feedback, 'warning', 'Unsaved changes', 'Save your changes as a new version before publishing.');
        return;
      }
      guarded(button, async () => {
        const go = await confirmDialog({
          title: 'Publish version ' + state.version,
          body: ['Requesters will see this version immediately. Submissions already made keep their original version.'],
          confirmLabel: 'Publish version ' + state.version,
        });
        if (!go) return;
        try {
          const { data: active } = await api('/api/admin/form/publish', { version: state.version });
          state.active = active;
          state.errors = [];
          render(null);
          banner(feedback, 'success', 'Version ' + active.version + ' published', 'Intake is accepting submissions.');
        } catch (err) {
          state.errors = errorsFromMap(err.fieldErrors);
          render(null);
          if (state.errors.length) $('#editor-errors').focus();
          throw err;
        }
      });
    });

    $('#disable-button').addEventListener('click', (event) => guarded(event.currentTarget, async () => {
      const { data: active } = await api('/api/admin/form/disable', {});
      state.active = active;
      render(null);
      banner(feedback, 'warning', 'Form disabled', 'New submissions are not accepted until a version is published again.');
    }));

    function sampleValue(f) {
      const options = parseOptions(f.optionsText);
      const spec = f.kind === 'native' ? catalog[f.nativeField] : null;
      const choices = options.length ? options.map((option) => option.value) : (spec && spec.enum ? data.enums[spec.enum] : []);
      if (f.type === 'boolean') return true;
      if (f.type === 'url') return 'https://vendor.example';
      if (f.type === 'email_list') return 'security@vendor.example';
      if (f.type === 'money') return '1200.50';
      if (f.type === 'select') return choices[0] || '';
      if (f.type === 'multiselect') return choices.slice(0, 1);
      if (f.kind === 'custom' && f.customType === 'NUMBER') return '12.5';
      return f.nativeField === 'name' ? 'Example Vendor' : 'Sample text';
    }

    $('#sample-answers').addEventListener('click', () => {
      const sample = {};
      state.fields.forEach((f) => { if (f.id) sample[f.id] = sampleValue(f); });
      $('#preview-answers').value = JSON.stringify(sample, null, 2);
      setText($('#preview-answers-error'), '');
    });

    function fillList(listId, wrapId, items, render1) {
      const list = $(listId);
      list.replaceChildren(...items.map(render1));
      $(wrapId).hidden = items.length === 0;
    }

    $('#preview-form').addEventListener('click', (event) => guarded(event.currentTarget, async () => {
      const box = $('#preview-answers');
      const answersError = $('#preview-answers-error');
      setText(answersError, '');
      box.removeAttribute('aria-invalid');
      let answers = {};
      if (box.value.trim()) {
        try {
          answers = JSON.parse(box.value);
        } catch (error) {
          box.setAttribute('aria-invalid', 'true');
          setText(answersError, 'Sample answers must be valid JSON.');
          box.focus();
          return;
        }
      }
      const { data: result } = await api('/api/admin/form/preview', { schema: buildSchema(), answers });
      state.errors = errorsFrom(result.schemaErrors);
      render(null);
      const out = $('#preview-output');
      out.hidden = false;
      $('#preview-banner').textContent = result.banner || 'Preview only \u2014 nothing sent to Drata.';
      fillList('#preview-schema-list', '#preview-schema-errors', state.errors, (entry) => el('li', { text: (entry.fieldId ? fieldName(entry.fieldId) + ': ' : '') + entry.message }));
      const preview = result.preview;
      const fieldErrors = preview ? Object.keys(preview.fieldErrors || {}) : [];
      fillList('#preview-field-list', '#preview-field-errors', fieldErrors, (key) => el('li', { text: fieldName(key) + ': ' + preview.fieldErrors[key] }));
      fillList('#preview-warning-list', '#preview-warnings', preview ? preview.warnings : [], (text) => el('li', { text }));
      fillList('#preview-retained-list', '#preview-retained', preview ? preview.retainedOnly : [], (entry) => el('li', { text: entry.label + ': ' + entry.reason }));
      const payloadWrap = $('#preview-payload-wrap');
      payloadWrap.hidden = !(preview && preview.payload);
      $('#preview-payload').textContent = preview && preview.payload ? JSON.stringify(preview.payload, null, 2) : '';
      out.focus();
    }));

    render(null);
  }

  function initDetail() {
    const root = $('[data-detail]');
    if (!root) return;
    const sid = root.dataset.submissionId;
    const base = '/api/admin/submissions/' + encodeURIComponent(sid);
    const payloadView = $('#payload-view');
    if (payloadView) {
      const payload = JSON.parse($('#payload-data').textContent);
      payloadView.textContent = payload === null ? '' : JSON.stringify(payload, null, 2);
    }

    const OUTCOMES = {
      VERIFIED: 'The existing Drata vendor was read back and verified.',
      MARKER_MATCH: 'A vendor carrying this submission marker was found in Drata and verified.',
      NO_MARKER_MATCH: 'No vendor with this marker was found. The outcome is still unconfirmed and nothing was resent.',
      NO_MARKER_MATCH_RETRYABLE: 'No vendor with this marker was found. The submission can now be retried.',
      NO_SNAPSHOT: 'No update snapshot exists to reconcile.',
      TARGET_MISSING: 'The update target no longer exists in Drata.',
      OBSERVED_APPLIED: 'The update is present in Drata and was verified.',
      NOT_OBSERVED: 'The update was not observed in Drata. Preview a new update before trying again.',
    };

    $$('[data-use-candidate]', root).forEach((button) => {
      button.addEventListener('click', () => {
        $$('input[name="targetDrataId"]', root).forEach((input) => { input.value = button.dataset.useCandidate; });
        const first = $('input[name="targetDrataId"]', root);
        if (first) first.focus();
      });
    });

    function lock() {
      $$('form[data-action] button, [data-update-section] button', root).forEach((button) => { button.disabled = true; });
      $$('[data-action] input, [data-action] textarea, [data-update-section] input', root).forEach((input) => { input.disabled = true; });
    }

    function finished(title, data) {
      const done = $('#action-done');
      $('#action-done-title').textContent = title;
      const outcome = data.reconcile ? ' ' + (OUTCOMES[data.reconcile] || '') : '';
      $('#action-done-message').textContent = (data.message || '') + outcome + ' Current status: ' + humanize(data.state) + '.';
      const links = $('#action-done-links');
      links.replaceChildren();
      if (data.submissionId && data.submissionId !== sid) {
        links.appendChild(el('a', { href: '/submissions/' + encodeURIComponent(data.submissionId), text: 'Open the new submission' }));
        links.appendChild(document.createTextNode(' '));
      }
      links.appendChild(el('button', { type: 'button', class: 'btn btn-secondary btn-sm', text: 'Reload details', on: { click: () => window.location.reload() } }));
      done.hidden = false;
      lock();
      done.focus();
    }

    function statusOf(form) {
      return $('[data-action-status]', form.closest('[data-update-section]') || form);
    }

    function fieldError(form, name, message) {
      const target = $('[data-error-for="' + name + '"]', form);
      if (target) setText(target, message);
      const control = form.elements[name];
      if (control instanceof Element) control.setAttribute('aria-invalid', 'true');
    }

    function reset(form) {
      $$('[data-error-for]', form).forEach((node) => setText(node, ''));
      $$('[aria-invalid]', form).forEach((node) => node.removeAttribute('aria-invalid'));
      const status = statusOf(form);
      if (status) status.hidden = true;
    }

    function showError(form, err) {
      const status = statusOf(form);
      let general = true;
      Object.keys(err.fieldErrors).forEach((name) => {
        fieldError(form, name, err.fieldErrors[name]);
        general = false;
      });
      if (general || err.status === 409) {
        const extra = err.status === 409 ? [el('button', { type: 'button', class: 'btn btn-secondary btn-sm', text: 'Reload details', on: { click: () => window.location.reload() } })] : [];
        status.replaceChildren(el('p', { text: describe(err) }), ...extra);
        status.hidden = false;
        status.focus();
      }
    }

    function invalid(form, problems) {
      const names = Object.keys(problems);
      names.forEach((name) => fieldError(form, name, problems[name]));
      if (names.length) {
        const control = form.elements[names[0]];
        if (control && control.focus) control.focus();
      }
      return names.length > 0;
    }

    function targetId(form) {
      const raw = form.elements.targetDrataId.value.trim();
      return /^\d+$/.test(raw) && Number(raw) > 0 ? Number(raw) : null;
    }

    function reasonOf(form, minimum) {
      const reason = form.elements.reason.value.trim();
      return reason.length >= minimum ? reason : null;
    }

    const PLANS = {
      retry: () => ({ nonce: { action: 'RETRY' }, path: '/retry', body: (nonce) => ({ nonce }), title: 'Retry finished' }),
      reconcile: () => ({ nonce: null, path: '/reconcile', body: () => ({}), title: 'Reconcile finished' }),
      link: (form) => {
        const target = targetId(form);
        const reason = reasonOf(form, 3);
        const problems = {};
        if (!target) problems.targetDrataId = 'Enter the numeric Drata vendor ID.';
        if (!reason) problems.reason = 'Enter a reason.';
        if (invalid(form, problems)) return null;
        return {
          nonce: { action: 'LINK_EXISTING', targetDrataId: target },
          path: '/resolve',
          body: (nonce) => ({ decision: 'LINK_EXISTING', targetDrataId: target, reason, nonce }),
          title: 'Linked to existing vendor',
        };
      },
      'confirm-new': (form) => {
        const reason = reasonOf(form, 10);
        if (invalid(form, reason ? {} : { reason: 'Write at least 10 characters.' })) return null;
        return { nonce: { action: 'CONFIRM_NEW' }, path: '/resolve', body: (nonce) => ({ decision: 'CONFIRM_NEW', reason, nonce }), title: 'Confirmed as a distinct vendor' };
      },
      recreate: (form) => {
        const reason = reasonOf(form, 10);
        const problems = {};
        if (!reason) problems.reason = 'Write at least 10 characters.';
        if (!form.elements.acknowledgeDuplicateRisk.checked) problems.acknowledgeDuplicateRisk = 'Acknowledge the duplicate risk to continue.';
        if (invalid(form, problems)) return null;
        return {
          nonce: { action: 'RECREATE' },
          path: '/resolve',
          body: (nonce) => ({ decision: 'RECREATE', reason, acknowledgeDuplicateRisk: true, nonce }),
          title: 'New submission started',
        };
      },
      cancel: (form) => {
        const reason = reasonOf(form, 10);
        if (invalid(form, reason ? {} : { reason: 'Write at least 10 characters.' })) return null;
        return { nonce: { action: 'CANCEL' }, path: '/resolve', body: (nonce) => ({ decision: 'CANCEL', reason, nonce }), title: 'Submission cancelled' };
      },
    };

    $$('form[data-action]', root).forEach((form) => {
      const plan = PLANS[form.dataset.action];
      if (!plan) return;
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const button = $('[type="submit"]', form);
        if (button.disabled) return;
        reset(form);
        const chosen = plan(form);
        if (!chosen) return;
        setBusy(button, true);
        try {
          const issued = chosen.nonce ? (await api(base + '/action-nonce', chosen.nonce)).data.nonce : null;
          const { data } = await api(base + chosen.path, chosen.body(issued));
          finished(chosen.title, data);
        } catch (err) {
          setBusy(button, false);
          showError(form, err);
        }
      });
    });

    const updateSection = $('[data-update-section]', root);
    if (updateSection) initUpdate(updateSection);

    function initUpdate(section) {
      const previewForm = $('form[data-action="update-preview"]', section);
      const diff = $('[data-update-diff]', section);
      const confirmButton = $('[data-update-confirm]', section);
      const countdown = $('[data-countdown]', section);
      const status = $('[data-action-status]', section);
      const labels = {};
      $$('input[name="fields"]', previewForm).forEach((box) => { labels[box.value] = box.dataset.label; });
      let nonce = null;
      let timer = null;

      function stop() {
        if (timer) clearInterval(timer);
        timer = null;
      }

      function startClock(expiresAt) {
        stop();
        const now = Date.now();
        const remaining = Date.parse(expiresAt) - now;
        const deadline = remaining > 0 && remaining <= APPROVAL_MS + 10000 ? now + remaining : now + APPROVAL_MS;
        const tick = () => {
          const left = Math.max(0, deadline - Date.now());
          const seconds = Math.ceil(left / 1000);
          countdown.textContent = Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0');
          if (left === 0) {
            stop();
            confirmButton.disabled = true;
            status.replaceChildren(el('p', { text: 'Approval expired. Preview the update again.' }));
            status.hidden = false;
          }
        };
        tick();
        timer = setInterval(tick, 1000);
      }

      function showDiff(result, note) {
        nonce = result.nonce;
        $('[data-update-target-name]', section).textContent = (result.target.name || 'vendor') + ' (ID ' + result.target.id + ')';
        $('[data-diff-body]', section).replaceChildren(...result.diff.map((row) => el('tr', { class: row.changed ? null : 'diff-unchanged' }, [
          el('td', { text: labels[row.field] || row.field }),
          el('td', { text: formatValue(row.before) }),
          el('td', { text: formatValue(row.after) }),
          el('td', { text: row.changed ? 'Will change' : 'No change' }),
        ])));
        diff.hidden = false;
        confirmButton.disabled = false;
        status.hidden = true;
        if (note) {
          status.replaceChildren(el('p', { text: note }));
          status.hidden = false;
        }
        startClock(result.expiresAt);
        diff.focus();
      }

      previewForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        const button = $('[type="submit"]', previewForm);
        reset(previewForm);
        const target = targetId(previewForm);
        const fields = $$('input[name="fields"]:checked', previewForm).map((box) => box.value);
        const problems = {};
        if (!target) problems.targetDrataId = 'Enter the numeric Drata vendor ID.';
        if (!fields.length) problems.fields = 'Select at least one field.';
        if (invalid(previewForm, problems)) return;
        setBusy(button, true);
        try {
          const { data } = await api(base + '/update-preview', { targetDrataId: target, fields });
          showDiff(data, null);
        } catch (err) {
          stop();
          diff.hidden = true;
          showError(previewForm, err);
        } finally {
          setBusy(button, false);
        }
      });

      confirmButton.addEventListener('click', async () => {
        setBusy(confirmButton, true);
        try {
          const { data } = await api(base + '/update-confirm', { nonce });
          stop();
          finished('Update applied', data);
        } catch (err) {
          setBusy(confirmButton, false);
          if (err.code === 'UPDATE_DRIFT' && err.detail.preview) {
            showDiff(err.detail.preview, 'The vendor changed since the preview. Review the new differences before confirming.');
            return;
          }
          confirmButton.disabled = err.code === 'NONCE_INVALID';
          status.replaceChildren(el('p', { text: describe(err) }));
          status.hidden = false;
          status.focus();
        }
      });
    }

    const exportButton = $('#export-button');
    if (exportButton) {
      exportButton.addEventListener('click', async () => {
        const status = $('#export-status');
        status.hidden = true;
        setBusy(exportButton, true);
        try {
          const { data } = await api(base + '/export', {});
          const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
          const url = URL.createObjectURL(blob);
          const link = el('a', { href: url, download: 'submission-' + sid + '.json' });
          document.body.appendChild(link);
          link.click();
          link.remove();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        } catch (err) {
          setText(status, describe(err));
          status.focus();
        } finally {
          setBusy(exportButton, false);
        }
      });
    }
  }

  function initUsers() {
    const root = $('[data-users-page]');
    if (!root) return;
    const body = $('#users-body');
    const panel = $('#secret-panel');
    const errorBox = $('#users-error');
    const createForm = $('#create-user-form');
    const createButton = $('#create-user-button');

    function showSecret(email, password) {
      $('[data-secret-email]', panel).textContent = email;
      $('[data-secret-value]', panel).textContent = password;
      setText($('[data-copy-status]', panel), '');
      panel.hidden = false;
      panel.focus();
    }

    function dismissSecret() {
      $('[data-secret-value]', panel).textContent = '';
      $('[data-secret-email]', panel).textContent = '';
      panel.hidden = true;
    }

    $('[data-dismiss-secret]', panel).addEventListener('click', dismissSecret);
    $('[data-copy-secret]', panel).addEventListener('click', async () => {
      const status = $('[data-copy-status]', panel);
      try {
        await navigator.clipboard.writeText($('[data-secret-value]', panel).textContent);
        setText(status, 'Copied.');
      } catch (error) {
        setText(status, 'Copy failed. Select the password and copy it manually.');
      }
    });

    function sync(row, user) {
      row.dataset.userId = user.id;
      row.dataset.active = String(user.active);
      row.dataset.role = user.role;
      $('[data-cell="email"]', row).textContent = user.email;
      $('[data-cell="role"]', row).textContent = user.role === 'ADMIN' ? 'Administrator' : 'Requester';
      const pill = $('[data-cell="status"]', row);
      pill.textContent = user.active ? 'Active' : 'Disabled';
      pill.dataset.tone = user.active ? 'success' : 'neutral';
      $('[data-cell="mustChange"]', row).hidden = !user.must_change_password;
      $$('[data-sr-email]', row).forEach((node) => { node.textContent = node.classList.contains('visually-hidden') ? ' ' + user.email : user.email; });
      const toggle = $('[data-user-action="toggle"]', row);
      if (toggle) toggle.firstChild.textContent = user.active ? 'Disable' : 'Enable';
      const select = $('[data-role-select]', row);
      if (select) {
        select.id = 'role-' + user.id;
        select.value = user.role;
        $('[data-role-label]', row).setAttribute('for', select.id);
      }
    }

    function rowStatus(row, text) {
      const node = $('[data-row-status]', row);
      node.textContent = text || '';
    }

    createForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      clearErrors(createForm);
      errorBox.hidden = true;
      const email = createForm.elements.email.value.trim();
      if (!email) {
        markError(createForm.elements.email.closest('.field'), 'Enter an email address.');
        createForm.elements.email.focus();
        return;
      }
      setBusy(createButton, true);
      try {
        const { data } = await api('/api/admin/users', { email, role: createForm.elements.role.value });
        const row = document.importNode($('#user-row-template').content, true).firstElementChild;
        sync(row, { id: data.id, email: data.email, role: data.role, active: true, must_change_password: true });
        body.prepend(row);
        createForm.reset();
        showSecret(data.email, data.temporaryPassword);
      } catch (err) {
        if (err.fieldErrors.email) {
          markError(createForm.elements.email.closest('.field'), err.fieldErrors.email);
          createForm.elements.email.focus();
        } else {
          setText(errorBox, describe(err));
          errorBox.focus();
        }
      } finally {
        setBusy(createButton, false);
      }
    });

    body.addEventListener('click', async (event) => {
      const button = event.target.closest('[data-user-action]');
      if (!button) return;
      const row = button.closest('tr');
      const email = $('[data-cell="email"]', row).textContent;
      const path = '/api/admin/users/' + encodeURIComponent(row.dataset.userId);
      const kind = button.dataset.userAction;
      let payload;
      if (kind === 'toggle') {
        const enable = row.dataset.active !== 'true';
        if (!enable) {
          const go = await confirmDialog({ title: 'Disable user', body: [email + ' will be signed out and cannot sign in until enabled again.'], confirmLabel: 'Disable user', danger: true });
          if (!go) return;
        }
        payload = { active: enable };
      } else if (kind === 'role') {
        payload = { role: $('[data-role-select]', row).value };
      } else {
        const go = await confirmDialog({ title: 'Reset password', body: ['Sets a new temporary password for ' + email + ' and signs them out.'], confirmLabel: 'Reset password' });
        if (!go) return;
        payload = { resetPassword: true };
      }
      rowStatus(row, '');
      button.disabled = true;
      try {
        const { data } = await api(path, payload);
        sync(row, { id: data.id, email, role: data.role, active: data.active, must_change_password: data.temporaryPassword ? true : row.querySelector('[data-cell="mustChange"]').hidden === false });
        rowStatus(row, 'Saved.');
        if (data.temporaryPassword) showSecret(email, data.temporaryPassword);
      } catch (err) {
        rowStatus(row, describe(err));
      } finally {
        button.disabled = false;
      }
    });
  }

  initTheme();
  initLogout();
  initReauth();
  initConfirm();
  localizeTimes();
  initLogin();
  initIntake();
  initHistory();
  initPassword();
  initConnection();
  initEditor();
  initDetail();
  initUsers();
})();
