import { Injectable } from '@angular/core';
import {
    ChatbotLoginRequest,
    ChatbotLoginResponse
} from '../../models/chatbot/chatbot-auth.model';

@Injectable({ providedIn: 'root' })
export class ChatbotAuthService {
    private readonly AUTH_KEY = 'csm_chatbot_auth';
    private readonly REMEMBER_KEY = 'chatbot_remember_id';

    /** ✅ Basit login: username=fb, password=1 */
    login(req: ChatbotLoginRequest): ChatbotLoginResponse {
        const u = (req.username || '').trim().toLowerCase();
        const p = String(req.password || '').trim();

        const ok = u === 'fb' && p === '1';

        if (!ok) {
            this.clearAuth();
            return { ok: false, message: 'Kullanıcı adı veya parola hatalı.' };
        }

        localStorage.setItem(this.AUTH_KEY, '1');

        if (req.remember) localStorage.setItem(this.REMEMBER_KEY, req.username.trim());
        else localStorage.removeItem(this.REMEMBER_KEY);

        return { ok: true };
    }

    logout(): void {
        this.clearAuth();
    }

    isLoggedIn(): boolean {
        return localStorage.getItem(this.AUTH_KEY) === '1';
    }

    getRememberedUsername(): string | null {
        const v = localStorage.getItem(this.REMEMBER_KEY);
        return v && v.trim() ? v : null;
    }

    private clearAuth(): void {
        localStorage.removeItem(this.AUTH_KEY);
    }
}
