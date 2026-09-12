(function (window, document) {
    'use strict';

    const SVG_NS = 'http://www.w3.org/2000/svg';

    function number(value) {
        const parsed = Number(value || 0);
        return Number.isFinite(parsed) ? parsed : 0;
    }

    function formatNumber(value) {
        try {
            return new Intl.NumberFormat('en-IN').format(number(value));
        } catch (error) {
            return String(number(value));
        }
    }

    function insertStyles() {
        if (document.getElementById('shvya-insights-fallback-styles')) return;
        const style = document.createElement('style');
        style.id = 'shvya-insights-fallback-styles';
        style.textContent = [
            '.ins-fallback-surface{position:relative;min-height:300px;width:100%;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",sans-serif;color:#6e6e73}',
            '.ins-fallback-surface svg{display:block;width:100%;height:300px;overflow:visible}',
            '.ins-fallback-legend{display:flex;align-items:center;justify-content:center;gap:16px;flex-wrap:wrap;margin:0 12px 6px;font-size:11px;color:#6e6e73}',
            '.ins-fallback-legend-item{display:inline-flex;align-items:center;gap:6px;white-space:nowrap}',
            '.ins-fallback-legend-swatch{width:26px;height:9px;border:2px solid currentColor;background:rgba(255,255,255,.88);border-radius:2px}',
            '.ins-fallback-tooltip{position:absolute;z-index:20;display:none;pointer-events:none;min-width:178px;max-width:290px;padding:10px 11px;border:1px solid rgba(255,255,255,.14);border-radius:9px;background:rgba(29,29,31,.95);box-shadow:0 12px 32px rgba(0,0,0,.18);color:#fff;font-size:11px;line-height:1.45;transform:translateY(-100%)}',
            '.ins-fallback-tooltip-title{margin-bottom:6px;font-size:12px;font-weight:650}',
            '.ins-fallback-tooltip-row{display:flex;align-items:center;gap:7px;margin:2px 0}',
            '.ins-fallback-tooltip-dot{width:8px;height:8px;flex:0 0 8px;border-radius:2px}',
            '.ins-fallback-empty{display:flex;align-items:center;justify-content:center;min-height:285px;color:#6e6e73;font-size:12.5px}',
            '.ins-fallback-pie-layout{display:grid;grid-template-columns:minmax(250px,1fr) minmax(180px,.65fr);align-items:center;min-height:300px}',
            '.ins-fallback-pie-legend{display:flex;flex-direction:column;gap:8px;padding:16px 16px 16px 0;font-size:11px}',
            '@media(max-width:720px){.ins-fallback-pie-layout{grid-template-columns:1fr}.ins-fallback-pie-legend{padding:0 16px 16px;flex-direction:row;flex-wrap:wrap}.ins-fallback-surface svg{height:280px}}'
        ].join('');
        document.head.appendChild(style);
    }

    function svgElement(name, attributes) {
        const node = document.createElementNS(SVG_NS, name);
        Object.keys(attributes || {}).forEach(function (key) {
            node.setAttribute(key, String(attributes[key]));
        });
        return node;
    }

    function textNode(svg, x, y, text, attributes) {
        const node = svgElement('text', Object.assign({
            x: x,
            y: y,
            fill: '#6e6e73',
            'font-size': '10.5',
            'font-family': '-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",sans-serif'
        }, attributes || {}));
        node.textContent = text;
        svg.appendChild(node);
        return node;
    }

    function prepareSurface(canvas) {
        if (!canvas) return null;
        canvas.hidden = true;
        const wrap = canvas.closest('.ins-chart-wrap');
        if (!wrap) return null;
        let surface = wrap.querySelector('.ins-fallback-surface');
        if (!surface) {
            surface = document.createElement('div');
            surface.className = 'ins-fallback-surface';
            wrap.appendChild(surface);
        }
        surface.innerHTML = '';
        return surface;
    }

    function showEmpty(canvasId, message) {
        const canvas = document.getElementById(canvasId);
        const surface = prepareSurface(canvas);
        if (!surface) return;
        const empty = document.createElement('div');
        empty.className = 'ins-fallback-empty';
        empty.textContent = message;
        surface.appendChild(empty);
    }

    function appendSummary(canvasId, text) {
        if (!text) return;
        const canvas = document.getElementById(canvasId);
        if (!canvas) return;
        const wrap = canvas.closest('.ins-chart-wrap');
        if (!wrap) return;
        let summary = wrap.nextElementSibling;
        if (!summary || !summary.classList.contains('ins-chart-summary')) {
            summary = document.createElement('div');
            summary.className = 'ins-chart-summary';
            wrap.insertAdjacentElement('afterend', summary);
        }
        summary.textContent = text;
    }

    function makeTooltip(surface) {
        const tooltip = document.createElement('div');
        tooltip.className = 'ins-fallback-tooltip';
        surface.appendChild(tooltip);
        return tooltip;
    }

    function fillTooltip(tooltip, title, rows) {
        tooltip.innerHTML = '';
        const titleNode = document.createElement('div');
        titleNode.className = 'ins-fallback-tooltip-title';
        titleNode.textContent = title || '';
        tooltip.appendChild(titleNode);
        rows.forEach(function (row) {
            const line = document.createElement('div');
            line.className = 'ins-fallback-tooltip-row';
            const dot = document.createElement('span');
            dot.className = 'ins-fallback-tooltip-dot';
            dot.style.backgroundColor = row.color;
            const value = document.createElement('span');
            value.textContent = row.label + ': ' + formatNumber(row.value);
            line.appendChild(dot);
            line.appendChild(value);
            tooltip.appendChild(line);
        });
    }

    function positionTooltip(surface, tooltip, clientX, clientY) {
        const rect = surface.getBoundingClientRect();
        const width = tooltip.offsetWidth || 210;
        let left = clientX - rect.left + 12;
        if (left + width > rect.width - 8) left = clientX - rect.left - width - 12;
        left = Math.max(8, left);
        const top = Math.max(60, clientY - rect.top - 10);
        tooltip.style.left = left + 'px';
        tooltip.style.top = top + 'px';
        tooltip.style.display = 'block';
    }

    function niceMax(maxValue) {
        if (maxValue <= 0) return 5;
        const exponent = Math.floor(Math.log10(maxValue));
        const magnitude = Math.pow(10, exponent);
        const normalized = maxValue / magnitude;
        let nice;
        if (normalized <= 1) nice = 1;
        else if (normalized <= 2) nice = 2;
        else if (normalized <= 5) nice = 5;
        else nice = 10;
        return Math.max(1, nice * magnitude);
    }

    function normalizeLineSeries(series, palette, addTotal, totalLabel) {
        const normalized = (series || []).map(function (item, index) {
            return {
                label: item.label || item.key || 'Series',
                values: (item.values || []).map(number),
                color: palette[index % palette.length],
                total: false
            };
        });
        if (addTotal && normalized.length) {
            const length = normalized.reduce(function (max, item) {
                return Math.max(max, item.values.length);
            }, 0);
            const totals = Array.from({ length: length }, function (_, index) {
                return normalized.reduce(function (sum, item) {
                    return sum + number(item.values[index]);
                }, 0);
            });
            normalized.push({
                label: totalLabel || 'Total',
                values: totals,
                color: '#1d1d1f',
                total: true
            });
        }
        return normalized;
    }

    function renderLegend(surface, series) {
        const legend = document.createElement('div');
        legend.className = 'ins-fallback-legend';
        series.forEach(function (item) {
            const entry = document.createElement('span');
            entry.className = 'ins-fallback-legend-item';
            const swatch = document.createElement('span');
            swatch.className = 'ins-fallback-legend-swatch';
            swatch.style.color = item.color;
            swatch.style.borderStyle = item.total ? 'dashed' : 'solid';
            const label = document.createElement('span');
            label.textContent = item.label;
            entry.appendChild(swatch);
            entry.appendChild(label);
            legend.appendChild(entry);
        });
        surface.appendChild(legend);
    }

    function renderLineChart(options) {
        const canvas = document.getElementById(options.canvasId);
        const surface = prepareSurface(canvas);
        if (!surface) return;
        const labels = options.labels || [];
        const series = normalizeLineSeries(options.series, options.palette, options.addTotal, options.totalLabel);
        const hasActivity = series.some(function (item) {
            return item.values.some(function (value) { return value !== 0; });
        });
        if (!labels.length || !series.length || !hasActivity) {
            showEmpty(options.canvasId, options.emptyText || 'No activity in the selected period.');
            if (options.summary) appendSummary(options.canvasId, options.summary(series));
            return;
        }

        renderLegend(surface, series);
        const width = 1200;
        const height = 286;
        const margin = { left: 66, right: 24, top: 12, bottom: 48 };
        const chartWidth = width - margin.left - margin.right;
        const chartHeight = height - margin.top - margin.bottom;
        const maxValue = series.reduce(function (max, item) {
            return Math.max(max, item.values.reduce(function (itemMax, value) { return Math.max(itemMax, value); }, 0));
        }, 0);
        const yMax = niceMax(maxValue * 1.08);
        const svg = svgElement('svg', { viewBox: '0 0 ' + width + ' ' + height, preserveAspectRatio: 'none', role: 'img' });
        surface.appendChild(svg);

        const yTicks = 5;
        for (let tick = 0; tick <= yTicks; tick += 1) {
            const value = (yMax / yTicks) * tick;
            const y = margin.top + chartHeight - (chartHeight * tick / yTicks);
            svg.appendChild(svgElement('line', {
                x1: margin.left, y1: y, x2: width - margin.right, y2: y,
                stroke: 'rgba(15,23,42,.10)', 'stroke-width': 1
            }));
            textNode(svg, margin.left - 10, y + 4, formatNumber(Math.round(value)), { 'text-anchor': 'end' });
        }

        const count = Math.max(labels.length, 1);
        const stepX = count > 1 ? chartWidth / (count - 1) : chartWidth;
        const maxXTicks = 16;
        const tickEvery = Math.max(1, Math.ceil(count / maxXTicks));
        labels.forEach(function (label, index) {
            const x = margin.left + (count > 1 ? index * stepX : chartWidth / 2);
            if (index % tickEvery === 0 || index === count - 1) {
                svg.appendChild(svgElement('line', {
                    x1: x, y1: margin.top, x2: x, y2: margin.top + chartHeight,
                    stroke: 'rgba(15,23,42,.075)', 'stroke-width': 1
                }));
                textNode(svg, x, margin.top + chartHeight + 22, label, {
                    'text-anchor': 'end', transform: 'rotate(-36 ' + x + ' ' + (margin.top + chartHeight + 22) + ')'
                });
            }
        });
        textNode(svg, width / 2, height - 4, 'Date', { 'text-anchor': 'middle', 'font-size': '11' });
        const yTitle = textNode(svg, 14, margin.top + chartHeight / 2, options.yTitle || 'Number of Messages', {
            'text-anchor': 'middle', 'font-size': '11'
        });
        yTitle.setAttribute('transform', 'rotate(-90 14 ' + (margin.top + chartHeight / 2) + ')');

        series.forEach(function (item) {
            const points = item.values.map(function (value, index) {
                const x = margin.left + (count > 1 ? index * stepX : chartWidth / 2);
                const y = margin.top + chartHeight - (number(value) / yMax) * chartHeight;
                return [x, y];
            });
            const path = svgElement('path', {
                d: points.map(function (point, index) { return (index ? 'L' : 'M') + point[0] + ' ' + point[1]; }).join(' '),
                fill: 'none', stroke: item.color, 'stroke-width': item.total ? 2.8 : 2.5,
                'stroke-linejoin': 'round', 'stroke-linecap': 'round'
            });
            if (item.total) path.setAttribute('stroke-dasharray', '5 5');
            svg.appendChild(path);
            points.forEach(function (point) {
                svg.appendChild(svgElement('circle', {
                    cx: point[0], cy: point[1], r: 3,
                    fill: '#fff', stroke: item.color, 'stroke-width': 2
                }));
            });
        });

        const guide = svgElement('line', {
            x1: margin.left, y1: margin.top, x2: margin.left, y2: margin.top + chartHeight,
            stroke: 'rgba(29,29,31,.22)', 'stroke-width': 1, 'stroke-dasharray': '3 3', visibility: 'hidden'
        });
        svg.appendChild(guide);
        const overlay = svgElement('rect', {
            x: margin.left, y: margin.top, width: chartWidth, height: chartHeight,
            fill: 'transparent', 'pointer-events': 'all'
        });
        svg.appendChild(overlay);
        const tooltip = makeTooltip(surface);
        overlay.addEventListener('mousemove', function (event) {
            const rect = svg.getBoundingClientRect();
            const svgX = ((event.clientX - rect.left) / rect.width) * width;
            let index = count > 1 ? Math.round((svgX - margin.left) / stepX) : 0;
            index = Math.max(0, Math.min(count - 1, index));
            const x = margin.left + (count > 1 ? index * stepX : chartWidth / 2);
            guide.setAttribute('x1', x);
            guide.setAttribute('x2', x);
            guide.setAttribute('visibility', 'visible');
            fillTooltip(tooltip, labels[index] || '', series.map(function (item) {
                return { label: item.label, value: item.values[index] || 0, color: item.color };
            }));
            positionTooltip(surface, tooltip, event.clientX, event.clientY);
        });
        overlay.addEventListener('mouseleave', function () {
            guide.setAttribute('visibility', 'hidden');
            tooltip.style.display = 'none';
        });
        if (options.summary) appendSummary(options.canvasId, options.summary(series));
    }

    function renderBarChart(canvasId, rows, palette) {
        const canvas = document.getElementById(canvasId);
        const surface = prepareSurface(canvas);
        if (!surface) return;
        const data = (rows || []).map(function (row) {
            return { label: row.label || 'Unassigned', value: number(row.count) };
        });
        if (!data.length || !data.some(function (row) { return row.value !== 0; })) {
            showEmpty(canvasId, 'No pipeline lead data in the selected period.');
            return;
        }
        const width = 900;
        const height = 286;
        const margin = { left: 58, right: 20, top: 18, bottom: 62 };
        const chartWidth = width - margin.left - margin.right;
        const chartHeight = height - margin.top - margin.bottom;
        const maxValue = data.reduce(function (max, row) { return Math.max(max, row.value); }, 0);
        const yMax = niceMax(maxValue * 1.08);
        const svg = svgElement('svg', { viewBox: '0 0 ' + width + ' ' + height, preserveAspectRatio: 'none' });
        surface.appendChild(svg);
        for (let tick = 0; tick <= 5; tick += 1) {
            const y = margin.top + chartHeight - chartHeight * tick / 5;
            svg.appendChild(svgElement('line', { x1: margin.left, y1: y, x2: width - margin.right, y2: y, stroke: 'rgba(15,23,42,.10)' }));
            textNode(svg, margin.left - 8, y + 4, formatNumber(Math.round(yMax * tick / 5)), { 'text-anchor': 'end' });
        }
        const slot = chartWidth / data.length;
        const barWidth = Math.min(48, Math.max(14, slot * .58));
        const tooltip = makeTooltip(surface);
        data.forEach(function (row, index) {
            const barHeight = row.value / yMax * chartHeight;
            const x = margin.left + index * slot + (slot - barWidth) / 2;
            const y = margin.top + chartHeight - barHeight;
            const bar = svgElement('rect', {
                x: x, y: y, width: barWidth, height: Math.max(1, barHeight), rx: 7,
                fill: 'rgba(59,156,232,.20)', stroke: palette[2] || '#3b9ce8', 'stroke-width': 2
            });
            svg.appendChild(bar);
            const label = String(row.label);
            textNode(svg, x + barWidth / 2, margin.top + chartHeight + 20, label.length > 16 ? label.slice(0, 15) + '…' : label, {
                'text-anchor': 'middle', 'font-size': '10'
            });
            bar.style.cursor = 'pointer';
            bar.addEventListener('mousemove', function (event) {
                fillTooltip(tooltip, row.label, [{ label: 'Leads', value: row.value, color: palette[2] || '#3b9ce8' }]);
                positionTooltip(surface, tooltip, event.clientX, event.clientY);
            });
            bar.addEventListener('mouseleave', function () { tooltip.style.display = 'none'; });
        });
        textNode(svg, width / 2, height - 4, 'Pipeline', { 'text-anchor': 'middle', 'font-size': '11' });
        const yTitle = textNode(svg, 14, margin.top + chartHeight / 2, 'Number of Leads', { 'text-anchor': 'middle', 'font-size': '11' });
        yTitle.setAttribute('transform', 'rotate(-90 14 ' + (margin.top + chartHeight / 2) + ')');
    }

    function renderDoughnutChart(canvasId, rows, palette) {
        const canvas = document.getElementById(canvasId);
        const surface = prepareSurface(canvas);
        if (!surface) return;
        const data = (rows || []).map(function (row, index) {
            return { label: row.label || 'No Stage', value: number(row.count), color: palette[index % palette.length] };
        }).filter(function (row) { return row.value > 0; });
        const total = data.reduce(function (sum, row) { return sum + row.value; }, 0);
        if (!data.length || !total) {
            showEmpty(canvasId, 'No stage lead data in the selected period.');
            return;
        }
        const layout = document.createElement('div');
        layout.className = 'ins-fallback-pie-layout';
        surface.appendChild(layout);
        const svg = svgElement('svg', { viewBox: '0 0 420 286', preserveAspectRatio: 'xMidYMid meet' });
        layout.appendChild(svg);
        const cx = 210;
        const cy = 132;
        const radius = 84;
        const strokeWidth = 38;
        const circumference = 2 * Math.PI * radius;
        let offset = 0;
        const tooltip = makeTooltip(surface);
        data.forEach(function (row) {
            const length = row.value / total * circumference;
            const circle = svgElement('circle', {
                cx: cx, cy: cy, r: radius, fill: 'none', stroke: row.color,
                'stroke-width': strokeWidth, 'stroke-dasharray': length + ' ' + (circumference - length),
                'stroke-dashoffset': -offset, transform: 'rotate(-90 ' + cx + ' ' + cy + ')',
                'stroke-linecap': 'butt', 'pointer-events': 'stroke'
            });
            circle.style.cursor = 'pointer';
            circle.addEventListener('mousemove', function (event) {
                const percent = ((row.value / total) * 100).toFixed(1);
                fillTooltip(tooltip, row.label, [{ label: 'Leads (' + percent + '%)', value: row.value, color: row.color }]);
                positionTooltip(surface, tooltip, event.clientX, event.clientY);
            });
            circle.addEventListener('mouseleave', function () { tooltip.style.display = 'none'; });
            svg.appendChild(circle);
            offset += length;
        });
        textNode(svg, cx, cy - 2, formatNumber(total), { 'text-anchor': 'middle', fill: '#1d1d1f', 'font-size': '24', 'font-weight': '700' });
        textNode(svg, cx, cy + 20, 'Total leads', { 'text-anchor': 'middle', 'font-size': '11' });
        const legend = document.createElement('div');
        legend.className = 'ins-fallback-pie-legend';
        data.forEach(function (row) {
            const item = document.createElement('div');
            item.className = 'ins-fallback-legend-item';
            const dot = document.createElement('span');
            dot.className = 'ins-fallback-tooltip-dot';
            dot.style.backgroundColor = row.color;
            const label = document.createElement('span');
            label.textContent = row.label + ' · ' + formatNumber(row.value);
            item.appendChild(dot);
            item.appendChild(label);
            legend.appendChild(item);
        });
        layout.appendChild(legend);
    }

    function sumSeries(series, label) {
        const target = (series || []).find(function (item) { return item.label === label; });
        return target ? target.values.reduce(function (sum, value) { return sum + number(value); }, 0) : 0;
    }

    window.ShvyaInsightsFallback = {
        render: function (payload) {
            insertStyles();
            const palette = payload.palette || ['#38bdb8', '#ff9138', '#3b9ce8', '#9162f5', '#ff5579', '#f6b83f', '#63c76a'];
            renderLineChart({
                canvasId: 'leadsOverTimeChart', labels: payload.labelsLeads, series: payload.seriesLeads,
                palette: palette, addTotal: true, totalLabel: 'Total Leads', yTitle: 'Number of Leads',
                emptyText: 'No leads were created in the selected period.',
                summary: function (series) { return 'Total Leads: ' + formatNumber(sumSeries(series, 'Total Leads')); }
            });
            renderLineChart({
                canvasId: 'aiWelcomeChart', labels: payload.labelsAI, series: payload.seriesAI,
                palette: palette, yTitle: 'Number of Messages',
                emptyText: 'No AI or welcome messages were sent in the selected period.',
                summary: function (series) {
                    return 'Total AI Messages Sent: ' + formatNumber(sumSeries(series, 'Total AI Messages')) +
                        '  |  Bump Up Messages: ' + formatNumber(sumSeries(series, 'Bump Up Messages')) +
                        '  |  Welcome Messages: ' + formatNumber(sumSeries(series, 'Welcome Messages'));
                }
            });
            renderLineChart({
                canvasId: 'emailAutomationChart', labels: payload.labelsEmail, series: payload.seriesEmail,
                palette: palette, addTotal: true, totalLabel: 'Total Email Messages', yTitle: 'Number of Messages',
                emptyText: 'No email automation messages were sent in the selected period.',
                summary: function (series) { return 'Total Email Messages Sent: ' + formatNumber(sumSeries(series, 'Total Email Messages')); }
            });
            renderLineChart({
                canvasId: 'whatsappAutomationChart', labels: payload.labelsWhatsapp, series: payload.seriesWhatsapp,
                palette: palette, addTotal: true, totalLabel: 'Total WhatsApp Messages', yTitle: 'Number of Messages',
                emptyText: 'No WhatsApp automation messages were sent in the selected period.',
                summary: function (series) { return 'Total WhatsApp Messages Sent: ' + formatNumber(sumSeries(series, 'Total WhatsApp Messages')); }
            });
            renderBarChart('leadsByPipelineChart', payload.pipelines, palette);
            renderDoughnutChart('leadsByStageChart', payload.stages, palette);
        }
    };
})(window, document);
