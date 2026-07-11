export type ChatbotLoginRequest = {
    email: string;
    password: string;
    remember?: boolean;
};

export type AdminUser = {
    id: number;
    email: string;
    full_name: string | null;
};

export type ChatbotLoginResponse = {
    ok: boolean;
    user?: AdminUser;
    message?: string;
};
