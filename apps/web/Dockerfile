# syntax=docker/dockerfile:1

# Readable Bun 1 slim tag retained; digest pins the multi-arch index.
FROM docker.io/oven/bun:1.4.4-slim@sha256:1846adf22496bd711036aae8be147e57bdebbaea2e5264e3e0b5af1db187d9a4 AS deps
WORKDIR /app

COPY package.json bun.lock ./
RUN bun install --frozen-lockfile

FROM docker.io/oven/bun:1.4.4-slim@sha256:1846adf22496bd711036aae8be147e57bdebbaea2e5264e3e0b5af1db187d9a4 AS builder
WORKDIR /app

ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN bun --bun next build
RUN bun -e 'const routes = require("./.next/server/app-paths-manifest.json"); for (const route of ["/api/reports/route", "/api/social/reports/route", "/api/social/moderation/reports/route", "/api/social/moderation/reports/[id]/route"]) { if (!routes[route]) throw new Error(`Missing report API: ${route}`); }'

FROM docker.io/oven/bun:1.4.4-slim@sha256:1846adf22496bd711036aae8be147e57bdebbaea2e5264e3e0b5af1db187d9a4 AS runner
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000

COPY --from=builder --chown=bun:bun /app/.next/standalone ./
COPY --from=builder --chown=bun:bun /app/.next/static ./.next/static

USER bun
EXPOSE 3000
CMD ["bun", "server.js"]
