(function () {
    "use strict";

    function textOf(element) {
        return (element && element.textContent ? element.textContent : "")
            .replace(/\s+/g, " ")
            .trim();
    }

    function findCardByHeading(label) {
        const headings = document.querySelectorAll(".sa-content h2");
        for (const heading of headings) {
            if (textOf(heading) === label) {
                return heading.closest(".bg-white.border.rounded-lg, section");
            }
        }
        return null;
    }

    function injectLayoutFixes() {
        const style = document.createElement("style");
        style.id = "sa-org-detail-layout-fixes";
        style.textContent = `
            .sa-org-account-card > div[hidden] {
                display: none !important;
            }

            .sa-org-account-card {
                margin: 0 0 16px !important;
            }

            .sa-org-account-card > div:first-child {
                min-height: 70px;
            }

            .sa-org-workspace-grid {
                grid-template-columns: minmax(0, 1.35fr) minmax(320px, .65fr) !important;
                gap: 16px !important;
                margin-bottom: 16px !important;
            }

            .sa-org-workspace-main,
            .sa-org-workspace-side {
                gap: 14px !important;
            }

            .sa-control-center-stacked {
                display: grid !important;
                grid-template-columns: minmax(0, 1fr) !important;
                gap: 16px !important;
            }

            .sa-control-center-stacked > .sa-premium-card {
                width: 100%;
                min-width: 0;
            }

            .sa-control-center-stacked .overflow-x-auto {
                max-width: 100%;
            }

            .sa-control-center-stacked table {
                width: 100%;
            }

            .sa-channel-activity {
                min-width: 220px !important;
            }

            .sa-channel-pipeline-list {
                display: grid;
                gap: 7px;
            }

            .sa-channel-pipeline-row {
                display: flex;
                align-items: center;
                flex-wrap: wrap;
                gap: 6px;
            }

            .sa-channel-pipeline-name {
                color: #667085;
                font-size: 10px;
                font-weight: 750;
                line-height: 1.25;
            }

            .sa-channel-pipeline-phone {
                color: #98a2b3;
                font-size: 9px;
                line-height: 1.25;
            }

            .sa-channel-pipeline-separator {
                color: #c7cfdb;
                font-size: 9px;
            }

            .sa-channel-pipeline-list .sa-channel-status {
                padding: 4px 8px;
                font-size: 10px;
            }

            @media (max-width: 1100px) {
                .sa-org-workspace-grid {
                    grid-template-columns: minmax(0, 1fr) !important;
                }
            }
        `;
        document.head.appendChild(style);
    }

    function makeAccountCardActuallyCollapsible() {
        const accountCard = findCardByHeading("Account Information");
        if (!accountCard) return;

        const body = accountCard.lastElementChild;
        if (body && body !== accountCard.firstElementChild) {
            body.hidden = true;
        }

        // The first V2 pass put this card into the right workspace column. Move it
        // back below the compact workspace so the short left column does not leave
        // a large empty vertical area before Users and Pipelines.
        const workspace = document.getElementById("workspace");
        if (workspace && accountCard.closest(".sa-org-workspace-grid") === workspace) {
            workspace.insertAdjacentElement("afterend", accountCard);
        }
    }

    function stackManagementCards() {
        const usersCard = findCardByHeading("Organization Users");
        const pipelinesCard = findCardByHeading("Pipeline Settings");
        if (!usersCard || !pipelinesCard || usersCard.parentElement !== pipelinesCard.parentElement) {
            return;
        }

        usersCard.parentElement.classList.add("sa-control-center-stacked");
        usersCard.classList.add("sa-management-card");
        pipelinesCard.classList.add("sa-management-card");
    }

    function pipelinePhoneMap() {
        const map = new Map();
        const card = findCardByHeading("Pipeline Settings");
        const table = card ? card.querySelector("table") : null;
        if (!table) return map;

        table.querySelectorAll("tbody tr").forEach(function (row) {
            const cells = row.querySelectorAll("td");
            if (cells.length < 3) return;
            const pipeline = textOf(cells[0]);
            if (!pipeline) return;
            const country = textOf(cells[1]);
            const phone = textOf(cells[2]);
            map.set(pipeline, [country, phone].filter(Boolean).join(" "));
        });
        return map;
    }

    function parsePipelineChannels(rawLabel) {
        return String(rawLabel || "")
            .split(" • ")
            .map(function (segment) {
                const clean = segment.trim();
                if (!clean) return null;

                const separatorIndex = clean.indexOf(" — ");
                let pipeline = "";
                let channel = clean;
                if (separatorIndex >= 0) {
                    pipeline = clean.slice(0, separatorIndex).trim();
                    channel = clean.slice(separatorIndex + 3).trim();
                }

                const inactive = /\sinactive$/i.test(channel);
                const active = !inactive && /\sactive$/i.test(channel);
                channel = channel.replace(/\s+(active|inactive)$/i, "").trim();

                return {
                    pipeline: pipeline,
                    channel: channel || "Not connected",
                    active: active,
                };
            })
            .filter(Boolean);
    }

    function renderPipelineChannelCell(cell, channels, phones, fallbackDetail, inactiveSince) {
        cell.innerHTML = "";
        cell.classList.add("sa-channel-activity");

        const list = document.createElement("div");
        list.className = "sa-channel-pipeline-list";

        channels.forEach(function (item) {
            const row = document.createElement("div");
            row.className = "sa-channel-pipeline-row";

            if (item.pipeline) {
                const pipeline = document.createElement("span");
                pipeline.className = "sa-channel-pipeline-name";
                pipeline.textContent = item.pipeline;
                row.appendChild(pipeline);

                const separator = document.createElement("span");
                separator.className = "sa-channel-pipeline-separator";
                separator.textContent = "•";
                row.appendChild(separator);
            }

            const badge = document.createElement("span");
            badge.className = "sa-channel-status " + (item.active ? "is-active" : "is-inactive");
            badge.textContent = item.channel;
            row.appendChild(badge);

            const phoneValue = item.pipeline ? phones.get(item.pipeline) : "";
            if (phoneValue) {
                const phone = document.createElement("span");
                phone.className = "sa-channel-pipeline-phone";
                phone.textContent = phoneValue;
                row.appendChild(phone);
            }

            list.appendChild(row);
        });

        if (!channels.length) {
            const fallback = document.createElement("div");
            fallback.className = "sa-channel-detail";
            fallback.textContent = fallbackDetail || "No WhatsApp channel linked";
            list.appendChild(fallback);
        } else if (channels.length === 1 && !channels[0].pipeline && fallbackDetail) {
            const detail = document.createElement("div");
            detail.className = "sa-channel-detail";
            detail.textContent = inactiveSince
                ? "Inactive since " + inactiveSince
                : fallbackDetail;
            list.appendChild(detail);
        }

        cell.appendChild(list);
    }

    function fixWhatsAppActivity() {
        const data = document.getElementById("sa-org-workspace-data");
        const usersCard = findCardByHeading("Organization Users");
        const table = usersCard ? usersCard.querySelector("table") : null;
        if (!data || !table) return;

        const headers = Array.from(table.querySelectorAll("thead th"));
        const index = headers.findIndex(function (header) {
            const label = textOf(header);
            return label === "WhatsApp Activity" || label === "Last Login";
        });
        if (index < 0) return;

        headers[index].textContent = "WhatsApp Activity";
        const channels = parsePipelineChannels(data.dataset.channelLabel);
        const phones = pipelinePhoneMap();

        table.querySelectorAll("tbody tr").forEach(function (row) {
            const cells = row.querySelectorAll("td");
            if (!cells[index]) return;
            renderPipelineChannelCell(
                cells[index],
                channels,
                phones,
                data.dataset.channelDetail || "",
                data.dataset.channelSince || ""
            );
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        if (!document.getElementById("sa-org-workspace-data")) return;
        injectLayoutFixes();
        makeAccountCardActuallyCollapsible();
        stackManagementCards();
        fixWhatsAppActivity();
    });
})();
