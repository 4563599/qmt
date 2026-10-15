(function() {
    'use strict';

    var config = window.SPECIAL_FOCUS_CONFIG || {};
    var core = window.SpecialFocusCore;
    var DEFAULTS = { requestDays: 60, displayDays: 60 };
    var REFRESH_INTERVAL_MS = 3000;
    var DATE_CHECK_EVERY_CYCLES = 20;
    var CHART_THEME = {
        background: '#ffffff',
        grid: '#f0f0f0',
        yang: '#8b8b8b',
        yin: '#202020',
        ma5: '#929292'
    };
    var MA_PERIODS = [5];
    var groupMode = ['board', 'yin10', 'xiasha', 'yxcs5'].indexOf(config.groupMode) >= 0
        ? config.groupMode
        : 'yin';
    var yinGroupField = config.yinGroupField || 'yin_count_20';
    var yinGroupLabel = config.yinGroupLabel || '20日';
    var allItems = [];
    var boardGroups = [];
    var selectedGroupValue = '';
    var boardFilterMode = 'all';
    var refreshInFlight = false;
    var refreshTimer = null;
    var refreshCycle = 0;
    var latestDate = '';
    var openKlineCode = '';
    var openKlineAnchor = null;

    function byId(id) {
        return document.getElementById(id);
    }

    function escapeHtml(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function setStatus(message, isError) {
        var status = byId('specialFocusStatus');
        if (!status) return;
        status.textContent = message;
        status.classList.toggle('error', !!isError);
    }

    function setToolStatus(message, isError) {
        var status = byId('specialFocusToolStatus');
        if (!status) return;
        status.textContent = message || '';
        status.classList.toggle('error', !!isError);
    }

    function setControlsDisabled(disabled) {
        byId('specialFocusSearch').disabled = !!disabled;
        byId('specialFocusSearchButton').disabled = !!disabled;
        var purpleButton = byId('specialFocusPurpleButton');
        if (purpleButton) purpleButton.disabled = !!disabled;
    }

    async function fetchJson(url) {
        var response = await fetch(url, { cache: 'no-store' });
        var payload;
        try {
            payload = await response.json();
        } catch (error) {
            throw new Error('接口返回的不是有效JSON');
        }
        if (!response.ok) {
            throw new Error(payload && payload.error ? payload.error : ('HTTP ' + response.status));
        }
        return payload;
    }

    async function loadEncryptedPayload(date, days) {
        var fallbackUrl = groupMode === 'board'
            ? '/api/ths/klines'
            : (groupMode === 'xiasha' ? '/api/20xiasha/klines' : '/api/yxcs/klines');
        var url = (config.klinesUrl || fallbackUrl)
            + '?date=' + encodeURIComponent(date)
            + '&days=' + encodeURIComponent(days);
        var responsePayload = await fetchJson(url);
        var payload = config.encryptedPayload === false
            ? responsePayload
            : decryptEnvelope(responsePayload);
        if (!payload || payload.success !== true) {
            throw new Error(payload && payload.error ? payload.error : '接口数据格式无效');
        }
        return payload;
    }

    function decryptEnvelope(envelope) {
        if (!window.forge || !window.forge.cipher) {
            throw new Error('AES-GCM解密组件加载失败');
        }
        if (!config.aesKeyB64) {
            throw new Error('页面未配置KLINE_API_AES_KEY_B64');
        }
        if (!envelope || envelope.success !== true || envelope.encrypted !== true) {
            throw new Error(envelope && envelope.error ? envelope.error : '加密响应格式无效');
        }

        var key = forge.util.decode64(config.aesKeyB64);
        var nonce = forge.util.decode64(String(envelope.nonce || ''));
        var packed = forge.util.decode64(String(envelope.ciphertext || ''));
        if (key.length !== 32 || nonce.length !== 12 || packed.length <= 16) {
            throw new Error('AES-GCM密钥、Nonce或密文长度无效');
        }

        var ciphertext = packed.slice(0, -16);
        var tag = packed.slice(-16);
        var decipher = forge.cipher.createDecipher('AES-GCM', key);
        decipher.start({
            iv: nonce,
            additionalData: forge.util.encodeUtf8(config.aad || 'yxcs-klines:v1'),
            tagLength: 128,
            tag: forge.util.createBuffer(tag)
        });
        decipher.update(forge.util.createBuffer(ciphertext));
        if (!decipher.finish()) throw new Error('AES-GCM认证失败，密钥或AAD不匹配');
        return JSON.parse(forge.util.decodeUtf8(decipher.output.getBytes()));
    }

    function formatGain(value) {
        var number = core.toNumber(value, 0);
        return (number >= 0 ? '+' : '') + number.toFixed(2) + '%';
    }

    function alignedGainMarkup(value) {
        var gain = core.alignedGain(value);
        return ''
            + '<span class="special-focus-gain-sign' + (gain.hideSign ? ' is-placeholder' : '') + '">'
            + escapeHtml(gain.sign)
            + '</span>'
            + '<span class="special-focus-gain-value">' + escapeHtml(gain.value) + '</span>';
    }

    function excellentMarkup(item) {
        var details = core.excellentDetails(item, groupMode);
        var labels = details.labels || [];
        if (!labels.length) return '<div class="special-focus-excellent" aria-label="无极品条件"></div>';
        return ''
            + '<div class="special-focus-excellent' + (details.tone === 'purple' ? ' is-purple' : '') + '">'
            + labels.map(function(label) {
                return '<span>' + escapeHtml(label) + '</span>';
            }).join('')
            + '</div>';
    }

    function rowIdentity(item) {
        var fields = core.stockFields(item);
        return {
            code: String(item && item.code || fields.code || ''),
            name: String(item && item.name || fields.name || fields.stock_name || item && item.code || '-'),
            gain: core.displayGain(item)
        };
    }

    function itemCode(item) {
        var fields = core.stockFields(item);
        var raw = String(item && item.code || fields.code || fields.stock_code_with_market || '');
        var match = raw.match(/\d{6}/);
        return match ? match[0] : raw;
    }

    function itemMap(items) {
        var result = new Map();
        (items || []).forEach(function(item) {
            var code = itemCode(item);
            if (code) result.set(code, item);
        });
        return result;
    }

    function findItemByCode(code) {
        if (!code) return null;
        return allItems.find(function(item) {
            return itemCode(item) === code;
        }) || null;
    }

    function stockMarkup(item, identity, group) {
        var gainMarkup = groupMode === 'board' || groupMode === 'xiasha' || groupMode === 'yxcs5'
            ? alignedGainMarkup(identity.gain)
            : formatGain(identity.gain);
        var fields = core.stockFields(item);
        var xiashaMarkup = groupMode === 'xiasha'
            ? '<div class="special-focus-xiasha-sum">'
                + core.toNumber(fields.close_below_ma5_abs_pct_sum_20d, 0).toFixed(2)
                + '%</div>'
            : '';
        var yxcs5Markup = groupMode === 'yxcs5' && group
            ? '<div class="special-focus-yxcs5-metric">'
                + Math.trunc(core.toNumber(fields[group.metricField], 0))
                + '</div>'
            : '';
        return ''
            + '<div class="special-focus-stock">'
            + '<div class="special-focus-name" title="' + escapeHtml(identity.name) + '">' + escapeHtml(identity.name) + '</div>'
            + '<div class="special-focus-code">' + escapeHtml(identity.code) + '</div>'
            + '<div class="special-focus-gain' + (identity.gain < 0 ? ' negative' : '') + '">' + gainMarkup + '</div>'
            + (groupMode === 'board' || groupMode === 'xiasha' || groupMode === 'yin10'
                ? excellentMarkup(item)
                : '')
            + xiashaMarkup
            + yxcs5Markup
            + '</div>';
    }

    function visibleBoardGroups() {
        return boardFilterMode === 'purple'
            ? core.filterPurpleBoardGroups(boardGroups)
            : boardGroups;
    }

    function groupedYinStocks() {
        if (groupMode === 'xiasha') return core.groupXiashaStocks(allItems || []);
        if (groupMode === 'yxcs5') return core.groupYxcsFocusStocks(allItems || []);
        if (groupMode === 'yin10') {
            return core.groupStocksByField(allItems || [], 'yin_count_10d');
        }
        return core.groupStocksByField(allItems || [], yinGroupField);
    }

    function visibleGroups() {
        if (groupMode === 'board') return visibleBoardGroups();
        var groups = groupedYinStocks();
        return (groupMode === 'yin10' || groupMode === 'xiasha')
                && boardFilterMode === 'purple'
            ? core.filterPurpleGroups(groups)
            : groups;
    }

    function groupKey(group) {
        return group && group.value != null ? String(group.value) : '';
    }

    function selectedGroup() {
        var groups = visibleGroups();
        var selected = groups.find(function(group) {
            return groupKey(group) === selectedGroupValue;
        });
        if (selected) return selected;
        selectedGroupValue = groups.length ? groupKey(groups[0]) : '';
        return groups.length ? groups[0] : null;
    }

    function boardGainMarkup(value) {
        if (value == null || value === '') {
            return '<span class="special-focus-board-gain is-missing">--</span>';
        }
        return '<span class="special-focus-board-gain">' + alignedGainMarkup(value) + '</span>';
    }

    function captureListFocus(host) {
        var active = document.activeElement;
        if (!active || !host.contains(active) || typeof active.closest !== 'function') return null;
        var groupButton = active.closest('[data-group-value]');
        if (groupButton && host.contains(groupButton)) {
            return {
                attribute: 'data-group-value',
                value: groupButton.getAttribute('data-group-value')
            };
        }
        var stockRow = active.closest('[data-stock-code]');
        if (stockRow && host.contains(stockRow)) {
            return {
                attribute: 'data-stock-code',
                value: stockRow.getAttribute('data-stock-code')
            };
        }
        return null;
    }

    function restoreListFocus(host, focusState) {
        if (!focusState) return;
        var candidates = host.querySelectorAll('[' + focusState.attribute + ']');
        for (var index = 0; index < candidates.length; index += 1) {
            if (candidates[index].getAttribute(focusState.attribute) !== focusState.value) continue;
            candidates[index].focus({ preventScroll: true });
            return;
        }
    }

    function renderSplitLayout() {
        var host = byId('specialFocusGroups');
        var focusState = captureListFocus(host);
        var groups = visibleGroups();
        var group = selectedGroup();
        var isXiashaMode = groupMode === 'xiasha';
        var isYxcs5Mode = groupMode === 'yxcs5';
        var isYinMode = groupMode !== 'board' && !isXiashaMode && !isYxcs5Mode;
        var isYin10Mode = groupMode === 'yin10';
        if (!group) {
            host.innerHTML = '<div class="special-focus-empty">当前日期没有股票数据</div>';
            restoreListFocus(host, focusState);
            return;
        }

        var sidebarLabel = isXiashaMode
            ? '拉升分组'
            : (isYxcs5Mode
                ? '条件分组'
                : (isYinMode ? '阴线次数分组' : '板块排行'));
        var sidebar = groups.map(function(candidate) {
            var value = groupKey(candidate);
            var selected = value === groupKey(group);
            var detail = groupMode === 'board'
                ? boardGainMarkup(candidate.boardChangePercent)
                : '<span class="special-focus-group-total">' + candidate.items.length + '只</span>';
            return ''
                + '<button type="button" class="special-focus-board-button' + (selected ? ' is-active' : '') + '"'
                + ' data-group-value="' + escapeHtml(value) + '"'
                + ' aria-pressed="' + String(selected) + '">'
                + '<span class="special-focus-board-name">'
                + escapeHtml(isYinMode ? ('阴线 ' + value + ' 次') : value)
                + '</span>'
                + detail
                + '</button>';
        }).join('');

        var rows = group.items.map(function(item) {
            var identity = rowIdentity(item);
            return ''
                + '<article class="special-focus-row special-focus-board-row"'
                + ' data-stock-code="' + escapeHtml(itemCode(item)) + '"'
                + ' role="button" tabindex="0">'
                + stockMarkup(item, identity, group)
                + '</article>';
        }).join('');

        var heading = isXiashaMode
            ? groupKey(group)
            : (isYxcs5Mode
                ? groupKey(group)
                : (isYinMode
                ? ((isYin10Mode ? '10日' : yinGroupLabel) + '阴线次数 = ' + groupKey(group))
                : groupKey(group)));
        var columns = isXiashaMode
            ? '<span>股票</span><span>代码</span><span>涨跌幅</span><span>极品</span><span>20日下穿累计</span>'
            : (isYxcs5Mode
                ? '<span>股票</span><span>代码</span><span>涨跌幅</span><span>'
                    + escapeHtml(group.metricLabel) + '</span>'
                : (isYin10Mode
                ? '<span>股票</span><span>代码</span><span>涨跌幅</span><span>极品</span>'
                : (isYinMode
                ? '<span>股票</span><span>代码</span><span>涨跌幅</span>'
                : '<span>股票</span><span>代码</span><span>涨跌幅</span><span>极品</span>')));
        var layoutModeClass = isXiashaMode
            ? ' is-xiasha-mode'
            : (isYxcs5Mode
                ? ' is-yxcs5-mode'
                : (isYin10Mode
                ? ' is-yin-mode is-yin10-mode'
                : (isYinMode ? ' is-yin-mode' : '')));
        host.innerHTML = ''
            + '<div class="special-focus-board-layout' + layoutModeClass + '">'
            + '<nav class="special-focus-board-sidebar" aria-label="' + sidebarLabel + '">'
            + '<div class="special-focus-board-sidebar-title">' + sidebarLabel + '</div>'
            + sidebar
            + '</nav>'
            + '<section class="special-focus-board-main">'
            + '<div class="special-focus-board-heading">'
            + '<strong>' + escapeHtml(heading) + '</strong>'
            + (groupMode === 'board' ? boardGainMarkup(group.boardChangePercent) : '')
            + '<span class="special-focus-group-count">' + group.items.length + '只</span>'
            + '</div>'
            + '<div class="special-focus-board-columns">' + columns + '</div>'
            + rows
            + '</section>'
            + '</div>';
        restoreListFocus(host, focusState);
    }

    function chartLayout(chartData, width, height) {
        var rows = chartData.rows || [];
        var movingAverages = chartData.movingAverages || {};
        var values = [];
        rows.forEach(function(row) {
            values.push(Number(row.low), Number(row.high));
        });
        MA_PERIODS.forEach(function(period) {
            (movingAverages[String(period)] || []).forEach(function(value) {
                if (value != null && Number.isFinite(value)) values.push(value);
            });
        });
        var min = Math.min.apply(Math, values);
        var max = Math.max.apply(Math, values);
        if (!Number.isFinite(min) || !Number.isFinite(max)) return null;
        var span = Math.max(max - min, max * 0.01, 0.01);
        min -= span * 0.07;
        max += span * 0.07;
        var left = 8;
        var right = 8;
        var top = 7;
        var bottom = 14;
        var plotWidth = Math.max(1, width - left - right);
        var plotHeight = Math.max(1, height - top - bottom);
        var step = plotWidth / Math.max(rows.length, 1);
        function x(index) { return left + step * (index + 0.5); }
        function y(value) { return top + (max - Number(value)) / (max - min) * plotHeight; }
        return {
            width: width,
            height: height,
            left: left,
            right: right,
            top: top,
            bottom: bottom,
            step: step,
            halfWidth: Math.max(2.2, Math.min(6.5, step * 0.3)),
            x: x,
            y: y,
            movingAverages: movingAverages,
            min: min,
            max: max
        };
    }

    function lineCanvas(ctx, line) {
        ctx.beginPath();
        ctx.moveTo(line.x1, line.y1);
        ctx.lineTo(line.x2, line.y2);
        ctx.stroke();
    }

    function drawGridCanvas(ctx, layout) {
        ctx.save();
        ctx.strokeStyle = CHART_THEME.grid;
        ctx.lineWidth = 1;
        for (var i = 1; i <= 3; i++) {
            var y = layout.top + (layout.height - layout.top - layout.bottom) * i / 4;
            lineCanvas(ctx, { x1: layout.left, x2: layout.width - layout.right, y1: y, y2: y });
        }
        ctx.restore();
    }

    function drawMovingAveragesCanvas(ctx, layout) {
        ctx.save();
        ctx.strokeStyle = CHART_THEME.ma5;
        ctx.lineWidth = 1;
        ctx.setLineDash([5, 4]);
        ctx.beginPath();
        var started = false;
        (layout.movingAverages['5'] || []).forEach(function(value, index) {
            if (value == null) return;
            var x = layout.x(index);
            var y = layout.y(value);
            if (!started) {
                ctx.moveTo(x, y);
                started = true;
            } else {
                ctx.lineTo(x, y);
            }
        });
        if (started) ctx.stroke();
        ctx.restore();
    }

    function drawCanvas(node, chartData) {
        var rows = chartData.rows || [];
        var width = Math.max(280, Math.round(node.clientWidth));
        var height = Math.max(90, Math.round(node.clientHeight));
        var ratio = Math.max(1, window.devicePixelRatio || 1);
        var canvas = document.createElement('canvas');
        canvas.width = Math.round(width * ratio);
        canvas.height = Math.round(height * ratio);
        canvas.style.width = width + 'px';
        canvas.style.height = height + 'px';
        node.replaceChildren(canvas);
        var ctx = canvas.getContext('2d');
        ctx.scale(ratio, ratio);
        ctx.fillStyle = CHART_THEME.background;
        ctx.fillRect(0, 0, width, height);
        var layout = chartLayout(chartData, width, height);
        if (!layout) return;
        drawGridCanvas(ctx, layout);
        rows.forEach(function(row, index) {
            var glyph = core.createOhlcGlyph(
                layout.x(index), layout.y(row.open), layout.y(row.close),
                layout.y(row.high), layout.y(row.low), layout.halfWidth,
                Number(row.close) >= Number(row.open)
            );
            ctx.strokeStyle = glyph.tone === 'light' ? CHART_THEME.yang : CHART_THEME.yin;
            ctx.lineWidth = glyph.tone === 'light' ? 1 : 1.25;
            lineCanvas(ctx, glyph.upperWick);
            lineCanvas(ctx, glyph.lowerWick);
            lineCanvas(ctx, glyph.open);
            lineCanvas(ctx, glyph.close);
            lineCanvas(ctx, glyph.left);
            lineCanvas(ctx, glyph.right);
        });
        drawMovingAveragesCanvas(ctx, layout);
    }

    function svgLine(svg, line, color, width, dash) {
        var element = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        element.setAttribute('x1', line.x1);
        element.setAttribute('x2', line.x2);
        element.setAttribute('y1', line.y1);
        element.setAttribute('y2', line.y2);
        element.setAttribute('stroke', color);
        element.setAttribute('stroke-width', width);
        element.setAttribute('vector-effect', 'non-scaling-stroke');
        if (dash) element.setAttribute('stroke-dasharray', dash);
        svg.appendChild(element);
    }

    function drawSvg(node, chartData) {
        var rows = chartData.rows || [];
        var width = Math.max(280, Math.round(node.clientWidth));
        var height = Math.max(90, Math.round(node.clientHeight));
        var layout = chartLayout(chartData, width, height);
        var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
        svg.setAttribute('preserveAspectRatio', 'none');
        node.replaceChildren(svg);
        if (!layout) return;
        for (var i = 1; i <= 3; i++) {
            var gridY = layout.top + (height - layout.top - layout.bottom) * i / 4;
            svgLine(svg, { x1: layout.left, x2: width - layout.right, y1: gridY, y2: gridY }, CHART_THEME.grid, 1);
        }
        rows.forEach(function(row, index) {
            var glyph = core.createOhlcGlyph(
                layout.x(index), layout.y(row.open), layout.y(row.close),
                layout.y(row.high), layout.y(row.low), layout.halfWidth,
                Number(row.close) >= Number(row.open)
            );
            var color = glyph.tone === 'light' ? CHART_THEME.yang : CHART_THEME.yin;
            var lineWidth = glyph.tone === 'light' ? 1 : 1.25;
            svgLine(svg, glyph.upperWick, color, lineWidth);
            svgLine(svg, glyph.lowerWick, color, lineWidth);
            svgLine(svg, glyph.open, color, lineWidth);
            svgLine(svg, glyph.close, color, lineWidth);
            svgLine(svg, glyph.left, color, lineWidth);
            svgLine(svg, glyph.right, color, lineWidth);
        });
        var previous = null;
        (layout.movingAverages['5'] || []).forEach(function(value, index) {
            if (value == null) return;
            var point = { x: layout.x(index), y: layout.y(value) };
            if (previous) {
                svgLine(svg, { x1: previous.x, x2: point.x, y1: previous.y, y2: point.y }, CHART_THEME.ma5, 1, '5 4');
            }
            previous = point;
        });
    }

    function drawEcharts(node, chartData, record) {
        if (!window.echarts) throw new Error('ECharts组件加载失败');
        var rows = chartData.rows || [];
        var movingAverages = chartData.movingAverages || {};
        node.replaceChildren();
        var chartHost = document.createElement('div');
        chartHost.className = 'echarts-instance';
        node.appendChild(chartHost);
        var chart = echarts.init(chartHost, null, { renderer: 'canvas' });
        record.instance = chart;
        var dates = rows.map(function(row) { return String(row.trade_date || ''); });
        chart.setOption({
            animation: false,
            backgroundColor: CHART_THEME.background,
            grid: { left: 8, right: 8, top: 7, bottom: 14, containLabel: false },
            tooltip: { show: false },
            xAxis: {
                type: 'category', data: dates, boundaryGap: true,
                axisLine: { show: false }, axisTick: { show: false }, axisLabel: { show: false },
                splitLine: { show: false }
            },
            yAxis: {
                type: 'value', scale: true,
                axisLine: { show: false }, axisTick: { show: false }, axisLabel: { show: false },
                splitNumber: 4, splitLine: { show: true, lineStyle: { color: CHART_THEME.grid, width: 1 } }
            },
            series: [
                {
                    name: 'OHLC', type: 'custom', silent: true,
                    data: rows.map(function(row, index) {
                        return [index, Number(row.open), Number(row.close), Number(row.low), Number(row.high)];
                    }),
                    renderItem: function(params, api) {
                        var index = api.value(0);
                        var openPoint = api.coord([index, api.value(1)]);
                        var closePoint = api.coord([index, api.value(2)]);
                        var lowPoint = api.coord([index, api.value(3)]);
                        var highPoint = api.coord([index, api.value(4)]);
                        var halfWidth = Math.max(2.2, Math.min(6.5, api.size([1, 0])[0] * 0.3));
                        var glyph = core.createOhlcGlyph(
                            openPoint[0], openPoint[1], closePoint[1], highPoint[1], lowPoint[1],
                            halfWidth, api.value(2) >= api.value(1)
                        );
                        var color = glyph.tone === 'light' ? CHART_THEME.yang : CHART_THEME.yin;
                        var width = glyph.tone === 'light' ? 1 : 1.25;
                        return {
                            type: 'group',
                            children: [glyph.upperWick, glyph.lowerWick, glyph.open, glyph.close, glyph.left, glyph.right].map(function(line) {
                                return { type: 'line', shape: line, style: { stroke: color, lineWidth: width } };
                            })
                        };
                    }
                },
                { name: 'MA5', type: 'line', silent: true, showSymbol: false, data: movingAverages['5'] || [], connectNulls: false, lineStyle: { color: CHART_THEME.ma5, width: 1, type: 'dashed' } }
            ]
        }, true);
    }

    function renderChart(node, record) {
        if (!record) return;
        if (!record.chartData.rows.length) {
            node.innerHTML = '<div class="special-focus-chart-placeholder">暂无K线数据</div>';
            return;
        }
        try {
            if (config.renderer === 'echarts') drawEcharts(node, record.chartData, record);
            else if (config.renderer === 'svg') drawSvg(node, record.chartData);
            else drawCanvas(node, record.chartData);
        } catch (error) {
            if (record.instance) {
                try {
                    record.instance.dispose();
                } catch (disposeError) {
                    console.error('[special-focus][chart-dispose]', disposeError);
                }
                record.instance = null;
            }
            node.innerHTML = '<div class="special-focus-chart-placeholder">K线绘制失败</div>';
            console.error('[special-focus][chart]', error);
        }
    }

    function closeFloatingKline() {
        openKlineCode = '';
        openKlineAnchor = null;
        var tooltip = byId('specialFocusKlineTooltip');
        var imageNode = byId('specialFocusKlineImage');
        if (tooltip) tooltip.hidden = true;
        if (imageNode) imageNode.removeAttribute('src');
    }

    function positionFloatingKline() {
        var tooltip = byId('specialFocusKlineTooltip');
        if (!tooltip || tooltip.hidden || !openKlineAnchor) return;
        var rect = tooltip.getBoundingClientRect();
        var edgeMargin = isMobileKlineMode() ? 4 : 10;
        var cursorGap = isMobileKlineMode() ? 8 : 15;
        var point = core.floatingPanelPosition(
            openKlineAnchor.x,
            openKlineAnchor.y,
            rect.width,
            rect.height,
            window.innerWidth,
            window.innerHeight,
            edgeMargin,
            cursorGap
        );
        if (isMobileKlineMode()
                && Number.isFinite(openKlineAnchor.rowTop)
                && Number.isFinite(openKlineAnchor.rowBottom)) {
            var belowTop = openKlineAnchor.rowBottom + cursorGap;
            var aboveTop = openKlineAnchor.rowTop - cursorGap - rect.height;
            if (belowTop + rect.height <= window.innerHeight - edgeMargin) {
                point.top = Math.round(belowTop);
            } else if (aboveTop >= edgeMargin) {
                point.top = Math.round(aboveTop);
            }
        }
        tooltip.style.right = 'auto';
        tooltip.style.left = point.left + 'px';
        tooltip.style.top = point.top + 'px';
    }

    function renderImageKline(item) {
        if (!item || !openKlineCode || !openKlineAnchor) return;
        var tooltip = byId('specialFocusKlineTooltip');
        var imageNode = byId('specialFocusKlineImage');
        var identity = rowIdentity(item);
        var imageUrl = core.eastmoneyKlineUrl(itemCode(item));
        if (!tooltip || !imageNode || !imageUrl) {
            closeFloatingKline();
            return;
        }
        imageNode.alt = identity.name + ' 日K线图';
        imageNode.src = imageUrl;
        tooltip.hidden = false;
        positionFloatingKline();
    }

    function showFloatingKline(item, anchor) {
        var code = itemCode(item);
        if (!code) return;
        openKlineCode = code;
        openKlineAnchor = {
            x: Number(anchor && anchor.x) || 0,
            y: Number(anchor && anchor.y) || 0,
            rowTop: Number(anchor && anchor.rowTop),
            rowBottom: Number(anchor && anchor.rowBottom)
        };
        renderImageKline(item);
    }

    function isMobileKlineMode() {
        if (window.innerWidth <= 768) return true;
        return !!(window.matchMedia
            && window.matchMedia('(hover: none), (pointer: coarse)').matches);
    }

    function stockRowFromTarget(target, host) {
        var row = target && target.closest
            ? target.closest('[data-stock-code]')
            : null;
        return row && host.contains(row) ? row : null;
    }

    function syncPurpleFilterButton() {
        var button = byId('specialFocusPurpleButton');
        if (!button) return;
        var pressed = boardFilterMode === 'purple';
        button.setAttribute('aria-pressed', String(pressed));
    }

    function renderCurrentView() {
        renderSplitLayout();
        syncPurpleFilterButton();
    }

    function mergeRefreshPayload(payload, fullReload) {
        var previousByCode = itemMap(allItems);
        var historyLimit = config.requestDays || DEFAULTS.requestDays;

        if (groupMode === 'board') {
            if (!Array.isArray(payload.boards)) {
                throw new Error('板块数据格式无效');
            }
            var mergedBoards = payload.boards.map(function(board) {
                var stocks = Array.isArray(board.stocks) ? board.stocks : [];
                return Object.assign({}, board, {
                    stocks: stocks.map(function(item) {
                        var code = itemCode(item);
                        var previous = code ? previousByCode.get(code) : null;
                        return !fullReload && previous
                            ? core.mergeRefreshedItem(previous, item, historyLimit)
                            : item;
                    })
                });
            });
            boardGroups = core.groupBoardStocks(mergedBoards);
            allItems = core.uniqueBoardStocks(boardGroups);
        } else {
            if (!Array.isArray(payload.data)) {
                throw new Error('股票数据格式无效');
            }
            boardGroups = [];
            allItems = payload.data.map(function(item) {
                var code = itemCode(item);
                var previous = code ? previousByCode.get(code) : null;
                return !fullReload && previous
                    ? core.mergeRefreshedItem(previous, item, historyLimit)
                    : item;
            });
        }
        selectedGroup();
    }

    function renderPreservingPosition() {
        var pageScrollTop = window.scrollY;
        var sidebar = document.querySelector('.special-focus-board-sidebar');
        var sidebarScrollTop = sidebar ? sidebar.scrollTop : null;
        renderCurrentView();
        window.requestAnimationFrame(function() {
            window.scrollTo(0, pageScrollTop);
            var refreshedSidebar = document.querySelector('.special-focus-board-sidebar');
            if (refreshedSidebar && sidebarScrollTop != null) {
                refreshedSidebar.scrollTop = sidebarScrollTop;
            }
        });
    }

    function updateMeta(payload) {
        var updated = payload.kline_updated_at ? (' · K线更新 ' + payload.kline_updated_at) : '';
        var missing = Array.isArray(payload.missing_codes) && payload.missing_codes.length
            ? (' · 缺少K线 ' + payload.missing_codes.length + '只') : '';
        var totalCount = groupMode === 'board'
            ? (payload.unique_stock_count || allItems.length)
            : (payload.count || allItems.length);
        byId('specialFocusMeta').textContent = (payload.date || latestDate)
            + ' · ' + totalCount + '只' + updated + missing;
    }

    async function refreshLatestData() {
        if (document.hidden || refreshInFlight) return;
        refreshInFlight = true;
        try {
            refreshCycle += 1;
            var fullReload = false;
            if (refreshCycle % DATE_CHECK_EVERY_CYCLES === 0) {
                var datesPayload = await fetchJson(config.datesUrl || '/api/yxcs/dates');
                if (datesPayload.success === true
                        && Array.isArray(datesPayload.dates)
                        && datesPayload.dates.length
                        && datesPayload.dates[0] !== latestDate) {
                    latestDate = datesPayload.dates[0];
                    fullReload = true;
                }
            }
            var payload = fullReload
                ? await loadEncryptedPayload(
                    latestDate,
                    config.requestDays || DEFAULTS.requestDays
                )
                : await loadEncryptedPayload(latestDate, 1);
            mergeRefreshPayload(payload, fullReload);
            renderPreservingPosition();
            var refreshedOpenItem = findItemByCode(openKlineCode);
            if (refreshedOpenItem) renderImageKline(refreshedOpenItem);
            else if (openKlineCode) closeFloatingKline();
            updateMeta(payload);
        } catch (error) {
            console.warn('[special-focus][refresh]', error);
        } finally {
            refreshInFlight = false;
        }
    }

    function startRefreshTimer() {
        if (refreshTimer) clearInterval(refreshTimer);
        refreshTimer = setInterval(function() {
            refreshLatestData();
        }, REFRESH_INTERVAL_MS);
    }

    function handlePurpleFilter() {
        closeFloatingKline();
        boardFilterMode = boardFilterMode === 'purple' ? 'all' : 'purple';
        selectedGroup();
        renderCurrentView();
    }

    async function handleSearch() {
        var query = byId('specialFocusSearch').value.trim();
        if (!query) {
            setToolStatus('请输入股票名称、拼音或代码', true);
            return;
        }
        var matches = core.searchStocks(allItems, query);
        if (!matches.length) {
            setToolStatus('未找到股票', true);
            return;
        }
        var item = matches[0];
        var matchedCode = itemCode(item);
        var searchGroups = groupMode === 'board' ? boardGroups : groupedYinStocks();
        var matchedGroupValue = core.findGroupValueByCode(searchGroups, matchedCode);
        if (!matchedGroupValue) {
            setToolStatus('未找到股票分组', true);
            return;
        }
        if (groupMode !== 'yin' && boardFilterMode === 'purple') {
            var visibleMatchValue = core.findGroupValueByCode(visibleGroups(), matchedCode);
            if (visibleMatchValue !== matchedGroupValue) boardFilterMode = 'all';
        }
        closeFloatingKline();
        selectedGroupValue = matchedGroupValue;
        renderPreservingPosition();
        var buttonRect = byId('specialFocusSearchButton').getBoundingClientRect();
        showFloatingKline(item, {
            x: buttonRect.right,
            y: buttonRect.bottom
        });
        setToolStatus(matches.length > 1 ? ('找到' + matches.length + '只，显示第一只') : '', false);
    }

    function bindControls() {
        var search = byId('specialFocusSearch');
        var groupsHost = byId('specialFocusGroups');
        byId('specialFocusSearchButton').addEventListener('click', handleSearch);
        var purpleButton = byId('specialFocusPurpleButton');
        if (purpleButton) purpleButton.addEventListener('click', handlePurpleFilter);
        groupsHost.addEventListener('mouseover', function(event) {
            if (isMobileKlineMode()) return;
            var row = stockRowFromTarget(event.target, groupsHost);
            if (!row || row.contains(event.relatedTarget)) return;
            var item = findItemByCode(row.getAttribute('data-stock-code'));
            if (item) showFloatingKline(item, { x: event.clientX, y: event.clientY });
        });
        groupsHost.addEventListener('mousemove', function(event) {
            if (isMobileKlineMode()) return;
            var row = stockRowFromTarget(event.target, groupsHost);
            if (!row) return;
            var item = findItemByCode(row.getAttribute('data-stock-code'));
            if (!item) return;
            if (itemCode(item) !== openKlineCode) {
                showFloatingKline(item, { x: event.clientX, y: event.clientY });
                return;
            }
            openKlineAnchor = { x: event.clientX, y: event.clientY };
            positionFloatingKline();
        });
        groupsHost.addEventListener('mouseout', function(event) {
            if (isMobileKlineMode()) return;
            var row = stockRowFromTarget(event.target, groupsHost);
            if (!row || row.contains(event.relatedTarget)) return;
            if (row.getAttribute('data-stock-code') === openKlineCode) closeFloatingKline();
        });
        document.addEventListener('mousemove', function(event) {
            if (isMobileKlineMode() || !openKlineCode) return;
            var row = stockRowFromTarget(event.target, groupsHost);
            if (row && row.getAttribute('data-stock-code') === openKlineCode) return;
            closeFloatingKline();
        });
        groupsHost.addEventListener('click', function(event) {
            var target = event.target;
            var row = stockRowFromTarget(target, groupsHost);
            if (row) {
                if (!isMobileKlineMode()) return;
                event.stopPropagation();
                var item = findItemByCode(row.getAttribute('data-stock-code'));
                if (item) {
                    if (itemCode(item) === openKlineCode) {
                        closeFloatingKline();
                    } else {
                        var rowRect = row.getBoundingClientRect();
                        showFloatingKline(item, {
                            x: event.clientX,
                            y: rowRect.bottom,
                            rowTop: rowRect.top,
                            rowBottom: rowRect.bottom
                        });
                    }
                }
                return;
            }
            var button = target && target.closest
                ? target.closest('[data-group-value]')
                : null;
            if (!button || !groupsHost.contains(button)) return;
            var groupValue = button.getAttribute('data-group-value');
            closeFloatingKline();
            if (groupValue === selectedGroupValue) return;
            selectedGroupValue = groupValue;
            renderPreservingPosition();
        });
        groupsHost.addEventListener('keydown', function(event) {
            if (event.key !== 'Enter' && event.key !== ' ') return;
            var target = event.target;
            var row = target && target.closest
                ? target.closest('[data-stock-code]')
                : null;
            if (!row || !groupsHost.contains(row)) return;
            event.preventDefault();
            event.stopPropagation();
            var item = findItemByCode(row.getAttribute('data-stock-code'));
            if (!item) return;
            var rowRect = row.getBoundingClientRect();
            showFloatingKline(item, { x: rowRect.right, y: rowRect.bottom });
        });
        document.addEventListener('click', function(event) {
            var target = event.target;
            if (!target || !target.closest) return;
            if (target.closest('.special-focus-tools')
                    || target.closest('#specialFocusKlineTooltip')
                    || target.closest('[data-group-value]')
                    || target.closest('[data-stock-code]')) return;
            closeFloatingKline();
        });
        document.addEventListener('keydown', function(event) {
            if (event.key === 'Escape') closeFloatingKline();
        });
        var imageNode = byId('specialFocusKlineImage');
        if (imageNode) {
            imageNode.addEventListener('load', positionFloatingKline);
            imageNode.addEventListener('error', function() {
                imageNode.alt = 'K线图片加载失败';
                positionFloatingKline();
            });
        }
        search.addEventListener('keydown', function(event) {
            if (event.key !== 'Enter') return;
            event.preventDefault();
            handleSearch();
        });
    }

    var resizeTimer = null;
    window.addEventListener('resize', function() {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(function() {
            if (!openKlineCode) return;
            if (findItemByCode(openKlineCode)) positionFloatingKline();
            else closeFloatingKline();
        }, 120);
    });

    document.addEventListener('visibilitychange', function() {
        if (!document.hidden && latestDate) refreshLatestData();
    });

    window.addEventListener('beforeunload', function() {
        if (refreshTimer) clearInterval(refreshTimer);
    });

    async function initialize() {
        if (!core) throw new Error('特别关注数据核心加载失败');
        bindControls();
        setControlsDisabled(true);
        var datesPayload = await fetchJson(config.datesUrl || '/api/yxcs/dates');
        if (datesPayload.success !== true || !Array.isArray(datesPayload.dates) || !datesPayload.dates.length) {
            throw new Error(groupMode === 'board'
                ? '没有可用的同花顺清洗日期'
                : (groupMode === 'yin10'
                    ? '没有可用的5拉升日期'
                    : (groupMode === 'xiasha'
                        ? '没有可用的20日下穿MA5累计日期'
                        : '没有可用的20日阴线次数日期')));
        }
        latestDate = datesPayload.dates[0];
        byId('specialFocusMeta').textContent = latestDate + ' · 正在加载60根K线';
        var payload = await loadEncryptedPayload(
            latestDate,
            config.requestDays || DEFAULTS.requestDays
        );
        selectedGroupValue = '';
        mergeRefreshPayload(payload, true);
        renderCurrentView();
        updateMeta(payload);
        setStatus('', false);
        byId('specialFocusStatus').style.display = 'none';
        setControlsDisabled(false);
        startRefreshTimer();
    }

    initialize().catch(function(error) {
        console.error('[special-focus][init]', error);
        setStatus('加载失败：' + (error && error.message ? error.message : error), true);
    });
}());
