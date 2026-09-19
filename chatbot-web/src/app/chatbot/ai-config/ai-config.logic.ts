/**
 * AI config ekranının saf (framework'süz) karar mantığı.
 *
 * Backend (`services/ai_registry.py`) doğrulamanın otoritatif kaynağıdır; bu
 * dosya yalnız aynı kuralları UX için yansıtır. Import içermez ve yalnız
 * silinebilir TypeScript sözdizimi kullanır, böylece `node --test` ile
 * tarayıcısız test edilebilir.
 */

export type AICapability = 'intent_analyzer' | 'selector';
export type ReasoningEffort = 'none' | 'low' | 'medium' | 'high';
export type QualificationStatus = 'UNTESTED' | 'QUALIFIED' | 'LEGACY_APPROVED' | 'BLOCKED';

export interface AIRegistryModel {
    id: number;
    display_name: string;
    provider: 'openai' | 'openrouter' | 'gemini';
    model_identifier: string;
    enabled: boolean;
    allowed_capabilities: AICapability[];
    supports_structured_output: boolean;
    supports_reasoning_effort: boolean;
    allowed_reasoning_efforts: ReasoningEffort[];
    qualification_status: QualificationStatus;
    qualified_at: string | null;
    qualified_by: string | null;
    qualification_reference: string | null;
}

export const ASSIGNABLE_QUALIFICATIONS: readonly QualificationStatus[] = [
    'QUALIFIED',
    'LEGACY_APPROVED'
];

/** Bir model bu capability'ye atanabilir mi? (backend validate_assignment aynası) */
export function isAssignable(model: AIRegistryModel, capability: AICapability): boolean {
    return (
        model.enabled &&
        ASSIGNABLE_QUALIFICATIONS.includes(model.qualification_status) &&
        model.allowed_capabilities.includes(capability) &&
        model.supports_structured_output
    );
}

/**
 * Dropdown seçenekleri: yalnız registry'deki uygun modeller. Backend'in
 * döndürdüğü `eligible_model_ids` verilirse onunla da kesiştirilir. Mevcut
 * atama uygunluğunu yitirmişse bile görünür kalır (sessizce kaybolmasın).
 */
export function eligibleModels(
    models: AIRegistryModel[],
    capability: AICapability,
    backendEligibleIds?: number[] | null,
    currentModelId?: number | null
): AIRegistryModel[] {
    return models.filter((m) => {
        if (currentModelId != null && m.id === currentModelId) return true;
        if (!isAssignable(m, capability)) return false;
        return backendEligibleIds ? backendEligibleIds.includes(m.id) : true;
    });
}

/** Reasoning kontrolü: modeli desteklemiyorsa kapalı, yalnız izinli seviyeler. */
export function reasoningOptions(model: AIRegistryModel | null | undefined): ReasoningEffort[] {
    if (!model || !model.supports_reasoning_effort) return [];
    return [...model.allowed_reasoning_efforts];
}

export function reasoningControlEnabled(model: AIRegistryModel | null | undefined): boolean {
    return reasoningOptions(model).length > 0;
}

/** Model değişirse, yeni modelde geçersiz kalan reasoning değerini temizle. */
export function normalizeReasoning(
    model: AIRegistryModel | null | undefined,
    value: ReasoningEffort | null
): ReasoningEffort | null {
    if (value == null) return null;
    return reasoningOptions(model).includes(value) ? value : null;
}

/** Kaydetmeden önce gösterilecek kısa onay metni (model değişikliği yoksa null). */
export function changeConfirmation(
    capability: AICapability,
    current: AIRegistryModel | null | undefined,
    next: AIRegistryModel | null | undefined
): string | null {
    if (!next || (current && current.id === next.id)) return null;
    const label = capability === 'selector' ? 'Selector' : 'Intent Analyzer';
    const from = current ? `${current.provider}/${current.model_identifier}` : '—';
    return `${label} modelini ${from} → ${next.provider}/${next.model_identifier} olarak değiştirmek üzeresiniz.`;
}

const ERROR_MESSAGES: Record<string, string> = {
    stale_version: 'Yapılandırma bu arada başka bir yönetici tarafından değiştirildi. Sayfa yenilendi; değişikliğinizi tekrar uygulayın.',
    model_not_qualified: 'Bu model production için henüz qualify edilmemiş.',
    model_disabled: 'Bu model devre dışı.',
    capability_not_allowed: 'Bu model bu capability için izinli değil.',
    structured_output_unsupported: 'Bu capability strict JSON çıktı gerektirir; model desteklemiyor.',
    model_in_use: 'Model aktif bir atamada kullanılıyor; önce capability’yi başka modele taşıyın.',
    rollback_invalid: 'Bu versiyon mevcut registry kurallarıyla geçersiz olduğu için geri alınamaz.',
    duplicate_model: 'Bu provider/model kombinasyonu registry’de zaten var.'
};

/** API hatasını kullanıcıya anlaşılır tek satıra çevirir. */
export function apiErrorMessage(error: { status?: number; error?: unknown } | null | undefined): string {
    const body = (error?.error ?? null) as { code?: string; detail?: unknown } | null;
    if (error?.status === 403) return 'Bu işlem için yetkiniz yok.';
    if (error?.status === 401) return 'Oturumunuz sona ermiş; tekrar giriş yapın.';
    if (body?.code && ERROR_MESSAGES[body.code]) {
        const detail = typeof body.detail === 'string' ? ` (${body.detail})` : '';
        return ERROR_MESSAGES[body.code] + (body.code === 'stale_version' ? '' : detail);
    }
    if (typeof body?.detail === 'string') return body.detail;
    if (Array.isArray(body?.detail)) return 'Geçersiz değer: alanları kontrol edin.';
    return 'Beklenmeyen bir hata oluştu.';
}

/** Sayı input'undan boş = provider varsayılanı (null). */
export function optionalNumber(value: unknown): number | null {
    if (value === null || value === undefined || value === '') return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
}
