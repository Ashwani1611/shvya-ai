(() => {
  'use strict';
  const root = document.getElementById('calendar-workspace');
  if (!root) return;
  const $ = id => document.getElementById(id);
  const zone = root.dataset.timezone;
  const today = root.dataset.today;
  let selected = today, view = 'day', events = [], requestId = 0, detailId = 0;
  let controller;
  const date = key => new Date(`${key}T12:00:00Z`);
  const key = d => d.toISOString().slice(0, 10);
  const add = (d, n) => { const value = date(d); value.setUTCDate(value.getUTCDate() + n); return key(value); };
  const format = (d, options) => date(d).toLocaleDateString('en-US', { timeZone: 'UTC', ...options });
  const weekStart = d => add(d, -((date(d).getUTCDay() + 6) % 7));
  const localParts = instant => Object.fromEntries(new Intl.DateTimeFormat('en-CA', { timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(new Date(instant)).map(p => [p.type, p.value]));
  const localKey = instant => { const p = localParts(instant); return `${p.year}-${p.month}-${p.day}`; };
  const minutes = instant => { const p = localParts(instant); return Number(p.hour) * 60 + Number(p.minute); };
  const timeLabel = instant => new Date(instant).toLocaleTimeString('en-US', { timeZone: zone, hour: 'numeric', minute: '2-digit' });
  const el = (tag, cls, text) => { const node = document.createElement(tag); if (cls) node.className = cls; if (text !== undefined) node.textContent = text; return node; };
  function range() {
    if (view === 'day') return { start: selected, days: 1 };
    if (view === 'week') return { start: weekStart(selected), days: 7 };
    return { start: weekStart(`${selected.slice(0, 7)}-01`), days: 42 };
  }
  function color(id) {
    let hash = 0; for (const c of id) hash = (hash * 31 + c.charCodeAt(0)) | 0;
    return [195, 260, 145, 30, 335][Math.abs(hash) % 5];
  }
  function eventButton(event) {
    const button = el('button', 'cw-event'); button.type = 'button';
    button.dataset.status = event.status;
    const hue = color(event.pipeline_id);
    button.style.setProperty('--event-color', `hsl(${hue} 35% 55%)`);
    button.style.setProperty('--event-bg', `hsl(${hue} 35% 92%)`);
    button.append(el('strong', '', event.title), el('small', '', `${timeLabel(event.start)} – ${timeLabel(event.end)} · ${event.pipeline}`));
    button.setAttribute('aria-label', `${event.title}, ${format(localKey(event.start), {month: 'short', day: 'numeric'})}, ${timeLabel(event.start)} to ${timeLabel(event.end)}, ${event.pipeline}, ${event.status}`);
    button.title = button.getAttribute('aria-label');
    button.addEventListener('click', () => openDetail(event.detail_url));
    return button;
  }
  function dayEvents(day) {
    return events.filter(e => localKey(e.start) <= day && (localKey(e.end) > day || (localKey(e.end) === day && minutes(e.end) > 0)));
  }
  function render() {
    $('cw-period').textContent = format(selected, { month: 'long', year: 'numeric' });
    $('cw-date').value = selected;
    $('cw-date-title').textContent = view === 'day' ? format(selected, { weekday: 'long', day: 'numeric', month: 'long' }) : (view === 'week' ? 'Your week at a glance' : 'Your month at a glance');
    $('cw-count').textContent = `${events.length} booking${events.length === 1 ? '' : 's'}`;
    root.querySelectorAll('[data-view]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.view === view)));
    const strip = $('cw-week-strip'); strip.replaceChildren(); strip.hidden = view === 'month';
    if (view !== 'month') for (let i = 0; i < 7; i++) {
      const day = add(weekStart(selected), i), button = el('button', day === today ? 'cw-is-today' : '');
      button.type = 'button'; button.setAttribute('aria-pressed', String(day === selected));
      button.setAttribute('aria-label', format(day, {weekday:'long', month:'long', day:'numeric'}));
      button.append(el('span', '', format(day, { weekday: 'short' })), el('strong', '', date(day).getUTCDate()));
      button.addEventListener('click', () => { selected = day; load(); }); strip.append(button);
    }
    const grid = $('cw-grid'); grid.replaceChildren();
    const { start, days } = range();
    if (view === 'month') {
      const month = el('div', 'cw-month');
      ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].forEach(d => month.append(el('div', 'cw-month-weekday', d)));
      for (let i = 0; i < days; i++) {
        const day = add(start, i), cell = el('div', `cw-month-cell${day.slice(0,7) !== selected.slice(0,7) ? ' cw-outside' : ''}`);
        const button = el('button', `cw-month-date${day === today ? ' cw-is-today' : ''}`, date(day).getUTCDate());
        button.setAttribute('aria-label', `Show ${format(day, {month:'long', day:'numeric'})}`);
        button.addEventListener('click', () => { selected = day; view = 'day'; load(); }); cell.append(button);
        dayEvents(day).forEach(e => cell.append(eventButton(e))); month.append(cell);
      }
      grid.append(month); grid.scrollTop = 0; return;
    }
    grid.style.setProperty('--days', days); grid.style.setProperty('--min-width', days === 7 ? '850px' : '0px');
    const header = el('div', 'cw-time-header'), body = el('div', 'cw-time-body'), hours = el('div', 'cw-hours');
    header.append(el('div', '', 'Time'));
    for (let h = 0; h < 24; h++) { const label = el('span', '', `${String(h).padStart(2,'0')}:00`); label.style.top = `${h * 60 + 4}px`; hours.append(label); }
    body.append(hours);
    for (let i = 0; i < days; i++) {
      const day = add(start, i), column = el('div', 'cw-day-column');
      header.append(el('div', '', format(day, { weekday:'short', day:'numeric' })));
      const items = dayEvents(day).map(event => ({event, start: localKey(event.start) < day ? 0 : minutes(event.start), end: localKey(event.end) > day ? 1440 : minutes(event.end)})).sort((a,b) => a.start - b.start || b.end - a.end);
      // Overlap groups get separate columns, including short event hit areas.
      let group = [], groupEnd = -1;
      function drawGroup() {
        const lanes = [];
        for (const item of group) {
          let lane = lanes.findIndex(end => end <= item.start);
          if (lane < 0) lane = lanes.length;
          lanes[lane] = Math.max(item.end, item.start + 18); item.lane = lane;
        }
        for (const item of group) {
          const button = eventButton(item.event);
          button.style.top = `${item.start}px`; button.style.height = `${Math.max(18, item.end - item.start - 2)}px`;
          button.style.left = `calc(${item.lane * 100 / lanes.length}% + 3px)`;
          button.style.width = `calc(${100 / lanes.length}% - 6px)`; column.append(button);
        }
      }
      for (const item of items) {
        if (item.start >= groupEnd && group.length) { drawGroup(); group = []; groupEnd = -1; }
        group.push(item); groupEnd = Math.max(groupEnd, item.end, item.start + 18);
      }
      drawGroup();
      if (day === localKey(new Date())) { const now = el('div', 'cw-now'); now.style.top = `${minutes(new Date())}px`; column.append(now); }
      body.append(column);
    }
    grid.append(header, body);
    const visible = events.filter(e => localKey(e.start) >= start && localKey(e.start) < add(start, days));
    grid.scrollTop = Math.max(0, (visible.length ? Math.min(...visible.map(e => minutes(e.start))) : 540) - 50);
  }
  async function jsonResponse(response) {
    if (response.redirected) throw new Error('Your session may have expired. Refresh the page and sign in again.');
    const data = await response.json().catch(() => { throw new Error('Unable to load the calendar. Please refresh and try again.'); });
    if (!response.ok) throw new Error(data.error || 'Unable to complete this request. Please try again.');
    return data;
  }
  async function load() {
    const id = ++requestId;
    if (controller) controller.abort(); controller = new AbortController();
    $('cw-message').textContent = 'Loading bookings…'; $('cw-grid').setAttribute('aria-busy','true');
    events = []; render();
    const { start, days } = range(); const all = []; let offset = 0;
    try {
      do {
        const url = new URL(root.dataset.eventsUrl, location.origin);
        url.search = new URLSearchParams({ start, end: add(start, days), pipeline: $('cw-pipeline').value, offset });
        const data = await jsonResponse(await fetch(url, {signal: controller.signal, headers:{Accept:'application/json'}}));
        if (id !== requestId) return; all.push(...data.events); offset = data.next_offset;
      } while (offset !== null);
      events = all; render(); $('cw-message').textContent = events.length ? '' : 'No bookings in this period. Choose another date or pipeline.';
    } catch (error) { if (id === requestId && error.name !== 'AbortError') $('cw-message').textContent = error.message; }
    finally { if (id === requestId) $('cw-grid').removeAttribute('aria-busy'); }
  }
  async function openDetail(url) {
    const id = ++detailId, dialog = $('cw-detail'), content = $('cw-detail-body');
    content.textContent = 'Loading booking…'; if (!dialog.open) dialog.showModal();
    try {
      const response = await fetch(url);
      if (response.redirected || !response.ok) throw new Error('This booking is unavailable or your session has expired. Refresh and try again.');
      const html = await response.text(); if (id !== detailId || !dialog.open) return;
      content.innerHTML = html; wireDetail(url);
    } catch (error) { if (id === detailId) content.textContent = error.message; }
  }
  function wireDetail(url) {
    const content = $('cw-detail-body');
    content.querySelectorAll('[data-copy-link]').forEach(button => button.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(button.dataset.copyLink); button.textContent = 'Copied'; }
      catch { button.textContent = 'Select and copy the link above'; }
    }));
    content.querySelectorAll('.cw-action-form').forEach(form => {
      const pipeline = form.elements.pipeline, stage = form.elements.stage;
      if (pipeline) pipeline.addEventListener('change', () => {
        stage.value = ''; stage.disabled = !pipeline.value;
        [...stage.options].forEach(option => { if (option.dataset.pipeline) { option.hidden = option.dataset.pipeline !== pipeline.value; option.disabled = option.hidden; } });
      });
      const dateInput = form.elements.slot_date;
      if (dateInput) {
        let slotGeneration = 0;
        const resetSlots = () => { form.elements.slot_start.replaceChildren(new Option('Choose an available time', '')); form.elements.slot_start.disabled = true; form.querySelector('[type=submit]').disabled = true; };
        dateInput.addEventListener('change', () => { slotGeneration++; resetSlots(); form.querySelector('.cw-slot-status').textContent = ''; });
        form.querySelector('[data-load-slots]').addEventListener('click', async () => {
          const generation = ++slotGeneration, button = form.querySelector('[data-load-slots]'), status = form.querySelector('.cw-slot-status');
          resetSlots(); button.disabled = true; status.textContent = 'Checking availability…';
          try {
            const endpoint = new URL(dateInput.dataset.slotsUrl, location.origin); endpoint.searchParams.set('date',dateInput.value);
            const data = await jsonResponse(await fetch(endpoint)); if (generation !== slotGeneration) return;
            data.slots.forEach(slot => form.elements.slot_start.add(new Option(slot.label, slot.value)));
            form.elements.slot_start.disabled = !data.slots.length;
            status.textContent = data.slots.length ? `Times in ${data.timezone}` : 'No available times. Try another date.';
          } catch (error) { if (generation === slotGeneration) status.textContent = error.message; }
          finally { button.disabled = false; }
        });
        form.elements.slot_start.addEventListener('change', () => { form.querySelector('[type=submit]').disabled = !form.elements.slot_start.value; });
      }
      form.addEventListener('submit', async event => {
        event.preventDefault(); const buttons = content.querySelectorAll('[type=submit]'); buttons.forEach(b => { b.disabled = true; });
        $('cw-action-message').textContent = 'Saving…';
        try {
          await jsonResponse(await fetch(form.getAttribute('action'), {method:'POST', body:new FormData(form), headers:{'X-Requested-With':'XMLHttpRequest'}}));
          await load(); await openDetail(url);
          const message = $('cw-action-message'); if (message) message.textContent = 'Booking updated.';
        } catch (error) { const message = $('cw-action-message'); if (message) message.textContent = error.message; buttons.forEach(b => { b.disabled = false; }); }
      });
    });
  }
  function navigate(direction) {
    if (view === 'month') { const d = date(`${selected.slice(0,7)}-01`); d.setUTCMonth(d.getUTCMonth() + direction); selected = key(d); }
    else selected = add(selected, direction * (view === 'week' ? 7 : 1));
    load();
  }
  $('cw-prev').addEventListener('click', () => navigate(-1)); $('cw-next').addEventListener('click', () => navigate(1));
  $('cw-today').addEventListener('click', () => { selected = localKey(new Date()); load(); });
  $('cw-date').addEventListener('change', event => { if (event.target.value && event.target.validity.valid && /^\d{4}-\d{2}-\d{2}$/.test(event.target.value)) { selected = event.target.value; load(); } });
  $('cw-pipeline').addEventListener('change', load);
  root.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => { view = button.dataset.view; load(); }));
  $('cw-close').addEventListener('click', () => $('cw-detail').close());
  $('cw-detail').addEventListener('close', () => { detailId++; });
  load();
})();
