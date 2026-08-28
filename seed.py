from apps.organizations.models import Organization
from apps.crm.models import Pipeline, Stage, Lead
from apps.accounts.models import User
from apps.channels.models import WhatsAppAccount, WhatsAppMessage

org = Organization.objects.create(name="Test Org")

pipeline = Pipeline.objects.create(organization=org, name="Leads")
stage = Stage.objects.create(pipeline=pipeline, name="New", display_order=0)

user = User.objects.create(
    organization=org,
    name="Ashwani",
    email="dashboard-test@example.com",
    role=User.Role.ADMIN,
    is_active=True,
)
user.set_password("Test@12345")
user.save()

account = WhatsAppAccount.objects.create(
    organization=org,
    connection_type="api",
    phone_number_id="test-phone-id-123",
    waba_id="test-waba-id",
    display_phone_number="+91 00000 00000",
    business_name="Test Business",
    status="connected",
)

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
print("Login email:", user.email)
print("Login password: Test@12345")
print("Lead ID:", lead.id)
