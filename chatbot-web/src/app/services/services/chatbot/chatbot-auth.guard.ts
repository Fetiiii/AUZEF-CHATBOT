import { inject } from '@angular/core';
import { CanActivateFn, Router, UrlTree } from '@angular/router';

export const chatbotAuthGuard: CanActivateFn = (_, state): boolean | UrlTree => {
    const router = inject(Router);

    const rawUrl = state?.url || '/';
    const url = rawUrl.toLowerCase();

    // ✅ chatbot login public
    if (url.startsWith('/chatbot/sign-in')) return true;

    // ✅ sadece /chatbot alanını koru
    if (!url.startsWith('/chatbot')) return true;

    const ok = localStorage.getItem('csm_chatbot_auth') === '1';
    if (ok) return true;

    return router.createUrlTree(['/chatbot/sign-in'], {
        queryParams: { returnUrl: rawUrl }
    });
};
