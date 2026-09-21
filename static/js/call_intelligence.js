document.addEventListener("DOMContentLoaded", function () {
    const form = document.querySelector("[data-call-settings]");
    if (!form) return;

    const pipeline = form.querySelector("[data-pipeline-select]");
    const stage = form.querySelector("[data-stage-select]");
    if (!pipeline || !stage) return;

    const syncStages = function () {
        const pipelineId = pipeline.value;
        Array.from(stage.options).forEach(function (option, index) {
            if (index === 0) {
                option.hidden = false;
                option.disabled = false;
                return;
            }
            const visible = !pipelineId || option.dataset.pipeline === pipelineId;
            option.hidden = !visible;
            option.disabled = !visible;
        });

        const selected = stage.options[stage.selectedIndex];
        if (selected && selected.disabled) {
            stage.value = "";
        }
    };

    pipeline.addEventListener("change", syncStages);
    syncStages();
});
