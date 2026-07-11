import { Injectable } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { BehaviorSubject, Observable, of } from 'rxjs';
import { catchError, map, tap } from 'rxjs/operators';
import {
    AdminUser,
    ChatbotLoginRequest,
    ChatbotLoginResponse
} from '../../models/chatbot/chatbot-auth.model';

/**
 * Gerçek oturum tabanlı kimlik doğrulama (backend: /api/auth/*).
 *
 * Oturumun gerçek kaynağı backend'in HttpOnly session cookie'sidir — JS bu
 * cookie'yi göremez (XSS'e karşı bilinçli tasarım). Bu servis yalnızca
 * "oturum açık mı" bilgisini bellek içinde önbellekler; sayfa yenilendiğinde
 * guard, /api/auth/me üzerinden cookie'nin hâlâ geçerli olduğunu doğrular.
 *
 * NOT: Eski sürümdeki localStorage tabanlı sahte login (fb/1) kaldırıldı —
 * bundle'ı okuyan herkes parolayı görebiliyor, DevTools'tan flag set edip
 * paneli açabiliyordu.
 */
@Injectable({ providedIn: 'root' })
export class ChatbotAuthService {
    private readonly REMEMBER_KEY = 'chatbot_remember_id';

    /** Reaktif kullanıcı durumu: header gibi bileşenler init sırasına
     *  bağımlı kalmadan (login/me hangi anda dönerse dönsün) güncellenir. */
    readonly user$ = new BehaviorSubject<AdminUser | null>(null);

    constructor(private http: HttpClient) { }

    private setUser(u: AdminUser | null): void {
        this.user$.next(u ?? null);
    }

    login(req: ChatbotLoginRequest): Observable<ChatbotLoginResponse> {
        const email = (req.email || '').trim();
        return this.http
            .post<{ ok: boolean; user: AdminUser }>('/api/auth/login', {
                email,
                password: req.password
            })
            .pipe(
                tap((res) => {
                    this.setUser(res.user);
                    if (req.remember) localStorage.setItem(this.REMEMBER_KEY, email);
                    else localStorage.removeItem(this.REMEMBER_KEY);
                }),
                map((res) => ({ ok: true, user: res.user } as ChatbotLoginResponse)),
                catchError((err: HttpErrorResponse) => {
                    this.setUser(null);
                    let message = 'Sunucuya ulaşılamadı. Lütfen tekrar deneyin.';
                    if (err.status === 401) {
                        message = 'E-posta ya da parola hatalı.';
                    } else if (err.status === 429 || err.status === 503) {
                        // nginx rate limit (limit_req) ya da geçici servis sorunu
                        message = 'Çok fazla deneme yapıldı ya da sunucu meşgul. Lütfen biraz bekleyip tekrar deneyin.';
                    }
                    return of({ ok: false, message } as ChatbotLoginResponse);
                })
            );
    }

    /** Oturum geçerli mi? Bellekte kullanıcı varsa hızlı döner; yoksa backend'e sorar. */
    checkSession(): Observable<boolean> {
        if (this.user$.value) return of(true);
        return this.http.get<{ user: AdminUser }>('/api/auth/me').pipe(
            tap((res) => this.setUser(res.user)),
            map((res) => !!res.user),
            catchError(() => {
                this.setUser(null);
                return of(false);
            })
        );
    }

    /** Backend'de oturumu kapatır (DB'den silinir → anında iptal). */
    logout(): Observable<void> {
        return this.http.post<{ ok: boolean }>('/api/auth/logout', {}).pipe(
            map(() => void 0),
            // Backend'e ulaşılamasa bile yerel durumu temizle; cookie süresi
            // dolunca oturum zaten geçersizleşir.
            catchError(() => of(void 0)),
            tap(() => this.setUser(null))
        );
    }

    /** Anlık (senkron) okuma; reaktif kullanım için user$'a abone olun. */
    user(): AdminUser | null {
        return this.user$.value;
    }

    /** 401 interceptor'ı için: yerel önbelleği düşür (yönlendirme interceptor'da). */
    clearLocalSession(): void {
        this.setUser(null);
    }

    getRememberedEmail(): string | null {
        const v = localStorage.getItem(this.REMEMBER_KEY);
        return v && v.trim() ? v : null;
    }
}
