(function () {
    'use strict';

    if ((window.location.pathname || '').indexOf('/dashboard/playbooks/') !== 0) return;

    function applyAIBrainLabel(scope) {
        scope = scope || document;
        var walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT, {
            acceptNode: function (node) {
                var parent = node.parentElement;
                if (!parent || /^(SCRIPT|STYLE|TEXTAREA|OPTION)$/i.test(parent.tagName)) {
                    return NodeFilter.FILTER_REJECT;
                }
                return NodeFilter.FILTER_ACCEPT;
            }
        });
        var nodes = [];
        var current;
        while ((current = walker.nextNode())) nodes.push(current);
        nodes.forEach(function (node) {
            if (node.nodeValue && node.nodeValue.indexOf('AI Setup') !== -1) {
                node.nodeValue = node.nodeValue.split('AI Setup').join('AI Brain');
            }
        });
    }

    function run() { applyAIBrainLabel(document); }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run, { once: true });
    else run();

    document.addEventListener('htmx:afterSwap', run);
})();
