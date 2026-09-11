from apps.core.middleware import TOAST_ASSET


def test_global_toast_uses_premium_glass_card_treatment():
    assert 'backdrop-filter: blur(24px) saturate(180%)' in TOAST_ASSET
    assert 'border-radius: 1.35rem' in TOAST_ASSET
    assert 'font-family: -apple-system' in TOAST_ASSET
    assert 'prefers-reduced-motion: reduce' in TOAST_ASSET


def test_success_toasts_are_allowlisted_to_major_changes():
    assert 'function isMajorMessage(message)' in TOAST_ASSET
    assert 'function majorMutationFor(input, method)' in TOAST_ASSET
    assert "if ((type === 'success' || type === 'info')" in TOAST_ASSET
    assert "return {message: 'Workflow added.'" in TOAST_ASSET
    assert "return {message: 'Reminder removed.'" in TOAST_ASSET
    assert "return {message: 'Attribute updated.'" in TOAST_ASSET
    assert "text === 'ai settings saved successfully.'" in TOAST_ASSET
    assert "text === 'file uploaded. processing has started.'" in TOAST_ASSET


def test_ai_sandbox_requests_never_show_global_toasts():
    assert 'function isAiSandboxUrl(input)' in TOAST_ASSET
    assert '/\\/playground(?:\\/|$)/' in TOAST_ASSET
    assert "if (isAiSandboxUrl(options.sourceUrl || options.url || '')) return null;" in TOAST_ASSET


def test_generic_mutations_no_longer_auto_emit_success_toasts():
    assert 'Changes saved successfully.' not in TOAST_ASSET
    assert "window.addEventListener('error'" not in TOAST_ASSET
    assert "window.addEventListener('unhandledrejection'" not in TOAST_ASSET
