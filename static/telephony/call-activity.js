(function () {
  'use strict';
  const rail = document.querySelector('.ci-outcome-stages');
  const scrollButtons = document.querySelectorAll('[data-ci-scroll]');
  if (rail) {
    function updateScrollButtons() {
      scrollButtons.forEach(button => {
        button.disabled = Number(button.dataset.ciScroll) < 0
          ? rail.scrollLeft < 2 : rail.scrollLeft + rail.clientWidth >= rail.scrollWidth - 2;
      });
    }
    scrollButtons.forEach(button => button.addEventListener('click', () => {
      rail.scrollBy({left: Number(button.dataset.ciScroll) * rail.clientWidth * .7,
        behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'});
    }));
    rail.addEventListener('scroll', updateScrollButtons, {passive: true});
    window.addEventListener('resize', updateScrollButtons);
    const selected = rail.querySelector('[aria-current="page"]');
    if (selected) rail.scrollLeft = Math.max(0, selected.offsetLeft - 12);
    updateScrollButtons();
  }
  const openId = new URLSearchParams(window.location.search).get('open');
  if (openId) {
    const item = document.getElementById('call-' + openId);
    if (item) { item.open = true; item.scrollIntoView({block: 'center'}); }
  }
  const dialog = document.getElementById('ci-create-lead-dialog');
  const form = document.getElementById('ci-create-lead-form');
  const catalog = document.getElementById('ci-lead-destinations');
  if (!dialog || !form || !catalog) return;
  const destinations = JSON.parse(catalog.textContent).filter(pipeline => pipeline.stages.length);
  const pipelineInput = form.elements.pipeline;
  const stageInput = form.elements.stage;
  const error = form.querySelector('.ci-lead-error');
  const submit = form.querySelector('[type="submit"]');
  destinations.forEach(pipeline => pipelineInput.add(new Option(pipeline.name, pipeline.id)));
  function updateStages() {
    const pipeline = destinations.find(item => item.id === pipelineInput.value);
    stageInput.replaceChildren(new Option(pipeline ? 'Select stage' : 'Select pipeline first', ''));
    stageInput.disabled = !pipeline;
    if (pipeline) pipeline.stages.forEach(stage => stageInput.add(new Option(stage.name, stage.id)));
  }
  pipelineInput.addEventListener('change', updateStages);
  document.querySelectorAll('[data-ci-create-lead]').forEach(button => {
    button.addEventListener('click', () => {
      form.reset();
      form.action = button.dataset.url;
      form.elements.name.value = button.dataset.name || '';
      form.elements.phone.value = button.dataset.phone;
      const preferred = destinations.find(pipeline => pipeline.owned) || (destinations.length === 1 ? destinations[0] : null);
      pipelineInput.value = preferred ? preferred.id : '';
      error.textContent = '';
      updateStages();
      dialog.showModal();
      form.elements.name.focus();
    });
  });
  document.querySelectorAll('[data-ci-close-lead]').forEach(button => button.addEventListener('click', () => dialog.close()));
  dialog.addEventListener('click', event => { if (event.target === dialog) {
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
  }});
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (submit.disabled) return;
    submit.disabled = true;
    error.textContent = '';
    try {
      const response = await fetch(form.action, {method: 'POST', body: new FormData(form),
        headers: {'X-Requested-With': 'XMLHttpRequest'}});
      if (!(response.headers.get('Content-Type') || '').includes('application/json'))
        throw new Error('Your session may have expired. Reload the page and try again.');
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || 'Could not create this lead.');
      window.location.assign(data.redirect_url);
    } catch (exception) {
      error.textContent = exception.message;
    } finally {
      submit.disabled = false;
    }
  });
})();
