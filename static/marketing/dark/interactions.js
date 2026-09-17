(() => {
  const motion = document.querySelector('.platform .quiet');
  const cards = [...document.querySelectorAll('.demo-card')];
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  let paused = reduced.matches, tick = 0;
  function draw() {
    cards.forEach(card => card.querySelectorAll('.demo-step').forEach((step, i) => {
      step.classList.toggle('current', i === tick);
      step.querySelector('span').textContent = i < tick ? '✓' : String(i + 1).padStart(2, '0');
      step.querySelector('i').textContent = i === tick ? '•••' : i < tick ? 'done' : '';
    }));
    if (motion) { motion.textContent = paused ? 'Resume motion ▶' : 'Pause motion Ⅱ'; motion.setAttribute('aria-pressed', String(paused)); }
  }
  motion?.addEventListener('click', () => { paused = !paused; draw(); });
  reduced.addEventListener('change', () => { paused = reduced.matches; draw(); });
  if (cards.length) setInterval(() => { if (!paused && !document.hidden) { tick = (tick + 1) % 4; draw(); } }, 2600);
  draw();
  const tabs = [...document.querySelectorAll('.split .tabs button')];
  if (tabs.length) fetch('/static/marketing/dark/conversations.json').then(r => { if (!r.ok) throw Error('Unavailable'); return r.json(); }).then(conversations => {
    tabs.forEach((button, index) => { button.setAttribute('aria-pressed', String(index === 0)); button.addEventListener('click', () => {
      tabs.forEach((tab, i) => { tab.classList.toggle('active', i === index); tab.setAttribute('aria-pressed', String(i === index)); });
      const c = conversations[index], section = button.closest('.split');
      section.querySelector('h2').textContent = c.title;
      section.querySelector('.body-copy').textContent = c.desc;
      const body = section.querySelector('.chat-body');
      body.replaceChildren();
      const day = document.createElement('span'); day.className = 'chat-day'; day.textContent = 'ILLUSTRATIVE CONVERSATION'; body.append(day);
      c.messages.forEach((message, i) => {
        const bubble = document.createElement('div'); bubble.className = 'bubble ' + (i % 2 === 0 ? 'out' : 'in'); bubble.style.animationDelay = `${i * .22}s`; bubble.textContent = message;
        const time = document.createElement('small'); time.textContent = `12:${10 + i} ${i % 2 === 0 ? '✓✓' : ''}`; bubble.append(time); body.append(bubble);
      });
      section.querySelector('.chat-result').textContent = '↗ ' + c.result;
    }); });
  }).catch(() => { tabs.forEach(button => { button.disabled = true; }); });
  const dialog = document.getElementById('intro-video'), opener = document.querySelector('.announcement');
  if (dialog && opener) {
    const video = dialog.querySelector('video');
    opener.addEventListener('click', () => { dialog.showModal(); video.play().catch(() => {}); });
    dialog.querySelector('.close').addEventListener('click', () => dialog.close());
    dialog.addEventListener('click', e => { if (e.target === dialog) dialog.close(); });
    dialog.addEventListener('close', () => { video.pause(); opener.focus(); });
  }
})();
