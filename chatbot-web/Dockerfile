# ─── Stage 1: Build Angular App ────────────────────────────────────────────────
FROM node:20-alpine AS build

WORKDIR /app

# Önce sadece package dosyaları — layer cache için
COPY package*.json ./
RUN npm ci --legacy-peer-deps

# Kaynak kodu kopyala ve build et
COPY . .
RUN npx ng build --configuration=production

# ─── Stage 2: Serve with nginx ─────────────────────────────────────────────────
FROM nginx:1.27-alpine

# Built dosyaları nginx'e kopyala
COPY --from=build /app/dist/chatbot-web /usr/share/nginx/html

# nginx config (SPA routing + API proxy)
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
