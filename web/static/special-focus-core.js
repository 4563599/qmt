(function(root, factory) {
    var api = factory();
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    } else {
        root.SpecialFocusCore = api;
    }
}(typeof self !== 'undefined' ? self : this, function() {
    'use strict';

    function toNumber(value, fallback) {
        var parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : (fallback == null ? 0 : fallback);
    }

    function stockFields(item) {
        return item && item.stock && typeof item.stock === 'object' ? item.stock : (item || {});
    }

    function sixDigitCode(value) {
        var match = String(value == null ? '' : value).match(/\d{6}/);
        return match ? match[0] : '';
    }

    function eastmoneyKlineUrl(value) {
        var code = sixDigitCode(value);
        if (!code) return '';
        var prefix = code.charAt(0) === '6' ? '1' : '0';
        return 'https://webquoteklinepic.eastmoney.com/GetPic.aspx?nid='
            + prefix + '.' + code
            + '&imageType=KXL'
            + '&token=44c9d251add88e27b65ed86506f6e5da'
            + '&at=1';
    }

    function findGroupValueByCode(groups, code) {
        var targetCode = sixDigitCode(code);
        if (!targetCode) return '';
        var matchedGroup = (groups || []).find(function(group) {
            return (group && group.items || []).some(function(item) {
                var fields = stockFields(item);
                return [item && item.code, fields.code, fields.stock_code_with_market]
                    .some(function(candidate) {
                        return sixDigitCode(candidate) === targetCode;
                    });
            });
        });
        return matchedGroup && matchedGroup.value != null ? String(matchedGroup.value) : '';
    }

    function floatingPanelPosition(
        anchorX,
        anchorY,
        panelWidth,
        panelHeight,
        viewportWidth,
        viewportHeight,
        margin,
        gap
    ) {
        var width = Math.max(0, toNumber(panelWidth, 0));
        var height = Math.max(0, toNumber(panelHeight, 0));
        var viewportW = Math.max(0, toNumber(viewportWidth, 0));
        var viewportH = Math.max(0, toNumber(viewportHeight, 0));
        var safeMargin = Math.max(0, toNumber(margin, 0));
        var safeGap = Math.max(0, toNumber(gap, 0));
        var anchorLeft = toNumber(anchorX, 0);
        var anchorTop = toNumber(anchorY, 0);
        var left = anchorLeft + safeGap;
        var top = anchorTop + safeGap;

        if (left + width > viewportW - safeMargin) {
            left = anchorLeft - safeGap - width;
        }
        if (top + height > viewportH - safeMargin) {
            top = anchorTop - safeGap - height;
        }

        var maxLeft = Math.max(safeMargin, viewportW - width - safeMargin);
        var maxTop = Math.max(safeMargin, viewportH - height - safeMargin);
        return {
            left: Math.round(Math.min(Math.max(left, safeMargin), maxLeft)),
            top: Math.round(Math.min(Math.max(top, safeMargin), maxTop))
        };
    }

    var PURPLE_REASON_LABELS = {
        quanyang_10d: '最近10日全阳',
        all_yin_no_touch: '10日所有阴线低不碰MA5',
        high_gain5_and_yin_pull: '5拉升阴前后拉',
        bare_foot_high_gain_low_above_ma5: '光脚拉升不碰',
        all_yin_up: '阴线都向上',
        all_yin_cross_star: '阴线都是十字星',
        is_quanyang_10d: '最近10日全阳',
        yin_count_20_lte_4: '20天阴线次数≤4',
        low_above_ma5_count_20d_gte_7: '20日低不碰次数≥7'
    };

    var RED_REASON_LABELS = {
        high_gain_10d_gte_5: '最近10日5拉升',
        all_yin_close_above_ma5: '阴线收盘都大于MA5',
        low_above_ma5_count_gte_4: '10日低不碰次数≥4',
        lower_shadow_high_gain_low_above_ma5: '光脚拉升不碰'
    };

    function mappedReasonLabels(reasons, labelsByReason) {
        var seen = new Set();
        return (reasons || []).map(function(reason) {
            return labelsByReason[String(reason || '')] || '';
        }).filter(function(label) {
            if (!label || seen.has(label)) return false;
            seen.add(label);
            return true;
        });
    }

    function excellentDetails(item, context) {
        var fields = stockFields(item);
        var groupColor = String(fields.group_color || '').toLowerCase();
        var isPurple = groupColor === 'purple' || toNumber(fields.purple_match, 0) === 1;
        var isRed = !isPurple && (
            groupColor === 'red' || toNumber(fields.red_match, 0) === 1
        );
        if (isPurple) {
            var purpleReasons = Array.isArray(fields.purple_reasons)
                ? fields.purple_reasons
                : [];
            var hasXiashaReason = purpleReasons.some(function(reason) {
                return reason === 'yin_count_20_lte_4'
                    || reason === 'low_above_ma5_count_20d_gte_7';
            });
            var purpleLabels = mappedReasonLabels(
                purpleReasons,
                PURPLE_REASON_LABELS
            );
            return {
                tone: 'purple',
                labels: context === 'yin10' || hasXiashaReason
                    ? purpleLabels
                    : ['20天阴线次数≤4'].concat(purpleLabels)
            };
        }
        if (isRed) {
            return {
                tone: 'default',
                labels: ['20天阴线次数5-6'].concat(
                    mappedReasonLabels(fields.red_reasons, RED_REASON_LABELS)
                )
            };
        }
        return { tone: 'default', labels: [] };
    }

    function alignedGain(value) {
        var numeric = toNumber(value, 0);
        var negative = numeric < 0;
        return {
            negative: negative,
            sign: negative ? '-' : '+',
            hideSign: !negative,
            value: Math.abs(numeric).toFixed(2) + '%'
        };
    }

    function mergeKlineRows(previousRows, refreshedRows, limit) {
        var rowsByDate = new Map();
        (previousRows || []).concat(refreshedRows || []).forEach(function(row) {
            var date = String(row && row.trade_date || '');
            if (date) rowsByDate.set(date, row);
        });
        return Array.from(rowsByDate.values()).sort(function(a, b) {
            return String(a.trade_date || '').localeCompare(String(b.trade_date || ''));
        }).slice(-Math.max(1, Math.trunc(toNumber(limit, 60))));
    }

    function mergeRefreshedItem(previous, refreshed, limit) {
        var oldItem = previous && typeof previous === 'object' ? previous : {};
        var newItem = refreshed && typeof refreshed === 'object' ? refreshed : {};
        var merged = Object.assign({}, oldItem, newItem);
        if (oldItem.stock || newItem.stock) {
            merged.stock = Object.assign({}, oldItem.stock || {}, newItem.stock || {});
        }
        merged.kline_data = mergeKlineRows(
            oldItem.kline_data || [],
            newItem.kline_data || [],
            limit
        );
        return merged;
    }

    function filterPurpleGroups(groups) {
        return (groups || []).map(function(group) {
            var items = (group.items || []).filter(function(item) {
                var fields = stockFields(item);
                return String(fields.group_color || '').toLowerCase() === 'purple'
                    || toNumber(fields.purple_match, 0) === 1;
            });
            return Object.assign({}, group, { items: items });
        }).filter(function(group) {
            return group.items.length > 0;
        });
    }

    function filterPurpleBoardGroups(groups) {
        return filterPurpleGroups(groups);
    }

    function displayGain(item) {
        var fields = stockFields(item);
        if (fields.today_change != null && fields.today_change !== '') {
            return toNumber(fields.today_change, 0);
        }
        if (item && item.today_change != null && item.today_change !== '') {
            return toNumber(item.today_change, 0);
        }
        return toNumber(fields.ref_gain != null ? fields.ref_gain : item && item.ref_gain, 0);
    }

    function groupStocksByField(items, fieldName) {
        var groups = new Map();
        (items || []).forEach(function(item) {
            var fields = stockFields(item);
            var value = Math.trunc(toNumber(fields[fieldName], 0));
            if (!groups.has(value)) groups.set(value, []);
            groups.get(value).push(item);
        });
        return Array.from(groups.entries())
            .sort(function(a, b) { return a[0] - b[0]; })
            .map(function(entry) {
                entry[1].sort(function(a, b) {
                    var gainDiff = displayGain(b) - displayGain(a);
                    if (gainDiff !== 0) return gainDiff;
                    return String(a && a.code || '').localeCompare(String(b && b.code || ''));
                });
                return { value: entry[0], items: entry[1] };
            });
    }

    function groupStocks(items) {
        return groupStocksByField(items, 'yin_count_20');
    }

    function groupXiashaStocks(items) {
        var groups = [
            { value: '拉升', items: [] },
            { value: '不拉升', items: [] }
        ];
        (items || []).forEach(function(item) {
            var fields = stockFields(item);
            var rawFlag = fields.is_high_gain;
            var normalizedFlag = String(rawFlag == null ? '' : rawFlag).toLowerCase();
            var isHighGain = rawFlag === true || rawFlag === 1
                || normalizedFlag === '1' || normalizedFlag === 'true';
            groups[isHighGain ? 0 : 1].items.push(item);
        });
        groups.forEach(function(group) {
            group.items.sort(function(a, b) {
                var aFields = stockFields(a);
                var bFields = stockFields(b);
                var distanceDiff = toNumber(
                    aFields.close_below_ma5_abs_pct_sum_20d, 0
                ) - toNumber(
                    bFields.close_below_ma5_abs_pct_sum_20d, 0
                );
                if (distanceDiff !== 0) return distanceDiff;
                var gainDiff = displayGain(b) - displayGain(a);
                if (gainDiff !== 0) return gainDiff;
                return String(a && a.code || aFields.code || '').localeCompare(
                    String(b && b.code || bFields.code || '')
                );
            });
        });
        return groups;
    }

    function truthyFlag(value) {
        var normalized = String(value == null ? '' : value).toLowerCase();
        return value === true || value === 1
            || normalized === '1' || normalized === 'true';
    }

    function groupYxcsFocusStocks(items) {
        var groups = [
            { value: '涨停20天阴线', metricField: 'yin_count_20', metricLabel: '20日阴线', sortAsc: true, items: [] },
            { value: '涨停20日不碰', metricField: 'low_above_ma5_count_20d', metricLabel: '20日不碰', items: [] },
            { value: '涨停10日不碰', metricField: 'low_above_ma5_count_10d', metricLabel: '10日不碰', items: [] },
            { value: '非涨停20天阴线', metricField: 'yin_count_20', metricLabel: '20日阴线', sortAsc: true, items: [] },
            { value: '非涨停拉升20日不碰', metricField: 'low_above_ma5_count_20d', metricLabel: '20日不碰', items: [] },
            { value: '非涨停拉升10日不碰', metricField: 'low_above_ma5_count_10d', metricLabel: '10日不碰', items: [] },
            { value: '光脚拉升不碰20日', metricField: 'low_above_ma5_count_20d', metricLabel: '20日不碰', items: [] },
            { value: '光脚拉升不碰10日', metricField: 'low_above_ma5_count_10d', metricLabel: '10日不碰', items: [] },
            { value: '10日低不碰>=6', metricField: 'low_above_ma5_count_10d', metricLabel: '10日不碰', items: [] },
            { value: '4不碰20天阴线', metricField: 'yin_count_20', metricLabel: '20日阴线', sortAsc: true, items: [] }
        ];

        (items || []).forEach(function(item) {
            var fields = stockFields(item);
            var hasSpecial5Marker = item && item.special5_member != null;
            var isSpecial5Member = !hasSpecial5Marker || truthyFlag(item.special5_member);
            var isFourAbove5Member = truthyFlag(item && item.fourabove5_member);
            var hasRefLimitFlag = fields.ref_is_limit_up != null
                && fields.ref_is_limit_up !== '';
            var isLimitUp = truthyFlag(
                hasRefLimitFlag ? fields.ref_is_limit_up : fields.is_limit_up
            );
            var isHighGain = truthyFlag(fields.is_high_gain);
            var isBareFootNoTouch = truthyFlag(fields.is_bare_foot_yang)
                && truthyFlag(fields.ref_low_above_ma5);

            if (isSpecial5Member) {
                if (isLimitUp) {
                    groups[0].items.push(item);
                    groups[1].items.push(item);
                    groups[2].items.push(item);
                }
                if (!isLimitUp && isHighGain) {
                    groups[3].items.push(item);
                    groups[4].items.push(item);
                    groups[5].items.push(item);
                }
                if (isBareFootNoTouch) {
                    groups[6].items.push(item);
                    groups[7].items.push(item);
                }
                if (toNumber(fields.low_above_ma5_count_10d, 0) >= 6) {
                    groups[8].items.push(item);
                }
            }
            if (isFourAbove5Member) {
                groups[9].items.push(item);
            }
        });

        groups.forEach(function(group) {
            group.items.sort(function(a, b) {
                var aFields = stockFields(a);
                var bFields = stockFields(b);
                var metricDiff = group.sortAsc
                    ? toNumber(aFields[group.metricField], 0) - toNumber(bFields[group.metricField], 0)
                    : toNumber(bFields[group.metricField], 0) - toNumber(aFields[group.metricField], 0);
                if (metricDiff !== 0) return metricDiff;
                var gainDiff = displayGain(b) - displayGain(a);
                if (gainDiff !== 0) return gainDiff;
                return String(a && a.code || aFields.code || '').localeCompare(
                    String(b && b.code || bFields.code || '')
                );
            });
        });
        return groups;
    }

    function groupBoardStocks(boards) {
        return (boards || []).map(function(board) {
            var value = String(board && board.board_name || '').trim() || '未分类';
            var rawBoardChange = board && board.board_change_percent;
            var hasBoardChange = rawBoardChange != null
                && rawBoardChange !== ''
                && Number.isFinite(Number(rawBoardChange));
            var boardChangePercent = hasBoardChange ? Number(rawBoardChange) : null;
            var items = Array.isArray(board && board.stocks) ? board.stocks.slice() : [];
            items.sort(function(a, b) {
                var gainDiff = displayGain(b) - displayGain(a);
                if (gainDiff !== 0) return gainDiff;
                return String(a && a.code || '').localeCompare(String(b && b.code || ''));
            });
            return {
                value: value,
                boardChangePercent: boardChangePercent,
                items: items
            };
        }).filter(function(group) {
            return group.items.length > 0;
        }).sort(function(a, b) {
            if (a.boardChangePercent == null && b.boardChangePercent != null) return 1;
            if (a.boardChangePercent != null && b.boardChangePercent == null) return -1;
            if (a.boardChangePercent != null && b.boardChangePercent != null) {
                var boardGainDiff = b.boardChangePercent - a.boardChangePercent;
                if (boardGainDiff !== 0) return boardGainDiff;
            }
            return a.value.localeCompare(b.value, 'zh-CN');
        });
    }

    function uniqueBoardStocks(groups) {
        var seen = new Set();
        var result = [];
        (groups || []).forEach(function(group) {
            (group.items || []).forEach(function(item) {
                var fields = stockFields(item);
                var code = String(
                    item && item.code || fields.code || fields.stock_code_with_market || ''
                );
                var key = code.replace(/\.(SH|SZ|BJ)$/i, '');
                if (!key || seen.has(key)) return;
                seen.add(key);
                result.push(item);
            });
        });
        return result;
    }

    function calculateMA(rows, period) {
        var size = Math.max(1, Math.trunc(toNumber(period, 1)));
        var windowSum = 0;
        return (rows || []).map(function(row, index) {
            windowSum += toNumber(row && row.close, 0);
            if (index >= size) windowSum -= toNumber(rows[index - size] && rows[index - size].close, 0);
            return index < size - 1 ? null : windowSum / size;
        });
    }

    function calculateMA5(rows) {
        return calculateMA(rows, 5);
    }

    function createOhlcGlyph(x, openY, closeY, highY, lowY, halfWidth, isYang) {
        var bodyTop = Math.min(openY, closeY);
        var bodyBottom = Math.max(openY, closeY);
        return {
            upperWick: { x1: x, x2: x, y1: highY, y2: bodyTop },
            lowerWick: { x1: x, x2: x, y1: bodyBottom, y2: lowY },
            open: { x1: x - halfWidth, x2: x + halfWidth, y1: openY, y2: openY },
            close: { x1: x - halfWidth, x2: x + halfWidth, y1: closeY, y2: closeY },
            left: { x1: x - halfWidth, x2: x - halfWidth, y1: openY, y2: closeY },
            right: { x1: x + halfWidth, x2: x + halfWidth, y1: openY, y2: closeY },
            tone: isYang ? 'light' : 'dark'
        };
    }

    var PINYIN_BOUNDARIES = [
        ['a', '阿'], ['b', '八'], ['c', '嚓'], ['d', '咑'], ['e', '妸'],
        ['f', '发'], ['g', '旮'], ['h', '铪'], ['j', '丌'], ['k', '咔'],
        ['l', '垃'], ['m', '妈'], ['n', '拿'], ['o', '噢'], ['p', '帊'],
        ['q', '七'], ['r', '呥'], ['s', '仨'], ['t', '他'], ['w', '屲'],
        ['x', '夕'], ['y', '丫'], ['z', '帀']
    ];

    function normalizeSearchTerm(value) {
        return String(value == null ? '' : value).trim().toLowerCase();
    }

    function pinyinInitials(value) {
        var collator = new Intl.Collator('zh-Hans-u-co-pinyin');
        return Array.from(String(value == null ? '' : value)).map(function(character) {
            if (/^[a-z0-9]$/i.test(character)) return character.toLowerCase();
            var initial = '';
            PINYIN_BOUNDARIES.some(function(entry) {
                if (collator.compare(character, entry[1]) >= 0) {
                    initial = entry[0];
                    return false;
                }
                return true;
            });
            return initial;
        }).join('');
    }

    function searchStocks(items, query) {
        var term = normalizeSearchTerm(query);
        if (!term) return [];
        return (items || []).map(function(item, index) {
            var fields = stockFields(item);
            var name = String(item && item.name || fields.name || fields.stock_name || '');
            var codes = [item && item.code, fields.code, fields.stock_code_with_market]
                .map(normalizeSearchTerm)
                .filter(Boolean);
            var bareTerm = term.replace(/\.(sh|sz|bj)$/i, '');
            var codeMatched = codes.some(function(code) {
                return code === term || code.replace(/\.(sh|sz|bj)$/i, '') === bareTerm;
            });
            var initials = pinyinInitials(name);
            var normalizedName = normalizeSearchTerm(name);
            var score = normalizedName === term ? 0
                : (codeMatched ? 1
                : (initials === term ? 2
                : (normalizedName.indexOf(term) >= 0 ? 3
                : (initials.indexOf(term) >= 0 ? 4 : -1))));
            return { item: item, index: index, score: score };
        }).filter(function(entry) {
            return entry.score >= 0;
        }).sort(function(a, b) {
            return a.score - b.score || a.index - b.index;
        }).map(function(entry) {
            return entry.item;
        });
    }

    function normalizeKlineRows(rows, days) {
        var limit = Math.max(1, Math.min(120, toNumber(days, 30)));
        return (rows || []).filter(function(row) {
            if (!row) return false;
            var open = Number(row.open);
            var high = Number(row.high);
            var low = Number(row.low);
            var close = Number(row.close);
            return [open, high, low, close].every(function(value) {
                return Number.isFinite(value) && value > 0;
            }) && low <= Math.min(open, close) && high >= Math.max(open, close);
        }).sort(function(a, b) {
            return String(a.trade_date || '').localeCompare(String(b.trade_date || ''));
        }).slice(-limit);
    }

    function buildChartData(rows, displayDays, periods) {
        var normalized = normalizeKlineRows(rows, 120);
        var visibleCount = Math.max(1, Math.min(120, Math.trunc(toNumber(displayDays, 30))));
        var startIndex = Math.max(0, normalized.length - visibleCount);
        var movingAverages = {};
        (periods || []).forEach(function(period) {
            var size = Math.max(1, Math.trunc(toNumber(period, 1)));
            movingAverages[String(size)] = calculateMA(normalized, size).slice(startIndex);
        });
        return {
            rows: normalized.slice(startIndex),
            movingAverages: movingAverages
        };
    }

    return {
        toNumber: toNumber,
        stockFields: stockFields,
        eastmoneyKlineUrl: eastmoneyKlineUrl,
        findGroupValueByCode: findGroupValueByCode,
        floatingPanelPosition: floatingPanelPosition,
        excellentDetails: excellentDetails,
        alignedGain: alignedGain,
        mergeKlineRows: mergeKlineRows,
        mergeRefreshedItem: mergeRefreshedItem,
        filterPurpleGroups: filterPurpleGroups,
        filterPurpleBoardGroups: filterPurpleBoardGroups,
        displayGain: displayGain,
        groupStocksByField: groupStocksByField,
        groupStocks: groupStocks,
        groupXiashaStocks: groupXiashaStocks,
        groupYxcsFocusStocks: groupYxcsFocusStocks,
        groupBoardStocks: groupBoardStocks,
        uniqueBoardStocks: uniqueBoardStocks,
        calculateMA: calculateMA,
        calculateMA5: calculateMA5,
        createOhlcGlyph: createOhlcGlyph,
        normalizeSearchTerm: normalizeSearchTerm,
        pinyinInitials: pinyinInitials,
        searchStocks: searchStocks,
        normalizeKlineRows: normalizeKlineRows,
        buildChartData: buildChartData
    };
}));
