"""
SHVYA Phase 1 seed script — matches the current Lead/Stage/Pipeline
schema (Lead has: name, phone, email, notes, attributes — no
ai_score/source/owner/status/priority/company_name).

Run with:
    python manage.py shell -c "exec(open('scripts/seed_demo.py').read())"
"""
import random

from apps.organizations.models import Organization
from apps.crm.models import Pipeline, Stage, Lead

org, _ = Organization.objects.get_or_create(
    name="TecKnow Academy Demo",
    defaults={"timezone": "Asia/Kolkata", "plan": "pro"},
)

pipeline, _ = Pipeline.objects.get_or_create(
    organization=org, name="Sales Pipeline", defaults={"is_active": True}
)

stage_names = ["New Lead", "Qualified", "Good Lead", "In Conversation", "Demo", "Won", "Lost"]
stages = []
for i, name in enumerate(stage_names):
    stage, _ = Stage.objects.get_or_create(
        pipeline=pipeline, display_order=i, defaults={"name": name, "is_active": True}
    )
    stages.append(stage)

first_names = ["Riya", "Rahul", "Aman", "Priya", "Karan", "Neha", "Sanjay", "Divya", "Ishaan", "Meera"]

created = 0
for i in range(30):
    lead = Lead(
        organization=org,
        pipeline=pipeline,
        stage=random.choice(stages),
        name=f"{random.choice(first_names)} {i}",
        phone=f"+9199999{i:05d}",
        email=f"lead{i}@example.com",
        notes="",
        attributes={"budget": f"\u20b9{random.randint(10, 80)}000", "timeline": "30 days"},
    )
    try:
        lead.full_clean()
        lead.save()
        created += 1
    except Exception as e:
        print(f"Skipped lead {i}: {e}")

print(f"Seed complete. Created={created} | Total leads for org={Lead.objects.filter(organization=org).count()}")