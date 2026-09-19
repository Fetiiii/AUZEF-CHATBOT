import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';

import {
    AICapability,
    AIRegistryModel,
    ReasoningEffort
} from '../../../chatbot/ai-config/ai-config.logic';

export interface AICapabilityView {
    eligible_model_ids: number[];
    max_tokens_range: [number, number];
    model?: AIRegistryModel;
    temperature?: number;
    max_tokens?: number;
    reasoning_effort?: ReasoningEffort | null;
    timeout_seconds?: number | null;
    max_retries?: number | null;
    structured_output_enabled?: boolean;
    config_fingerprint?: string;
    /** Atanan provider'ın API anahtarı var mı (değer asla dönmez). */
    provider_key_configured: boolean | null;
}

export interface AIConfigView {
    status: 'OK' | 'NOT_CONFIGURED' | 'CONFIG_INVALID';
    error: string | null;
    version: number | null;
    llm_enabled: boolean;
    capabilities: Record<AICapability, AICapabilityView>;
    bounds: {
        temperature: [number, number];
        timeout_seconds: [number, number];
        max_retries: [number, number];
    };
    propagation_seconds: number;
}

export interface AICapabilityUpdate {
    expected_version: number | null;
    model_registry_id: number;
    temperature: number;
    max_tokens: number;
    reasoning_effort: ReasoningEffort | null;
    timeout_seconds: number | null;
    max_retries: number | null;
    structured_output_enabled: boolean;
}

export interface AIConfigVersionRow {
    version: number;
    change_type: 'BOOTSTRAP' | 'UPDATE' | 'ROLLBACK';
    previous_version: number | null;
    source_version: number | null;
    summary: string | null;
    created_by: string | null;
    created_at: string | null;
    snapshot: { capabilities: Record<AICapability, { provider: string; model: string }> };
}

export interface AIModelCreate {
    display_name: string;
    provider: AIRegistryModel['provider'];
    model_identifier: string;
    allowed_capabilities: AICapability[];
    supports_structured_output: boolean;
    supports_reasoning_effort: boolean;
    allowed_reasoning_efforts: ReasoningEffort[];
}

/** AI model registry / capability config API (okuma: admin, yazma: super_admin). */
@Injectable({ providedIn: 'root' })
export class AIConfigApiService {
    private readonly base = '/api/ai-config';

    constructor(private http: HttpClient) { }

    getConfig(): Observable<AIConfigView> {
        return this.http.get<AIConfigView>(`${this.base}/config`);
    }

    getModels(): Observable<{ models: AIRegistryModel[] }> {
        return this.http.get<{ models: AIRegistryModel[] }>(`${this.base}/models`);
    }

    getHistory(limit = 50): Observable<{ versions: AIConfigVersionRow[] }> {
        return this.http.get<{ versions: AIConfigVersionRow[] }>(`${this.base}/config/history`, {
            params: { limit }
        });
    }

    updateCapability(capability: AICapability, body: AICapabilityUpdate): Observable<{ version: number; changed: boolean; warnings?: string[] }> {
        return this.http.put<{ version: number; changed: boolean; warnings?: string[] }>(`${this.base}/config/${capability}`, body);
    }

    rollback(version: number, expectedVersion: number | null): Observable<{ version: number }> {
        return this.http.post<{ version: number }>(`${this.base}/config/rollback/${version}`, {
            expected_version: expectedVersion
        });
    }

    createModel(body: AIModelCreate): Observable<AIRegistryModel> {
        return this.http.post<AIRegistryModel>(`${this.base}/models`, body);
    }

    updateModel(id: number, body: Partial<AIRegistryModel> & { qualification_reference?: string }): Observable<AIRegistryModel> {
        return this.http.patch<AIRegistryModel>(`${this.base}/models/${id}`, body);
    }
}
