import { inject } from '@angular/core';
import { CanActivateFn, Router, UrlTree } from '@angular/router';
import { Observable, map } from 'rxjs';

import { ChatbotAuthService } from './chatbot-auth.service';

/**
 * Oturum tabanlı guard: backend'e /api/auth/me sorarak cookie'nin geçerli
 * olduğunu doğrular (async). Eski localStorage bayrağı kaldırıldı — DevTools
 * üzerinden set edilebiliyordu, gerçek koruma sağlamıyordu.
 */
export const chatbotAuthGuard: CanActivateFn = (
    _,
    state
): boolean | Observable<boolean | UrlTree> => {
    const router = inject(Router);
    const auth = inject(ChatbotAuthService);

    const rawUrl = state?.url || '/';
    const url = rawUrl.toLowerCase();

    // ✅ chatbot login public
    if (url.startsWith('/chatbot/sign-in')) return true;

    // ✅ sadece /chatbot alanını koru
    if (!url.startsWith('/chatbot')) return true;

    return auth.checkSession().pipe(
        map((ok) =>
            ok
                ? true
                : router.createUrlTree(['/chatbot/sign-in'], {
                      queryParams: { returnUrl: rawUrl }
                  })
        )
    );
};
