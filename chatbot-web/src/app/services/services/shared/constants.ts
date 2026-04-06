import { isDevMode } from "@angular/core";

export const apiUrl = isDevMode()
    ? 'http://localhost:4200'
    : 'https://161.9.141.143:8080';