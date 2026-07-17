# syntax=docker/dockerfile:1
#
# Frontend image — build the Vite/React app, serve the static bundle with nginx.
# Build context is ./frontend.

# --- build stage ---
FROM node:20-alpine AS build
WORKDIR /app
# Cache deps: copy manifests first, then install, then source.
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build          # → /app/dist

# --- serve stage ---
FROM nginx:1.27-alpine AS serve
# SPA-aware nginx config (history fallback + asset caching)
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD wget -qO- http://localhost/ >/dev/null 2>&1 || exit 1
