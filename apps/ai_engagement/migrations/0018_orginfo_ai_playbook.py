import re

from django.db import migrations, models


def _legacy_question_section(instructions):
    """Extract only unmistakable questions; never weaken a legacy constraint."""
    heading = re.search(r"(?im)^[ \t]*(#{1,6})[ \t]*qualification criteria[ \t]*:?[ \t\r]*$", instructions)
    if heading is None:
        return "", instructions
    depth = len(heading.group(1))
    body_start = heading.end()
    body_end = len(instructions)
    for next_heading in re.finditer(r"(?m)^[ \t]*(#{1,6})[ \t]*\S.*$", instructions[body_start:]):
        if len(next_heading.group(1)) <= depth:
            body_end = body_start + next_heading.start()
            break
    question_lines, criterion_lines, pending_labels = [], [], []
    active_question = False
    has_question = False
    for line in instructions[body_start:body_end].splitlines():
        text = re.sub(r"^[ \t]*#{1,6}[ \t]*", "", line).strip()
        if re.fullmatch(r"(?:question|q)[ \t]*\d+[.:]?[ \t]*|</?question_content>", text, re.I):
            pending_labels.append(line)
            continue
        # A rhetorical condition or numeric constraint may contain a question
        # mark. Keep it authoritative rather than guessing it is a question.
        content = re.sub(r"</?question_content>", "", text, flags=re.I).strip()
        content = re.sub(r"^(?:(?:question|q)\s*)?\d+[.):]\s*", "", content, flags=re.I)
        direct_question = re.match(
            r"^(?:what|which|who|how|where|when|do you|does your|did you|are you|is your|have you|would you|could you|can you|will you)\b", content, re.I,
        )
        question = bool(direct_question and "?" in content and not re.search(r"\b(?:qualify|qualified)\b|[<>]=?", content, re.I))
        option = active_question and bool(re.match(r"^(?:[A-Za-z][.)]|\([A-Za-z]\))[ \t]+\S", text))
        if question or option:
            question_lines.extend(pending_labels)
            pending_labels = []
            question_lines.append(line)
            active_question = True
            has_question = has_question or question
        elif not text and active_question:
            question_lines.append(line)
        else:
            criterion_lines.extend(pending_labels)
            pending_labels = []
            criterion_lines.append(line)
            if text:
                active_question = False
    criterion_lines.extend(pending_labels)
    if not has_question:
        return "", instructions
    # Every uncertain/non-question line remains a criterion. Unsupported prose
    # therefore requires administrator review instead of granting qualification.
    criteria = "\n".join(criterion_lines).strip()
    replacement = "## Qualification Criteria\n" + criteria + "\n"
    rewritten = instructions[:heading.start()] + replacement + instructions[body_end:]
    return "\n".join(question_lines).strip(), rewritten


def merge_playbook(apps, schema_editor):
    OrgInfo = apps.get_model("ai_engagement", "OrgInfo")
    for info in OrgInfo.objects.using(schema_editor.connection.alias).iterator():
        questions = info.qualification_requirements or ""
        instructions = info.engagement_instructions or ""
        if not questions:
            # Some legacy organizations placed their questions in Criteria.
            # Preserve all actual constraints, including those without the word
            # "qualify", while moving only recognizable question content.
            questions, instructions = _legacy_question_section(instructions)
        # Preserve both complete authored sources verbatim. Heading parsing is
        # handled at runtime; no migration guesses at or deletes business rules.
        info.ai_playbook = (
            f"## Rules\n{instructions}\n\n"
            "## Welcome Message\n\n"
            f"## Qualification Questions\n{questions}\n\n"
            "## Acknowledgment Message\n\n"
            "## Qualification Criteria\n\n"
            "## Stage shifting logic\n\n"
            "## Attribute mapping logic\n\n"
            "## Reminder creation logic\n"
        ) if questions or instructions else ""
        info.save(update_fields=["ai_playbook"])


def split_playbook(apps, schema_editor):
    OrgInfo = apps.get_model("ai_engagement", "OrgInfo")
    for info in OrgInfo.objects.using(schema_editor.connection.alias).iterator():
        info.engagement_instructions = info.ai_playbook
        question_section = re.search(r"## Qualification Questions\n(.*?)\n\n## Acknowledgment Message", info.ai_playbook, re.S)
        info.qualification_requirements = question_section.group(1) if question_section else ""
        info.save(update_fields=["engagement_instructions", "qualification_requirements"])


class Migration(migrations.Migration):
    dependencies = [("ai_engagement", "0017_leadsignal")]
    operations = [
        migrations.AddField(
            model_name="orginfo", name="ai_playbook",
            field=models.TextField(blank=True, help_text="The organization's complete AI operating specification: rules, messages, qualification, CRM routing, attributes and reminders."),
        ),
        migrations.RunPython(merge_playbook, split_playbook),
        migrations.RemoveField(model_name="orginfo", name="qualification_requirements"),
        migrations.RemoveField(model_name="orginfo", name="engagement_instructions"),
    ]
