(function() {
    'use strict';

    var STORAGE_KEY = 'stock-web-simple-theme';
    var COOKIE_KEY = 'stock_web_simple_theme';
    var ENABLED_CLASS = 'monochrome-theme';
    var BUTTON_ID = 'monochromeThemeToggle';

    function storageAvailable() {
        try {
            var testKey = STORAGE_KEY + '-test';
            window.localStorage.setItem(testKey, '1');
            window.localStorage.removeItem(testKey);
            return true;
        } catch (e) {
            return false;
        }
    }

    var canStore = storageAvailable();

    function getCookieValue() {
        var parts = String(document.cookie || '').split(';');
        for (var i = 0; i < parts.length; i += 1) {
            var part = parts[i].trim();
            if (part.indexOf(COOKIE_KEY + '=') === 0) {
                return decodeURIComponent(part.slice(COOKIE_KEY.length + 1));
            }
        }
        return null;
    }

    function setCookieValue(enabled) {
        try {
            var maxAge = 60 * 60 * 24 * 365;
            document.cookie = COOKIE_KEY + '=' + encodeURIComponent(enabled ? '1' : '0')
                + '; path=/; max-age=' + maxAge + '; SameSite=Lax';
        } catch (e) {}
    }

    function getSavedEnabled() {
        if (canStore) {
            var saved = window.localStorage.getItem(STORAGE_KEY);
            if (saved === '1' || saved === '0') return saved === '1';
        }
        var cookieValue = getCookieValue();
        if (cookieValue === '1' || cookieValue === '0') return cookieValue === '1';
        return true;
    }

    function saveEnabled(enabled) {
        if (canStore) {
            window.localStorage.setItem(STORAGE_KEY, enabled ? '1' : '0');
        }
        setCookieValue(enabled);
    }

    function setEnabled(enabled) {
        var root = document.documentElement;
        if (!root) return;
        root.classList.toggle(ENABLED_CLASS, !!enabled);
        root.setAttribute('data-monochrome-theme', enabled ? 'on' : 'off');
        saveEnabled(enabled);
        updateButton();
    }

    function isEnabled() {
        return document.documentElement && document.documentElement.classList.contains(ENABLED_CLASS);
    }

    function updateButton() {
        var button = document.getElementById(BUTTON_ID);
        if (!button) return;
        var enabled = isEnabled();
        button.textContent = enabled ? '彩色模式' : '简约';
        button.setAttribute('aria-pressed', enabled ? 'true' : 'false');
        button.title = enabled ? '切换回彩色样式' : '切换为简约样式';
    }

    function createButton() {
        if (document.getElementById(BUTTON_ID)) {
            updateButton();
            return;
        }

        var button = document.createElement('button');
        button.type = 'button';
        button.id = BUTTON_ID;
        button.className = 'monochrome-toggle';
        button.setAttribute('aria-label', '切换简约样式');
        button.addEventListener('click', function() {
            setEnabled(!isEnabled());
        });

        document.body.appendChild(button);
        updateButton();
    }

    function init() {
        setEnabled(getSavedEnabled());
        createButton();
    }

    if (getSavedEnabled()) {
        document.documentElement.classList.add(ENABLED_CLASS);
        document.documentElement.setAttribute('data-monochrome-theme', 'on');
    } else {
        document.documentElement.setAttribute('data-monochrome-theme', 'off');
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    window.setMonochromeTheme = setEnabled;
    window.isMonochromeThemeEnabled = isEnabled;
})();
