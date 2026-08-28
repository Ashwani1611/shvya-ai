from apps.organizations.models import Organization
from apps.crm.models import Pipeline, Stage, Lead
from apps.channels.models import WhatsAppAccount, WhatsAppMessage

org = Organization.objects.get(name="Ashwani")
pipeline = Pipeline.objects.get(organization=org, name="Leads")
stage = Stage.objects.get(pipeline=pipeline, name="New leads")
account = WhatsAppAccount.objects.get(organization=org)

lead = Lead.objects.create(
    organization=org,
    pipeline=pipeline,
    stage=stage,
    name="Test Lead",
    phone="+919876543210",
)

WhatsAppMessage.objects.create(
    organization=org,
    account=account,
    lead=lead,
    direction=WhatsAppMessage.Direction.INBOUND,
    external_id="test-msg-1",
    from_number=lead.phone,
    to_number=account.phone_number_id,
    body="Hello, this is a test inbound message",
    status=WhatsAppMessage.Status.RECEIVED,
    is_read=False,
)

print("DONE")
print("Lead ID:", lead.id)
print("Login as: ashwani@gmail.com (use the password you signed up with)")
