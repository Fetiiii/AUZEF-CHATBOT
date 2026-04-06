export type ChatbotLoginRequest = {
    username: string;
    password: string;
    remember?: boolean;
};

export type ChatbotLoginResponse = {
    ok: boolean;
    message?: string;
};
