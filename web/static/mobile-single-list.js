(function() {
    'use strict';

    var MOBILE_MAX_WIDTH = 768;
    var CONTAINER_SELECTORS = ['.columns-grid', '.tables-layout', '.ths-layout'];
    var GROUP_PANEL_SELECTOR = '.group-container';
    var PANEL_CLASSES = [
        'metric-column',
        'metric-column-stack',
        'task-section',
        'focus-alert-panels',
        'day-column',
        'table-section',
        'stacked-column',
        'ths-panel',
        'group-container'
    ];
    var TITLE_SELECTORS = [
        '.col-header-main',
        '.col-title',
        '.col-header',
        '.table-header .th-title',
        '.table-header',
        '.day-header',
        '.group-header .group-name',
        '.group-name',
        '.task-section-title',
        'h2',
        'h3'
    ];
    var managed = new WeakMap();
    var managedGroups = new WeakMap();
    var refreshTimer = 0;
    var nextPanelId = 1;
    var nextGroupId = 1;

    function isPanel(node) {
        if (!node || node.nodeType !== 1 || !node.classList) return false;
        return PANEL_CLASSES.some(function(cls) { return node.classList.contains(cls); });
    }

    function panelOrder(panel, domIndex) {
        var raw = panel && panel.getAttribute ? panel.getAttribute('data-mobile-list-order') : null;
        if (raw == null || raw === '') return 1000 + domIndex;
        var parsed = Number(raw);
        return Number.isFinite(parsed) ? parsed : 1000 + domIndex;
    }

    function getPanels(container) {
        return Array.prototype.filter.call(container.children || [], isPanel)
            .map(function(panel, index) { return { panel: panel, index: index, order: panelOrder(panel, index) }; })
            .sort(function(a, b) {
                if (a.order !== b.order) return a.order - b.order;
                return a.index - b.index;
            })
            .map(function(item) { return item.panel; });
    }

    function hasOwnListContainer(container) {
        return CONTAINER_SELECTORS.some(function(selector) {
            return !!container.querySelector(':scope > ' + selector);
        });
    }

    function findContainers() {
        var found = [];
        CONTAINER_SELECTORS.forEach(function(selector) {
            document.querySelectorAll(selector).forEach(function(container) {
                if (getPanels(container).length > 1) found.push(container);
            });
        });

        document.querySelectorAll('#mainContent').forEach(function(container) {
            if (hasOwnListContainer(container)) return;
            var panels = getPanels(container).filter(function(panel) {
                return panel.matches(GROUP_PANEL_SELECTOR);
            });
            if (panels.length > 1) found.push(container);
        });

        return found;
    }

    function textOf(node) {
        return node ? String(node.textContent || '').replace(/\s+/g, ' ').trim() : '';
    }

    function titleForPanel(panel, index) {
        var explicit = panel.getAttribute('data-mobile-list-title');
        if (explicit) return normalizeTitle(explicit, index);

        for (var i = 0; i < TITLE_SELECTORS.length; i += 1) {
            var found = panel.querySelector(TITLE_SELECTORS[i]);
            var title = textOf(found);
            if (title) return normalizeTitle(title, index);
        }

        return '列表' + (index + 1);
    }

    function normalizeTitle(raw, index) {
        var title = String(raw || '').replace(/\s+/g, ' ').trim();
        if (!title) return '列表' + (index + 1);
        return title.length > 16 ? title.slice(0, 16) : title;
    }

    function panelKey(panel) {
        if (!panel.dataset.mobileSingleListPanelId) {
            panel.dataset.mobileSingleListPanelId = String(nextPanelId++);
        }
        return panel.dataset.mobileSingleListPanelId;
    }

    function groupKey(panel) {
        if (!panel.dataset.mobileGroupPanelId) {
            panel.dataset.mobileGroupPanelId = String(nextGroupId++);
        }
        return panel.dataset.mobileGroupPanelId;
    }

    function existingPageTabs(container) {
        var prev = container.previousElementSibling;
        return prev && prev.classList && prev.classList.contains('mobile-list-tabs')
            && prev.getAttribute('data-mobile-single-list-generated') !== '1';
    }

    function setActive(container, index, options) {
        options = options || {};
        var state = managed.get(container);
        if (!state) return;
        var panels = getPanels(container);
        if (!panels.length) return;
        var safeIndex = Math.max(0, Math.min(index, panels.length - 1));
        state.index = safeIndex;

        panels.forEach(function(panel, panelIndex) {
            var active = panelIndex === safeIndex;
            panel.classList.toggle('mobile-active-list', active);
            panel.setAttribute('aria-hidden', active ? 'false' : 'true');
        });

        Array.prototype.forEach.call(state.tabs.children, function(tab, tabIndex) {
            var active = tabIndex === safeIndex;
            tab.classList.toggle('active', active);
            tab.setAttribute('aria-selected', active ? 'true' : 'false');
            if (active && options.scrollTab && window.innerWidth <= MOBILE_MAX_WIDTH) {
                tab.scrollIntoView({ block: 'nearest', inline: 'nearest' });
            }
        });

        if (options.hideKline && typeof window.hideKline === 'function') {
            window.hideKline();
        }
    }

    function switchActive(container, direction) {
        var state = managed.get(container);
        if (!state) return;
        setActive(container, state.index + direction, { hideKline: true, scrollTab: true });
    }

    function bindSwipe(container, state) {
        if (state.swipeBound) return;
        state.swipeBound = true;
        var touchStartX = 0;
        var touchStartY = 0;
        var pointerStartX = 0;
        var pointerStartY = 0;
        var pointerActive = false;

        container.addEventListener('touchstart', function(e) {
            if (!e.touches || !e.touches.length) return;
            touchStartX = e.touches[0].clientX;
            touchStartY = e.touches[0].clientY;
        }, { passive: true });

        container.addEventListener('touchend', function(e) {
            if (!e.changedTouches || !e.changedTouches.length || window.innerWidth > MOBILE_MAX_WIDTH) return;
            var deltaX = e.changedTouches[0].clientX - touchStartX;
            var deltaY = e.changedTouches[0].clientY - touchStartY;
            if (Math.abs(deltaX) <= Math.abs(deltaY) || Math.abs(deltaX) < 60) return;
            if (deltaX < -60) switchActive(container, 1);
            if (deltaX > 60) switchActive(container, -1);
        }, { passive: true });

        container.addEventListener('pointerdown', function(e) {
            if (window.innerWidth > MOBILE_MAX_WIDTH) return;
            if (e.pointerType === 'mouse' && e.button !== 0) return;
            pointerActive = true;
            pointerStartX = e.clientX;
            pointerStartY = e.clientY;
        }, { passive: true });

        container.addEventListener('pointerup', function(e) {
            if (!pointerActive || window.innerWidth > MOBILE_MAX_WIDTH) return;
            pointerActive = false;
            var deltaX = e.clientX - pointerStartX;
            var deltaY = e.clientY - pointerStartY;
            if (Math.abs(deltaX) <= Math.abs(deltaY) || Math.abs(deltaX) < 60) return;
            if (deltaX < -60) switchActive(container, 1);
            if (deltaX > 60) switchActive(container, -1);
        }, { passive: true });

        container.addEventListener('pointercancel', function() {
            pointerActive = false;
        }, { passive: true });
    }

    function getGroupPanels(container) {
        return Array.prototype.filter.call(container.children || [], function(node) {
            return node && node.nodeType === 1 && node.classList && node.classList.contains('mobile-group-panel');
        });
    }

    function titleForGroupPanel(panel, index) {
        var title = panel.getAttribute('data-mobile-group-title') || '';
        title = String(title).replace(/\s+/g, ' ').trim();
        if (!title) title = '分组' + (index + 1);
        return title.length > 18 ? title.slice(0, 18) : title;
    }

    function setActiveGroup(container, index, options) {
        options = options || {};
        var state = managedGroups.get(container);
        if (!state) return;
        var panels = getGroupPanels(container);
        if (!panels.length) return;
        var safeIndex = Math.max(0, Math.min(index, panels.length - 1));
        state.index = safeIndex;

        panels.forEach(function(panel, panelIndex) {
            var active = panelIndex === safeIndex;
            panel.classList.toggle('mobile-active-group', active);
            panel.setAttribute('aria-hidden', active ? 'false' : 'true');
        });

        Array.prototype.forEach.call(state.tabs.children, function(tab, tabIndex) {
            var active = tabIndex === safeIndex;
            tab.classList.toggle('active', active);
            tab.setAttribute('aria-selected', active ? 'true' : 'false');
            if (active && options.scrollTab && window.innerWidth <= MOBILE_MAX_WIDTH) {
                tab.scrollIntoView({ block: 'nearest', inline: 'nearest' });
            }
        });
    }

    function switchActiveGroup(container, direction) {
        var state = managedGroups.get(container);
        if (!state) return;
        setActiveGroup(container, state.index + direction, { scrollTab: true });
    }

    function bindGroupSwipe(container, state) {
        if (state.swipeBound) return;
        state.swipeBound = true;
        var touchStartX = 0;
        var touchStartY = 0;
        var pointerStartX = 0;
        var pointerStartY = 0;
        var pointerActive = false;

        container.addEventListener('touchstart', function(e) {
            if (!e.touches || !e.touches.length) return;
            touchStartX = e.touches[0].clientX;
            touchStartY = e.touches[0].clientY;
        }, { passive: true });

        container.addEventListener('touchend', function(e) {
            if (!e.changedTouches || !e.changedTouches.length || window.innerWidth > MOBILE_MAX_WIDTH) return;
            var deltaX = e.changedTouches[0].clientX - touchStartX;
            var deltaY = e.changedTouches[0].clientY - touchStartY;
            if (Math.abs(deltaX) <= Math.abs(deltaY) || Math.abs(deltaX) < 60) return;
            if (deltaX < -60) switchActiveGroup(container, 1);
            if (deltaX > 60) switchActiveGroup(container, -1);
        }, { passive: true });

        container.addEventListener('pointerdown', function(e) {
            if (window.innerWidth > MOBILE_MAX_WIDTH) return;
            if (e.pointerType === 'mouse' && e.button !== 0) return;
            pointerActive = true;
            pointerStartX = e.clientX;
            pointerStartY = e.clientY;
        }, { passive: true });

        container.addEventListener('pointerup', function(e) {
            if (!pointerActive || window.innerWidth > MOBILE_MAX_WIDTH) return;
            pointerActive = false;
            var deltaX = e.clientX - pointerStartX;
            var deltaY = e.clientY - pointerStartY;
            if (Math.abs(deltaX) <= Math.abs(deltaY) || Math.abs(deltaX) < 60) return;
            if (deltaX < -60) switchActiveGroup(container, 1);
            if (deltaX > 60) switchActiveGroup(container, -1);
        }, { passive: true });

        container.addEventListener('pointercancel', function() {
            pointerActive = false;
        }, { passive: true });
    }

    function rebuildGroupTabs(container, panels, state) {
        state.tabs.innerHTML = '';
        panels.forEach(function(panel, index) {
            groupKey(panel);
            var tab = document.createElement('button');
            tab.type = 'button';
            tab.className = 'mobile-group-tab';
            tab.setAttribute('role', 'tab');
            tab.textContent = titleForGroupPanel(panel, index);
            tab.addEventListener('click', function() {
                setActiveGroup(container, index, { scrollTab: true });
            });
            state.tabs.appendChild(tab);
        });
    }

    function setupGroupSwitch(container) {
        var panels = getGroupPanels(container);
        if (panels.length <= 1) return;
        var titles = panels.map(titleForGroupPanel).join('|');
        var keys = panels.map(groupKey).join('|');
        var state = managedGroups.get(container);
        if (!state) {
            state = { index: 0, keys: '', titles: '', tabs: null, swipeBound: false };
            state.tabs = document.createElement('div');
            state.tabs.className = 'mobile-group-tabs';
            state.tabs.setAttribute('role', 'tablist');
            state.tabs.setAttribute('data-mobile-group-tabs-generated', '1');
            container.insertBefore(state.tabs, panels[0]);
            managedGroups.set(container, state);
        }

        bindGroupSwipe(container, state);

        if (state.keys !== keys || state.titles !== titles || state.tabs.children.length !== panels.length) {
            state.keys = keys;
            state.titles = titles;
            rebuildGroupTabs(container, panels, state);
        }

        setActiveGroup(container, Math.min(state.index, panels.length - 1));
    }

    function setupGroupSwitches() {
        document.querySelectorAll('.mobile-group-switch').forEach(setupGroupSwitch);
    }

    function rebuildTabs(container, panels, state) {
        state.tabs.innerHTML = '';
        panels.forEach(function(panel, index) {
            panelKey(panel);
            var tab = document.createElement('button');
            tab.type = 'button';
            tab.className = 'mobile-list-tab';
            tab.setAttribute('role', 'tab');
            tab.textContent = titleForPanel(panel, index);
            tab.addEventListener('click', function() {
                setActive(container, index, { hideKline: true, scrollTab: true });
            });
            state.tabs.appendChild(tab);
        });
    }

    function setupContainer(container) {
        if (existingPageTabs(container)) return;

        var panels = getPanels(container);
        if (panels.length <= 1) return;

        var titles = panels.map(titleForPanel).join('|');
        var keys = panels.map(panelKey).join('|');
        var state = managed.get(container);
        if (!state) {
            state = { index: 0, keys: '', titles: '', tabs: null, swipeBound: false };
            state.tabs = document.createElement('div');
            state.tabs.className = 'mobile-list-tabs';
            state.tabs.setAttribute('role', 'tablist');
            state.tabs.setAttribute('data-mobile-single-list-generated', '1');
            container.parentNode.insertBefore(state.tabs, container);
            managed.set(container, state);
        }

        container.classList.add('mobile-single-list');
        container.classList.add('mobile-single-list-managed');
        bindSwipe(container, state);

        if (state.keys !== keys || state.titles !== titles || state.tabs.children.length !== panels.length) {
            state.keys = keys;
            state.titles = titles;
            rebuildTabs(container, panels, state);
        }

        setActive(container, Math.min(state.index, panels.length - 1));
    }

    function refreshAll() {
        updateMobileEnabledClass();
        findContainers().forEach(setupContainer);
        setupGroupSwitches();
    }

    function updateMobileEnabledClass() {
        var width = Math.min(
            window.innerWidth || MOBILE_MAX_WIDTH + 1,
            document.documentElement ? (document.documentElement.clientWidth || MOBILE_MAX_WIDTH + 1) : MOBILE_MAX_WIDTH + 1
        );
        document.documentElement.classList.toggle('mobile-single-list-enabled', width <= MOBILE_MAX_WIDTH);
    }

    function scheduleRefresh() {
        if (refreshTimer) window.clearTimeout(refreshTimer);
        refreshTimer = window.setTimeout(function() {
            refreshTimer = 0;
            refreshAll();
        }, 80);
    }

    function start() {
        refreshAll();
        var observer = new MutationObserver(scheduleRefresh);
        observer.observe(document.body, { childList: true, subtree: true });
        window.addEventListener('resize', scheduleRefresh);
        window.refreshMobileSingleListTabs = refreshAll;
        window.mobileSingleListNext = function() {
            document.querySelectorAll('.mobile-single-list-managed').forEach(function(container) {
                switchActive(container, 1);
            });
        };
        window.mobileSingleListPrev = function() {
            document.querySelectorAll('.mobile-single-list-managed').forEach(function(container) {
                switchActive(container, -1);
            });
        };
        window.refreshMobileGroupSwitches = setupGroupSwitches;
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
})();
