import {
    HttpInterceptorFn,
    HttpRequest,
    HttpHandlerFn,
    HttpErrorResponse
} from '@angular/common/http';
import { inject } from '@angular/core';
import { Router } from '@angular/router';
import { catchError } from 'rxjs/operators';
import { throwError } from 'rxjs';
import { PersonnelAuthApiService } from 'src/app/services/personnel-auth-api.service';




function readTokenFallback(): string | null {
    return (
        // standart
        localStorage.getItem('accessToken') ||
        localStorage.getItem('accessToken') ||

        // bazı projelerde
        localStorage.getItem('access_token') ||
        localStorage.getItem('access_token') ||

        // backend response anahtarı
        localStorage.getItem('jwtToken') ||
        localStorage.getItem('jwtToken') ||

        // eğer bir yerde bunu kullandıysan diye
        localStorage.getItem('student_token') ||
        localStorage.getItem('student_token') ||

        null
    );
}

function isServiceCall(url: string): boolean {
    // relative '/api/...'
    if (url.startsWith('/api/')) return true;

    // absolute 'https://.../api/...'
    try {
        const u = new URL(url);
        return u.pathname.startsWith('/api/');
    } catch {
        return false;
    }
}

function isAdminServiceCall(_url: string): boolean {
    // admin'e özel çağrı yok — chatbot için ayırt etmeye gerek yok
    return false;
}

export const authInterceptor: HttpInterceptorFn = (
    req: HttpRequest<unknown>,
    next: HttpHandlerFn
) => {
    const authApi = inject(PersonnelAuthApiService);
    const router = inject(Router);

    // sadece backend çağrılarına dokun
    if (!isServiceCall(req.url)) {
        return next(req);
    }

    const isAdminApi = isAdminServiceCall(req.url);

    // cookie + session kullanan endpointler için de dursun
    // (mevcut davranışı koruyoruz)
    let authReq = req.clone({ withCredentials: true });

    // ✅ 1) Eğer Authorization zaten set edilmişse (adminAuthInterceptor gibi), ASLA ezme
    //    (admin dahil tüm senaryolarda çakışmayı bitirir)
    if (req.headers.has('Authorization')) {
        return next(authReq).pipe(
            catchError((err: HttpErrorResponse) => {
                if (err.status === 401) {
                    const url = router.routerState.snapshot.url || '';
                    const isStudentArea = url.startsWith('/student');
                    const isAdminArea = url.startsWith('/admin');

                    // admin tarafında personnel logout çalıştırma
                    if (isAdminArea || isAdminApi) {
                        router.navigate(['/admin/sign-in'], {
                            queryParams: { returnUrl: url }
                        });
                    } else {
                        try { authApi.logout(); } catch { /* boş */ }

                        router.navigate([isStudentArea ? '/student/sign-in' : '/auth/sign-in'], {
                            queryParams: { returnUrl: url }
                        });
                    }
                }
                return throwError(() => err);
            })
        );
    }

    // ✅ 2) Admin API çağrılarında Personnel token ekleme (adminAuthInterceptor ekleyecek)
    if (!isAdminApi) {
        const token = authApi.accessToken || readTokenFallback();

        // token varsa Bearer ekle
        if (token) {
            authReq = authReq.clone({
                setHeaders: { Authorization: `Bearer ${token}` }
            });
        }
    }

    return next(authReq).pipe(
        catchError((err: HttpErrorResponse) => {
            if (err.status === 401) {
                // login endpointi 401 dönerse sonsuz redirect olmasın
                const url = router.routerState.snapshot.url || '';
                const isStudentArea = url.startsWith('/student');
                const isAdminArea = url.startsWith('/admin');

                // admin tarafında personnel logout çalıştırma
                if (isAdminArea || isAdminApi) {
                    router.navigate(['/admin/sign-in'], {
                        queryParams: { returnUrl: url }
                    });
                } else {
                    try { authApi.logout(); } catch { /* boş */ }

                    router.navigate([isStudentArea ? '/student/sign-in' : '/auth/sign-in'], {
                        queryParams: { returnUrl: url }
                    });
                }
            }
            return throwError(() => err);
        })
    );
};
