import { HttpInterceptorFn } from '@angular/common/http';

const API_BASE = '/service';

export const apiCredentialsInterceptor: HttpInterceptorFn = (req, next) => {
    // Sadece bizim API’ye giderken cookie taşı
    if (!req.url.startsWith(API_BASE)) return next(req);

    return next(
        req.clone({
            withCredentials: true
        })
    );
};
