/* SHVYA's server-rendered blog editor. The server owns sanitisation and publishing.
 * A single request queue prevents autosave from racing a publish or restore.
 */
(() => {
    'use strict';
    const form = document.getElementById('blog-editor-form');
    if (!form) return;
    const body = document.getElementById('blog-body-editor');
    const bodyInput = form.elements.namedItem('body_html');
    const status = document.getElementById('blog-save-status');
    const message = document.getElementById('blog-editor-message');
    const media = new Map();
    const fields = name => form.elements.namedItem(name);
    const value = name => fields(name)?.value || '';
    const csrf = () => value('csrfmiddlewaretoken');
    let changeNumber = 0;
    let savedNumber = 0;
    let saveTimer;
    let savePromise = null;
    let actionInProgress = false;
    let conflict = false;
    let leaving = false;
    let savedRange = null;
    let imageTarget = 'body';
    let editingImage = null;
    let editingLink = null;
    let uploadInProgress = false;
    let wasScheduled = document.getElementById('blog-current-status').textContent.trim() === 'Scheduled';
    let slugManuallyEdited = Boolean(value('slug'));

    for (const node of document.querySelectorAll('#blog-image-data [data-id]')) {
        media.set(node.dataset.id, {
            id: node.dataset.id, url: node.dataset.publicUrl, preview_url: node.dataset.previewUrl,
            alt_text: node.dataset.alt, caption: node.dataset.caption,
            width: Number(node.dataset.width), height: Number(node.dataset.height),
        });
    }

    function setStatus(text, state = '') {
        status.textContent = text;
        status.dataset.state = state;
    }

    function clearMessage() {
        message.hidden = true;
        message.replaceChildren();
    }

    function showMessage(text, isError = true, focus = false) {
        message.textContent = text;
        message.hidden = false;
        message.classList.toggle('is-error', isError);
        if (focus) message.focus();
    }

    function safeHref(input) {
        const link = String(input || '').trim();
        if (!link || /[\u0000-\u0020\\]/.test(link) || link.startsWith('//')) return '';
        if (link.startsWith('/') || link.startsWith('#')) return link;
        try {
            const parsed = new URL(link);
            if (['https:', 'http:'].includes(parsed.protocol) && parsed.hostname && !parsed.username && !parsed.password) return link;
        } catch (_) { /* Invalid URLs stay unlinked. */ }
        return '';
    }

    function imageForSource(source) {
        for (const item of media.values()) {
            if (source === item.url || source === item.preview_url) return item;
        }
        return null;
    }

    // Positive allowlist used for paste and initial rendering. No event handlers,
    // pasted remote images, inline styles, embeds, or active content enter the DOM.
    // Server sanitisation remains the authority for all saved HTML.
    function cleanHTML(html, editorMode = false) {
        const parsed = new DOMParser().parseFromString(html, 'text/html');
        const output = document.createElement('div');
        const allowed = new Set(['p', 'h2', 'h3', 'h4', 'strong', 'em', 'u', 's', 'ul', 'ol', 'li', 'blockquote', 'hr', 'br', 'a', 'img', 'figure', 'figcaption', 'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'pre', 'code', 'div', 'span', 'sup', 'sub']);
        const blocked = new Set(['script', 'style', 'iframe', 'object', 'embed', 'svg', 'math', 'template', 'form', 'input', 'button', 'textarea', 'select', 'noscript']);
        function walk(node, parent, depth = 0) {
            if (depth > 80) return;
            if (node.nodeType === Node.TEXT_NODE) { parent.append(document.createTextNode(node.textContent)); return; }
            if (node.nodeType !== Node.ELEMENT_NODE) return;
            let tag = node.tagName.toLowerCase();
            if (blocked.has(tag)) return;
            if (tag === 'b') tag = 'strong';
            if (tag === 'i') tag = 'em';
            if (tag === 'h1') tag = 'h2';
            if (!allowed.has(tag)) {
                Array.from(node.childNodes).forEach(child => walk(child, parent, depth + 1));
                return;
            }
            const element = document.createElement(tag);
            if (tag === 'a') {
                const href = safeHref(node.getAttribute('href'));
                if (href) element.setAttribute('href', href);
                element.setAttribute('rel', 'noopener noreferrer');
            }
            if (tag === 'img') {
                const item = imageForSource(node.getAttribute('src'));
                if (!item) return;
                element.setAttribute('src', editorMode ? item.preview_url : item.url);
                element.setAttribute('alt', (node.getAttribute('alt') || '').slice(0, 300));
                if (item.width) element.setAttribute('width', item.width);
                if (item.height) element.setAttribute('height', item.height);
                element.setAttribute('loading', 'lazy');
            }
            if (['th', 'td'].includes(tag)) {
                for (const attr of ['rowspan', 'colspan']) {
                    const count = Number(node.getAttribute(attr));
                    if (Number.isInteger(count) && count > 1) element.setAttribute(attr, Math.min(count, 20));
                }
            }
            if (['blockquote', 'div', 'p'].includes(tag) && ['callout', 'callout-note'].includes(node.className)) element.className = node.className;
            Array.from(node.childNodes).forEach(child => walk(child, element, depth + 1));
            parent.append(element);
        }
        Array.from(parsed.body.childNodes).forEach(node => walk(node, output));
        return output.innerHTML;
    }

    function syncBody() { bodyInput.value = cleanHTML(body.innerHTML); }

    function rememberSelection() {
        const selection = window.getSelection();
        if (selection.rangeCount && body.contains(selection.anchorNode) && body.contains(selection.focusNode)) savedRange = selection.getRangeAt(0).cloneRange();
    }

    function restoreSelection() {
        body.focus();
        const selection = window.getSelection();
        selection.removeAllRanges();
        if (savedRange && body.contains(savedRange.commonAncestorContainer)) {
            selection.addRange(savedRange);
        } else {
            const range = document.createRange();
            range.selectNodeContents(body);
            range.collapse(false);
            selection.addRange(range);
        }
    }

    function insertHTML(html) {
        restoreSelection();
        // execCommand retains the native browser undo stack for text operations.
        document.execCommand('insertHTML', false, html);
        rememberSelection();
        markChanged();
    }

    function updateMediaPreview(name, imageId, placeholderId, fallbackId = '') {
        const item = media.get(value(name)) || media.get(value(fallbackId));
        const image = document.getElementById(imageId);
        const placeholder = document.getElementById(placeholderId);
        image.hidden = !item;
        placeholder.hidden = Boolean(item);
        if (item) image.src = item.preview_url;
        else image.removeAttribute('src');
    }

    function updatePreviews() {
        const title = value('title').trim();
        const seoTitle = value('seo_title').trim() || title;
        const description = value('meta_description').trim() || value('excerpt').trim();
        const text = body.innerText.trim();
        const words = text ? text.split(/\s+/).length : 0;
        document.getElementById('blog-word-count').textContent = `${words.toLocaleString()} words · ${Math.max(1, Math.ceil(words / 220))} min read`;
        document.getElementById('blog-snippet-title').textContent = seoTitle || 'Your article title';
        document.getElementById('blog-snippet-description').textContent = description || 'Add a concise description of what readers will learn.';
        document.getElementById('blog-snippet-url').textContent = `${form.dataset.siteUrl}/blog/${value('slug') || 'your-article-slug'}/`;
        document.getElementById('blog-seo-title-count').textContent = `${seoTitle.length} characters · around 50–60 often displays well`;
        document.getElementById('blog-description-count').textContent = `${value('meta_description').length} characters · around 140–160 often displays well`;
        document.getElementById('blog-social-preview-title').textContent = value('social_title') || seoTitle || 'Your article title';
        document.getElementById('blog-social-preview-description').textContent = value('social_description') || description || 'A useful story starts with a clear introduction.';
        updateMediaPreview('cover_image', 'blog-cover-preview-image', 'blog-cover-placeholder');
        updateMediaPreview('social_image', 'blog-social-preview-image', 'blog-social-placeholder', 'cover_image');
        const focus = value('focus_keyword').trim().toLocaleLowerCase();
        const checks = [
            [Boolean(title), 'Article title supplied'],
            [Boolean(value('meta_description').trim()), 'Meta description supplied'],
            [Boolean(body.querySelector('h2, h3')), 'Section headings included'],
            [!Array.from(body.querySelectorAll('img')).some(image => !image.getAttribute('alt')?.trim()), 'Image alt text supplied'],
            [Boolean(value('author_name').trim()), 'Author attribution'],
            [Boolean(focus) && `${title} ${text}`.toLocaleLowerCase().includes(focus), 'Focus phrase included'],
        ];
        const container = document.getElementById('blog-seo-checks');
        container.replaceChildren(...checks.map(([passed, label]) => {
            const item = document.createElement('li');
            item.className = passed ? 'is-passed' : '';
            item.textContent = label;
            return item;
        }));
    }

    function markChanged() {
        syncBody();
        changeNumber += 1;
        updatePreviews();
        if (!conflict) setStatus('Unsaved changes');
        if (wasScheduled) {
            showMessage('Editing a scheduled article cancels its schedule when saved. Schedule it again when your changes are ready.', false);
            wasScheduled = false;
        }
        clearTimeout(saveTimer);
        if (!actionInProgress && !conflict) saveTimer = setTimeout(() => saveDraft().catch(() => {}), 1800);
    }

    function errorText(data, fallback) {
        if (data.errors) {
            const errors = Object.entries(data.errors).map(([name, errors]) => {
                const detail = (Array.isArray(errors) ? errors : [errors]).map(error => typeof error === 'object' ? error.message : error).join(' ');
                const label = name.replaceAll('_', ' ');
                return `${label}: ${detail}`;
            });
            if (errors.length) return errors.join(' ');
        }
        return data.detail || fallback;
    }

    async function responseJSON(response) {
        if (!response.headers.get('content-type')?.includes('application/json')) {
            throw new Error(response.redirected || response.status === 403 || response.status === 401
                ? 'Your session may have expired. Keep this tab open and sign in again in another tab before retrying.'
                : 'The server could not save your article. Your edits are still in this tab. Please try again.');
        }
        return response.json();
    }

    function downloadBackup() {
        syncBody();
        const data = Object.fromEntries(new FormData(form));
        delete data.csrfmiddlewaretoken;
        const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'}));
        const link = document.createElement('a');
        link.href = url;
        link.download = `${value('slug') || 'article'}-draft-backup.json`;
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    }

    function showConflict(detail) {
        conflict = true;
        clearTimeout(saveTimer);
        setStatus('Version conflict — edits retained', 'error');
        showMessage(detail || 'This article changed in another tab or by another editor. Your local edits are preserved here. Download a backup before loading the latest version.', true, true);
        const actions = document.createElement('div');
        actions.className = 'blog-actions';
        const download = document.createElement('button');
        download.type = 'button'; download.className = 'blog-button blog-button-secondary'; download.textContent = 'Download my draft backup';
        download.addEventListener('click', downloadBackup);
        const reload = document.createElement('button');
        reload.type = 'button'; reload.className = 'blog-button blog-button-secondary'; reload.textContent = 'Load latest version';
        reload.addEventListener('click', () => {
            if (window.confirm('Load the latest version? Unsaved edits in this tab will be lost. Download your backup first if you need to keep them.')) {
                leaving = true;
                window.location.reload();
            }
        });
        actions.append(download, reload); message.append(actions);
    }

    function acceptSave(data, savedAt) {
        fields('version').value = data.version;
        if (data.edit_url) {
            form.dataset.editUrl = data.edit_url;
            form.action = data.edit_url;
            if (!form.dataset.autosaveUrl) window.history.replaceState(null, '', data.edit_url);
        }
        if (data.autosave_url) form.dataset.autosaveUrl = data.autosave_url;
        if (data.preview_url) form.dataset.previewUrl = data.preview_url;
        savedNumber = savedAt;
        if (message.classList.contains('is-error')) clearMessage();
        if (data.status) {
            const badge = document.getElementById('blog-current-status');
            const labels = {draft: 'Draft', review: 'In review', published: 'Published', scheduled: 'Scheduled', archived: 'Archived'};
            badge.textContent = labels[data.status] || data.status;
            badge.className = `blog-badge blog-badge-${data.status}`;
        }
        setStatus(changeNumber === savedAt ? 'All changes saved' : 'Unsaved changes', changeNumber === savedAt ? 'saved' : '');
    }

    async function saveDraft(force = false) {
        clearTimeout(saveTimer);
        if (conflict) throw new Error('Resolve the version conflict before saving.');
        while (savePromise) {
            await savePromise;
            if (changeNumber === savedNumber && form.dataset.editUrl) return;
        }
        if (actionInProgress || (!force && changeNumber === savedNumber)) return;
        if (!value('title').trim()) { setStatus('Add a title to start autosaving'); return; }
        syncBody();
        const capturedChange = changeNumber;
        const payload = new FormData(form);
        payload.set('action', 'save');
        const endpoint = form.dataset.autosaveUrl || form.dataset.newUrl;
        setStatus('Saving…', 'saving');
        savePromise = (async () => {
            try {
                const response = await fetch(endpoint, {method: 'POST', body: payload, credentials: 'same-origin', headers: {'X-Requested-With': 'XMLHttpRequest', Accept: 'application/json'}});
                const data = await responseJSON(response);
                if (response.status === 409) { showConflict(data.detail); throw new Error('Version conflict'); }
                if (!response.ok || !data.ok) throw new Error(errorText(data, 'The draft could not be saved.'));
                acceptSave(data, capturedChange);
            } catch (error) {
                if (!conflict) { setStatus('Not saved — retry', 'error'); showMessage(error.message); }
                throw error;
            } finally {
                savePromise = null;
            }
        })();
        await savePromise;
        if (changeNumber !== savedNumber && !actionInProgress && !conflict) saveTimer = setTimeout(() => saveDraft().catch(() => {}), 1800);
    }

    // Restore/publication wait for any existing save, then post a fresh snapshot
    // with its returned version. No trailing autosave can undo an explicit action.
    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (actionInProgress) return;
        if (conflict) { message.focus(); return; }
        if (uploadInProgress) { showMessage('Wait for your image upload to finish before saving or publishing.', false, true); return; }
        const action = event.submitter?.value || 'save';
        if (action === 'schedule' && !value('schedule_at')) {
            showMessage('Choose a future publication date and time in IST.', true, true);
            fields('schedule_at').focus(); return;
        }
        if (['unpublish', 'archive'].includes(action) && !window.confirm(action === 'unpublish' ? 'Remove this article from the public website? Its content and revisions will be kept.' : 'Archive this article? It will be removed from the public website and kept in your manager.')) return;
        actionInProgress = true;
        clearTimeout(saveTimer);
        try {
            if (savePromise) await savePromise;
            if (conflict) return;
            syncBody();
            const hidden = document.createElement('input');
            hidden.type = 'hidden'; hidden.name = 'action'; hidden.value = action;
            form.append(hidden);
            setStatus(action === 'publish' ? 'Publishing…' : 'Saving…', 'saving');
            leaving = true;
            HTMLFormElement.prototype.submit.call(form);
        } catch (error) {
            if (!conflict) showMessage(`${error.message} Your action was not submitted. Retry after your draft is saved.`, true, true);
        } finally {
            if (!leaving) actionInProgress = false;
        }
    });

    document.getElementById('blog-preview-button').addEventListener('click', async () => {
        if (conflict) { message.focus(); return; }
        if (uploadInProgress) { showMessage('Wait for your image upload to finish before opening a preview.', false); return; }
        if (!value('title').trim()) { fields('title').focus(); showMessage('Add an article title before opening a preview.', false); return; }
        const previewWindow = window.open('about:blank', '_blank');
        if (previewWindow) previewWindow.opener = null;
        try {
            await saveDraft(true);
            // Save changes typed while the first request was in flight, if any.
            if (changeNumber !== savedNumber) await saveDraft(true);
            if (!form.dataset.previewUrl) throw new Error('Save your article before opening its preview.');
            if (previewWindow) previewWindow.location.href = form.dataset.previewUrl;
            else {
                showMessage('Your preview is ready. Allow pop-ups or open the link below.', false);
                const link = document.createElement('a'); link.href = form.dataset.previewUrl; link.target = '_blank'; link.rel = 'noopener'; link.textContent = 'Open article preview'; message.append(' ', link);
            }
        } catch (error) { previewWindow?.close(); if (!conflict) showMessage(error.message); }
    });

    document.querySelectorAll('[data-restore-url]').forEach(button => button.addEventListener('click', async () => {
        if (actionInProgress || conflict) return;
        if (!window.confirm('Restore this revision as your draft? Save your current changes first if you want to keep them in revision history. The live article will stay unchanged.')) return;
        actionInProgress = true;
        clearTimeout(saveTimer);
        try {
            if (savePromise) await savePromise;
            if (conflict) return;
            const restoreForm = document.createElement('form');
            restoreForm.method = 'POST'; restoreForm.action = button.dataset.restoreUrl;
            for (const [name, data] of Object.entries({csrfmiddlewaretoken: csrf(), version: value('version'), expected_version: value('version')})) {
                const input = document.createElement('input'); input.type = 'hidden'; input.name = name; input.value = data; restoreForm.append(input);
            }
            document.body.append(restoreForm); leaving = true; restoreForm.submit();
        } catch (error) { if (!conflict) showMessage(error.message); }
        finally { if (!leaving) actionInProgress = false; }
    }));

    function openDialog(dialog) { rememberSelection(); dialog.showModal(); }
    function imageUploadPending(dialog) { return dialog.id === 'blog-image-dialog' && uploadInProgress; }
    document.querySelectorAll('[data-close-dialog]').forEach(button => button.addEventListener('click', () => {
        const dialog = button.closest('dialog');
        if (!imageUploadPending(dialog)) dialog.close();
    }));
    document.querySelectorAll('.blog-dialog').forEach(dialog => {
        dialog.addEventListener('click', event => { if (event.target === dialog && !imageUploadPending(dialog)) dialog.close(); });
        dialog.addEventListener('cancel', event => { if (imageUploadPending(dialog)) event.preventDefault(); });
    });

    // Preserve the text selection when a toolbar button receives focus.
    document.querySelector('.blog-toolbar').addEventListener('pointerdown', event => {
        rememberSelection();
        if (event.target.closest('button[data-command]')) event.preventDefault();
    });
    document.querySelectorAll('[data-command]').forEach(button => button.addEventListener('click', () => {
        rememberSelection();
        restoreSelection();
        document.execCommand(button.dataset.command, false, button.dataset.value || null);
        rememberSelection(); markChanged();
    }));
    document.getElementById('blog-block-format').addEventListener('change', event => {
        rememberSelection();
        restoreSelection(); document.execCommand('formatBlock', false, event.target.value); rememberSelection(); markChanged();
    });
    document.addEventListener('selectionchange', () => {
        rememberSelection();
        const selection = window.getSelection();
        if (!body.contains(selection.anchorNode)) return;
        for (const command of ['bold', 'italic', 'underline']) {
            document.querySelector(`[data-command="${command}"]`).setAttribute('aria-pressed', String(document.queryCommandState(command)));
        }
    });
    // selectionchange may be deferred until the next rendering cycle. Capture
    // keyboard/pointer changes synchronously before a toolbar gets focus.
    body.addEventListener('keyup', rememberSelection);
    body.addEventListener('mouseup', rememberSelection);
    body.addEventListener('input', rememberSelection);

    body.addEventListener('paste', event => {
        event.preventDefault(); rememberSelection();
        const html = event.clipboardData?.getData('text/html');
        if (html) insertHTML(cleanHTML(html, true));
        else {
            restoreSelection(); document.execCommand('insertText', false, event.clipboardData?.getData('text/plain') || ''); markChanged();
        }
    });
    body.addEventListener('drop', event => {
        // An upload dialog provides alt text and an authenticated upload boundary.
        // Never allow the browser to insert an arbitrary local/remote image URL.
        event.preventDefault();
        if (event.dataTransfer?.files.length) showMessage('Use the Image button to upload an image and add its description.', false);
    });

    function openImage(target = 'body', current = null) {
        rememberSelection(); imageTarget = target; editingImage = current;
        const dialog = document.getElementById('blog-image-dialog');
        document.getElementById('blog-image-file').value = '';
        document.getElementById('blog-image-error').textContent = '';
        const item = current ? imageForSource(current.getAttribute('src')) : media.get(value(`${target}_image`));
        document.getElementById('blog-existing-image').value = item?.id || '';
        document.getElementById('blog-image-alt').value = current?.getAttribute('alt') ?? item?.alt_text ?? '';
        document.getElementById('blog-image-caption').value = current?.closest('figure')?.querySelector('figcaption')?.textContent ?? item?.caption ?? '';
        document.getElementById('blog-image-dialog-title').textContent = current ? 'Edit article image' : target === 'body' ? 'Add an image' : `Choose ${target} image`;
        document.getElementById('blog-insert-image').textContent = current ? 'Update image' : target === 'body' ? 'Insert image' : 'Use image';
        document.getElementById('blog-remove-image').hidden = !(current || (target !== 'body' && item));
        setImageMetadataState();
        dialog.showModal();
    }

    function setImageMetadataState() {
        const useOriginal = imageTarget !== 'body' && Boolean(document.getElementById('blog-existing-image').value) && !document.getElementById('blog-image-file').files.length;
        document.getElementById('blog-image-alt').disabled = useOriginal;
        document.getElementById('blog-image-caption').disabled = useOriginal;
    }

    document.getElementById('blog-image-button').addEventListener('click', () => openImage());
    document.querySelectorAll('[data-open-image]').forEach(button => button.addEventListener('click', () => openImage(button.dataset.openImage)));
    body.addEventListener('click', event => {
        if (event.target.tagName === 'IMG') openImage('body', event.target);
        const anchor = event.target.closest('a');
        if (anchor) event.preventDefault();
    });
    document.getElementById('blog-existing-image').addEventListener('change', event => {
        const item = media.get(event.target.value);
        if (item) {
            document.getElementById('blog-image-file').value = '';
            document.getElementById('blog-image-alt').value = item.alt_text;
            document.getElementById('blog-image-caption').value = item.caption || '';
        }
        setImageMetadataState();
    });
    document.getElementById('blog-image-file').addEventListener('change', event => {
        if (event.target.files.length) document.getElementById('blog-existing-image').value = '';
        setImageMetadataState();
    });

    document.getElementById('blog-insert-image').addEventListener('click', async () => {
        if (uploadInProgress) return;
        const button = document.getElementById('blog-insert-image');
        const file = document.getElementById('blog-image-file').files[0];
        const alt = document.getElementById('blog-image-alt').value.trim();
        const caption = document.getElementById('blog-image-caption').value.trim();
        const error = document.getElementById('blog-image-error');
        let item = media.get(document.getElementById('blog-existing-image').value);
        error.textContent = '';
        if (!file && !item) { error.textContent = 'Upload an image or choose a previous upload.'; return; }
        uploadInProgress = true; button.disabled = true;
        const imageDialog = document.getElementById('blog-image-dialog');
        imageDialog.setAttribute('aria-busy', 'true');
        imageDialog.querySelectorAll('input,select,button').forEach(control => { control.disabled = true; });
        const originalText = button.textContent;
        try {
            if (file) {
                button.textContent = 'Uploading…';
                const payload = new FormData(); payload.set('csrfmiddlewaretoken', csrf()); payload.set('image', file); payload.set('alt_text', alt); payload.set('caption', caption);
                const response = await fetch(form.dataset.uploadUrl, {method: 'POST', body: payload, credentials: 'same-origin', headers: {'X-Requested-With': 'XMLHttpRequest', Accept: 'application/json'}});
                const data = await responseJSON(response);
                if (!response.ok || !data.ok) throw new Error(errorText(data, 'The image could not be uploaded.'));
                item = {...data, caption, url: data.url || data.public_url};
                media.set(String(item.id), item);
                const option = document.createElement('option'); option.value = item.id; option.textContent = `${item.alt_text || 'Image'} · ${item.width} × ${item.height}`;
                document.getElementById('blog-existing-image').append(option);
            }
            // A modal makes the editor inert. Close it before restoring the
            // original selection and invoking the browser's editing command.
            document.getElementById('blog-image-dialog').close();
            if (imageTarget !== 'body') {
                fields(`${imageTarget}_image`).value = item.id;
                markChanged();
            } else {
                const figure = document.createElement('figure');
                const img = document.createElement('img');
                img.src = item.preview_url; img.alt = alt;
                img.setAttribute('src', item.preview_url);
                if (item.width) img.width = item.width;
                if (item.height) img.height = item.height;
                figure.append(img);
                if (caption) { const text = document.createElement('figcaption'); text.textContent = caption; figure.append(text); }
                if (editingImage && body.contains(editingImage)) {
                    const old = editingImage.closest('figure') || editingImage;
                    const range = document.createRange(); range.selectNode(old); savedRange = range;
                    insertHTML(figure.outerHTML);
                } else insertHTML(`${figure.outerHTML}<p><br></p>`);
            }
        } catch (failure) { error.textContent = failure.message; }
        finally {
            uploadInProgress = false;
            imageDialog.removeAttribute('aria-busy');
            imageDialog.querySelectorAll('input,select,button').forEach(control => { control.disabled = false; });
            setImageMetadataState();
            button.textContent = originalText;
        }
    });

    document.getElementById('blog-remove-image').addEventListener('click', () => {
        document.getElementById('blog-image-dialog').close();
        if (imageTarget !== 'body') fields(`${imageTarget}_image`).value = '';
        else if (editingImage && body.contains(editingImage)) {
            const range = document.createRange(); range.selectNode(editingImage.closest('figure') || editingImage); savedRange = range;
            restoreSelection(); document.execCommand('delete');
        }
        markChanged();
    });

    document.getElementById('blog-link-button').addEventListener('click', () => {
        rememberSelection();
        const selection = window.getSelection();
        const anchor = selection.anchorNode?.nodeType === Node.ELEMENT_NODE ? selection.anchorNode : selection.anchorNode?.parentElement;
        editingLink = anchor?.closest('a');
        if (editingLink && !body.contains(editingLink)) editingLink = null;
        document.getElementById('blog-link-text').value = editingLink?.textContent || selection.toString();
        document.getElementById('blog-link-url').value = editingLink?.getAttribute('href') || '';
        document.getElementById('blog-link-error').textContent = '';
        document.getElementById('blog-unlink').hidden = !editingLink;
        openDialog(document.getElementById('blog-link-dialog'));
    });
    document.getElementById('blog-insert-link').addEventListener('click', () => {
        const href = safeHref(document.getElementById('blog-link-url').value);
        const text = document.getElementById('blog-link-text').value.trim();
        if (!href || !text) { document.getElementById('blog-link-error').textContent = 'Add link text and a valid HTTP/HTTPS URL, site path, or section anchor.'; return; }
        const anchor = document.createElement('a'); anchor.href = href; anchor.textContent = text; anchor.rel = 'noopener noreferrer';
        if (editingLink && body.contains(editingLink)) { const range = document.createRange(); range.selectNode(editingLink); savedRange = range; }
        document.getElementById('blog-link-dialog').close();
        insertHTML(anchor.outerHTML);
    });
    document.getElementById('blog-unlink').addEventListener('click', () => {
        document.getElementById('blog-link-dialog').close();
        if (editingLink && body.contains(editingLink)) {
            const range = document.createRange(); range.selectNodeContents(editingLink); savedRange = range;
            restoreSelection(); document.execCommand('unlink'); markChanged();
        }
    });
    document.getElementById('blog-table-button').addEventListener('click', () => openDialog(document.getElementById('blog-table-dialog')));
    document.getElementById('blog-insert-table').addEventListener('click', () => {
        const rowInput = document.getElementById('blog-table-rows');
        const colInput = document.getElementById('blog-table-columns');
        if (!rowInput.reportValidity() || !colInput.reportValidity()) return;
        const rows = Math.min(20, Math.max(1, Number(rowInput.value) || 3));
        const columns = Math.min(8, Math.max(1, Number(colInput.value) || 2));
        let html = '<table><thead><tr>' + '<th>Heading</th>'.repeat(columns) + '</tr></thead><tbody>';
        for (let row = 0; row < rows; row++) html += '<tr>' + '<td>Text</td>'.repeat(columns) + '</tr>';
        html += '</tbody></table><p><br></p>';
        document.getElementById('blog-table-dialog').close(); insertHTML(html);
    });

    form.addEventListener('input', event => {
        if (event.target === fields('slug')) slugManuallyEdited = Boolean(event.target.value);
        if (event.target === fields('title') && !slugManuallyEdited) {
            fields('slug').value = value('title').normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase().replace(/[^a-z0-9\s-]/g, '').trim().replace(/[-\s]+/g, '-').slice(0, 180);
        }
        markChanged();
    });
    form.addEventListener('change', event => {
        if (event.target.matches('select,input[type=checkbox],input[type=datetime-local]')) markChanged();
    });
    window.addEventListener('beforeunload', event => {
        if (!leaving && (changeNumber !== savedNumber || savePromise || uploadInProgress)) { event.preventDefault(); event.returnValue = ''; }
    });
    document.addEventListener('keydown', event => {
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
            event.preventDefault(); if (!actionInProgress) saveDraft(true).catch(() => {});
        }
    });

    body.innerHTML = cleanHTML(bodyInput.value, true);
    document.execCommand('defaultParagraphSeparator', false, 'p');
    document.execCommand('styleWithCSS', false, false);
    document.getElementById('blog-rich-editor').hidden = false;
    document.getElementById('blog-html-fallback').hidden = true;
    fields('category').setAttribute('list', 'blog-categories');
    fields('title').setAttribute('placeholder', 'Give your article a clear title');
    fields('slug').setAttribute('placeholder', 'your-article-slug');
    fields('author_name').setAttribute('autocomplete', 'name');
    bodyInput.required = false;
    updatePreviews();
})();
