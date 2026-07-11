import { HttpErrorResponse, HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';
import { Router } from '@angular/router';
import { throwError } from 'rxjs';
import { catchError } from 'rxjs/operators';

import { ChatbotAuthService } from './chatbot-auth.service';

/**
 * Oturum süresi dolduğunda (herhangi bir /api çağrısı 401 döndüğünde)
 * kullanıcıyı login sayfasına, kaldığı yeri returnUrl ile taşıyarak yönlendirir.
 *
 * /api/auth/* hariç tutulur: login'in kendi 401'i "parola hatalı" akışıdır,
 * me'nin 401'i guard'ın normal "oturum yok" cevabıdır — ikisi de yönlendirme
 * döngüsü yaratmamalı.
 */
export const chatbotSessionInterceptor: HttpInterceptorFn = (req, next) => {
    const router = inject(Router);
    const auth = inject(ChatbotAuthService);

    const isApi = req.url.startsWith('/api/');
    const isAuthCall = req.url.startsWith('/api/auth/');
    if (!isApi || isAuthCall) return next(req);

    return next(req).pipe(
        catchError((err: HttpErrorResponse) => {
            if (err.status === 401) {
                auth.clearLocalSession();
                const current = router.routerState.snapshot.url || '/chatbot';
                if (!current.toLowerCase().startsWith('/chatbot/sign-in')) {
                    router.navigate(['/chatbot/sign-in'], {
                        queryParams: { returnUrl: current }
                    });
                }
            }
            return throwError(() => err);
        })
    );
};
