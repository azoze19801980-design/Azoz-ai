# AZOZ AI Pro

بوت Telegram عربي لتحليل أزواج الفوركس وصور الشارت عبر Groq، مع فحص سوق وإشارات فنية دورية.

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `python -m telegram_bot.bot` — run the AZOZ AI Pro Telegram bot
- `pip install -r requirements.txt` — install bot dependencies
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required env: `DATABASE_URL` — Postgres connection string
- Bot secrets: `TELEGRAM_BOT_TOKEN`, `GROQ_API_KEY`

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `telegram_bot/` — Telegram bot, Groq vision/chat, Yahoo Finance market analysis, and subscriber persistence
- `requirements.txt` — Python bot dependencies
- `lib/api-spec/openapi.yaml` — API contract source of truth
- `lib/db/src/schema/` — database schema

## Architecture decisions

- Yahoo Finance spot-FX rates are not Quotex OTC quotes; the bot does not label them OTC.
- The displayed 78–93% is a technical-confluence score, not a historically validated win rate.

## Product

Users can scan supported spot-FX pairs, submit chart images for Groq Vision analysis, chat in Arabic about technical/fundamental analysis, and subscribe to recurring signals.

## User preferences

- Keep Telegram and Groq credentials in Replit Secrets, never in source files.

## Gotchas

- Yahoo can return fewer than 100 one-minute candles; market analysis intentionally requires at least 25.
- Telegram signals use Yahoo spot-FX data, not Quotex OTC data, and are not guarantees of profit.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
