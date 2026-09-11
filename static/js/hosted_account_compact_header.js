(function () {
    'use strict';

    var path = window.location.pathname || '';
    if (path.indexOf('/dashboard/whatsapp/connect/hosted/') !== 0) return;

    function apply() {
        var isChat = path.indexOf('/chats/') !== -1;
        var shellTitle = document.querySelector('#app-sidebar + div > header > h1');
        var hero = document.querySelector('#app-sidebar + div > main > .shvya-page-hero');

        if (shellTitle) shellTitle.textContent = isChat ? 'Chats' : 'Hosted Account';
        if (!hero) return;

        if (isChat) {
            hero.hidden = true;
            hero.style.display = 'none';
            return;
        }

        var eyebrow = hero.querySelector('.shvya-page-hero__eyebrow');
        var title = hero.querySelector('h1');
        var description = hero.querySelector('p');
        if (eyebrow) eyebrow.textContent = 'WhatsApp';
        if (title) title.textContent = 'Hosted Account';
        if (description) description.textContent = 'Chats and automation.';
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', apply, { once: true });
    } else {
        apply();
    }
})();
