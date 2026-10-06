(function () {
    "use strict";

    const pipeline = document.getElementById("id_pipeline");
    const stage = document.getElementById("id_stage");
    const data = document.getElementById("indiamart-stage-options");
    const help = document.getElementById("id_stage_help");
    if (!pipeline || !stage || !data) return;

    const stages = JSON.parse(data.textContent);

    function updateStages() {
        const selectedStage = stage.value;
        const options = stages.filter(function (item) {
            return item.pipeline === pipeline.value;
        });
        let placeholder = "Select a stage";
        if (!pipeline.value) placeholder = "Choose a pipeline first";
        else if (!options.length) placeholder = "No active stages in this pipeline";

        stage.replaceChildren(new Option(placeholder, ""));
        options.forEach(function (item) {
            stage.add(new Option(item.name, item.id));
        });
        stage.value = options.some(function (item) {
            return item.id === selectedStage;
        }) ? selectedStage : "";
        stage.disabled = !options.length;
        if (help) {
            help.textContent = !pipeline.value
                ? "Select a pipeline to see its stages."
                : options.length
                    ? "New leads will enter this stage."
                    : "Add an active stage to this pipeline before generating a URL.";
        }
    }

    pipeline.addEventListener("change", updateStages);
    window.addEventListener("pageshow", updateStages);
    updateStages();
})();
