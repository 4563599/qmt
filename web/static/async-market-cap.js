(function() {
    let observer = null;
    let state = null;

    function getTodayKey() {
        const now = new Date();
        const year = now.getFullYear();
        const month = String(now.getMonth() + 1).padStart(2, '0');
        const day = String(now.getDate()).padStart(2, '0');
        return `${year}-${month}-${day}`;
    }

    function buildCacheKey(cacheNamespace) {
        return `asyncMarketCap:${cacheNamespace}:${getTodayKey()}`;
    }

    function loadCache(cacheKey) {
        try {
            const raw = localStorage.getItem(cacheKey);
            if (!raw) return {};
            const parsed = JSON.parse(raw);
            return parsed && typeof parsed === 'object' ? parsed : {};
        } catch (error) {
            return {};
        }
    }

    function saveCache() {
        if (!state) return;
        try {
            localStorage.setItem(state.cacheKey, JSON.stringify(state.cache));
        } catch (error) {
        }
    }

    function formatMarketCap(el, value) {
        const n = Number(value);
        const emptyText = el.dataset.marketCapEmpty || '-';
        if (!Number.isFinite(n) || n <= 0) return emptyText;
        const decimals = Number(el.dataset.marketCapDecimals || 2);
        const unit = el.dataset.marketCapUnit !== undefined ? el.dataset.marketCapUnit : '亿';
        return `${n.toFixed(Number.isFinite(decimals) ? decimals : 2)}${unit}`;
    }

    function applyCachedValue(el, value) {
        el.textContent = formatMarketCap(el, value);
        el.dataset.marketCapResolved = '1';
    }

    function applyMarketCap(code, value) {
        const n = Number(value);
        if (!Number.isFinite(n) || n <= 0) return;
        state.cache[code] = n;
        saveCache();
        syncStores(code, n);
        document.querySelectorAll(`${state.selector}[data-market-cap-code="${code}"]`).forEach(function(el) {
            applyCachedValue(el, n);
        });
        document.dispatchEvent(new CustomEvent('async-market-cap:updated', {
            detail: { code: code, marketCap: n }
        }));
        if (typeof state.onUpdate === 'function') {
            try {
                state.onUpdate({ code: code, marketCap: n });
            } catch (error) {
            }
        }
    }

    function syncStores(code, marketCap) {
        if (!state || !Array.isArray(state.dataStores)) return;
        state.dataStores.forEach(function(getter) {
            try {
                const root = typeof getter === 'function' ? getter() : getter;
                syncValue(root, code, marketCap, new WeakSet());
            } catch (error) {
            }
        });
    }

    function syncValue(value, code, marketCap, seen) {
        if (!value || (typeof value !== 'object' && !Array.isArray(value))) return;
        if (typeof value === 'object') {
            if (seen.has(value)) return;
            seen.add(value);
        }
        if (Array.isArray(value)) {
            value.forEach(function(item) {
                syncValue(item, code, marketCap, seen);
            });
            return;
        }
        if (String(value.code || '') === code) {
            value.market_cap = marketCap;
        }
        Object.keys(value).forEach(function(key) {
            syncValue(value[key], code, marketCap, seen);
        });
    }

    function collectElement(el) {
        if (!el || !el.dataset) return;
        const code = String(el.dataset.marketCapCode || '').trim();
        if (!code) return;
        const cachedValue = Number(state.cache[code]);
        if (Number.isFinite(cachedValue) && cachedValue > 0) {
            applyCachedValue(el, cachedValue);
        }
        if (state.queued.has(code)) return;
        state.queued.add(code);
        state.queue.push(code);
        pumpQueue();
    }

    function scanNode(node) {
        if (!node || node.nodeType !== 1) return;
        if (node.matches && node.matches(state.selector)) {
            collectElement(node);
        }
        if (!node.querySelectorAll) return;
        node.querySelectorAll(state.selector).forEach(collectElement);
    }

    async function pumpQueue() {
        if (!state || state.running) return;
        state.running = true;
        while (state.queue.length) {
            const code = state.queue.shift();
            try {
                const response = await fetch(`${state.endpointPrefix}${encodeURIComponent(code)}`);
                const result = await response.json();
                if (result && result.success && Number(result.market_cap) > 0) {
                    applyMarketCap(code, result.market_cap);
                }
            } catch (error) {
            }
            if (state.delayMs > 0) {
                await new Promise(function(resolve) { setTimeout(resolve, state.delayMs); });
            }
        }
        state.running = false;
    }

    window.initAsyncMarketCapRefresh = function initAsyncMarketCapRefresh(options) {
        const opts = options || {};
        if (observer) {
            observer.disconnect();
            observer = null;
        }

        const cacheNamespace = opts.cacheNamespace || window.location.pathname || 'default';
        state = {
            selector: opts.selector || '[data-market-cap-code]',
            endpointPrefix: opts.endpointPrefix || '/api/market_cap/',
            delayMs: Number.isFinite(Number(opts.delayMs)) ? Number(opts.delayMs) : 150,
            cacheKey: buildCacheKey(cacheNamespace),
            cache: {},
            dataStores: Array.isArray(opts.dataStores) ? opts.dataStores : [],
            onUpdate: typeof opts.onUpdate === 'function' ? opts.onUpdate : null,
            queue: [],
            queued: new Set(),
            running: false
        };
        state.cache = loadCache(state.cacheKey);

        const root = document.body || document.documentElement;
        if (!root) return;
        scanNode(root);

        observer = new MutationObserver(function(mutations) {
            mutations.forEach(function(mutation) {
                mutation.addedNodes.forEach(scanNode);
            });
        });
        observer.observe(root, { childList: true, subtree: true });
    };
})();
