import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { AdminRole } from '../../models/chatbot/chatbot-auth.model';

export interface SettingsUser {
    id: number;
    email: string;
    full_name: string | null;
    role: AdminRole;
    is_active: boolean;
    created_at: string | null;
}

export interface UserCreateRequest {
    email: string;
    password: string;
    full_name?: string | null;
    role: AdminRole;
}

export interface UserUpdateRequest {
    full_name?: string | null;
    role?: AdminRole;
    is_active?: boolean;
    password?: string;
}

export interface LLMSettings {
    enabled: boolean;
    openrouter_key_masked: string | null;
    key_source: 'db' | 'env' | 'none';
}

/** Ayarlar sayfası API'si — backend yalnızca super_admin'e izin verir. */
@Injectable({ providedIn: 'root' })
export class SettingsApiService {
    private readonly base = '/api/settings';

    constructor(private http: HttpClient) { }

    listUsers(): Observable<{ users: SettingsUser[] }> {
        return this.http.get<{ users: SettingsUser[] }>(`${this.base}/users`);
    }

    createUser(body: UserCreateRequest): Observable<SettingsUser> {
        return this.http.post<SettingsUser>(`${this.base}/users`, body);
    }

    updateUser(id: number, body: UserUpdateRequest): Observable<SettingsUser> {
        return this.http.put<SettingsUser>(`${this.base}/users/${id}`, body);
    }

    getLLM(): Observable<LLMSettings> {
        return this.http.get<LLMSettings>(`${this.base}/llm`);
    }

    updateLLM(body: { enabled?: boolean; openrouter_api_key?: string }): Observable<LLMSettings> {
        return this.http.put<LLMSettings>(`${this.base}/llm`, body);
    }
}
