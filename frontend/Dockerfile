FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install --legacy-peer-deps
COPY . .
RUN npm run build

FROM nginx:alpine
# openssl: генерация самоподписанного TLS-сертификата при первом запуске (ТЗ 13)
RUN apk add --no-cache openssl
COPY --from=build /app/dist /usr/share/nginx/html
# /etc/nginx/snippets/ (не conf.d/) — nginx:alpine's nginx.conf делает
# `include /etc/nginx/conf.d/*.conf` в контексте http{}, где директива
# location недопустима; снипет должен подключаться только явно, изнутри
# server{}.
COPY nginx-locations.conf /etc/nginx/snippets/nginx-locations.conf
COPY nginx-security-headers.conf /etc/nginx/snippets/nginx-security-headers.conf
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh
EXPOSE 80 443
ENTRYPOINT ["/docker-entrypoint.sh"]
# worker_shutdown_timeout: STOPSIGNAL образа nginx — SIGQUIT (плавная
# остановка), а она ждёт закрытия проксируемых WebSocket Стены и статусов
# камер, то есть не кончается вовсе, пока вкладка открыта. Замерено: без
# директивы nginx не вышел и за 25 с (Docker добивает через 10 с, код 137,
# D09), с ней — 5.0 с. Директива main-контекста, в conf.d её не задать.
CMD ["nginx", "-g", "daemon off; worker_shutdown_timeout 5s;"]
