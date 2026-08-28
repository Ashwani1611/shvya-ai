from apps.organizations.models import Organization
from apps.crm.models import Pipeline, Stage, Lead
from apps.accounts.models import User
from apps.channels.models import WhatsAppAccount, WhatsAppMessage

print("Orgs:", list(Organization.objects.values("id", "name")))
print("Pipelines:", list(Pipeline.objects.values("id", "name", "organization_id")))
print("Stages:", list(Stage.objects.values("id", "name", "pipeline_id")))
print("Users:", list(User.objects.values("id", "email", "role", "organization_id", "is_superuser")))
print("Accounts:", list(WhatsAppAccount.objects.values("id", "display_phone_number", "status")))
print("Leads:", list(Lead.objects.values("id", "name", "phone")))
print("Messages:", list(WhatsAppMessage.objects.values("id", "body", "direction", "is_read")))
