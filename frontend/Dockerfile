FROM node:22-alpine AS build

WORKDIR /app

COPY frontend/package.json frontend/package-lock.json /app/
RUN npm ci

COPY frontend /app
ARG VITE_API_BASE_URL=
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
RUN npm run build

FROM nginx:1.27-alpine

COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
