from django.db import migrations


def seed(apps, schema_editor):
    db = schema_editor.connection.alias
    Status = apps.get_model("support", "TicketStatus")
    Priority = apps.get_model("support", "TicketPriority")
    Category = apps.get_model("support", "TicketCategory")
    Issue = apps.get_model("support", "TicketIssue")
    Settings = apps.get_model("support", "SupportSettings")
    Reply = apps.get_model("support", "SavedReply")
    for order, (key, label) in enumerate([
        ("open", "Open"), ("in_progress", "In Progress"), ("answered", "Answered"),
        ("on_hold", "On Hold"), ("closed", "Closed"),
    ]):
        Status.objects.using(db).get_or_create(key=key,
            defaults={"name": label, "behavior": key, "system": True, "order": order})
    priorities = {}
    for order, key in enumerate(["low", "medium", "high", "urgent"]):
        priorities[key], _ = Priority.objects.using(db).get_or_create(key=key,
            defaults={"name": key.title(), "order": order})
    catalog = {
        "WhatsApp": ["Account connection", "Messages or media", "Chat sync", "Templates"],
        "AI & Playbooks": ["AI response", "Qualification", "Knowledge or file sharing"],
        "CRM & Pipelines": ["Lead or attribute", "Pipeline or stage", "Import or export"],
        "Cadence & Workflows": ["Trigger or condition", "Action execution", "Sequence timing"],
        "Integrations": ["Instagram", "Connect Hub", "API or webhook"],
        "Account & Billing": ["Access or login", "Plan or billing", "Team permissions"],
        "General": ["Email request", "Product feedback", "Other issue"],
    }
    email_issue = None
    for order, (name, issues) in enumerate(catalog.items()):
        category, _ = Category.objects.using(db).get_or_create(name=name, defaults={"order": order})
        for index, issue_name in enumerate(issues):
            issue, _ = Issue.objects.using(db).get_or_create(category=category, name=issue_name,
                defaults={"order": index})
            if name == "General" and issue_name == "Email request":
                email_issue = issue
    Settings.objects.using(db).get_or_create(pk=1,
        defaults={"email_issue_id": email_issue.pk, "email_priority_id": priorities["medium"].pk})
    Reply.objects.using(db).get_or_create(name="We’re investigating", defaults={
        "body": "Thank you for the details. Our team is reviewing the issue. We’ll share an update in this ticket as soon as we have a confirmed next step."})
    Reply.objects.using(db).get_or_create(name="Request reproduction steps", defaults={
        "body": "Could you share the steps that lead to this issue, what you expected, and what happened instead? A screenshot or short screen recording would help. Please hide passwords, API keys, and unrelated customer data."})


class Migration(migrations.Migration):
    dependencies = [("support", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
