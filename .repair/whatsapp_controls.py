"""One-use, assertion-checked repair; deleted after validation succeeds."""

import ast
from pathlib import Path
from textwrap import dedent


changed = set()


def replace(path, old, new, count=1):
    target = Path(path)
    source = target.read_text()
    if source.count(old) != count:
        raise RuntimeError(f"Unexpected source in {path}: {old[:100]!r}")
    target.write_text(source.replace(old, new))
    changed.add(path)


def edit_function(path, name, transform):
    target = Path(path)
    source = target.read_text()
    nodes = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == name]
    if len(nodes) != 1:
        raise RuntimeError(f"Expected exactly one {path}:{name}")
    node = nodes[0]
    first = min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1
    lines = source.splitlines(keepends=True)
    original = "".join(lines[first:node.end_lineno])
    updated = transform(original)
    if updated == original:
        raise RuntimeError(f"No change for {path}:{name}")
    lines[first:node.end_lineno] = [updated.rstrip() + "\n"]
    target.write_text("".join(lines))
    changed.add(path)


def append(path, text):
    target = Path(path)
    target.write_text(target.read_text().rstrip() + "\n\n\n" + dedent(text).strip() + "\n")
    changed.add(path)


session = "services/channels/hosted_whatsapp_service.py"
replace(session, '        defaults["ai_auto_reply"] = True\n', '''        defaults["ai_auto_reply"] = True
        # Existing API numbers created leads before the gear existed. Preserve
        # that default only until a customer explicitly saves their preference.
        defaults["auto_lead_creation"] = True
''')
replace(session, '    org_settings = account.organization.settings or {}\n', '''    # Worker-held account/organization instances can outlive a settings save.
    # Read persisted controls, never a cached related object's JSON snapshot.
    org_settings = Organization.objects.filter(pk=account.organization_id).values_list(
        "settings", flat=True
    ).first() or {}
''')
replace(session, '    pipeline = get_pipeline_for_account(account=account)\n    if pipeline:\n        # Pipeline.ai_enabled', '''    for key in ("ai_auto_reply", "auto_lead_creation", "bump_up_messages", "auto_follow_up"):
        settings[key] = _as_bool(settings[key])
    pipeline = get_pipeline_for_account(account=account)
    if pipeline:
        # Pipeline.ai_enabled''')
replace(session, '    for key in (\n        "ai_auto_reply",\n', '    previous_settings = deepcopy(current)\n\n    for key in (\n        "ai_auto_reply",\n')
replace(session, '    return deepcopy(current)\n', '''    timing_keys = {
        "auto_follow_up", "business_hours_start", "business_hours_end",
        "active_conversation_delay_value", "active_conversation_delay_unit",
    }
    if any(current[key] != previous_settings[key] for key in timing_keys):
        from services.followup_service import reschedule_account_followups

        # Never acquire state locks while holding the organization-settings lock.
        # A running sender owns its state until completion; subsequent work sees
        # the newly committed controls, without restarting completed steps.
        transaction.on_commit(
            lambda account_id=account.pk, organization_id=organization.pk,
            previous=deepcopy(previous_settings): reschedule_account_followups(
                account_id=account_id, organization_id=organization_id,
                previous_settings=previous,
            ),
            robust=True,
        )
    return deepcopy(current)
''')
append(session, '''
def account_ai_block_reason(*, account, lead, bump_up_number=None):
    """Evaluate live account controls at queue/send time for either provider."""
    from apps.ai_engagement.models import OrgInfo
    from apps.ai_engagement.services.ai_permissions import AIPermissionService

    account = WhatsAppAccount.objects.select_related("organization").filter(
        pk=account.pk, organization_id=account.organization_id,
    ).first()
    if account is None or not account.is_active:
        return "whatsapp_account_inactive"
    if account.status != WhatsAppAccount.Status.CONNECTED:
        return "whatsapp_account_not_connected"
    if lead is None:
        return "lead_missing"
    lead = Lead.objects.select_related("organization", "pipeline", "stage").filter(
        pk=lead.pk, organization_id=account.organization_id,
    ).first()
    if lead is None:
        return "lead_organization_mismatch"
    decision = AIPermissionService().evaluate(organization=lead.organization, lead=lead)
    if not decision.allowed:
        return decision.reason
    controls = get_session_settings(account=account)
    if not controls["ai_auto_reply"]:
        return "ai_auto_reply_disabled"
    if bump_up_number is not None:
        if not controls["bump_up_messages"]:
            return "bump_up_messages_disabled"
        org_info = OrgInfo.objects.filter(organization_id=account.organization_id).first()
        if not org_info or not org_info.bump_up_enabled:
            return "organization_bump_up_disabled"
        try:
            number = int(bump_up_number)
            limit = min(int(org_info.bump_up_count), int(controls["bump_up_count"]))
        except (TypeError, ValueError):
            return "invalid_bump_up_count"
        if number < 1 or number > limit:
            return "bump_up_limit_reached"
    return ""
''')

whatsapp = "services/channels/whatsapp_service.py"

def fix_inbound(source):
    start = source.index('    lead = None\n')
    end = source.index('    # --------------------------------------------------------\n    # CREATE INBOUND MESSAGE', start)
    return source[:start] + dedent('''
        from services.channels.hosted_whatsapp_service import get_session_settings

        # OFF prevents creation, not receipt of messages or attachment to an
        # existing CRM lead. Never reassign an existing lead's pipeline/stage.
        lead = Lead.objects.filter(
            organization=organization, phone=normalized_lead_phone,
        ).first()
        controls = get_session_settings(account=account)
        if lead is None and controls["auto_lead_creation"]:
            pipeline = resolve_pipeline(
                organization=organization,
                to_number=account.display_phone_number or to_number,
            )
            stage = _first_stage(pipeline) if pipeline else None
            if stage:
                try:
                    lead, _created = upsert_lead(
                        organization=organization, pipeline=pipeline, stage=stage,
                        name=from_number, phone=normalized_lead_phone,
                        lead_source="whatsapp_api",
                    )
                except DjangoValidationError:
                    lead = Lead.objects.filter(
                        organization=organization, phone=normalized_lead_phone,
                    ).first()

    ''').replace('\n', '\n    ').lstrip('\n') + source[end:]

# Dedent/reindent only the replacement block, leaving the function contract intact.
def inbound_transform(source):
    updated = fix_inbound(source)
    start = updated.index('from services.channels.hosted_whatsapp_service import get_session_settings')
    if updated[start - 4:start] != '    ':
        updated = updated[:start] + '    ' + updated[start:]
    return updated

edit_function(whatsapp, "handle_inbound_message", inbound_transform)

def add_api_send_gate(source):
    old = '    client = WhatsAppClient(\n'
    gate = '''    payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    ai_metadata = payload.get("shvya_ai") or {}
    if ai_metadata:
        from services.channels.hosted_whatsapp_service import account_ai_block_reason

        reason = account_ai_block_reason(
            account=account, lead=message.lead,
            bump_up_number=(ai_metadata.get("number", 1) if ai_metadata.get("origin") == "bump_up" else None),
        )
        if reason:
            message.status = WhatsAppMessage.Status.FAILED
            message.error = f"AI send cancelled: {reason}"
            message.save(update_fields=["status", "error", "updated_at"])
            raise WhatsAppSendError(message.error)

'''
    if source.count(old) != 1:
        raise RuntimeError("API send boundary changed")
    return source.replace(old, gate + old)

edit_function(whatsapp, "send_outbound_message", add_api_send_gate)

transport = "services/channels/hosted_whatsapp_transport.py"
replace(transport, '        if reason:\n            message.status = WhatsAppMessage.Status.FAILED\n', '''        if not reason:
            from services.channels.hosted_whatsapp_service import account_ai_block_reason

            ai_metadata = raw_payload["shvya_ai"]
            reason = account_ai_block_reason(
                account=account, lead=message.lead,
                bump_up_number=(ai_metadata.get("number", 1) if ai_metadata.get("origin") == "bump_up" else None),
            )
        if reason:
            message.status = WhatsAppMessage.Status.FAILED
''')
replace(transport, '    media_url = None\n', '''    if raw_payload.get("shvya_auto_followup"):
        from services.followup_service import live_followup_due

        execution = message.followup_executions.select_related(
            "state__organization", "state__lead__pipeline",
            "state__sequence__whatsapp_account",
        ).filter(organization_id=message.organization_id).first()
        if execution is None:
            raise WhatsAppSendError("Follow-up execution is missing.")
        eligible_at = live_followup_due(execution.state)
        if eligible_at > timezone.now():
            raise HostedAutomationPaused(eligible_at)

    media_url = None
''')

followup = "services/followup_service.py"
append(followup, '''
def _refresh_conversation_pause(state, automation_settings):
    """Recompute the quiet period from real activity and the current setting."""
    activity = [value for value in (state.last_inbound_at, state.last_manual_outbound_at) if value]
    if activity:
        pause_until = max(activity) + _delay_delta(
            automation_settings.get("active_conversation_delay_value", 2),
            automation_settings.get("active_conversation_delay_unit", "hours"),
        )
        if state.paused_until != pause_until:
            state.paused_until = pause_until
            state.save(update_fields=["paused_until", "updated_at"])
    return state.paused_until


def live_followup_due(state, *, automation_settings=None, now=None):
    """One execution-time eligibility calculation shared by API and Hosted."""
    now = now or timezone.now()
    controls = automation_settings or _automation_settings_for_state(state)
    if (
        state.status != LeadSequenceState.Status.ACTIVE
        or not state.lead_auto_followup_enabled or not state.sequence.is_active
        or not controls or not controls.get("auto_follow_up", True)
    ):
        return now + timedelta(minutes=5)
    pause_until = _refresh_conversation_pause(state, controls)
    due = max(now, pause_until) if pause_until else now
    return _move_into_business_hours(
        organization=state.organization, due=due, automation_settings=controls,
    )


def reschedule_account_followups(*, account_id, organization_id, previous_settings):
    """Refresh pending deadlines without resetting step, retry, or lead controls."""
    account = WhatsAppAccount.objects.filter(
        pk=account_id, organization_id=organization_id,
        is_active=True, status=WhatsAppAccount.Status.CONNECTED,
    ).first()
    if account is None:
        return
    candidates = LeadSequenceState.objects.filter(
        organization_id=organization_id, status=LeadSequenceState.Status.ACTIVE,
        lead_auto_followup_enabled=True, sequence__is_active=True,
        sequence__whatsapp_account__connection_type=account.connection_type,
        next_step__isnull=False,
    ).values_list("pk", flat=True)
    for state_id in candidates.iterator(chunk_size=200):
        with transaction.atomic():
            state = LeadSequenceState.objects.select_for_update(of=("self",)).select_related(
                "organization", "lead__pipeline", "sequence__whatsapp_account", "next_step",
            ).filter(
                pk=state_id, organization_id=organization_id,
                status=LeadSequenceState.Status.ACTIVE, lead_auto_followup_enabled=True,
                sequence__is_active=True,
            ).first()
            if state is None or not state.next_step:
                continue
            try:
                linked = _validate_lead_sender(state.lead, state.sequence)
            except FollowupError:
                continue
            if linked.pk != account.pk:
                continue
            latest = state.executions.filter(step=state.next_step).order_by("-created_at").first()
            if latest and latest.status == FollowupExecution.Status.PROCESSING:
                continue
            from services.channels.hosted_whatsapp_service import get_session_settings

            controls = get_session_settings(account=linked)
            now = timezone.now()
            reference = state.last_step_completed_at or state.activated_at or state.assigned_at
            base_due = calculate_step_due(step=state.next_step, reference=reference, organization=state.organization)
            old_pause = state.paused_until
            old_expected = _move_into_business_hours(
                organization=state.organization,
                due=max(base_due, old_pause) if old_pause else base_due,
                automation_settings=previous_settings,
            )
            old_hours_first = _move_into_business_hours(
                organization=state.organization, due=base_due,
                automation_settings=previous_settings,
            )
            if old_pause:
                old_hours_first = max(old_hours_first, old_pause)
            old_from_now = _move_into_business_hours(
                organization=state.organization,
                due=max(now, old_pause) if old_pause else now,
                automation_settings=previous_settings,
            )
            pause_until = _refresh_conversation_pause(state, controls)
            due = max(now, base_due, pause_until or now)
            # Do not shorten unrelated sender pacing or custom future deadlines.
            recognized_waits = {old_expected, old_hours_first, old_from_now, old_pause}
            if (
                previous_settings.get("auto_follow_up", True)
                and state.upcoming_send_at and state.upcoming_send_at > now
                and state.upcoming_send_at not in recognized_waits
            ):
                due = max(due, state.upcoming_send_at)
            if latest and latest.status == FollowupExecution.Status.RETRY_WAIT and latest.next_retry_at:
                due = max(due, latest.next_retry_at)
            if not controls.get("auto_follow_up", True):
                due = max(due, now + timedelta(minutes=5))
            state.upcoming_send_at = _move_into_business_hours(
                organization=state.organization, due=due, automation_settings=controls,
            )
            state.save(update_fields=["upcoming_send_at", "updated_at"])
''')

def fix_api_runner(source):
    start = source.index('    automation_settings = _automation_settings_for_state(state)\n')
    end = source.index('    if not state.next_step:', start)
    replacement = '''    automation_settings = _automation_settings_for_state(state)
    now = timezone.now()
    adjusted = live_followup_due(state, automation_settings=automation_settings, now=now)
    if adjusted > now:
        state.upcoming_send_at = max(state.upcoming_send_at or adjusted, adjusted)
        state.save(update_fields=["upcoming_send_at", "updated_at"])
        return False
'''
    source = source[:start] + replacement + source[end:]
    # live_followup_due already applied the exact account's business hours.
    start = source.index('    adjusted = _move_into_business_hours(', source.index('    if not state.next_step:'))
    end = source.index('    step = state.next_step', start)
    return source[:start] + source[end:]

edit_function(followup, "process_due_state", fix_api_runner)

# Apply conversation protection before business-hour projection when scheduling.
def fix_schedule(source):
    old = '''    due = _move_into_business_hours(
        organization=state.organization,
        due=due,
        automation_settings=_automation_settings_for_state(state),
    )
    if state.paused_until and due < state.paused_until:
        due = state.paused_until
'''
    new = '''    controls = _automation_settings_for_state(state)
    if controls:
        _refresh_conversation_pause(state, controls)
    if state.paused_until and due < state.paused_until:
        due = state.paused_until
    due = _move_into_business_hours(
        organization=state.organization, due=due, automation_settings=controls,
    )
'''
    if old in source:
        return source.replace(old, new)
    old = ''.join('    ' + line if line.strip() else line for line in old.splitlines(keepends=True))
    new = ''.join('    ' + line if line.strip() else line for line in new.splitlines(keepends=True))
    if old not in source:
        raise RuntimeError("Schedule projection changed")
    return source.replace(old, new)

edit_function(followup, "_set_next_step", fix_schedule)
edit_function(followup, "_repeat_or_advance", fix_schedule)

# A final gate after template rendering also covers a settings save while a
# due execution was being prepared. Deferred work retains its pending step.
def final_api_gate(source):
    old = '    message = WhatsAppMessage.objects.create(\n'
    gate = '''    eligible_at = live_followup_due(state)
    if eligible_at > timezone.now():
        execution.status = FollowupExecution.Status.PENDING
        execution.started_at = None
        execution.scheduled_for = eligible_at
        execution.save(update_fields=["status", "started_at", "scheduled_for", "updated_at"])
        state.upcoming_send_at = eligible_at
        state.save(update_fields=["upcoming_send_at", "updated_at"])
        return

'''
    if source.count(old) != 1:
        raise RuntimeError("Template send boundary changed")
    return source.replace(old, gate + old)

edit_function(followup, "_send_whatsapp_step", final_api_gate)

# Serialize activity updates with a running sequence, and ignore stale events.
for name, field in (("register_lead_reply", "last_inbound_at"), ("register_manual_outbound", "last_manual_outbound_at")):
    def activity_transform(source, field=field):
        source = '@transaction.atomic\n' + source
        source = source.replace('state = LeadSequenceState.objects.filter(', 'state = LeadSequenceState.objects.select_for_update(of=("self",)).filter(')
        source = source.replace(f'    state.{field} = at\n', f'    state.{field} = max(at, state.{field}) if state.{field} else at\n')
        source = source.replace('    state.paused_until = at + _state_conversation_delay(state)\n', '''    latest_activity = max(value for value in (state.last_inbound_at, state.last_manual_outbound_at) if value)
    state.paused_until = latest_activity + _state_conversation_delay(state)
''')
        return source
    edit_function(followup, name, activity_transform)

hosted = "services/channels/hosted_automation_service.py"
def fix_hosted_runner(source):
    source = source.replace('        _move_into_business_hours,\n', '        live_followup_due,\n')
    source = source.replace('        get_auto_followup_settings,\n', '')
    source = source.replace('    if not get_auto_followup_settings(state.organization).enabled:\n        return False\n', '')
    start = source.index('    if not session_settings.get("auto_follow_up", True):')
    end = source.index('    health_pause = automation_pause_until(account=account)', start)
    source = source[:start] + '''    now = timezone.now()
    adjusted = live_followup_due(state, automation_settings=session_settings, now=now)
    if adjusted > now:
        _defer_state(state, max(state.upcoming_send_at or adjusted, adjusted))
        return False
''' + source[end:]
    old = '''    adjusted = _move_into_business_hours(organization=state.organization, due=now)
    adjusted = _hosted_business_hours_due(state=state, account=account, due=adjusted)
    if adjusted > now:
        _defer_state(state, adjusted)
        return False

'''
    if old not in source:
        raise RuntimeError("Hosted hours boundary changed")
    source = source.replace(old, '')
    # A final send-time deferral may already own an unsent message. Reuse it;
    # do not leave an orphan queued row or duplicate a previously sent row.
    start = source.index('        message = WhatsAppMessage.objects.create(\n')
    end = source.index('        execution.whatsapp_message = message\n', start)
    block = source[start:end]
    indented = ''.join('    ' + line if line.strip() else line for line in block.splitlines(keepends=True))
    source = source[:start] + '''        message = execution.whatsapp_message
        if message is None or message.status != WhatsAppMessage.Status.QUEUED:
''' + indented + source[end:]
    return source

edit_function(hosted, "process_hosted_due_state", fix_hosted_runner)

# Remove the duplicate business-hours implementation while retaining its callers.
edit_function(hosted, "_hosted_business_hours_due", lambda source: '''def _hosted_business_hours_due(*, state, account, due):
    from services.channels.hosted_whatsapp_service import get_session_settings
    from services.followup_service import _move_into_business_hours

    return _move_into_business_hours(
        organization=state.organization, due=due,
        automation_settings=get_session_settings(account=account),
    )
''')
# datetime imports may still be used elsewhere; retain only actually used names.
source = Path(hosted).read_text()
parsed = ast.parse(source)
used_names = {node.id for node in ast.walk(parsed) if isinstance(node, ast.Name)}
if 'datetime' not in used_names and 'datetime_timezone' not in used_names:
    replace(hosted, 'from datetime import datetime, timedelta, timezone as datetime_timezone\n', 'from datetime import timedelta\n')

for name in ("dispatch_one_hosted_due_state", "dispatch_one_api_due_state"):
    def dispatcher_transform(source):
        source = source.replace('        state_id = (\n', '        state_ids = list(\n')
        source = source.replace('                organization__auto_followup_settings__enabled=True,\n', '')
        source = source.replace('            .first()\n', '            [:50]\n')
        source = source.replace('        if not state_id:\n', '        if not state_ids:\n')
        if '        processed = process_hosted_due_state(state_id)\n' in source:
            old = '''        processed = process_hosted_due_state(state_id)
        return {
            "status": "processed" if processed else "deferred",
            "state_id": str(state_id),
        }
'''
            new = '''        for state_id in state_ids:
            if process_hosted_due_state(state_id):
                return {"status": "processed", "state_id": str(state_id)}
        return {"status": "deferred", "state_id": str(state_ids[0])}
'''
        else:
            old = '''        processed = process_due_state(state_id)
        return {
            "status": "processed" if processed else "deferred",
            "state_id": str(state_id),
        }
'''
            new = '''        for state_id in state_ids:
            if process_due_state(state_id):
                return {"status": "processed", "state_id": str(state_id)}
        return {"status": "deferred", "state_id": str(state_ids[0])}
'''
        if old not in source:
            raise RuntimeError("Dispatcher boundary changed")
        return source.replace(old, new)
    edit_function(hosted, name, dispatcher_transform)

channel_tasks = "apps/channels/tasks.py"
replace(channel_tasks, '                and payload.get("shvya_ai")\n', '''                and payload.get("shvya_ai")
                and payload["shvya_ai"].get("origin") != "bump_up"
''')

ai_tasks = "apps/ai_engagement/tasks.py"
def recheck_bump(source):
    old = '            outbound = queue_outbound_message(\n'
    gate = '''            from services.channels.hosted_whatsapp_service import account_ai_block_reason

            if account_ai_block_reason(
                account=account, lead=locked_lead, bump_up_number=len(bump_messages) + 1,
            ):
                continue
'''
    if source.count(old) != 1:
        raise RuntimeError("Bump queue boundary changed")
    return source.replace(old, gate + old)

edit_function(ai_tasks, "dispatch_bump_ups", recheck_bump)

for path in sorted(changed):
    ast.parse(Path(path).read_text(), filename=path)
    print(f"Reviewed edit: {path}")
