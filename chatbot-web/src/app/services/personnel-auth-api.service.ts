import { Injectable } from '@angular/core';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { map, tap, finalize } from 'rxjs/operators';
import { Observable } from 'rxjs';
import { apiUrl } from './services/shared/constants';

import type { StudentGetLoginHistoryResponse } from './models/login-log.model';

interface LoginResponse {
    jwtToken: string;
    refreshToken?: string;
}

export interface PersonnelJwtPayload {
    user_id: string;
    discriminator: string;
    username: string;
    person_name: string;
    jti_token: string;
    role_personnel: string;
    nbf: number;
    exp: number;
    iss: string;
    aud: string;
}

@Injectable({
    providedIn: 'root'
})
export class PersonnelAuthApiService {
    private readonly baseUrl = `${apiUrl}/service`;
    private readonly storage = localStorage;

    constructor(private http: HttpClient) { }

    get accessToken(): string | null {
        const t = this.storage.getItem('jwtToken');
        if (!t || t === 'null' || t === 'undefined' || t.trim() === '') return null;
        return t;
    }

    get refreshToken(): string | null {
        const t = this.storage.getItem('refreshToken');
        if (!t || t === 'null' || t === 'undefined' || t.trim() === '') return null;
        return t;
    }

    get isLoggedIn(): boolean {
        return !!this.accessToken;
    }

    get decodedAccessToken(): PersonnelJwtPayload | null {
        const token = this.accessToken;
        if (!token) return null;

        const decoded = this.decodeJwt(token);
        if (!decoded) return null;

        return decoded as PersonnelJwtPayload;
    }

    get currentPersonName(): string | null {
        const payload = this.decodedAccessToken;
        return payload?.person_name ?? null;
    }

    /**
     * ✅ DEV LOGIN (sign-in-dev.component.ts bunu çağırıyor)
     * POST /service/personnel/auth/login-dev  body: { username, password }
     */
    login(username: string, password: string): Observable<void> {
        const url = `${this.baseUrl}/personnel/auth/login-dev`;
        const body = { username, password };

        return this.http.post<LoginResponse>(url, body).pipe(
            tap((res) => {
                this.storage.setItem('jwtToken', res.jwtToken);
                if (res.refreshToken) this.storage.setItem('refreshToken', res.refreshToken);
                else this.storage.removeItem('refreshToken');
            }),
            map(() => void 0)
        );
    }

    /**
     * ✅ SSO LOGIN (Swagger'daki TEK login)
     * POST /service/personnel/auth/login  body: { ssoToken }
     */
    loginWithSsoToken(ssoToken: string): Observable<void> {
        const url = `${this.baseUrl}/personnel/auth/login`;

        return this.http.post<LoginResponse>(url, { ssoToken }).pipe(
            tap((res) => {
                this.storage.setItem('jwtToken', res.jwtToken);
                if (res.refreshToken) this.storage.setItem('refreshToken', res.refreshToken);
                else this.storage.removeItem('refreshToken');
            }),
            map(() => void 0)
        );
    }

    /**
     * POST /personnel/auth/refresh
     */
    refresh(): Observable<void> {
        const url = `${this.baseUrl}/personnel/auth/refresh`;
        const jwtToken = this.accessToken;
        const refreshToken = this.refreshToken;

        return this.http.post<LoginResponse>(url, { jwtToken, refreshToken }).pipe(
            tap((res) => {
                this.storage.setItem('jwtToken', res.jwtToken);
                if (res.refreshToken) this.storage.setItem('refreshToken', res.refreshToken);
            }),
            map(() => void 0)
        );
    }

    /**
     * GET /personnel/auth/logout
     */
    logout(): Observable<void> {
        const url = `${this.baseUrl}/personnel/auth/logout`;

        return this.http.get<void>(url, { withCredentials: true }).pipe(
            tap(() => this.clearTokens()),
            finalize(() => this.clearTokens()),
            map(() => void 0)
        );
    }

    /**
     * GET /personnel/auth/logout-from-all-devices
     */
    logoutFromAllDevices(): Observable<void> {
        const url = `${this.baseUrl}/personnel/auth/logout-from-all-devices`;

        return this.http.get<void>(url, { withCredentials: true }).pipe(
            tap(() => this.clearTokens()),
            finalize(() => this.clearTokens()),
            map(() => void 0)
        );
    }

    /**
     * GET /personnel/auth/get-login-history
     */
    getLoginHistory(): Observable<StudentGetLoginHistoryResponse> {
        const url = `${this.baseUrl}/personnel/auth/get-login-history`;

        const token = this.accessToken;
        const headers = token ? new HttpHeaders({ Authorization: `Bearer ${token}` }) : undefined;

        return this.http.get<StudentGetLoginHistoryResponse>(url, {
            headers,
            withCredentials: true
        });
    }

    clearTokens(): void {
        this.storage.removeItem('jwtToken');
        this.storage.removeItem('refreshToken');
    }

    private decodeJwt(token: string): any | null {
        try {
            const parts = token.split('.');
            if (parts.length !== 3) return null;

            const payload = parts[1];
            let base64 = payload.replace(/-/g, '+').replace(/_/g, '/');

            const pad = base64.length % 4;
            if (pad) base64 = base64.padEnd(base64.length + (4 - pad), '=');

            const json = atob(base64);
            return JSON.parse(json);
        } catch (err) {
            console.error('JWT decode error', err);
            return null;
        }
    }
}