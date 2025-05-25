# Stage 1: build
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --omit=dev
COPY . ./
RUN npm run build

# Stage 2: runtime
FROM node:20-alpine
WORKDIR /app
COPY --from=build /app/dist ./dist
COPY package.json package-lock.json* ./
RUN npm ci --omit=dev && adduser -D mcpuser
USER mcpuser
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD curl -f http://localhost:3000/health || exit 1
CMD ["node", "dist/main.js"]
