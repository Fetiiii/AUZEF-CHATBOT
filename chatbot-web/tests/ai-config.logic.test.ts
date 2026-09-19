/**
 * AI config ekranı karar mantığı testleri (tarayıcısız).
 *
 * Projede Angular test harness'ı (Karma/Jasmine/Jest) yok; bu testler Node'un
 * yerleşik test koşucusuyla çalışır:
 *     node --test tests/
 * (Node ≥ 22.18 TypeScript type-stripping gerekir.)
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import type { AIRegistryModel } from '../src/app/chatbot/ai-config/ai-config.logic.ts';
import {
    apiErrorMessage,
    changeConfirmation,
    eligibleModels,
    isAssignable,
    normalizeReasoning,
    optionalNumber,
    reasoningControlEnabled,
    reasoningOptions
} from '../src/app/chatbot/ai-config/ai-config.logic.ts';

function model(overrides: Partial<AIRegistryModel> = {}): AIRegistryModel {
    return {
        id: 1,
        display_name: 'gpt-4o-mini',
        provider: 'openrouter',
        model_identifier: 'openai/gpt-4o-mini',
        enabled: true,
        allowed_capabilities: ['intent_analyzer', 'selector'],
        supports_structured_output: true,
        supports_reasoning_effort: false,
        allowed_reasoning_efforts: [],
        qualification_status: 'LEGACY_APPROVED',
        qualified_at: null,
        qualified_by: null,
        qualification_reference: null,
        ...overrides
    };
}

test('dropdown shows only enabled, qualified, capability-compatible, structured models', () => {
    const models = [
        model({ id: 1 }),
        model({ id: 2, enabled: false }),
        model({ id: 3, qualification_status: 'UNTESTED' }),
        model({ id: 4, qualification_status: 'BLOCKED' }),
        model({ id: 5, allowed_capabilities: ['intent_analyzer'] }),
        model({ id: 6, supports_structured_output: false }),
        model({ id: 7, qualification_status: 'QUALIFIED' })
    ];
    assert.deepEqual(eligibleModels(models, 'selector').map((m) => m.id), [1, 7]);
    assert.equal(isAssignable(models[2], 'selector'), false);
});

test('backend eligible ids are authoritative and the current assignment stays visible', () => {
    const models = [model({ id: 1 }), model({ id: 7, qualification_status: 'QUALIFIED' }), model({ id: 9, enabled: false })];
    assert.deepEqual(eligibleModels(models, 'selector', [7]).map((m) => m.id), [7]);
    assert.deepEqual(eligibleModels(models, 'selector', [7], 9).map((m) => m.id), [7, 9]);
});

test('reasoning control is disabled for unsupported models and limited to allowed levels', () => {
    assert.deepEqual(reasoningOptions(model()), []);
    assert.equal(reasoningControlEnabled(model()), false);
    const reasoner = model({ supports_reasoning_effort: true, allowed_reasoning_efforts: ['low', 'medium'] });
    assert.deepEqual(reasoningOptions(reasoner), ['low', 'medium']);
    assert.equal(reasoningControlEnabled(reasoner), true);
    assert.equal(normalizeReasoning(reasoner, 'high'), null);
    assert.equal(normalizeReasoning(reasoner, 'low'), 'low');
    assert.equal(normalizeReasoning(model(), 'low'), null);
});

test('model change asks for confirmation; parameter-only change does not', () => {
    const current = model({ id: 1 });
    const next = model({ id: 2, provider: 'openai', model_identifier: 'gpt-4o-mini' });
    assert.equal(changeConfirmation('selector', current, current), null);
    assert.equal(
        changeConfirmation('selector', current, next),
        'Selector modelini openrouter/openai/gpt-4o-mini → openai/gpt-4o-mini olarak değiştirmek üzeresiniz.'
    );
});

test('API validation errors become understandable messages', () => {
    assert.match(apiErrorMessage({ status: 409, error: { code: 'stale_version', detail: 'x' } }), /başka bir yönetici/);
    assert.match(
        apiErrorMessage({ status: 400, error: { code: 'model_not_qualified', detail: 'UNTESTED' } }),
        /qualify edilmemiş\. \(UNTESTED\)/
    );
    assert.equal(apiErrorMessage({ status: 403, error: {} }), 'Bu işlem için yetkiniz yok.');
    assert.equal(apiErrorMessage({ status: 400, error: { detail: 'Serbest mesaj' } }), 'Serbest mesaj');
    assert.match(apiErrorMessage({ status: 422, error: { detail: [{ loc: ['body'] }] } }), /Geçersiz değer/);
    assert.equal(apiErrorMessage(null), 'Beklenmeyen bir hata oluştu.');
});

test('empty numeric inputs mean provider default', () => {
    assert.equal(optionalNumber(''), null);
    assert.equal(optionalNumber(null), null);
    assert.equal(optionalNumber('8'), 8);
    assert.equal(optionalNumber('abc'), null);
});
