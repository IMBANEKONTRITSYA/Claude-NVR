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
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh
EXPOSE 80 443
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]
