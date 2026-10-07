/* Touchpoint CRUD, CRM placeholder authoring and protected file attachments. */
(() => {
  'use strict';
  const root = document.querySelector('.touchpoint-workspace');
  if (!root) return;
  const dialog = document.getElementById('touchpoint-dialog');
  const form = document.getElementById('touchpoint-form');
  const deletion = document.getElementById('touchpoint-delete-dialog');
  const existingFiles = document.getElementById('touchpoint-existing-files');
  const fileInput = document.getElementById('touchpoint-files');
  const fileSelection = document.getElementById('touchpoint-file-selection');
  const cards = [...root.querySelectorAll('[data-managed-reply]')];
  let category = '', deleteData;

  function filter() {
    const query = document.getElementById('touchpoint-search').value.toLowerCase();
    cards.forEach(card => {
      card.hidden = Boolean(category && card.dataset.category !== category) ||
        !card.textContent.toLowerCase().includes(query);
    });
    document.getElementById('touchpoint-empty').hidden = cards.some(card => !card.hidden);
    document.getElementById('touchpoint-category-actions').hidden = !category;
    root.querySelectorAll('[data-filter-category]').forEach(button =>
      button.setAttribute('aria-current', String(button.dataset.filterCategory === category))
    );
  }

  function showExistingFiles(card) {
    existingFiles.replaceChildren();
    if (!card) return;
    card.querySelectorAll('[data-stored-attachment]').forEach(item => {
      const row = document.createElement('label');
      row.className = 'flex items-center gap-2 rounded-lg border border-gray-200 bg-white px-3 py-2 text-xs';
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.name = 'remove_attachments';
      box.value = item.dataset.storedAttachment;
      const name = document.createElement('span');
      name.className = 'min-w-0 flex-1 truncate';
      name.textContent = item.dataset.filename || 'Attachment';
      const remove = document.createElement('span');
      remove.className = 'text-red-600';
      remove.textContent = 'Remove';
      row.append(box, name, remove);
      existingFiles.appendChild(row);
    });
  }

  function open(kind, card) {
    form.reset();
    const isCategory = kind === 'category';
    form.elements.action.value = isCategory ? 'save_category' : 'save_reply';
    form.elements.reply_id.value = card?.dataset.managedReply || '';
    const select = form.querySelector('[data-category-select]');
    const selectedId = card?.dataset.category || category || select.value;
    form.querySelector('[data-category-id]').value = isCategory ? category : selectedId;
    select.value = selectedId;

    root.querySelectorAll('[data-category-field]').forEach(node => { node.hidden = !isCategory; });
    root.querySelectorAll('[data-reply-field]').forEach(node => { node.hidden = isCategory; });
    form.elements.name.required = isCategory;
    form.elements.title.required = !isCategory;
    form.elements.body.required = !isCategory;
    document.getElementById('touchpoint-form-error').textContent = '';
    document.getElementById('touchpoint-dialog-title').textContent =
      isCategory ? (category ? 'Rename category' : 'New category') :
      (card ? 'Edit Touchpoint' : 'New Touchpoint');

    if (isCategory && category) {
      const option = [...select.options].find(item => item.value === category);
      form.elements.name.value = option?.textContent?.trim() || '';
    }
    if (card && !isCategory) {
      form.elements.title.value = card.querySelector('h3').textContent.trim();
      form.elements.body.value = card.querySelector('[data-body]').textContent.trim();
    }
    showExistingFiles(isCategory ? null : card);
    fileInput.value = '';
    fileSelection.textContent = '';
    dialog.showModal();
  }

  async function post(data) {
    const response = await fetch(location.pathname, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'X-CSRFToken': form.querySelector('[name=csrfmiddlewaretoken]').value },
      body: data,
    });
    const result = await response.json();
    if (!response.ok || response.redirected) throw new Error(result.error || 'Unable to save');
    location.reload();
  }

  root.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button) return;
    if (button.matches('[data-filter-category]')) {
      category = button.dataset.filterCategory;
      filter();
    }
    if (button.matches('[data-new-category]')) { category = ''; open('category'); }
    if (button.matches('[data-edit-category]')) open('category');
    if (button.matches('[data-new-reply]')) {
      if (!form.querySelector('[data-category-select]').options.length) {
        document.getElementById('touchpoint-status').textContent = 'Create a category first.';
        return;
      }
      open('reply');
    }
    if (button.matches('[data-edit-reply]')) open('reply', button.closest('[data-managed-reply]'));
    if (button.matches('[data-close-dialog]')) button.closest('dialog').close();
    if (button.matches('[data-delete-category],[data-delete-reply]')) {
      deleteData = new FormData();
      const isCategory = button.matches('[data-delete-category]');
      deleteData.set('action', isCategory ? 'delete_category' : 'delete_reply');
      deleteData.set(isCategory ? 'category_id' : 'reply_id', isCategory ?
        category : button.closest('[data-managed-reply]').dataset.managedReply);
      document.getElementById('touchpoint-delete-description').textContent =
        isCategory ? 'This deletes the category and all replies inside it. Messages already sent stay unchanged.' :
        'This removes the saved reply and its attached files. Messages already sent stay unchanged.';
      document.getElementById('touchpoint-delete-error').textContent = '';
      deletion.showModal();
    }
  });

  fileInput.addEventListener('change', () => {
    const files = [...(fileInput.files || [])];
    const kept = existingFiles.querySelectorAll('input:not(:checked)').length;
    const size = files.reduce((total, file) => total + file.size, 0);
    fileSelection.textContent = files.length ?
      files.length + ' new file(s) selected · ' + (size / (1024 * 1024)).toFixed(1) + ' MB' +
      ' · ' + (kept + files.length) + '/5 attachments' : '';
  });

  form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('[type=submit]');
    button.disabled = true;
    const data = new FormData(form);
    if (data.get('action') === 'save_reply') {
      data.set('category_id', form.querySelector('[data-category-select]').value);
    }
    try { await post(data); }
    catch (error) {
      document.getElementById('touchpoint-form-error').textContent = error.message;
      button.disabled = false;
    }
  });

  document.getElementById('touchpoint-delete-form').addEventListener('submit', async event => {
    event.preventDefault();
    const button = event.target.querySelector('[type=submit]');
    button.disabled = true;
    try { await post(deleteData); }
    catch (error) {
      document.getElementById('touchpoint-delete-error').textContent = error.message;
      button.disabled = false;
    }
  });
  document.getElementById('touchpoint-search').addEventListener('input', filter);
  filter();
})();
