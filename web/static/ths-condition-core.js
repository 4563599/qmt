(function(root, factory) {
    var api = factory();
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    } else {
        root.ThsConditionCore = api;
    }
}(typeof self !== 'undefined' ? self : this, function() {
    'use strict';

    var conditions = [
        { key: 'limit_up', label: '涨停' },
        { key: 'qiangchi', label: '5拉强吃' },
        { key: 'yi_yin_yi_yang', label: '5拉一阴一阳' },
        { key: 'is_quanyang_10d', label: '10日无阴线' },
        { key: 'yin_count_10d', label: '10日阴线次数', grouped: true },
        { key: 'yin_count_20', label: '20日阴线次数', grouped: true }
    ];

    function isTrue(value) {
        return ['1', 'true', 'yes', 'y'].includes(String(value).trim().toLowerCase());
    }

    function countValue(stock, field) {
        var raw = stock[field];
        if ((typeof raw !== 'number' && typeof raw !== 'string') || String(raw).trim() === '') return null;
        var value = Number(raw);
        return Number.isInteger(value) && value >= 0 ? value : null;
    }

    function matches(stock, condition) {
        if (condition.grouped) return countValue(stock, condition.key) !== null;
        if (condition.key === 'limit_up') {
            return isTrue(stock.ref_is_limit_up == null ? stock.is_limit_up : stock.ref_is_limit_up);
        }
        return isTrue(stock[condition.key]);
    }

    function defaultGain(stock) {
        return stock.today_change == null ? (stock.ref_gain == null ? 0 : stock.ref_gain) : stock.today_change;
    }

    function boardName(board) {
        return String(board.board_name || '未分类');
    }

    function build(sourceBoards, conditionKey, selectedName, getGain) {
        var condition = conditions.find(function(item) { return item.key === conditionKey; }) || conditions[0];
        var gain = getGain || defaultGain;
        function compareStocks(left, right) {
            var leftGain = Number(gain(left));
            var rightGain = Number(gain(right));
            return (Number.isFinite(rightGain) ? rightGain : 0) - (Number.isFinite(leftGain) ? leftGain : 0)
                || String(left.code || '').localeCompare(String(right.code || ''));
        }
        var boards = (Array.isArray(sourceBoards) ? sourceBoards : []).filter(function(board) {
            return board && typeof board === 'object';
        }).map(function(board) {
            var stocks = (Array.isArray(board.stocks) ? board.stocks : []).filter(function(stock) {
                return stock && typeof stock === 'object' && matches(stock, condition);
            }).sort(compareStocks);
            return Object.assign({}, board, { stocks: stocks });
        }).filter(function(board) { return board.stocks.length; });
        var uniqueCodes = new Set();
        boards.forEach(function(board) {
            board.stocks.forEach(function(stock) {
                if (stock.code) uniqueCodes.add(String(stock.code));
            });
        });
        if (condition.grouped) {
            var listed = new Map();
            var countBuckets = new Map();
            boards.forEach(function(board) {
                var name = boardName(board);
                board.stocks.forEach(function(stock) {
                    var identity = stock.code ? String(stock.code) : stock;
                    if (listed.has(identity)) {
                        var names = listed.get(identity).ths_board_names;
                        if (!names.includes(name)) names.push(name);
                        return;
                    }
                    var copy = Object.assign({}, stock, { ths_board_names: [name] });
                    listed.set(identity, copy);
                    var value = countValue(stock, condition.key);
                    if (!countBuckets.has(value)) countBuckets.set(value, []);
                    countBuckets.get(value).push(copy);
                });
            });
            var countGroups = Array.from(countBuckets.keys()).sort(function(left, right) {
                return left - right;
            }).map(function(value) { return { value: value, stocks: countBuckets.get(value) }; });
            var selectedGroup = countGroups.find(function(group) {
                return String(group.value) === String(selectedName);
            }) || countGroups[0];
            return {
                condition: condition,
                boards: boards,
                boardName: '',
                groupValue: selectedGroup ? selectedGroup.value : null,
                groups: countGroups,
                stocks: selectedGroup ? selectedGroup.stocks : [],
                uniqueCount: uniqueCodes.size
            };
        }
        var selected = boards.find(function(board) { return boardName(board) === selectedName; }) || boards[0];
        var buckets = new Map();
        (selected ? selected.stocks : []).forEach(function(stock) {
            var value = null;
            if (!buckets.has(value)) buckets.set(value, []);
            buckets.get(value).push(stock);
        });
        var groups = Array.from(buckets.keys()).sort(function(left, right) { return left - right; }).map(function(value) {
            return { value: value, stocks: buckets.get(value).sort(compareStocks) };
        });
        return {
            condition: condition,
            boards: boards,
            boardName: selected ? boardName(selected) : '',
            groups: groups,
            stocks: groups.reduce(function(items, group) { return items.concat(group.stocks); }, []),
            uniqueCount: uniqueCodes.size
        };
    }

    return { conditions: conditions, build: build };
}));
