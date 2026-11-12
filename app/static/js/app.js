// Open and close the "new endpoint" dialog. Clicking the backdrop closes it too.
document.querySelectorAll('[data-open-modal]').forEach((button) =>
  button.addEventListener('click', () => document.getElementById(button.dataset.openModal)?.showModal()));

document.querySelectorAll('[data-close-modal]').forEach((button) =>
  button.addEventListener('click', () => document.getElementById(button.dataset.closeModal)?.close()));

document.querySelectorAll('dialog.modal').forEach((dialog) =>
  dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); }));

// Copy the text of the element a button points at.
document.querySelectorAll('[data-copy]').forEach((button) =>
  button.addEventListener('click', async () => {
    const target = document.querySelector(button.dataset.copy);
    if (!target) return;
    const original = button.innerText;
    try {
      await navigator.clipboard.writeText(target.innerText);
      button.innerText = 'Copied';
    } catch {
      button.innerText = 'Copy failed';
    }
    setTimeout(() => { button.innerText = original; }, 1200);
  }));

// A replay or an API call can take seconds, so show that something is running.
document.querySelectorAll('form').forEach((form) =>
  form.addEventListener('submit', (event) => {
    const button = event.submitter;
    if (!button) return;
    button.classList.add('busy');
    setTimeout(() => form.querySelectorAll('button[type="submit"], button:not([type])').forEach((b) => { b.disabled = true; }), 0);
  }));
