import { inject } from '@angular/core';
import { ActivatedRouteSnapshot, CanActivateFn, Router, UrlTree } from '@angular/router';
import { Observable, map } from 'rxjs';

import { ChatbotAuthService } from './chatbot-auth.service';
import { roleLevel } from '../../models/chatbot/chatbot-auth.model';

/**
 * Oturum + ROL guard'ı:
 * 1) Backend'e /api/auth/me sorarak cookie'nin geçerli olduğunu doğrular (async).
 * 2) Route data'sında minRole varsa kullanıcının rol seviyesini kontrol eder;
 *    yetersizse herkese açık varsayılan sayfaya yönlendirir.
 * Gerçek yaptırım backend middleware'indedir (403) — bu guard yalnızca UX içindir.
 */
export const chatbotAuthGuard: CanActivateFn = (
    route: ActivatedRouteSnapshot,
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

    const minRole: string | undefined = route?.data?.['minRole'];

    return auth.checkSession().pipe(
        map((ok) => {
            if (!ok) {
                return router.createUrlTree(['/chatbot/sign-in'], {
                    queryParams: { returnUrl: rawUrl }
                });
            }
            if (minRole && roleLevel(auth.user()?.role) < roleLevel(minRole)) {
                // Yetkisi olmayan sayfa: en düşük rolün erişebildiği sayfaya dön.
                return router.createUrlTree(['/chatbot/document-upload']);
            }
            return true;
        })
    );
};
