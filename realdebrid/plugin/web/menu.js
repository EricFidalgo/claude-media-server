// Adds "Real-Debrid" and "Captions" to Jellyfin's side menu and user menu, for admins only. The page and everything behind it check for themselves too.
// It copies one of Jellyfin's own menu rows, so it always looks like the rows around it.
(function () {
    'use strict';
    var ENTRIES = [
        { link: 'captionsLink', href: '../Captions/app', text: 'Captions', icon: 'M19 4H5c-1.11 0-2 .9-2 2v12c0 1.1.89 2 2 2h14c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm-8 7H9.5v-.5h-2v3h2V13H11v1c0 .55-.45 1-1 1H7c-.55 0-1-.45-1-1v-4c0-.55.45-1 1-1h3c.55 0 1 .45 1 1v1zm7 0h-1.5v-.5h-2v3h2V13H18v1c0 .55-.45 1-1 1h-3c-.55 0-1-.45-1-1v-4c0-.55.45-1 1-1h3c.55 0 1 .45 1 1v1z' },
        { link: 'realDebridLink', href: '../RealDebrid/app', text: 'Real-Debrid', icon: 'M19.35 10.04C18.67 6.59 15.64 4 12 4 9.11 4 6.6 5.64 5.35 8.04 2.34 8.36 0 10.91 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96zM17 13l-5 5-5-5h3V9h4v4h3z' },
    ];
    // Each place: a row to copy, how much of it counts as one row, and which side of it the new row goes.
    var PLACES = [
        { from: '.MuiDrawer-root a[href="#/home?tab=1"]', row: 'li', side: 'after' },
        { from: '.MuiMenu-list a[href="#/dashboard"]', row: 'a', side: 'before' },
    ];
    var admin = null;
    var askedFor = null;
    var pending = null;

    function checkAdmin() {
        var client = window.ApiClient;
        var user = client && client.getCurrentUserId && client.getCurrentUserId();
        if (!user) {
            admin = null;
            askedFor = null;
            return;
        }
        if (askedFor === user) {
            return;
        }
        askedFor = user;
        admin = null;
        client.getCurrentUser().then(function (u) {
            admin = !!(u && u.Policy && u.Policy.IsAdministrator);
            place();
        }, function () {
            askedFor = null;
        });
    }

    function makeRow(original, rowTag, entry) {
        var row = original.closest(rowTag).cloneNode(true);
        var link = row.matches('a') ? row : row.querySelector('a');
        link.href = entry.href;
        link.classList.add(entry.link);
        link.classList.remove('Mui-selected');
        link.removeAttribute('aria-current');
        var path = row.querySelector('svg path');
        if (path) {
            path.setAttribute('d', entry.icon);
        }
        var text = row.querySelector('.MuiListItemText-root span') || row.querySelector('.MuiListItemText-root');
        if (text) {
            text.textContent = entry.text;
        }
        return row;
    }

    function place() {
        PLACES.forEach(function (where) {
            var original = document.querySelector(where.from);
            if (!original) {
                return;
            }
            var row = original.closest(where.row);
            ENTRIES.forEach(function (entry) {
                var existing = row.parentElement.querySelector('.' + entry.link);
                if (!admin) {
                    if (existing) {
                        existing.closest(where.row).remove();
                    }
                } else if (!existing) {
                    row[where.side](makeRow(original, where.row, entry));
                }
            });
        });
    }

    new MutationObserver(function () {
        if (pending === null) {
            pending = setTimeout(function () {
                pending = null;
                checkAdmin();
                place();
            }, 100);
        }
    }).observe(document.documentElement, { childList: true, subtree: true });
    checkAdmin();
})();
