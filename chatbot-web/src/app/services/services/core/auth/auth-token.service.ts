import { Injectable } from '@angular/core';

@Injectable({ providedIn: 'root' })
export class AuthTokenService {
    private readonly LS_KEY = 'jwtToken';
    private readonly SS_KEY = 'jwtToken';

    getToken(): string | null {
        return (
            localStorage.getItem(this.LS_KEY) ||
            localStorage.getItem(this.SS_KEY) ||
            localStorage.getItem('accessToken') ||
            localStorage.getItem('access_token') ||
            localStorage.getItem('accessToken') ||
            localStorage.getItem('access_token')
        );
    }

    setToken(token: string, remember = true): void {
        if (remember) localStorage.setItem(this.LS_KEY, token);
        else localStorage.setItem(this.SS_KEY, token);
    }

    clear(): void {
        localStorage.removeItem(this.LS_KEY);
        localStorage.removeItem(this.SS_KEY);
        localStorage.removeItem('accessToken');
        localStorage.removeItem('access_token');
        localStorage.removeItem('accessToken');
        localStorage.removeItem('access_token');
    }
}
