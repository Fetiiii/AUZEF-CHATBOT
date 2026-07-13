export type ChatbotLoginRequest = {
    email: string;
    password: string;
    remember?: boolean;
};

export type AdminRole = 'super_admin' | 'admin' | 'editor';

export type AdminUser = {
    id: number;
    email: string;
    full_name: string | null;
    role: AdminRole;
};

/** Rol hiyerarşisi — backend auth.py ROLE_LEVEL ile birebir aynı. */
export const ROLE_LEVEL: Record<AdminRole, number> = {
    editor: 1,
    admin: 2,
    super_admin: 3
};

export function roleLevel(role: string | null | undefined): number {
    return ROLE_LEVEL[(role || '') as AdminRole] ?? 0;
}

export type ChatbotLoginResponse = {
    ok: boolean;
    user?: AdminUser;
    message?: string;
};
