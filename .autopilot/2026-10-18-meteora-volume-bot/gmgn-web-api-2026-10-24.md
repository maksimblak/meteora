# GMGN: внутренний API сайта — разведка 2026-10-24

Пользователь: «предлагаю сначала попробовать зареверсить гмгн», «да изучи нужные нам ручки». Разведка в обычном (не headless) Chromium Playwright, вход в аккаунт сделал пользователь. Токены: Bonk `DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263` (старый), Grok `Dx7D4Xg338GoUUjqPb4DXVYc43Ko1e9gjAKiJ7acpump` (2 ч от запуска, Pump.fun → pump_amm).

## Где это работает, а где нет

- **Изнутри страницы gmgn.ai** (тот же origin, `fetch` в контексте открытой вкладки) — все адреса ниже отвечают 200, `code: 0`. Служебные параметры запроса (`device_id`, `fp_did`, `client_id`, `app_ver`, `tz_*`, `app_lang`, `os`, `worker`) не обязательны: `token_stat` без них — 200.
- **Снаружи браузера** (обычный HTTP-клиент, без cookie) — и API, и сама страница токена: **403 от Cloudflare** (проверено 2026-10-24). Пропуск — cookie `__cf_bm` (~30 мин) и зашифрованный токен, привязанные к браузеру, плюс сессия `sid`/`_wt`. Перенос cookie в бота = обход антибота — **не делаем** (решение оркестратора, сказано пользователю).
- Значит, единственный путь без ключа OpenAPI — **браузер-посредник**: видимый Chrome с входом пользователя; бот просит его выполнить `fetch` этих адресов изнутри страницы. Против правил GMGN (риск бана аккаунта с торговым кошельком), живёт, пока открыт браузер, ломается при обновлении сайта (`app_ver=20261023-…`).

## Адреса

Все ответы — `{"code":0,"message":"success","data":…}`. `sol` — цепочка.

| Метод, адрес | Тело | Что внутри (сверено со страницей) |
|---|---|---|
| `POST /api/v1/mutil_window_token_info` | `{"chain":"sol","addresses":[mint,…]}` — **пачка** | `price.volume_{1m,5m,1h,6h,24h}` (USD), `buys_*`, `sells_*`, `swaps_*`, `price.price`, `holder_count`, `circulating_supply`, `total_supply`, `liquidity`, `creation_timestamp`, `open_timestamp` (unix c), `total_fee` (**SOL**, на странице «Total Fees 0.79»), `priority_fee`, `tip_fee`, `trade_fee`, `dev.creator_address`, `dev.creator_token_balance`, `dev.creator_token_status` (видели `creator_close`), `dev.top_10_holder_rate`, `dev.cto_flag`, `dev.twitter_*`, `image_dup_count`, `biggest_pool_address`, `pool.exchange` |
| `GET /api/v1/token_stat/sol/{mint}` | — | `top_10_holder_rate`, `creator_hold_rate` (страница «DEV 0%»), `dev_team_hold_rate`, `top70_sniper_hold_rate` («Snipers 13.55%»), `top_bundler_trader_percentage` («Bundler 13.6%»), `top_rat_trader_percentage` («Insiders 80%»), `top_entrapment_trader_percentage`, `bot_degen_rate`, `fresh_wallet_rate`, `holder_count`, `creator_created_count` |
| `GET /vas/api/v1/token_holder_stat/sol/{mint}` | — | счётчики `sniper_count`, `insider_count`, `bundler_count`, `fresh_wallet_count`, `dex_bot_count`, `dev_count`, `smart_degen_count`, `renowned_count`; **насыщаются на 1000** (у Bonk почти все = 1000) |
| `GET /api/v1/token_fee_info/sol/{mint}` | — | `security`: `renounced_mint`, `renounced_freeze_account`, `burn_ratio`/`burn_status`, `blacklist`, `honeypot`, налоги, `lock_summary`; `launchpad`: `launchpad_platform`, `migrated_pool_exchange`, `launch_quote_address` |
| `GET /api/v1/token_mcap_candles/sol/{mint}?pool_type=tpool&resolution={1m,1h,1d}&limit=N` | — | свечи **капитализации** `{time(ms), open, high, low, close, volume}`; ATH капы = max(`high`) по `resolution=1d` (1m×501 покрывает только ~8 ч) |
| `GET /api/v1/tokens/top_buyers/sol/{mint}` | — | первые 70 покупателей: `statusNow{hold, bought_more, sold_part, sold}`, `top70_sniper_hold_rate`, `holderInfo[]{wallet_address, status, tags}` |
| `GET /vas/api/v1/token_holders/sol/{mint}?limit=100&orderby=amount_percentage&direction=desc` | — | держатели: `amount_percentage`, `tags`, `addr_type`, `is_suspicious`, `exchange` (биржи/пулы), PnL |
| `POST /rrs/api/v1/trending_multichain` | `{"params":[{"chain":"sol","interval":"1h","filter":{"filters":["frozen"],"limit":500,"launchpad_platform":[…]}}]}` | Trending: до ~85 токенов одним запросом, короткие ключи: `a` адрес, `s` символ, `v` объём за интервал, `mc` капа, `hhmc` исторический максимум капы (ATH), `t10` топ-10, `snp` снайперы, `bdr`/`bdrr` бандлеры (доли), `dhr` доля девелопера, `d_ts` статус девелопера, `d_ct` девелопер, `iwt` wash trading, `rat` крысы, `hd` холдеры, `sw`/`bu`/`se` сделки, `ot`/`ct` время, `lp`/`lpp` лаунчпад, `ex` площадка, `s_nm`/`s_blk` mint/blacklist, `rug`, `m_x` твиттер. Расшифровка ключей — по совпадению значений, не по документации |

## Что закрывает в боте (`EnhancedTokenSignals`, guide-v1)

| Сигнал бота | Источник |
|---|---|
| объём токена 5m / 1h (устойчивый объём, «как в Trending») | `mutil_window_token_info.price.volume_5m/1h` |
| цена, капа | `price.price` × `circulating_supply` |
| ATH токена (вход Spot на ATH) | max `high` из `token_mcap_candles` `resolution=1d` (капа), или `hhmc` из Trending |
| снайперы (число, доля) | `token_holder_stat.sniper_count`, `token_stat.top70_sniper_hold_rate` |
| статус девелопера, DEV_SOLD | `dev.creator_token_status` (+ `creator_hold_rate`; значения кроме `creator_close` не видели) |
| инсайдеры | `token_stat.top_rat_trader_percentage` («Insiders»), `insider_count` |
| бандлинг | `token_stat.top_bundler_trader_percentage` |
| total fees (детектор накрутки: 30 SOL × капа / 500k) | `mutil_window_token_info.total_fee` (SOL) |
| wash trading | `iwt` из Trending |
| возраст токена | `creation_timestamp` / `open_timestamp` |
| top-10, mint/freeze, blacklist, honeypot | `token_stat.top_10_holder_rate`, `token_fee_info.security` |

Нагрузка на кандидата: 1 пакетный `mutil_window_token_info` на всех + по токену `token_stat`, `token_holder_stat`, `token_fee_info`, свечи `1d` — 4 запроса. Кандидатов после дешёвых фильтров — единицы в час.

## Не проверено

- другие значения `creator_token_status` (держит / докупил / продал частично);
- лимиты частоты сайта и реакция на частые запросы из одной вкладки;
- живёт ли вход и пропуск Cloudflare сутками в открытом браузере без действий пользователя.
