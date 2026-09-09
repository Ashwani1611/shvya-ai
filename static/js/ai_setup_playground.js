(function () {
    "use strict";

    const PLAYGROUND_ENDPOINT = "/api/v1/ai-engagement/playground/";
    const SEND_SELECTOR = "#playground-send-message";
    const RESTART_SELECTOR = "#playground-restart-chat";
    const INPUT_SELECTOR = "#playground-message-input";
    const MESSAGES_SELECTOR = "#playground-messages";
    const PAGE_SELECTOR = "#ai-setup-page";
    const MAX_HISTORY_MESSAGES = 40;

    let history = [];
    let busy = false;
    let hasStarted = false;

    function getPage() {
        return document.querySelector(PAGE_SELECTOR);
    }

    function getMessages() {
        return document.querySelector(MESSAGES_SELECTOR);
    }

    function getInput() {
        return document.querySelector(INPUT_SELECTOR);
    }

    function getSendButton() {
        return document.querySelector(SEND_SELECTOR);
    }

    function createSessionId() {
        if (
            window.crypto &&
            typeof window.crypto.randomUUID === "function"
        ) {
            return window.crypto.randomUUID();
        }

        return (
            "playground-" +
            Date.now().toString(36) +
            "-" +
            Math.random().toString(36).slice(2)
        );
    }

    function ensureSessionId() {
        const page = getPage();

        if (!page) {
            return "";
        }

        if (!page.dataset.playgroundSessionId) {
            page.dataset.playgroundSessionId = createSessionId();
        }

        return page.dataset.playgroundSessionId;
    }

    function getCsrfToken() {
        const page = getPage() || document;
        const input = page.querySelector('input[name="csrfmiddlewaretoken"]');

        if (input && input.value) {
            return input.value;
        }

        const cookie = document.cookie
            .split(";")
            .map(function (part) {
                return part.trim();
            })
            .find(function (part) {
                return part.startsWith("csrftoken=");
            });

        return cookie
            ? decodeURIComponent(cookie.slice("csrftoken=".length))
            : "";
    }

    function scrollToBottom() {
        const messages = getMessages();

        if (messages) {
            messages.scrollTop = messages.scrollHeight;
        }
    }

    function clearSampleMessages() {
        if (hasStarted) {
            return;
        }

        const messages = getMessages();

        if (messages) {
            messages.innerHTML = "";
        }

        hasStarted = true;
    }

    function appendMessage(role, text, options) {
        const messages = getMessages();

        if (!messages) {
            return null;
        }

        const wrapper = document.createElement("div");
        const bubble = document.createElement("div");
        const isUser = role === "user";
        const isError = Boolean(options && options.error);
        const isPending = Boolean(options && options.pending);

        wrapper.className = isUser ? "flex justify-end" : "flex justify-start";

        if (isUser) {
            bubble.className = [
                "max-w-[85%]",
                "rounded-2xl",
                "rounded-tr-md",
                "bg-blue-600",
                "px-3",
                "py-2.5",
                "text-sm",
                "text-white",
                "break-words",
            ].join(" ");
        } else if (isError) {
            bubble.className = [
                "max-w-[85%]",
                "rounded-2xl",
                "rounded-tl-md",
                "border",
                "border-red-200",
                "bg-red-50",
                "px-3",
                "py-2.5",
                "text-sm",
                "text-red-700",
                "break-words",
            ].join(" ");
        } else {
            bubble.className = [
                "max-w-[85%]",
                "rounded-2xl",
                "rounded-tl-md",
                "border",
                "border-gray-200",
                "bg-white",
                "px-3",
                "py-2.5",
                "text-sm",
                "text-gray-700",
                "break-words",
            ].join(" ");
        }

        if (isPending) {
            wrapper.dataset.playgroundPending = "true";
            bubble.innerHTML =
                '<span class="inline-flex items-center gap-2">' +
                '<i class="ti ti-loader-2 animate-spin"></i>' +
                "Thinking..." +
                "</span>";
        } else {
            bubble.textContent = text;
        }

        wrapper.appendChild(bubble);
        messages.appendChild(wrapper);
        scrollToBottom();

        return wrapper;
    }

    function removePendingMessage() {
        const pending = document.querySelector(
            MESSAGES_SELECTOR + ' [data-playground-pending="true"]'
        );

        if (pending) {
            pending.remove();
        }
    }

    function setBusy(value) {
        busy = value;

        const input = getInput();
        const button = getSendButton();

        if (input) {
            input.disabled = value;
        }

        if (button) {
            button.disabled = value;
            button.classList.toggle("opacity-60", value);
            button.classList.toggle("cursor-not-allowed", value);
        }
    }

    async function getErrorMessage(response) {
        try {
            const payload = await response.json();
            return (
                payload.error ||
                payload.detail ||
                payload.message ||
                "Unable to generate a playground response."
            );
        } catch (error) {
            if (response.status === 403) {
                return "Your session could not be verified. Refresh the page and try again.";
            }

            return "Unable to generate a playground response.";
        }
    }

    async function sendMessage() {
        const input = getInput();

        if (!input || busy) {
            return;
        }

        const messageText = input.value.trim();

        if (!messageText) {
            input.focus();
            return;
        }

        const sessionId = ensureSessionId();
        const csrfToken = getCsrfToken();

        clearSampleMessages();
        appendMessage("user", messageText);
        input.value = "";

        if (!sessionId || !csrfToken) {
            appendMessage(
                "assistant",
                "Unable to verify the playground request. Refresh the page and try again.",
                { error: true }
            );
            input.focus();
            return;
        }

        setBusy(true);
        appendMessage("assistant", "", { pending: true });

        try {
            const response = await fetch(PLAYGROUND_ENDPOINT, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": csrfToken,
                    "X-Requested-With": "XMLHttpRequest",
                },
                body: JSON.stringify({
                    session_id: sessionId,
                    message: messageText,
                    history: history.slice(-MAX_HISTORY_MESSAGES),
                }),
            });

            if (!response.ok) {
                throw new Error(await getErrorMessage(response));
            }

            const payload = await response.json();
            const responseText = String(payload.response || "").trim();
            const assistantText = responseText ||
                "AI is configured not to respond to this message.";

            removePendingMessage();
            appendMessage("assistant", assistantText);

            history.push({
                role: "user",
                content: messageText,
            });
            history.push({
                role: "assistant",
                content: assistantText,
            });
            history = history.slice(-MAX_HISTORY_MESSAGES);
        } catch (error) {
            removePendingMessage();
            appendMessage(
                "assistant",
                error.message || "Unable to generate a playground response.",
                { error: true }
            );
        } finally {
            setBusy(false);
            input.focus();
        }
    }

    function restartChat() {
        const page = getPage();
        const messages = getMessages();
        const input = getInput();

        history = [];
        hasStarted = true;

        if (page) {
            page.dataset.playgroundSessionId = createSessionId();
        }

        if (messages) {
            messages.innerHTML = "";
        }

        if (input) {
            input.value = "";
            input.focus();
        }
    }

    document.addEventListener(
        "click",
        function (event) {
            const sendButton = event.target.closest(SEND_SELECTOR);

            if (sendButton) {
                event.preventDefault();
                event.stopImmediatePropagation();
                sendMessage();
                return;
            }

            const restartButton = event.target.closest(RESTART_SELECTOR);

            if (restartButton) {
                event.preventDefault();
                event.stopImmediatePropagation();
                restartChat();
            }
        },
        true
    );

    document.addEventListener(
        "keydown",
        function (event) {
            const input = event.target.closest
                ? event.target.closest(INPUT_SELECTOR)
                : null;

            if (
                !input ||
                event.key !== "Enter" ||
                event.shiftKey
            ) {
                return;
            }

            event.preventDefault();
            event.stopImmediatePropagation();
            sendMessage();
        },
        true
    );
})();
