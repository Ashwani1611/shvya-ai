import json

from django.db import transaction
from django.db.models import Max
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from apps.crm.decorators import crm_login_required
from apps.crm.models import Lead, Stage
from apps.crm.views.api import get_user_pipelines


STAGE_DESCRIPTION_MAX_LENGTH = 1200


def _allowed_pipelines(user):
    return (
        get_user_pipelines(user)
        .filter(
            organization=user.organization,
            is_active=True,
        )
    )


def _get_pipeline(user, pipeline_id):
    return get_object_or_404(
        _allowed_pipelines(user),
        id=pipeline_id,
    )


def _get_stage(user, stage_id):
    allowed = _allowed_pipelines(user)
    return get_object_or_404(
        Stage.objects.select_related("pipeline"),
        id=stage_id,
        pipeline__in=allowed,
        is_active=True,
    )


def _normalize_active_stage(pipeline, active_stage_id):
    value = str(active_stage_id or "").strip()
    if value and Stage.objects.filter(
        id=value,
        pipeline=pipeline,
        is_active=True,
    ).exists():
        return value

    first_stage_id = (
        Stage.objects
        .filter(
            pipeline=pipeline,
            is_active=True,
        )
        .order_by("display_order", "name")
        .values_list("id", flat=True)
        .first()
    )
    return str(first_stage_id) if first_stage_id else ""


def _modal_context(*, pipeline, active_stage_id, error=""):
    stages = (
        Stage.objects
        .filter(
            pipeline=pipeline,
            is_active=True,
        )
        .order_by("display_order", "name")
    )
    return {
        "pipeline": pipeline,
        "stages": stages,
        "stage_count": stages.count(),
        "active_stage_id": _normalize_active_stage(
            pipeline,
            active_stage_id,
        ),
        "stage_editor_error": error,
        "description_max_length": STAGE_DESCRIPTION_MAX_LENGTH,
    }


def _render_modal(
    request,
    *,
    pipeline,
    active_stage_id,
    error="",
    changed=False,
):
    response = render(
        request,
        "crm/partials/stage_editor_modal.html",
        _modal_context(
            pipeline=pipeline,
            active_stage_id=active_stage_id,
            error=error,
        ),
    )

    if changed:
        response["HX-Trigger"] = json.dumps(
            {
                "stageEditorChanged": {
                    "pipeline_id": str(pipeline.id),
                    "active_stage_id": _normalize_active_stage(
                        pipeline,
                        active_stage_id,
                    ),
                }
            }
        )

    return response


def _clean_name(value):
    name = str(value or "").strip()
    if not name:
        raise ValueError("Stage name is required.")
    if len(name) > 100:
        raise ValueError("Stage name must be 100 characters or fewer.")
    return name


def _clean_description(value):
    description = str(value or "").strip()
    if len(description) > STAGE_DESCRIPTION_MAX_LENGTH:
        raise ValueError(
            f"Stage description must be {STAGE_DESCRIPTION_MAX_LENGTH} characters or fewer."
        )
    return description


@crm_login_required
@require_GET
def stage_editor_modal(request):
    user = request.crm_user
    pipeline_id = request.GET.get("pipeline", "").strip()
    active_stage_id = request.GET.get("active_stage", "").strip()

    if not pipeline_id:
        return HttpResponse("Pipeline is required.", status=400)

    pipeline = _get_pipeline(user, pipeline_id)
    return _render_modal(
        request,
        pipeline=pipeline,
        active_stage_id=active_stage_id,
    )


@crm_login_required
@require_POST
def stage_editor_create(request):
    user = request.crm_user
    pipeline_id = request.POST.get("pipeline", "").strip()
    active_stage_id = request.POST.get("active_stage", "").strip()

    if not pipeline_id:
        return HttpResponse("Pipeline is required.", status=400)

    pipeline = _get_pipeline(user, pipeline_id)

    try:
        name = _clean_name(request.POST.get("name", ""))
        description = _clean_description(request.POST.get("description", ""))
    except ValueError as exc:
        return _render_modal(
            request,
            pipeline=pipeline,
            active_stage_id=active_stage_id,
            error=str(exc),
        )

    if Stage.objects.filter(
        pipeline=pipeline,
        is_active=True,
        name__iexact=name,
    ).exists():
        return _render_modal(
            request,
            pipeline=pipeline,
            active_stage_id=active_stage_id,
            error="A stage with this name already exists.",
        )

    max_order = (
        Stage.objects
        .filter(pipeline=pipeline)
        .aggregate(max_order=Max("display_order"))
        .get("max_order")
    )

    with transaction.atomic():
        Stage.objects.create(
            pipeline=pipeline,
            name=name,
            description=description,
            display_order=(max_order or 0) + 1,
            is_active=True,
            ai_on=True,
        )

    return _render_modal(
        request,
        pipeline=pipeline,
        active_stage_id=active_stage_id,
        changed=True,
    )


@crm_login_required
@require_POST
def stage_editor_update(request, stage_id):
    user = request.crm_user
    stage = _get_stage(user, stage_id)

    try:
        name = _clean_name(request.POST.get("name", stage.name))
        description = _clean_description(request.POST.get("description", ""))
    except ValueError as exc:
        return HttpResponse(str(exc), status=400)

    if stage.is_system_locked and name.casefold() != stage.name.casefold():
        return HttpResponse(
            f"{stage.name} is a required SHVYA system stage and cannot be renamed.",
            status=409,
        )

    if not stage.is_system_locked and Stage.objects.filter(
        pipeline=stage.pipeline,
        is_active=True,
        name__iexact=name,
    ).exclude(id=stage.id).exists():
        return HttpResponse(
            "A stage with this name already exists.",
            status=400,
        )

    update_fields = ["description", "updated_at"]

    with transaction.atomic():
        if not stage.is_system_locked:
            stage.name = name
            update_fields.insert(0, "name")
        stage.description = description
        stage.save(update_fields=update_fields)

    response = HttpResponse(status=204)
    response["HX-Trigger"] = json.dumps(
        {
            "stageEditorChanged": {
                "pipeline_id": str(stage.pipeline_id),
                "active_stage_id": request.POST.get("active_stage", "").strip(),
            },
            "stageEditorSaved": {
                "stage_id": str(stage.id),
            },
        }
    )
    return response


@crm_login_required
@require_POST
def stage_editor_ai_toggle(request, stage_id):
    user = request.crm_user
    stage = _get_stage(user, stage_id)
    active_stage_id = request.POST.get("active_stage", "").strip()

    stage.ai_on = not stage.ai_on
    stage.save(update_fields=["ai_on", "updated_at"])

    return _render_modal(
        request,
        pipeline=stage.pipeline,
        active_stage_id=active_stage_id,
        changed=True,
    )


@crm_login_required
@require_POST
def stage_editor_delete(request, stage_id):
    user = request.crm_user
    stage = _get_stage(user, stage_id)
    pipeline = stage.pipeline
    active_stage_id = request.POST.get("active_stage", "").strip()

    if stage.is_system_locked:
        return _render_modal(
            request,
            pipeline=pipeline,
            active_stage_id=active_stage_id,
            error=f"{stage.name} is a required SHVYA system stage and cannot be deleted.",
        )

    if Lead.objects.filter(
        organization=user.organization,
        pipeline=pipeline,
        stage=stage,
    ).exists():
        return _render_modal(
            request,
            pipeline=pipeline,
            active_stage_id=active_stage_id,
            error=(
                "This stage still contains leads. Move those leads to another stage "
                "before deleting it."
            ),
        )

    stage.is_active = False
    stage.save(update_fields=["is_active", "updated_at"])

    if active_stage_id == str(stage.id):
        active_stage_id = ""

    return _render_modal(
        request,
        pipeline=pipeline,
        active_stage_id=active_stage_id,
        changed=True,
    )
