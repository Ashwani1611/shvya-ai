"""One-shot, branch-only assembly; removed before the review commit."""
from pathlib import Path


def change(path, old, new, count=1):
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if text.count(old) != count:
        raise RuntimeError(f"Unexpected source in {path}: expected {count} matching anchors")
    p.write_text(text.replace(old, new), encoding="utf-8")


# Preserve both existing transports. Hosted sends must use their health guard,
# not be silently removed from the workflow action's supported senders.
for path in ("services/triggers/rules.py", "services/triggers/actions.py"):
    p = Path(path)
    text = p.read_text()
    text = text.replace("connection_type=WhatsAppAccount.ConnectionType.API,", "connection_type__in=[WhatsAppAccount.ConnectionType.API, WhatsAppAccount.ConnectionType.coexisted],")
    text = text.replace("Select a connected WhatsApp API account. For Hosted WhatsApp, use a Cadence sequence.", "Select a connected WhatsApp account from your organization.")
    p.write_text(text)

change("services/triggers/actions.py",
       'or message.account.connection_type != WhatsAppAccount.ConnectionType.API):',
       'or message.account.connection_type not in (WhatsAppAccount.ConnectionType.API, WhatsAppAccount.ConnectionType.coexisted)):')
change("services/triggers/actions.py",
       '    if not WhatsAppMessage.objects.filter(\n        organization_id=message.organization_id,',
       '    if message.account.connection_type == WhatsAppAccount.ConnectionType.API and not WhatsAppMessage.objects.filter(\n        organization_id=message.organization_id,')
change("services/triggers/actions.py",
       '        if not WhatsAppMessage.objects.filter(\n            organization=org,',
       '        if account.connection_type == WhatsAppAccount.ConnectionType.API and not WhatsAppMessage.objects.filter(\n            organization=org,')
change("services/triggers/actions.py", '"queued", "Queued through the WhatsApp API."', '"queued", "Queued through the selected WhatsApp account."')
# Mark the claim time with a lease rather than setting a completion timestamp
# before the connected mailbox has actually sent anything.
change("services/triggers/actions.py", '        status="sending"\n    ):', '        status="sending", due_at=timezone.now() + timedelta(minutes=10)\n    ):')
change("services/triggers/actions.py",
       '    run.finished_at = timezone.now()\n    run.save(update_fields=["status", "detail", "finished_at"])',
       '    # A deleted workflow must not be recreated by a late provider response.\n    TriggerRun.objects.filter(pk=run.pk, status="sending").update(\n        status=run.status, detail=run.detail, finished_at=timezone.now()\n    )')

# Workflow messages retain their delivery identity at every provider boundary.
change("services/channels/whatsapp_service.py",
       'for key in ("shvya_welcome", "shvya_auto_followup"):',
       'for key in ("shvya_welcome", "shvya_auto_followup", "shvya_workflow"):', 2)
change("services/channels/hosted_whatsapp_transport.py", '        "shvya_auto_followup",', '        "shvya_auto_followup",\n        "shvya_workflow",')
change("services/channels/hosted_health_guard.py", '    "shvya_welcome",', '    "shvya_welcome",\n    "shvya_workflow",')

# Reuse the canonical sender and its atomic message claim; do not install a new
# provider wrapper. This check runs on every task attempt, including retries.
change("apps/channels/tasks.py", '            message.status = _WHATSAPP_SENDING_STATUS\n', '''            if "shvya_workflow" in payload:
                from services.triggers.actions import workflow_message_block_reason

                reason = workflow_message_block_reason(message)
                if reason:
                    message.status = WhatsAppMessage.Status.FAILED
                    message.error = f"Workflow send cancelled: {reason}"
                    message.save(update_fields=["status", "error", "updated_at"])
                    return {"status": "skipped", "reason": reason, "message_id": str(message_id)}

            message.status = _WHATSAPP_SENDING_STATUS
''')
change("apps/channels/tasks.py", '''        original = exc.__cause__

        if isinstance(original, WhatsAppAPIError) and (''', '''        original = exc.__cause__

        if "shvya_workflow" in payload and isinstance(original, WhatsAppAPIError):
            from apps.triggers.models import TriggerRun

            # A timeout may follow a successful provider send. Never resend an
            # uncertain workflow automatically. Explicit 5xx responses retain
            # the canonical bounded retry path; exhausting it is terminal.
            uncertain = original.status_code is None
            exhausted = self.request.retries >= self.max_retries
            if uncertain or exhausted:
                _persist_whatsapp_message_failure(message_id=message_id, error=exc)
                TriggerRun.objects.filter(
                    message_id=message_id, status__in=["queued", "dispatching"]
                ).update(
                    status="needs_review" if uncertain else "failed",
                    detail=("Provider outcome is uncertain. Check delivery before retrying."
                            if uncertain else "WhatsApp retry limit reached."),
                    finished_at=timezone.now(),
                )
                return {"status": "needs_review" if uncertain else "failed", "message_id": str(message_id)}

        if isinstance(original, WhatsAppAPIError) and (''')

# Never turn imported chat history into fresh automation, and never emit an
# outbound clock for an in-memory status omitted from a partial save.
change("apps/triggers/signals.py", '''def message_event(sender, instance, created, raw=False, **kwargs):
    if (''', '''def message_event(sender, instance, created, raw=False, **kwargs):
    payload = instance.raw_payload if isinstance(instance.raw_payload, dict) else {}
    if raw or payload.get("isHistory") is True:
        return
    fields = kwargs.get("update_fields")
    status_written = created or fields is None or "status" in fields
    if (
        status_written
        and''')

# An interrupted email claim must stay at-most-once and visibly request review.
change("apps/triggers/tasks.py", '    _dispatch_messages()\n', '''    TriggerRun.objects.filter(status="sending", due_at__lte=timezone.now()).update(
        status="needs_review", finished_at=timezone.now(),
        detail="Email delivery was interrupted. Check the connected mailbox before retrying.",
    )
    _dispatch_messages()
''')

# Update the existing small isolated mailbox fixture for the newly checked
# tenant/cancellation contract without removing its delivery assertions.
change("apps/triggers/tests/test_connected_email_delivery.py", 'organization = SimpleNamespace(name="Acme")', 'organization = SimpleNamespace(id="org-1", name="Acme", is_active=True)')
change("apps/triggers/tests/test_connected_email_delivery.py", '            organization=organization,', '            organization=organization,\n            organization_id="org-1",', 2)
# The second occurrence is the expected send call, not the lead fixture.
change("apps/triggers/tests/test_connected_email_delivery.py", '            organization=organization,\n            organization_id="org-1",\n            to=', '            organization=organization,\n            to=')
change("apps/triggers/tests/test_connected_email_delivery.py", '                enabled=True,', '                enabled=True, organization_id="org-1",')
change("apps/triggers/tests/test_connected_email_delivery.py", '            id="run-1",', '            id="run-1", pk="run-1",\n            event=SimpleNamespace(organization_id="org-1", kind="lead_created"),')
change("apps/triggers/tests/test_connected_email_delivery.py", '        run.save.assert_called_once()', '''        objects.filter.assert_any_call(pk="run-1", status="sending")
        self.assertEqual(objects.filter.return_value.update.call_args.kwargs["status"], "completed")''')

# A real fixed source field: it is separate from tenant-defined attributes and
# cannot be deleted/renamed or populated with a custom SOURCE answer.
change("templates/triggers/dashboard.html", '<span class="st-eyebrow">AUTOMATION, CONNECTED</span>', '<span class="st-eyebrow st-module-label"><span aria-hidden="true"></span>AUTOMATION</span>')
change("templates/triggers/dashboard.html", 'The right action. At the right moment.', 'Connect triggers and actions that keep every lead moving.')
change("templates/triggers/dashboard.html", '<div id="st-conditions"></div>', '''<section class="st-source-condition" aria-labelledby="st-source-title"><div class="st-section-label"><h3 id="st-source-title">Source <span class="st-muted">Lead creation origin</span></h3><span class="st-muted">Built-in</span></div><p class="st-hint" id="st-source-help">Match the source recorded when the lead was created. Leave all unchecked for any source. This is not a custom CRM attribute.</p><div id="st-sources" aria-describedby="st-source-help"></div></section><div id="st-conditions"></div>''')
change("templates/triggers/dashboard.html", '?v=20260918-1', '?v=20260918-2', 2)
change("templates/triggers/dashboard.html", "triggers/dashboard.js' %}?v=1", "triggers/dashboard.js' %}?v=20260918-2")
change("apps/triggers/tests/test_workflows_presentation.py", '?v=20260918-1', '?v=20260918-2')

js = "static/triggers/dashboard.js"
change(js, 'return `<label for="${id}">${text}</label>${html}`;', 'return `<div class="st-field"><label for="${id}">${text}</label>${html}</div>`;')
change(js, "'PUT',{...rule,enabled:!rule.enabled}", "'PUT',{enabled:!rule.enabled}")
change(js, "renderTrigger();renderScopes();renderConditions();renderAction();summary();$('st-name').focus();", "renderTrigger();renderScopes();renderSources();renderConditions();renderAction();summary();$('st-name').focus();")
start = Path(js).read_text()
old = next(line for line in start.splitlines() if "if(kind==='sequence_ended')html=" in line)
change(js, old, '''    if(kind==='sequence_ended') {
      const selected=c.sequences?.length?c.sequences:[''];
      html=selected.map((id,i)=>`<div class="st-sequence-choice">${label(i?'Additional sequence':'Sequence',`<select id="c-sequences${i?'-'+i:''}" data-trigger-sequence required>${options(cat.sequences,id)}</select>`)}${i?`<button type="button" class="st-icon-button" data-remove-sequence="${i}" aria-label="Remove additional sequence">×</button>`:''}</div>`).join('')+
        `<p class="st-hint">${cat.sequences.length?'Runs when any selected sequence completes. Pipeline filters are optional.':'No active sequences available. Create a sequence in Cadence first.'}</p><button type="button" class="st-link" id="c-add-sequence" ${cat.sequences.length?'':'disabled'}>＋ Add another sequence</button>`;
    }''')
change(js, "    $('st-trigger-extra').innerHTML=html;", '''    $('st-trigger-extra').innerHTML=html;
    $('c-add-sequence')?.addEventListener('click',()=>{readDraft();draft.conditions.sequences.push('');dirty=true;renderTrigger();});
    $('st-trigger-extra').querySelectorAll('[data-remove-sequence]').forEach(button=>button.onclick=()=>{readDraft();draft.conditions.sequences.splice(Number(button.dataset.removeSequence),1);dirty=true;renderTrigger();});''')
change(js, "    if($('c-sequences'))c.sequences=[...$('c-sequences').selectedOptions].map(o=>o.value);", "    if($('c-sequences'))c.sequences=[...root.querySelectorAll('[data-trigger-sequence]')].map(el=>el.value);")
change(js, "    const c=draft.conditions;\n", "    const c=draft.conditions;\n    c.sources=[...$('st-sources').querySelectorAll('input:checked')].map(el=>el.value);\n")
change(js, "draft.conditions={scopes:draft.conditions.scopes,attributes:draft.conditions.attributes};dirty=true;renderTrigger();summary();", "draft.conditions={scopes:draft.conditions.scopes,attributes:draft.conditions.attributes,sources:draft.conditions.sources};if(draft.trigger_type==='sequence_ended')draft.conditions.scopes=draft.conditions.scopes.filter(s=>s.pipeline);dirty=true;renderTrigger();renderScopes();summary();")
change(js, "  function renderConditions(){", '''  function renderSources(){
    $('st-sources').innerHTML=Object.entries(cat.sources||{}).map(([key,name])=>`<label class="st-check"><input type="checkbox" value="${esc(key)}" ${(draft.conditions.sources||[]).includes(key)?'checked':''}>${esc(name)}</label>`).join('');
    $('st-sources').onchange=()=>{draft.conditions.sources=[...$('st-sources').querySelectorAll('input:checked')].map(el=>el.value);dirty=true;summary();};
  }
  function renderConditions(){''')
change(js, "${r.conditions.attributes.length ?", "${(r.conditions.sources||[]).length ? 'Source: '+esc(r.conditions.sources.map(s=>cat.sources?.[s]||s).join(', '))+' · ' : ''}${r.conditions.attributes.length ?")
change(js, "<div class=\"st-chips\">", "<div class=\"st-chips\" aria-label=\"Personalization fields\">")
change(js, "label('WhatsApp API number'", "label('WhatsApp number'")
change(js, "Free-text messages use Shvya’s WhatsApp API and require a reply from the lead within the last 24 hours. For approved templates, use a follow-up sequence.", "Uses the selected account’s existing transport. API messages require a reply within 24 hours; Hosted messages respect Account Health. For approved templates, use a Cadence sequence.")

# Repair nesting, not just offsets: labels and inputs remain in the same cell.
css = Path("static/triggers/apple_workflows.css")
text = css.read_text()
text += '''
/* Source and sequence editor controls: contained, labelled and responsive. */
html.shvya-page-workflows #smart-triggers .st-field { min-width: 0; width: 100%; }
html.shvya-page-workflows #smart-triggers .st-columns { grid-template-columns: repeat(2, minmax(0, 1fr)); align-items: end; }
html.shvya-page-workflows #smart-triggers .st-condition .st-columns { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) 36px; }
html.shvya-page-workflows #smart-triggers .st-condition .st-columns > .st-icon-button { grid-column: auto; }
html.shvya-page-workflows #smart-triggers :is(input, select, textarea, .st-chips, .st-info, .st-hint) { max-width: 100%; min-width: 0; overflow-wrap: anywhere; }
html.shvya-page-workflows #smart-triggers .st-chip { max-width: 100%; white-space: normal; overflow-wrap: anywhere; text-align: left; }
html.shvya-page-workflows #smart-triggers .st-sequence-choice { display: flex; align-items: end; gap: 8px; min-width: 0; }
html.shvya-page-workflows #smart-triggers .st-sequence-choice .st-icon-button { flex-shrink: 0; }
html.shvya-page-workflows #smart-triggers .st-source-condition { margin: 16px 0; padding: 14px; border: 1px solid var(--st-line); border-radius: 12px; background: #fafafb; }
html.shvya-page-workflows #smart-triggers .st-source-condition .st-section-label { margin: 0; padding: 0; border: 0; }
html.shvya-page-workflows #smart-triggers #st-sources { display: flex; flex-wrap: wrap; column-gap: 16px; }
html.shvya-page-workflows #smart-triggers #st-sources .st-check { max-width: 100%; overflow-wrap: anywhere; }
html.shvya-page-workflows #smart-triggers #st-sources input { flex-shrink: 0; }
html.shvya-page-workflows #smart-triggers .st-intro .st-module-label { display: flex; align-items: center; gap: 10px; color: var(--st-blue) !important; font-size: 12px; letter-spacing: .07em !important; }
html.shvya-page-workflows #smart-triggers .st-module-label > span { width: 8px; height: 8px; border-radius: 50%; background: var(--st-blue); box-shadow: 0 0 0 6px #e3effb; }
html.shvya-page-workflows #smart-triggers .st-intro h1 { margin: 16px 0 10px; font-size: clamp(36px, 4vw, 56px) !important; font-weight: 650; }
html.shvya-page-workflows #smart-triggers .st-intro p { max-width: 65ch; font-size: 17px; }
@media (max-width: 520px) {
  html.shvya-page-workflows #smart-triggers .st-condition .st-columns { grid-template-columns: minmax(0, 1fr); }
  html.shvya-page-workflows #smart-triggers .st-summary .st-check { flex-shrink: 1; }
  html.shvya-page-workflows #smart-triggers .st-card-title h2 { font-size: 17px; }
}
'''
css.write_text(text)
