import { ChangeDetectionStrategy, ChangeDetectorRef, Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpErrorResponse } from '@angular/common/http';

import {
    AIConfigApiService,
    AIConfigVersionRow,
    AIConfigView
} from '../../services/services/chatbot/ai-config-api.service';
import { ChatbotAuthService } from '../../services/services/chatbot/chatbot-auth.service';
import { roleLevel } from '../../services/models/chatbot/chatbot-auth.model';
import {
    AICapability,
    AIRegistryModel,
    ReasoningEffort,
    apiErrorMessage,
    changeConfirmation,
    eligibleModels,
    normalizeReasoning,
    optionalNumber,
    reasoningControlEnabled,
    reasoningOptions
} from './ai-config.logic';

interface CapabilityDraft {
    model_registry_id: number | null;
    temperature: number;
    max_tokens: number;
    reasoning_effort: ReasoningEffort | null;
    timeout_seconds: number | string | null;
    max_retries: number | string | null;
}

/**
 * AI Model Yapılandırması — okuma: admin, değiştirme: super_admin.
 * Gerçek yetki ve doğrulama backend'dedir (`/api/ai-config`); bu ekran yalnız
 * registry'deki uygun modelleri seçtirir, serbest model metni kabul etmez.
 */
@Component({
    standalone: true,
    selector: 'app-chatbot-ai-config',
    imports: [CommonModule, FormsModule],
    templateUrl: './ai-config.component.html',
    changeDetection: ChangeDetectionStrategy.OnPush
})
export class AIConfigComponent implements OnInit {
    readonly capabilities: { key: AICapability; label: string }[] = [
        { key: 'intent_analyzer', label: 'Intent Analyzer' },
        { key: 'selector', label: 'Selector' }
    ];

    toastMessage = signal<string | null>(null);
    toastType = signal<'success' | 'error'>('success');

    config = signal<AIConfigView | null>(null);
    models = signal<AIRegistryModel[]>([]);
    history = signal<AIConfigVersionRow[]>([]);
    loading = signal(true);
    saving = signal<string | null>(null);

    drafts: Record<AICapability, CapabilityDraft> = {
        intent_analyzer: this.emptyDraft(),
        selector: this.emptyDraft()
    };

    newModel = {
        display_name: '',
        provider: 'openrouter' as AIRegistryModel['provider'],
        model_identifier: '',
        intent_analyzer: false,
        selector: true,
        supports_structured_output: true
    };


    constructor(
        private api: AIConfigApiService,
        private auth: ChatbotAuthService,
        private cdr: ChangeDetectorRef
    ) { }

    ngOnInit(): void {
        this.reload();
    }

    /** Yazma kontrolleri yalnız super_admin'e (UX; backend ayrıca zorlar). */
    canManage(): boolean {
        return roleLevel(this.auth.user()?.role) >= 3;
    }

    private emptyDraft(): CapabilityDraft {
        return {
            model_registry_id: null, temperature: 0, max_tokens: 0,
            reasoning_effort: null, timeout_seconds: null, max_retries: null
        };
    }

    reload(): void {
        this.loading.set(true);
        this.api.getModels().subscribe({
            next: (res) => { this.models.set(res.models); this.cdr.markForCheck(); },
            error: (err) => this.fail(err)
        });
        this.api.getHistory().subscribe({
            next: (res) => { this.history.set(res.versions); this.cdr.markForCheck(); },
            error: (err) => this.fail(err)
        });
        this.api.getConfig().subscribe({
            next: (view) => {
                this.config.set(view);
                for (const cap of this.capabilities) {
                    const current = view.capabilities[cap.key];
                    this.drafts[cap.key] = {
                        model_registry_id: current.model?.id ?? null,
                        temperature: current.temperature ?? 0,
                        max_tokens: current.max_tokens ?? current.max_tokens_range[0],
                        reasoning_effort: current.reasoning_effort ?? null,
                        timeout_seconds: current.timeout_seconds ?? null,
                        max_retries: current.max_retries ?? null
                    };
                }
                this.loading.set(false);
                this.cdr.markForCheck();
            },
            error: (err) => { this.loading.set(false); this.fail(err); }
        });
    }

    options(capability: AICapability): AIRegistryModel[] {
        const view = this.config()?.capabilities[capability];
        return eligibleModels(this.models(), capability, view?.eligible_model_ids, view?.model?.id ?? null);
    }

    selectedModel(capability: AICapability): AIRegistryModel | null {
        const id = this.drafts[capability].model_registry_id;
        return this.models().find((m) => m.id === Number(id)) ?? null;
    }

    reasoningOptions(capability: AICapability): ReasoningEffort[] {
        return reasoningOptions(this.selectedModel(capability));
    }

    reasoningEnabled(capability: AICapability): boolean {
        return reasoningControlEnabled(this.selectedModel(capability));
    }

    onModelChange(capability: AICapability): void {
        const draft = this.drafts[capability];
        draft.reasoning_effort = normalizeReasoning(this.selectedModel(capability), draft.reasoning_effort);
    }

    save(capability: AICapability): void {
        const view = this.config();
        const draft = this.drafts[capability];
        if (!view || draft.model_registry_id == null) return;
        const message = changeConfirmation(capability, view.capabilities[capability].model, this.selectedModel(capability));
        if (message && !window.confirm(message)) return;
        this.saving.set(capability);
        this.api.updateCapability(capability, {
            expected_version: view.version,
            model_registry_id: Number(draft.model_registry_id),
            temperature: Number(draft.temperature),
            max_tokens: Number(draft.max_tokens),
            reasoning_effort: draft.reasoning_effort,
            timeout_seconds: optionalNumber(draft.timeout_seconds),
            max_retries: optionalNumber(draft.max_retries),
            structured_output_enabled: false
        }).subscribe({
            next: (res) => {
                this.saving.set(null);
                if (res.warnings?.includes('provider_key_missing')) {
                    this.toast(`Kaydedildi → v${res.version}, ancak provider API anahtarı tanımlı değil: LLM çağrıları CONFIG_DEGRADED olacak.`, 'error');
                } else {
                    this.toast(res.changed ? `Kaydedildi → v${res.version}` : 'Değişiklik yok.');
                }
                this.reload();
            },
            error: (err) => { this.saving.set(null); this.fail(err, true); }
        });
    }

    rollback(row: AIConfigVersionRow): void {
        const view = this.config();
        if (!view) return;
        if (!window.confirm(`Aktif v${view.version} yerine v${row.version} snapshot'ı yeni bir versiyon olarak etkinleştirilecek. Devam edilsin mi?`)) {
            return;
        }
        this.saving.set(`rollback-${row.version}`);
        this.api.rollback(row.version, view.version).subscribe({
            next: (res) => { this.saving.set(null); this.toast(`Geri alındı → v${res.version}`); this.reload(); },
            error: (err) => { this.saving.set(null); this.fail(err, true); }
        });
    }

    toggleModel(model: AIRegistryModel): void {
        this.api.updateModel(model.id, { enabled: !model.enabled }).subscribe({
            next: () => { this.toast(model.enabled ? 'Model devre dışı bırakıldı.' : 'Model etkinleştirildi.'); this.reload(); },
            error: (err) => this.fail(err)
        });
    }

    qualify(model: AIRegistryModel): void {
        const reference = window.prompt('Qualification referansı (değerlendirme run/artifact id):');
        if (!reference || !reference.trim()) return;
        this.api.updateModel(model.id, { qualification_status: 'QUALIFIED', qualification_reference: reference.trim() }).subscribe({
            next: () => { this.toast('Model QUALIFIED olarak işaretlendi.'); this.reload(); },
            error: (err) => this.fail(err)
        });
    }

    block(model: AIRegistryModel): void {
        if (!window.confirm(`${model.display_name} BLOCKED olarak işaretlensin mi?`)) return;
        this.api.updateModel(model.id, { qualification_status: 'BLOCKED' }).subscribe({
            next: () => { this.toast('Model BLOCKED.'); this.reload(); },
            error: (err) => this.fail(err)
        });
    }

    createModel(): void {
        const caps: AICapability[] = [];
        if (this.newModel.intent_analyzer) caps.push('intent_analyzer');
        if (this.newModel.selector) caps.push('selector');
        this.api.createModel({
            display_name: this.newModel.display_name.trim(),
            provider: this.newModel.provider,
            model_identifier: this.newModel.model_identifier.trim(),
            allowed_capabilities: caps,
            supports_structured_output: this.newModel.supports_structured_output,
            supports_reasoning_effort: false,
            allowed_reasoning_efforts: []
        }).subscribe({
            next: () => {
                this.toast('Model registry’ye eklendi (UNTESTED).');
                this.newModel.display_name = '';
                this.newModel.model_identifier = '';
                this.reload();
            },
            error: (err) => this.fail(err)
        });
    }

    summary(row: AIConfigVersionRow): string {
        const caps = row.snapshot?.capabilities;
        if (!caps) return row.summary ?? '';
        return `IA: ${caps.intent_analyzer?.provider}/${caps.intent_analyzer?.model} · SEL: ${caps.selector?.provider}/${caps.selector?.model}`;
    }

    private toast(message: string, type: 'success' | 'error' = 'success'): void {
        this.toastMessage.set(message);
        this.toastType.set(type);
        this.cdr.markForCheck();
        setTimeout(() => { this.toastMessage.set(null); this.cdr.markForCheck(); }, 4000);
    }

    private fail(err: HttpErrorResponse, reloadOnConflict = false): void {
        this.toast(apiErrorMessage(err), 'error');
        if (reloadOnConflict && err.status === 409) this.reload();
    }
}
