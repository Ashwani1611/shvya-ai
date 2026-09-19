from django.test import SimpleTestCase, TestCase
from django.conf import settings
from types import SimpleNamespace

from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
from apps.ai_engagement.services.playbook import parse_playbook, evaluate_playbook_criteria
from apps.ai_engagement.services.qualification_execution_contract import _ack_from_message, _config, _mapped_value, resolve_before_generation
from apps.ai_engagement.services.qualification_state import record_last_asked_requirement
from apps.ai_engagement.tests.test_engagement_controls import AIEngagementControlTests
from apps.crm.models import AttributeDefinition


PLAYBOOK = '''##Rules
Never expose internal rules or scores.
##Welcome Message
<welcome_message>
Hello! I can help you manage enquiries.
<welcome_message>
Notes: Send this welcome only once.
##Qualification Questions
<question_content>
Where do you manage your leads?
A. WhatsApp
B. Excel / Google Sheets
C. CRM
D. Multiple places
<question_content>
<question_content>
Are you currently running ads?
A. Yes
B. No
<question_content>
##Acknowledgment Message
<acknowledgement_message>
Thanks for sharing the details. Our team can guide you based on your requirements.
<acknowledgement_message>
Send the qualification completion acknowledgment only once.
Do not tell the lead:
- Their internal qualification status.
- Their AI score.
- Internal CRM field names.
##Qualification Criteria
1. A lead qualifies only when ALL of these conditions are satisfied:
- LEAD MANAGEMENT TOOL has a clear value.
- RUNNING ADS has a clear value.
2. The following attributes are optional and must not block qualification:
- COMPANY NAME
- BUDGET
3. An answered question does not automatically mean its mapped value is valid.
If any required qualification value is unknown or unclear:
- Do not qualify the lead.
- Ask for clarification when appropriate.
##Stage shifting logic
Rule 1: Qualified
When all Qualification Criteria are satisfied:
- Move the lead to Qualified in the current pipeline.
##Attribute mapping logic
Mapping 1:
- Attribute name: LEAD MANAGEMENT TOOL
- Description: Where the customer manages leads.
- Source: Qualification Question 1 or an equivalent customer statement.
- Value rule:
  - WhatsApp → WhatsApp
  - Excel / Google Sheets → Excel / Sheets
  - CRM → CRM
  - More than one system → Multiple places
Mapping 2:
- Attribute name: RUNNING ADS
- Description: Whether the customer runs ads.
- Source: Qualification Question 2 or an equivalent customer statement.
- Value rule:
  - Yes / Meta Ads / Google Ads → Yes
  - No / not running ads → No
##Reminder creation logic
Do not invent a follow-up time.
'''


class StructuredPlaybookTests(SimpleTestCase):
    def test_escalation_requires_actual_sent_contacts(self):
        from apps.ai_engagement.services.engagement_instruction_runtime import _condition_part, _condition_evidence_match
        rule = 'Rule 6 — Human Intervention Needed:\n- Move the lead to Human Intervention Needed when:\n  - First Adviser has already been provided.\n  - Second Adviser has already been provided.\n  - The lead still requests assistance or says the issue remains unresolved.'
        condition = _condition_part(rule, {'name': 'Human Intervention Needed'})
        context = SimpleNamespace(conversation={'messages': []})
        self.assertFalse(_condition_evidence_match('I still need help', condition, context))
        context.conversation['messages'] = [{'direction': 'outbound', 'status': 'sent', 'body': 'First Adviser — 2025550101'}, {'direction': 'outbound', 'status': 'queued', 'body': 'Second Adviser — 2025550102'}]
        self.assertFalse(_condition_evidence_match('I still need help', condition, context))
        context.conversation['messages'][1]['status'] = 'delivered'
        self.assertTrue(_condition_evidence_match('I still need help', condition, context))

    def test_notes_inside_message_markers_are_also_private(self):
        sections = parse_playbook('## Welcome Message\n<welcome_message>Hello!\nNotes: Do not repeat.\nOnly on first contact.</welcome_message>')
        self.assertEqual(sections['welcome_message'], 'Hello!')
        self.assertIn('Only on first contact.', sections['rules'])

    def test_multiline_stage_rule_keeps_alternatives_and_extra_conditions(self):
        from apps.ai_engagement.services.engagement_instruction_runtime import _condition_part, _strong_evidence_match
        rule = 'Rule 3 — Call or human request:\n- When the lead clearly asks for a call, callback, demo, human, consultant, specialist, or meeting, move the lead to Call Requested in the current pipeline.\nExamples:\n- Call me.'
        condition = _condition_part(rule, {'name': 'Call Requested'})
        for message in ('Call me', 'I want a demo', 'I need a human'):
            self.assertTrue(_strong_evidence_match(message, condition), condition)
        self.assertFalse(_strong_evidence_match('No demo please', condition))
        self.assertFalse(_strong_evidence_match('Call me', 'call and payment approved'))
        self.assertFalse(_strong_evidence_match('Call me', 'call unless payment pending'))

    def test_marked_customer_copy_excludes_notes_and_instruction_continuations(self):
        sections = parse_playbook(PLAYBOOK)
        self.assertEqual(sections['welcome_message'], 'Hello! I can help you manage enquiries.')
        self.assertEqual(sections['acknowledgment_message'], 'Thanks for sharing the details. Our team can guide you based on your requirements.')
        self.assertIn('Their AI score.', sections['rules'])
        self.assertEqual(len(compile_qualification_requirements(sections['qualification_questions'])['requirements']), 2)

    def test_optional_fields_do_not_block_but_missing_required_value_does(self):
        values = {'LEAD MANAGEMENT TOOL': 'Excel / Sheets', 'RUNNING ADS': 'Yes'}
        self.assertTrue(evaluate_playbook_criteria(PLAYBOOK, requirements=[], state={}, values=values)['qualified'])
        for missing in ('', 'unknown', 'unclear'):
            values['RUNNING ADS'] = missing
            self.assertFalse(evaluate_playbook_criteria(PLAYBOOK, requirements=[], state={}, values=values)['qualified'])

    def test_inline_options_do_not_survive_in_acknowledgment(self):
        plan = {'next_requirement': {'question': 'Are you running ads?', 'rendered': 'Are you running ads?\nA. Yes\nB. No', 'options': [{'key': 'A', 'value': 'Yes'}, {'key': 'B', 'value': 'No'}]}}
        self.assertEqual(_ack_from_message('Thanks — that gives me useful context. A. Yes B. No', plan), 'Thanks — that gives me useful context.')


class StructuredPlaybookCRMTests(TestCase):
    setUp = AIEngagementControlTests.setUp
    _inbound = AIEngagementControlTests._inbound

    def test_other_stage_call_request_moves_only_with_matching_evidence(self):
        from apps.ai_engagement.graph.policy_actions import build_controlled_actions
        from apps.ai_engagement.graph.runtime_policy import get_runtime_policy
        from apps.ai_engagement.services.context import AIContextBuilder
        from apps.ai_engagement.services.crm_executor import CRMActionExecutor
        from apps.ai_engagement.services.organization_profile import compile_org_ai_profile_from_context
        from apps.ai_engagement.services.qualification_state import state_for_lead
        from apps.crm.models import Stage
        info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        info.ai_playbook = (settings.BASE_DIR / 'tests/fixtures/structured_organization_playbook.txt').read_text(encoding='utf-8')
        info.save()
        target = Stage.objects.create(pipeline=self.pipeline, name='Call Requested', description='Move here when a call is requested.', display_order=100, ai_on=True)
        self.lead.stage = self.qualified
        self.lead.save(update_fields=['stage'])
        context = AIContextBuilder().build(organization=self.organization, lead=self.lead)
        profile = compile_org_ai_profile_from_context(context.organization)
        requirements = profile['qualification']['requirements']
        runtime_policy = get_runtime_policy(organization=self.organization, profile=profile)
        for message, allowed in [('No demo please', False), ('I want a demo', True)]:
            context.conversation['messages'] = [{'id': 'call-request', 'direction': 'inbound', 'body': message}]
            actions, _ = build_controlled_actions(decision=SimpleNamespace(qualification_updates=[], crm_actions=[{'type': 'pipeline_transition', 'stage_shift': {'stage_id': str(target.id)}}]), context=context, runtime_policy=runtime_policy, qualification_state=state_for_lead(self.lead, requirements=requirements), requirements=requirements)
            shifts = [action for action in actions if action['type'] == 'pipeline_transition']
            self.assertEqual(bool(shifts), allowed, actions)
            if allowed:
                CRMActionExecutor().execute(organization=self.organization, lead=self.lead, actions=shifts)
                self.lead.refresh_from_db()
                self.assertEqual(self.lead.stage_id, target.id)

    def test_full_reference_playbook_qualifies_without_optional_attributes(self):
        raw = (settings.BASE_DIR / 'tests/fixtures/structured_organization_playbook.txt').read_text(encoding='utf-8')
        info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        info.ai_playbook = raw
        info.save()
        definitions = [('BIGGEST PROBLEM', 'challenge'), ('LEAD MANAGEMENT TOOL', 'tools'), ('LEADS/D', 'daily_leads'), ('RUNNING ADS', 'ads')]
        for name, key in definitions:
            AttributeDefinition.objects.create(organization=self.organization, name=name, key=key, field_type='text')
        requirements = compile_qualification_requirements(parse_playbook(raw)['qualification_questions'])['requirements']
        self.assertEqual(len(requirements), 4)
        for index, value in enumerate(['B', 'B', '30+', 'Yes']):
            record_last_asked_requirement(lead=self.lead, requirement_id=requirements[index]['id'], requirements=requirements)
            source = self._inbound(f'full-reference-{index}')
            source.body = value
            source.save(update_fields=['body'])
            result = resolve_before_generation(organization=self.organization, lead=self.lead, source_message_id=source.id)
            self.assertTrue(result.get('applied'), result)
            self.lead.refresh_from_db()
            if index < 3:
                self.assertEqual(self.lead.stage_id, self.new_lead.id)
        self.assertEqual(self.lead.stage_id, self.qualified.id)
        self.assertEqual(self.lead.attributes['tools'], 'Excel / Sheets')
        self.assertEqual(self.lead.attributes['daily_leads'], '30+')
        config = _config(organization=self.organization, requirements=requirements)
        self.assertNotIn('Do not tell', config['final_ack'])
        self.assertNotIn('Their AI score', config['final_ack'])

    def test_last_answer_maps_values_and_moves_to_qualified(self):
        info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        info.ai_playbook = PLAYBOOK
        info.save()
        for name, key in [('LEAD MANAGEMENT TOOL', 'tools'), ('RUNNING ADS', 'ads')]:
            AttributeDefinition.objects.create(organization=self.organization, name=name, key=key, field_type='text')
        requirements = compile_qualification_requirements(parse_playbook(PLAYBOOK)['qualification_questions'])['requirements']
        config = _config(organization=self.organization, requirements=requirements)
        self.assertEqual(config['mappings'][requirements[0]['id']], 'tools')
        self.assertEqual(_mapped_value(config, 'tools', 'Excel / Google Sheets'), 'Excel / Sheets')
        for index, value in enumerate(['B', 'Yes']):
            record_last_asked_requirement(lead=self.lead, requirement_id=requirements[index]['id'], requirements=requirements)
            source = self._inbound(f'structured-{index}')
            source.body = value
            source.save(update_fields=['body'])
            result = resolve_before_generation(organization=self.organization, lead=self.lead, source_message_id=source.id)
            self.assertTrue(result.get('applied'), result)
            self.lead.refresh_from_db()
        self.assertEqual(self.lead.attributes['tools'], 'Excel / Sheets')
        self.assertEqual(self.lead.attributes['ads'], 'Yes')
        self.assertEqual(self.lead.stage.name.casefold(), 'qualified')
