"""Support integration/security regressions for SHVYA's PostgreSQL CI environment."""
import tempfile
import uuid
from datetime import timedelta
from email.message import EmailMessage
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import Http404
from django.template.loader import get_template
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import resolve, reverse
from django.utils import timezone

from apps.organizations.models import Organization
from apps.crm.models import Pipeline
from apps.support import services, views
from apps.support.access import visible_tickets
from apps.support.fields import public_custom
from apps.support.jobs import deliver_pending, maintain_tickets
from apps.support.mail import RejectedEmail, ingest_verified_email, trusted_imap_sender
from apps.support.models import (Attachment, CustomField, EmailDelivery, InboundReceipt,
    OrganizationSupportPolicy, SharedAccess, SupportSettings, Ticket, TicketCategory,
    TicketIssue, TicketPriority, TicketStatus)
from apps.support.storage import PrivateSupportStorage


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    SUPPORT_PUBLIC_BASE_URL='https://support.example.test',
    SUPPORT_FROM_EMAIL='care@example.test', SUPPORT_REPLY_TO_EMAIL='care@example.test',
    SUPPORT_ATTACHMENT_SCANNER='', SUPPORT_REQUIRE_SCANNER=False,
    ALLOWED_HOSTS=['testserver'], CACHES={'default': {'BACKEND':'django.core.cache.backends.locmem.LocMemCache'}})
class SupportIntegrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User=get_user_model()
        cls.org=Organization.objects.create(name='Support test A')
        cls.other=Organization.objects.create(name='Support test B')
        cls.user=User.objects.create_user(email='requester@example.test',
            organization=cls.org, name='Requester', role='admin')
        cls.agent=User.objects.create_user(email='agent@example.test', organization=cls.org, name='Agent', role='agent')
        cls.outsider=User.objects.create_user(email='other@example.test', organization=cls.other, name='Other', role='admin')
        cls.staff=User.objects.create_superuser(email='ops@example.test', name='Ops')
        cls.pipeline=Pipeline.objects.create(organization=cls.org,name='Support test pipeline',owner=cls.user)
        cls.foreign_pipeline=Pipeline.objects.create(organization=cls.other,name='Other pipeline',owner=cls.outsider)
        cls.category=TicketCategory.objects.create(name='Integration tests')
        cls.issue=TicketIssue.objects.create(category=cls.category,name='Synchronization')
        cls.priority,_=TicketPriority.objects.get_or_create(key='medium', defaults={'name':'Medium'})
        for key in ('open','in_progress','answered','on_hold','closed'):
            TicketStatus.objects.get_or_create(key=key,defaults={'name':key.replace('_',' ').title(),'behavior':key,'system':True})
        config=SupportSettings.load()
        config.email_issue=cls.issue;config.email_priority=cls.priority;config.save()

    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        fs=FileSystemStorage(location=self.directory.name)
        for context in [patch.object(Attachment._meta.get_field('file'),'storage',fs),
            patch('apps.support.services.private_storage',fs), patch('apps.support.notifications.wake_delivery',lambda:None)]:
            context.start();self.addCleanup(context.stop)
        self.factory=RequestFactory()

    def create(self, **overrides):
        data=dict(actor=self.user,category_id=self.category.pk,issue_id=self.issue.pk,
            priority_id=self.priority.pk,subject='Sync stops',body='Steps to reproduce.',pipeline_id=self.pipeline.pk)
        data.update(overrides)
        return services.create_ticket(**data)

    def request(self, path='/', *, user=None, method='get', data=None):
        request=getattr(self.factory,method)(path,data=data or {},secure=True)
        request.user=user or self.user;request.crm_user=request.user
        request.shvya_session_area='superadmin' if request.user.is_superuser else 'dashboard'
        request.session={}
        return request

    def test_auto_context_and_initial_state(self):
        ticket=self.create(source_path='/dashboard/?token=redacted&pipeline=other')
        self.assertEqual(ticket.organization,self.org)
        self.assertEqual(ticket.requester,self.user)
        self.assertEqual(ticket.pipeline,self.pipeline)
        self.assertEqual(ticket.status.key,'open')
        self.assertEqual(ticket.custom_values,{})
        self.assertEqual(ticket.context['source_path'],'/dashboard/')
        self.assertNotIn('redacted',str(ticket.context))

    def test_pipeline_and_issue_relationships_are_validated(self):
        with self.assertRaises(Http404):self.create(pipeline_id=self.foreign_pipeline.pk)
        other_category=TicketCategory.objects.create(name='Unrelated')
        other_issue=TicketIssue.objects.create(category=other_category,name='Other')
        with self.assertRaises(Http404):self.create(issue_id=other_issue.pk)
        self.assertFalse(Ticket.objects.exists())

    def test_organization_and_own_user_visibility(self):
        ticket=self.create()
        self.assertFalse(visible_tickets(self.outsider).filter(pk=ticket.pk).exists())
        self.assertTrue(visible_tickets(self.agent).filter(pk=ticket.pk).exists())
        OrganizationSupportPolicy.objects.create(organization=self.org,own_tickets_only=True)
        self.assertFalse(visible_tickets(self.agent).filter(pk=ticket.pk).exists())
        with self.assertRaises(Http404):services.reply(ticket_id=ticket.pk,actor=self.agent,body='Unauthorized')
        self.assertTrue(visible_tickets(self.user).filter(pk=ticket.pk).exists())

    def test_customer_reply_transition_matrix(self):
        ticket=self.create()
        for key in ('open','in_progress','answered','on_hold','closed'):
            with self.subTest(key=key):
                Ticket.objects.filter(pk=ticket.pk).update(status=TicketStatus.objects.get(key=key))
                updated=services.reply(ticket_id=ticket.pk,actor=self.user,body='Customer response')
                self.assertEqual(updated.status.key,'in_progress' if key=='in_progress' else 'open')

    def test_staff_reply_default_assignment_and_internal_note(self):
        config=SupportSettings.load();config.auto_assign_first_reply=True;config.save()
        ticket=self.create()
        ticket=services.reply(ticket_id=ticket.pk,actor=self.staff,body='Private work note',internal=True)
        self.assertEqual(ticket.status.key,'open');self.assertIsNone(ticket.assignee_id)
        ticket=services.reply(ticket_id=ticket.pk,actor=self.staff,body='Public response')
        self.assertEqual(ticket.status.key,'answered');self.assertEqual(ticket.assignee,self.staff)
        self.assertIsNotNone(ticket.first_staff_reply_at)
        with self.assertRaises(PermissionDenied):services.reply(ticket_id=ticket.pk,actor=self.user,body='Private?',internal=True)

    def test_inactive_previous_assignee_does_not_block_customer(self):
        ticket=self.create()
        services.reply(ticket_id=ticket.pk,actor=self.staff,body='Response',assign_me=True)
        get_user_model().objects.filter(pk=self.staff.pk).update(is_active=False)
        updated=services.reply(ticket_id=ticket.pk,actor=self.user,body='Still experiencing it')
        self.assertEqual(updated.status.key,'open')

    def test_custom_status_family_and_system_key_protection(self):
        custom=TicketStatus.objects.create(name='Engineering investigating',key='engineering',behavior='in_progress')
        ticket=self.create()
        services.update_ticket(ticket_id=ticket.pk,actor=self.staff,status_id=custom.pk)
        updated=services.reply(ticket_id=ticket.pk,actor=self.user,body='Additional details')
        self.assertEqual(updated.status,custom)
        system=TicketStatus.objects.get(key='open');system.behavior='closed'
        with self.assertRaises(ValidationError):system.full_clean()

    def test_customer_cannot_update_properties_or_other_org(self):
        ticket=self.create()
        with self.assertRaises(Http404):services.update_ticket(ticket_id=ticket.pk,actor=self.outsider,status_id=ticket.status_id)
        with self.assertRaises(PermissionDenied):services.update_ticket(ticket_id=ticket.pk,actor=self.user,change_assignee=True,assignee_id=self.staff.pk)
        with self.assertRaises(PermissionDenied):services.update_ticket(ticket_id=ticket.pk,actor=self.user,status_id=TicketStatus.objects.get(key='answered').pk)

    def test_optimistic_update_conflict(self):
        ticket=self.create();original=ticket.version
        services.update_ticket(ticket_id=ticket.pk,actor=self.staff,version=original)
        with self.assertRaises(services.Conflict):services.update_ticket(ticket_id=ticket.pk,actor=self.staff,version=original)

    def test_creation_and_reply_idempotency(self):
        key=uuid.uuid4();one=self.create(client_key=key);two=self.create(client_key=key)
        self.assertEqual(one.pk,two.pk)
        key=uuid.uuid4()
        for _ in range(2):services.reply(ticket_id=one.pk,actor=self.user,body='Same submit',client_key=key)
        self.assertEqual(one.messages.filter(body='Same submit').count(),1)

    def test_required_and_internal_custom_fields(self):
        CustomField.objects.create(name='Reproduce steps',key='steps',kind='text',required=True)
        with self.assertRaises(ValidationError):self.create()
        ticket=self.create(custom={'steps':'Open Chats'})
        CustomField.objects.create(name='Internal resolution',key='resolution',kind='text',customer_visible=False,required_on_close=True)
        with self.assertRaises(ValidationError):self.create(custom={'steps':'X','resolution':'cannot write this'})
        with self.assertRaises(ValidationError):services.update_ticket(ticket_id=ticket.pk,actor=self.user,status_id=TicketStatus.objects.get(key='closed').pk)
        services.update_ticket(ticket_id=ticket.pk,actor=self.staff,custom={'resolution':'Reviewed'})
        ticket.refresh_from_db();self.assertNotIn(('Internal resolution','Reviewed'),public_custom(ticket.custom_values))

    def test_private_upload_exact_count_limit(self):
        config=SupportSettings.load();config.max_files=1;config.save()
        ticket=self.create(files=[SimpleUploadedFile('recording.webm',b'audio-data')])
        file=Attachment.objects.get(message__ticket=ticket)
        self.assertEqual(file.original_name,'recording.webm')
        with self.assertRaises(ValidationError):self.create(files=[SimpleUploadedFile('a.txt',b'a'),SimpleUploadedFile('b.txt',b'b')])
        with self.assertRaises(ValueError):PrivateSupportStorage().url('never-public')

    def test_shared_readonly_expiry_and_revoke(self):
        ticket=self.create();grant,raw=services.issue_share(actor=self.user,ticket_id=ticket.pk,label='Viewer')
        self.assertNotEqual(grant.token_hash,raw)
        self.assertEqual(services.resolve_grant(raw).pk,grant.pk)
        with self.assertRaises(PermissionDenied):services.reply(ticket_id=ticket.pk,grant=grant,body='Not allowed')
        SharedAccess.objects.filter(pk=grant.pk).update(expires_at=timezone.now()-timedelta(seconds=1))
        with self.assertRaises(PermissionDenied):services.resolve_grant(raw)
        grant,raw=services.issue_share(actor=self.user,ticket_id=ticket.pk,label='Another')
        services.revoke_share(actor=self.user,ticket_id=ticket.pk,grant_id=grant.pk)
        with self.assertRaises(PermissionDenied):services.resolve_grant(raw)

    def test_public_share_never_renders_or_downloads_internal_note(self):
        ticket=self.create()
        services.reply(ticket_id=ticket.pk,actor=self.staff,body='INTERNAL-SENTINEL',internal=True,
            files=[SimpleUploadedFile('private.txt',b'private')])
        grant,raw=services.issue_share(actor=self.user,ticket_id=ticket.pk,label='Viewer')
        response=views.shared_ticket(self.request(),token=raw)
        self.assertEqual(response.status_code,200)
        self.assertNotIn(b'INTERNAL-SENTINEL',response.content)
        attachment=Attachment.objects.get(message__internal=True)
        with self.assertRaises(Http404):views.shared_attachment(self.request(),token=raw,attachment_id=attachment.pk)

    def test_shared_write_requires_csrf_through_http_middleware(self):
        ticket=self.create();_,raw=services.issue_share(actor=self.user,ticket_id=ticket.pk,label='Viewer',can_reply=True)
        client=Client(enforce_csrf_checks=True)
        response=client.post(reverse('support-shared:reply',args=[raw]),{'body':'csrf missing','client_key':str(uuid.uuid4())},secure=True)
        self.assertEqual(response.status_code,403)

    def test_merge_preserves_internal_work_and_resolves_old_reply(self):
        primary=self.create();source=self.create(subject='Second ticket')
        services.reply(ticket_id=source.pk,actor=self.staff,body='Source internal',internal=True)
        work=services.add_work_item(actor=self.staff,ticket_id=source.pk,kind='task',title='Original task',
            due_at=timezone.now()+timedelta(hours=1),assignee_id=self.staff.pk)
        _,raw=services.issue_share(actor=self.user,ticket_id=source.pk,label='Old link',can_reply=True)
        services.merge_tickets(actor=self.staff,primary_id=primary.pk,source_ids=[source.pk])
        source.refresh_from_db();work.refresh_from_db()
        self.assertEqual(source.merged_into,primary);self.assertEqual(source.status.key,'closed')
        self.assertEqual(work.ticket,source)
        self.assertTrue(source.messages.filter(internal=True).exists())
        with self.assertRaises(PermissionDenied):services.resolve_grant(raw)
        updated=services.reply(ticket_id=source.pk,actor=self.user,body='Reply through old reference')
        self.assertEqual(updated.pk,primary.pk)
        self.assertTrue(primary.messages.filter(body='Reply through old reference').exists())

    def test_cross_org_merge_is_rejected(self):
        primary=self.create()
        other=self.create(actor=self.outsider,pipeline_id=self.foreign_pipeline.pk)
        with self.assertRaises(ValidationError):services.merge_tickets(actor=self.staff,primary_id=primary.pk,source_ids=[other.pk])

    def test_merge_does_not_collide_on_source_scoped_retry_keys(self):
        primary=self.create();source=self.create();key=uuid.uuid4()
        for t in (primary,source):services.reply(ticket_id=t.pk,actor=self.user,body='Message',client_key=key)
        services.merge_tickets(actor=self.staff,primary_id=primary.pk,source_ids=[source.pk])
        self.assertEqual(primary.messages.filter(body='Message').count(),2)

    def test_auto_close_exclusions_and_elapsed_hours(self):
        config=SupportSettings.load();config.auto_close_hours=24;config.save()
        tickets={key:self.create(subject=key) for key in ('open','in_progress','answered','on_hold','closed')}
        for key,t in tickets.items():Ticket.objects.filter(pk=t.pk).update(status=TicketStatus.objects.get(key=key),last_public_activity_at=timezone.now()-timedelta(hours=25))
        self.assertEqual(maintain_tickets()['auto_closed'],2)
        for key,t in tickets.items():
            t.refresh_from_db();self.assertEqual(t.status.key,'closed' if key in ('open','answered') else key)

    def email(self, *, sender=None, subject='Email issue', message_id='one', body='Reproduce steps'):
        msg=EmailMessage();msg['From']=sender or self.user.email;msg['To']='care@example.test'
        msg['Subject']=subject;msg['Message-ID']=f'<{message_id}@example.test>';msg.set_content(body)
        return msg

    def enable_mail(self):
        config=SupportSettings.load();config.email_intake_enabled=True;config.save()

    def test_verified_email_dedup_and_thread_reply(self):
        self.enable_mail();raw=self.email().as_bytes()
        one=ingest_verified_email(raw,verified_sender=self.user.email)
        two=ingest_verified_email(raw,verified_sender=self.user.email)
        self.assertEqual(one.pk,two.pk);self.assertEqual(InboundReceipt.objects.count(),1)
        raw=self.email(subject=f'Re: [{one.ticket.reference}]',message_id='two').as_bytes()
        ingest_verified_email(raw,verified_sender=self.user.email)
        self.assertEqual(one.ticket.messages.count(),2)

    def test_email_mismatched_sender_and_other_org_reference(self):
        self.enable_mail();ticket=self.create()
        with self.assertRaises(RejectedEmail):ingest_verified_email(self.email().as_bytes(),verified_sender=self.outsider.email)
        msg=self.email(sender=self.outsider.email,subject=f'[{ticket.reference}]')
        with self.assertRaises(RejectedEmail):ingest_verified_email(msg.as_bytes(),verified_sender=self.outsider.email)

    @override_settings(SUPPORT_IMAP_TRUST_RECEIVER=False,SUPPORT_IMAP_AUTHSERV_ID='mx.example.test')
    def test_untrusted_authentication_results_fail_closed(self):
        msg=self.email();msg['Authentication-Results']='mx.example.test; dmarc=pass header.from=example.test'
        with self.assertRaises(RejectedEmail):trusted_imap_sender(msg)

    def test_email_delivery_skips_now_disabled_recipient(self):
        ticket=self.create()
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
        deliver_pending(limit=50)
        self.assertFalse(EmailDelivery.objects.filter(event__ticket=ticket,recipient=self.user,state='sent').exists())

    def test_wrong_area_and_org_admin_never_get_platform_access(self):
        request=self.request(user=self.user)
        with self.assertRaises(PermissionDenied):views.staff_list(request)
        request=self.request(user=self.staff);request.shvya_session_area='admin'
        with self.assertRaises(PermissionDenied):views.staff_list(request)

    def test_old_sidebar_url_resolves_real_support(self):
        self.assertEqual(reverse('crm-support-portal'),'/dashboard/support-portal/')
        self.assertEqual(resolve('/dashboard/support-portal/').func,views.customer_list)

    def test_templates_compile_and_both_lists_render(self):
        self.create()
        for name in ('list','detail','create','configuration','share_created','public_base'):
            get_template(f'support/{name}.html')
        for fn,user in ((views.customer_list,self.user),(views.staff_list,self.staff)):
            with self.subTest(user=user.email):
                response=fn(self.request(user=user));self.assertEqual(response.status_code,200)
                self.assertIn(b'Sync stops',response.content)
