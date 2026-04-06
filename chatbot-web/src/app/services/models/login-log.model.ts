// src/app/shared/services/models/login-log.model.ts

export interface UserLookupDto {
    id: string;                 // Guid => string
    avatar?: string | null;
    name?: string | null;
    createdDate: string; // DateTime (ISO string)

}

export interface LoginLogListItemDto {
    id: string;                 // Guid => string
    user: UserLookupDto;
    userAgent?: string | null;  // string? => optional + null
    createdDate: string; // DateTime (ISO string)
}

// Backend: StudentGetLoginHistoryResponse(List<LoginLogListItemDto> LoginLogList)
// JSON genelde camelCase => loginLogList
export interface StudentGetLoginHistoryResponse {
    loginLogList: LoginLogListItemDto[];
}
