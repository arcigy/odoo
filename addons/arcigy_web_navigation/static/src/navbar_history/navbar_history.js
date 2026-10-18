import { NavBar } from "@web/webclient/navbar/navbar";
import { browser } from "@web/core/browser/browser";
import { patch } from "@web/core/utils/patch";
import { useExternalListener, useState } from "@odoo/owl";

// Odoo installed as an app, or opened on a phone, has no browser back/forward buttons.
// The web client restores views from the URL on popstate, so browser history is enough.
patch(NavBar.prototype, {
    setup() {
        super.setup();
        this.arcigyHistory = useState({ canGoBack: true, canGoForward: true });
        // The Navigation API tells whether there is a page to go to; without it both stay enabled.
        const navigation = window.navigation;
        if (navigation && "canGoBack" in navigation) {
            const update = () => {
                this.arcigyHistory.canGoBack = navigation.canGoBack;
                this.arcigyHistory.canGoForward = navigation.canGoForward;
            };
            update();
            useExternalListener(navigation, "currententrychange", update);
        }
    },

    arcigyGoBack() {
        browser.history.back();
    },

    arcigyGoForward() {
        browser.history.forward();
    },
});
