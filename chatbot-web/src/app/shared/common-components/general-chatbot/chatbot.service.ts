import { Injectable, computed, effect, signal } from '@angular/core';

export type Sender = 'user' | 'bot';
export type ActionType = 'navigate' | 'copy';

export interface BotAction {
    label: string;
    type: ActionType;
    value: string; // route or copy text
}

export interface ChatMessage {
    id: string;
    sender: Sender;
    text: string;
    at: number;
    actions?: BotAction[];
}

export type Flow = 'none' | 'create-request';

export interface BotState {
    flow: Flow;
    step: number;
    draft?: {
        title?: string;
        detail?: string;
    };
}

export interface RuleResult {
    text: string;
    actions?: BotAction[];
    suggestions?: string[];
}

interface BotRule {
    id: string;
    patterns: RegExp[];
    run: (userText: string) => RuleResult;
}

@Injectable({ providedIn: 'root' })
export class ChatbotService {
    messages = signal<ChatMessage[]>([]);
    state = signal<BotState>({ flow: 'none', step: 0, draft: {} });
    suggestions = signal<string[]>(this.defaultSuggestions());

    // Typing + Header status
    isTyping = signal<boolean>(false);
    statusText = computed(() => (this.isTyping() ? 'Bekleyiniz…' : 'Çevrim içi'));
    statusDotClass = computed(() => (this.isTyping() ? 'bg-amber-400' : 'bg-emerald-400'));

    private KEY_MSG = 'csm_general_chatbot_messages_v2';
    private KEY_STATE = 'csm_general_chatbot_state_v2';

    private rules: BotRule[] = [
        {
            id: 'greeting',
            patterns: [/^(merhaba|selam|hey|günaydın|iyi akşamlar|iyi geceler)\b/i],
            run: () => ({
                text:
                    'Merhaba 👋 Ben Çözüm Botuyum.\n' +
                    'Aşağıdan bir konu seçebilir veya sorunu yazabilirsin.',
                suggestions: [
                    'Şifre / giriş sorunu',
                    'Sınav / takvim',
                    'Canlı ders bağlantısı',
                    'Talep oluştur',
                    'Talebimin durumu'
                ]
            })
        },
        {
            id: 'login_password',
            patterns: [/şifre|parola|giriş yapam|login|oturum|hesabıma girem/i],
            run: () => ({
                text:
                    'Giriş/şifre için hızlı kontrol:\n' +
                    '1) CapsLock açık mı?\n' +
                    '2) Gizli sekme ile dene\n' +
                    '3) Çerez/cache temizle\n' +
                    '4) “Şifremi unuttum” ile sıfırla\n\n' +
                    'Devam ederse “Talep oluştur” yaz, hızlıca kayıt açalım.',
                actions: [{ label: 'Talep oluştur', type: 'navigate', value: '/create-request' }],
                suggestions: ['Talep oluştur', 'Teknik hata yaşıyorum']
            })
        },
        {
            id: 'exam',
            patterns: [/sınav|final|vize|quiz|takvim|tarih/i],
            run: () => ({
                text:
                    'Sınav/takvim için genelde 2 yer önemli:\n' +
                    '• Duyurular\n' +
                    '• Ders sayfasındaki sınav bilgileri\n\n' +
                    'İstersen ilgili ekrana yönlendirebilirim.',
                actions: [
                    { label: 'Duyurular', type: 'navigate', value: '/announcements' },
                    { label: 'Sınav Takvimi', type: 'navigate', value: '/exam-schedule' }
                ],
                suggestions: ['Talep oluştur', 'Talebimin durumu']
            })
        },
        {
            id: 'live_course',
            patterns: [/canlı ders|zoom|meeting|bağlantı|link|katılam/i],
            run: () => ({
                text:
                    'Canlı ders için hızlı kontrol:\n' +
                    '• Tarih/saat doğru mu?\n' +
                    '• Tarayıcı izinleri (mikrofon/kamera)\n' +
                    '• Farklı tarayıcı ile dene\n\n' +
                    'Dilersen canlı ders sayfasına yönlendirebilirim.',
                actions: [{ label: 'Canlı Dersler', type: 'navigate', value: '/live-courses' }],
                suggestions: ['Teknik hata yaşıyorum', 'Talep oluştur']
            })
        },
        {
            id: 'payment',
            patterns: [/harç|ödeme|ücret|taksit|fatura|dekont/i],
            run: () => ({
                text:
                    'Ödeme/harç konularında genelde şu bilgiler lazım:\n' +
                    '• Hangi dönem?\n' +
                    '• Hata mesajı var mı?\n' +
                    '• Dekont/işlem numarası\n\n' +
                    'İstersen “Talep oluştur” diyerek akış başlatabilirsin.',
                suggestions: ['Talep oluştur', 'Talebimin durumu']
            })
        },
        {
            id: 'request_status',
            patterns: [/talebim|talep durum|durum|bekleyen|id|ticket|başvuru/i],
            run: () => ({
                text:
                    'Talep durumunu “Benim Taleplerim” ekranından takip edebilirsin.\n' +
                    'İstersen yönlendireyim.',
                actions: [{ label: 'Benim Taleplerim', type: 'navigate', value: '/my-requests' }],
                suggestions: ['Talep oluştur']
            })
        },
        {
            id: 'technical',
            patterns: [/hata|error|500|404|çalışmıyor|açılmıyor|beyaz ekran/i],
            run: () => ({
                text:
                    'Teknik hata için hızlı teşhis:\n' +
                    '1) Hard refresh (Ctrl+F5)\n' +
                    '2) Gizli sekme / farklı tarayıcı\n' +
                    '3) Eklenti/AdBlock kapat\n' +
                    '4) Hata metnini aynen kopyala\n\n' +
                    'İstersen “Talep oluştur” deyip kayıt açalım.',
                suggestions: ['Talep oluştur']
            })
        }
    ];

    constructor() {
        this.restore();

        if (this.messages().length === 0) {
            this.pushBot('Merhaba 👋 Ben Çözüm Botuyum. Aşağıdan bir konu seçebilir veya sorunu yazabilirsin.');
        }

        effect(() => {
            const msgs = this.messages();
            const st = this.state();
            try {
                localStorage.setItem(this.KEY_MSG, JSON.stringify(msgs));
                localStorage.setItem(this.KEY_STATE, JSON.stringify(st));
            } catch {
                // ignore
            }
        });
    }

    reset() {
        this.messages.set([]);
        this.state.set({ flow: 'none', step: 0, draft: {} });
        this.suggestions.set(this.defaultSuggestions());
        this.isTyping.set(false);
        this.pushBot('Sıfırlandı ✅ Konunu yaz veya aşağıdaki önerilerden seç.');
    }

    // ✅ Component'in copy/navigate sonrası mesaj bırakması için
    notify(text: string) {
        this.pushBot(text);
    }

    async sendUserMessage(raw: string) {
        const text = (raw ?? '').trim();
        if (!text) return;

        if (this.isTyping()) return;

        this.pushUser(text);
        this.isTyping.set(true);

        // “insan gibi” bekleme
        const base = 420;
        const extra = Math.min(950, text.length * 18);
        await this.delay(base + extra);

        const st = this.state();

        // Flow
        if (st.flow !== 'none') {
            await this.delay(160);
            this.handleFlow(text);
            this.isTyping.set(false);
            return;
        }

        // Flow tetikleyici
        if (this.includesAny(text, ['talep oluştur', 'talep aç', 'ticket oluştur', 'başvuru oluştur'])) {
            await this.delay(140);
            this.startCreateRequestFlow();
            this.isTyping.set(false);
            return;
        }

        // Rules
        const res = this.runRules(text);
        await this.delay(120);
        this.pushBot(res.text, res.actions);
        if (res.suggestions?.length) this.suggestions.set(res.suggestions);

        this.isTyping.set(false);
    }

    // ---------------- FLOW ----------------
    private startCreateRequestFlow() {
        this.state.set({ flow: 'create-request', step: 0, draft: {} });
        this.pushBot('Talep oluşturalım ✅ Önce kısa bir başlık yaz: (örn: “Giriş yapamıyorum”)');
        this.suggestions.set(['İptal', 'Şifre / giriş sorunu', 'Teknik hata yaşıyorum']);
    }

    private handleFlow(userText: string) {
        const t = this.normalize(userText);

        if (t === 'iptal') {
            this.state.set({ flow: 'none', step: 0, draft: {} });
            this.pushBot('İptal edildi ✅ Başka bir konuda yardımcı olayım mı?');
            this.suggestions.set(this.defaultSuggestions());
            return;
        }

        const st = this.state();

        if (st.flow === 'create-request') {
            if (st.step === 0) {
                this.state.set({ flow: 'create-request', step: 1, draft: { title: userText } });
                this.pushBot('Detay yaz: Hata mesajı / tarih / ekran adı gibi bilgileri ekle.');
                return;
            }

            if (st.step === 1) {
                const title = st.draft?.title ?? 'Talep';
                const detail = userText;

                this.state.set({ flow: 'none', step: 0, draft: {} });

                const payload = `Başlık: ${title}\nDetay: ${detail}`;

                this.pushBot(
                    'Harika ✅ Aşağıdaki metni talep formuna yapıştırabilirsin. İstersen kopyalayayım ve talep sayfasına götüreyim:',
                    [
                        { label: 'Metni kopyala', type: 'copy', value: payload },
                        { label: 'Talep sayfası', type: 'navigate', value: '/create-request' }
                    ]
                );

                this.suggestions.set(['Talebimin durumu', 'Teknik hata yaşıyorum', 'Canlı ders bağlantısı']);
                return;
            }
        }

        this.state.set({ flow: 'none', step: 0, draft: {} });
    }

    // ---------------- RULES ----------------
    private runRules(userText: string): RuleResult {
        const text = this.normalize(userText);

        for (const r of this.rules) {
            if (r.patterns.some(p => p.test(text))) return r.run(userText);
        }

        return {
            text:
                'Bunu tam eşleştiremedim. Şunlardan hangisine yakın?\n' +
                '• Giriş/şifre\n' +
                '• Sınav/takvim\n' +
                '• Canlı ders\n' +
                '• Teknik hata\n' +
                '• Talep durumu\n\n' +
                'İstersen “Talep oluştur” yazıp hızlıca kayıt açalım.',
            suggestions: this.defaultSuggestions()
        };
    }

    // ---------------- MESSAGES ----------------
    private pushUser(text: string) {
        this.messages.update(arr => [...arr, { id: this.uid(), sender: 'user', text, at: Date.now() }]);
    }

    private pushBot(text: string, actions?: BotAction[]) {
        this.messages.update(arr => [...arr, { id: this.uid(), sender: 'bot', text, at: Date.now(), actions }]);
    }

    // ---------------- STORAGE ----------------
    private restore() {
        try {
            const m = localStorage.getItem(this.KEY_MSG);
            const s = localStorage.getItem(this.KEY_STATE);

            if (m) {
                const parsed = JSON.parse(m) as ChatMessage[];
                if (Array.isArray(parsed)) this.messages.set(parsed);
            }

            if (s) {
                const parsed = JSON.parse(s) as BotState;
                if (parsed && typeof parsed === 'object') this.state.set(parsed);
            }
        } catch {
            // ignore
        }
    }

    // ---------------- HELPERS ----------------
    private defaultSuggestions(): string[] {
        return [
            'Şifre / giriş sorunu',
            'Sınav / takvim',
            'Canlı ders bağlantısı',
            'Talep oluştur',
            'Talebimin durumu',
            'Teknik hata yaşıyorum'
        ];
    }

    private uid(): string {
        try {
            return crypto.randomUUID();
        } catch {
            return `${Date.now()}_${Math.random().toString(16).slice(2)}`;
        }
    }

    private normalize(s: string): string {
        return (s ?? '').toLocaleLowerCase('tr-TR').trim();
    }

    private includesAny(raw: string, list: string[]): boolean {
        const t = this.normalize(raw);
        return list.some(x => t.includes(this.normalize(x)));
    }

    private delay(ms: number) {
        return new Promise<void>(resolve => setTimeout(resolve, ms));
    }
}
