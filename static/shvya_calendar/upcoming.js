(() => {
  'use strict';
  async function json(response) {
    if (response.redirected) throw new Error('Refresh and sign in again.');
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Unable to update this booking.');
    return data;
  }
  document.querySelectorAll('[data-booking-details]').forEach(row => {
    let generation = 0;
    row.querySelectorAll('[data-booking-action]').forEach(button => button.addEventListener('click', async () => {
      const id = ++generation, status = row.querySelector('[data-booking-status]'), panel = row.querySelector('[data-booking-action-panel]');
      panel.hidden = true; panel.replaceChildren(); status.textContent = 'Loading…';
      try {
        const response = await fetch(row.dataset.detailUrl);
        if (!response.ok || response.redirected) throw new Error('Unable to load this booking. Refresh and try again.');
        const source = document.createElement('div'); source.innerHTML = await response.text();
        if (id !== generation) return;
        const form = [...source.querySelectorAll('.cw-action-form')].find(f => f.elements.action.value === button.dataset.bookingAction);
        if (!form) throw new Error('This action is no longer available. Refresh the page.');
        panel.append(form); panel.hidden = false; status.textContent = '';
        const pipeline = form.elements.pipeline, stage = form.elements.stage;
        if (pipeline) pipeline.addEventListener('change', () => {
          stage.value = ''; stage.disabled = !pipeline.value;
          [...stage.options].forEach(option => { if (option.dataset.pipeline) { option.hidden = option.dataset.pipeline !== pipeline.value; option.disabled = option.hidden; } });
        });
        const day = form.elements.slot_date, slot = form.elements.slot_start, submit = form.querySelector('[type=submit]');
        if (day) {
          let slotGeneration = 0;
          function reset() { slot.replaceChildren(new Option('Choose an available time', '')); slot.disabled = true; submit.disabled = true; }
          day.addEventListener('change', () => { slotGeneration++; reset(); });
          form.querySelector('[data-load-slots]').addEventListener('click', async event => {
            const attempt = ++slotGeneration, control = event.currentTarget, message = form.querySelector('.cw-slot-status');
            reset(); control.disabled = true; message.textContent = 'Checking availability…';
            try {
              const url = new URL(day.dataset.slotsUrl, location.origin); url.searchParams.set('date', day.value);
              const data = await json(await fetch(url)); if (attempt !== slotGeneration) return;
              data.slots.forEach(item => slot.add(new Option(item.label, item.value))); slot.disabled = !data.slots.length;
              message.textContent = data.slots.length ? `Times in ${data.timezone}` : 'No times available. Choose another date.';
            } catch (error) { if (attempt === slotGeneration) message.textContent = error.message; }
            finally { control.disabled = false; }
          });
          slot.addEventListener('change', () => { submit.disabled = !slot.value; });
        }
        form.addEventListener('submit', async event => {
          event.preventDefault();
          if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) return;
          submit.disabled = true; status.textContent = 'Saving…';
          try {
            await json(await fetch(form.getAttribute('action'), {method:'POST', body:new FormData(form)}));
            location.reload();
          } catch (error) { status.textContent = error.message; submit.disabled = false; }
        });
      } catch (error) { if (id === generation) status.textContent = error.message; }
    }));
  });
})();
