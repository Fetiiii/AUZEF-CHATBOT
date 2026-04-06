import { HttpInterceptorFn } from '@angular/common/http';

export const adminAuthInterceptor: HttpInterceptorFn = (req, next) => {
    // /service/admin veya /service/admin/... hepsini yakala
    const isAdminApi = req.url.includes('/service/admin');
    if (!isAdminApi) return next(req);

    // ✅ cookie de gitsin (bazı ortamlarda gerekli)
    let r = req.clone({ withCredentials: true });

    // Login çağrısına header eklemeyelim
    if (req.url.includes('/service/admin/auth/login')) return next(r);

    // Zaten Authorization varsa dokunma
    if (r.headers.has('Authorization')) return next(r);

    const token = localStorage.getItem('csm_admin_jwt');
    if (!token) return next(r);

    return next(
        r.clone({
            setHeaders: { Authorization: `Bearer ${token}` }
        })
    );
};
