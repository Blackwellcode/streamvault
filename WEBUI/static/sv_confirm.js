/* Accessible confirmation dialog for forms and buttons (replaces inline confirm() handlers).
 *
 * <form data-confirm="Delete X? This cannot be undone." data-confirm-ok="Delete" data-confirm-danger>
 * <button data-confirm="..."> inside a form works the same way for that submitter only.
 * Uses a native <dialog> (focus moves in, Escape cancels, focus returns to the trigger).
 */
(function () {
  'use strict';

  var dialog = null;
  var msgEl = null;
  var okBtn = null;
  var cancelBtn = null;

  function label(name, fallback) {
    var meta = document.querySelector('meta[name="sv-i18n-' + name + '"]');
    return (meta && meta.getAttribute('content')) || fallback;
  }

  function ensureDialog() {
    if (dialog) return dialog;
    dialog = document.createElement('dialog');
    dialog.className = 'sv-dialog sv-confirm';
    dialog.setAttribute('aria-labelledby', 'sv-confirm-title');
    dialog.setAttribute('aria-describedby', 'sv-confirm-msg');

    var body = document.createElement('div');
    body.className = 'sv-dialog__body';
    var title = document.createElement('h2');
    title.id = 'sv-confirm-title';
    title.className = 'sv-dialog__title';
    title.textContent = label('confirm-title', 'Please confirm');
    msgEl = document.createElement('p');
    msgEl.id = 'sv-confirm-msg';
    msgEl.className = 'sv-confirm__msg';
    body.appendChild(title);
    body.appendChild(msgEl);

    var foot = document.createElement('div');
    foot.className = 'sv-dialog__foot';
    cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'sv-btn sv-btn--secondary';
    cancelBtn.value = 'cancel';
    okBtn = document.createElement('button');
    okBtn.type = 'button';
    okBtn.value = 'ok';
    foot.appendChild(cancelBtn);
    foot.appendChild(okBtn);

    dialog.appendChild(body);
    dialog.appendChild(foot);
    document.body.appendChild(dialog);

    cancelBtn.addEventListener('click', function () { dialog.close('cancel'); });
    okBtn.addEventListener('click', function () { dialog.close('ok'); });
    dialog.addEventListener('click', function (e) { if (e.target === dialog) dialog.close('cancel'); });
    return dialog;
  }

  function ask(trigger, source) {
    return new Promise(function (resolve) {
      var dlg = ensureDialog();
      if (typeof dlg.showModal !== 'function') {
        resolve(window.confirm(source.getAttribute('data-confirm')));
        return;
      }
      msgEl.textContent = source.getAttribute('data-confirm');
      okBtn.textContent = source.getAttribute('data-confirm-ok') || label('confirm-ok', 'OK');
      okBtn.className = 'sv-btn ' + (source.hasAttribute('data-confirm-danger') ? 'sv-btn--danger' : 'sv-btn--primary');
      cancelBtn.textContent = source.getAttribute('data-confirm-cancel') || label('cancel', 'Cancel');
      dlg.returnValue = '';
      dlg.addEventListener('close', function onClose() {
        dlg.removeEventListener('close', onClose);
        if (trigger && typeof trigger.focus === 'function') trigger.focus();
        resolve(dlg.returnValue === 'ok');
      });
      dlg.showModal();
      cancelBtn.focus();
    });
  }

  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    var submitter = e.submitter || null;
    var source = submitter && submitter.hasAttribute('data-confirm') ? submitter
      : (form.hasAttribute('data-confirm') ? form : null);
    if (!source) return;
    if (form.__svConfirmed) {
      form.__svConfirmed = false;
      return;
    }
    e.preventDefault();
    e.stopImmediatePropagation();
    ask(submitter || form.querySelector('[type="submit"]'), source).then(function (ok) {
      if (!ok) return;
      form.__svConfirmed = true;
      if (typeof form.requestSubmit === 'function') {
        form.requestSubmit(submitter && submitter.form === form ? submitter : undefined);
      } else {
        form.submit();
      }
    });
  }, true);

  window.svConfirm = function (message, okLabel, danger) {
    var src = document.createElement('div');
    src.setAttribute('data-confirm', message);
    if (okLabel) src.setAttribute('data-confirm-ok', okLabel);
    if (danger) src.setAttribute('data-confirm-danger', '');
    return ask(document.activeElement, src);
  };
})();
