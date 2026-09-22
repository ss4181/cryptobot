# cryptobot — Paper Trading Bot (Simülasyon)

> ⚠️ **Bu bot gerçek emir gönderemez.** API anahtarı istemez, imzalı istek üretmez, cüzdana erişmez.
> Sadece **halka açık piyasa verisi** okur ve **sanal** alım-satım yapar.
> Yatırım tavsiyesi değildir. Ayrıntılı uyarılar: [`RISK.md`](RISK.md) · Devir teslim: [`HANDOVER.md`](HANDOVER.md)

Başlangıç sanal bakiyesi **50 USDT**, hedef **işlem başına net %2** (komisyon ve slippage düşüldükten *sonra*).

---

## 1. Ne yapar / ne yapmaz

| Yapar | **Yapmaz** |
| --- | --- |
| Binance'in halka açık kline verisini indirir (anahtarsız) | Borsaya emir göndermez (kodda böyle bir yol **yok**) |
| Cache'e yazar, backtest'i çevrimdışı tekrarlanabilir yapar | API anahtarı / secret kullanmaz (ortamda olsa bile yok sayar) |
| Bollinger + RSI + trend filtreli mean-reversion stratejisi çalıştırır | Kaldıraç, futures, margin, short işlem yapmaz |
| Komisyon + slippage uygulayarak **sanal** dolum yapar | Para çekmez/yatırmaz |
| SQLite defterine her açılış/kapanışı yazar, bakiyeyi yeniden hesaplar | Kazanç garantisi vermez (bkz. `RISK.md`) |
| Rapor üretir: JSON + Markdown + PNG equity eğrisi | — |
| Telefona kısa bildirim gönderir: ntfy (ücretsiz, hesapsız) / telegram / webhook / console+file | Bildirim için ödeme veya hesap **gerektirmez**; bildirim katmanında emir yolu yoktur |

**Canlı mod yapısal olarak yoktur.** `mode` yalnızca `paper` veya `backtest` olabilir; `live`, `real`,
`production`, `trade` gibi bir değer verilirse `cryptobot/safety.py` sert hata (`LiveTradingForbidden`)
fırlatır ve program başlamaz. Bu kontrol `config.yaml`, ortam değişkeni (`CRYPTOBOT_MODE`) ve CLI
(`--mode`) dahil her giriş yolunda çalışır.

---

## 2. Mimari

```
cryptobot/
  safety.py                  Canlı işlem koruması (tek geçiş noktası)
  config.py                  Config yükleme + doğrulama (config.yaml < env < CLI)
  config.yaml                Kullanıcı parametreleri (Türkçe açıklamalı)
  data/feed.py               Binance halka açık REST, retry/backoff, cache, kesinti yönetimi
  strategy/base.py           Strategy arayüzü + registry (Signal: enter/exit/hold)
  strategy/indicators.py     Saf göstergeler: SMA, rolling std, Bollinger, RSI (Wilder)
  strategy/mean_reversion.py Bollinger dip alımı + trend filtresi + RSI aşırı satım filtresi
  execution/costs.py         Komisyon/slippage matematiği + net↔brüt hedef dönüşümü
  execution/paper_broker.py  Sanal dolum, nakit/pozisyon defteri, red/kısmi dolum enjeksiyonu
  risk/manager.py            Stop-loss, net take-profit, pozisyon boyutu, günlük limit, cooldown
  ledger/store.py            SQLite defter (açılış/kapanış/emir/equity/olay) + CSV/JSON + mutabakat
  backtest/engine.py         Olay güdümlü, deterministik geri test motoru
  backtest/metrics.py        İşlem sayısı, kazanma oranı, net kâr, max drawdown, profit factor, Sharpe
  backtest/report.py         JSON + Markdown + PNG rapor üretimi
  monitor/logging_setup.py   Yapılandırılmış (JSON satır) dosya logu + okunabilir konsol logu
  monitor/daily_report.py    Günlük özet (reports/daily_*.md)
  notify/                    MOBİL BİLDİRİM katmanı: ntfy/telegram/webhook/console/file,
                             filtreler (dedupe/saat limiti/sessiz saat), denetim kaydı, redaksiyon
  runner.py                  Paper loop (sınırlı/sınırsız, replay modu, kooperatif durdurma)
  cli.py / __main__.py       `python -m cryptobot <komut>` (+ `notify test|status|log|export`)
  tests/                     405 çevrimdışı unittest (ağ erişimi yok; bildirimler yerel sink ile)
  scripts/verify_all.py      Tek komutla tam doğrulama
  scripts/notify_check.py    Bildirim katmanını tek komutla çevrimdışı doğrula
  scripts/paperbot.py        Bu ortam için başlatıcı (aşağıya bakın)
  scripts/runbook_rollback.py Sürüm geri alma tatbikatı
  data/cache/*.csv           İndirilen mum verisi (tekrarlanabilirlik için)
  data/ledger.sqlite         Defter
  reports/                   Üretilen raporlar
  logs/                      Yapılandırılmış log dosyaları + notifications.jsonl (bildirim denetimi)
  run/                       paper_state.json / paper.pid / paper.stop
```

Ek modüller (spesifikasyona ek olarak, gerekçeli): `strategy/indicators.py` (saf göstergeler ayrı ve
test edilebilir), `execution/costs.py` (hem risk hem broker hem backtest aynı matematiği kullanır),
`runner.py` (paper döngüsü CLI'dan ayrı, test edilebilir).

---

## 3. Kurulum

Gereken paketler: `numpy, pandas, matplotlib, requests, PyYAML` (+ isteğe bağlı `ccxt` yedek besleme).
Bu çalışma ortamında **hepsi zaten kurulu**; yeni paket kurulmamıştır.

```bash
pip install -r requirements.txt      # normal bir Python 3.11+ ortamında
```

### Bu ortamdaki Python tuhaflığı (önemli)

Bu makinedeki gömülü Python (`C:\Program Files\AutoClaw\resources\python`) `python313._pth` dosyasını
**kendi dizinine göre** çözer ve `PYTHONPATH`'i yok sayar; bu yüzden proje kökü `sys.path`'e hiç
girmez ve `python -m cryptobot ...` çalışmaz. Normal Python kurulumlarında (Docker, GitHub Actions,
kullanıcı makinesi) `python -m cryptobot ...` **doğrudan çalışır**.

Bu ortamda aynı CLI'yi kullanmak için ince bir başlatıcı var:

```bash
python cryptobot/scripts/paperbot.py <komut> [seçenekler]      # = python -m cryptobot <komut>
```

Tüm belgelerde `python -m cryptobot <komut>` yazacaktır; burada yaşıyorsanız başına
`python cryptobot/scripts/` ekleyin (yani `python cryptobot/scripts/paperbot.py <komut>`).

---

## 4. Hızlı başlangıç

```bash
# 1) Veriyi indir (anahtarsız, halka açık veri) — 180 gün
python -m cryptobot download --timeframe 1h
python -m cryptobot download --timeframe 15m

# 2) Backtest + raporlar (JSON/MD/PNG)
python -m cryptobot backtest --timeframe 1h
python -m cryptobot backtest --timeframe 15m

# 3) Paper modda sınırlı çalıştır (burada: 40 döngü, cache üzerinde ileri-yürüyüş replay)
python -m cryptobot run --timeframe 15m --offline --replay --replay-bars 400 --cycles 40

# 4) Gerçek zamanlı paper döngüsü (varsayılan: 60 sn'de bir yeni bar kontrolü)
python -m cryptobot run --timeframe 1h --interval-seconds 60

# 5) Durum / durdurma / mutabakat / dışa aktarma / rapor
python -m cryptobot status
python -m cryptobot stop
python -m cryptobot verify
python -m cryptobot export --out reports/exports
python -m cryptobot report --day latest

# 6) Mobil bildirimler (ücretsiz ntfy; hesap gerekmez) — ayrıntı: NOTIFICATIONS.md
#    Kapsam varsayılanı: YALNIZCA pozisyon AÇILIŞ/KAPANIŞ bildirimi.
#    Otomasyon (testler/verify_all/acceptance_check) gerçek ağa göndermez (harness guard).
#    Replay/offline ağa göndermez (açık --notify-send gerekir).
export CRYPTOBOT_NTFY_HOST="ntfy.envs.net"                 # erişilebilir ücretsiz host
export CRYPTOBOT_NTFY_TOPIC="cryptobot-paper-<rastgele-uzun-ad>"
python -m cryptobot notify status          # aktif/pasif olaylar + "gerçek gönderim mümkün mü"
python -m cryptobot notify preview         # yeni mesaj biçimini GÖNDERMEDEN gör (13 olay)
python -m cryptobot notify preview --event position_closed   # yalnızca tek olay
python -m cryptobot notify test            # telefona canlı test bildirimi
python -m cryptobot notify log --limit 20  # son gönderim denemeleri (denetim)

# 7) Tek komutla tüm doğrulama (testler + güvenlik taraması + backtest + paper + mutabakat)
python cryptobot/scripts/verify_all.py --timeframe 1h
```

`run` **her zaman sınırlanabilir**: `--cycles N` veya `--duration-seconds S` verilmezse Ctrl+C ya da
`stop` komutuna kadar çalışır. Durma kooperatiftir: `stop` bir `run/paper.stop` dosyası bırakır,
döngü mevcut turu bitirip raporlarını yazdıktan sonra temiz kapanır.

### Bilinçli olarak ağa çıkmayan mod

`--offline` sadece cache'ten okur (testler ve tekrarlanabilir backtest için). `--replay`, cache
üzerinde ileri-yürüyüş simülasyonu yapar: her turda bir sonraki N barı "şimdi" gibi işler; gerçek
zaman beklemeden onlarca tam işlem döngüsü üretir ve **tamamen deterministiktir**.

Gerçek zamanlı paper modda (`--replay` ve `--offline` **yokken**) cache tazeliği denetlenir: en yeni
bar `data.max_cache_age_bars` bar'dan daha eskiyse cache bayat sayılır, borsadan tazelenmeye
çalışılır; ağ yoksa sonuç `cache-stale` + `complete=False` olur ve motor yeni giriş açmaz. Bu denetim
backtest ve `--replay` için **kasıtlı olarak devre dışıdır** (orada eski tarihsel barlar beklenen
davranıştır).

---

## 5. `config.yaml` referansı

| Parametre | Varsayılan | Anlamı | Ortam değişkeni | CLI |
| --- | --- | --- | --- | --- |
| `mode` | `paper` | `paper` \| `backtest`. Canlı mod **yok** | `CRYPTOBOT_MODE` | `--mode` |
| `initial_capital_usdt` | `50.0` | Sanal başlangıç bakiyesi | `CRYPTOBOT_INITIAL_CAPITAL_USDT` | `--initial-capital` |
| `net_profit_target_pct` | `2.0` | **Net** kâr hedefi (komisyon+slippage sonrası) | `CRYPTOBOT_NET_PROFIT_TARGET_PCT` | `--net-target-pct` |
| `gross_take_profit_pct` | `null` | Brüt TP'yi elle sabitleme (null = net hedeften türet) | `CRYPTOBOT_GROSS_TAKE_PROFIT_PCT` | — |
| `stop_loss_pct` | `2.5` | Giriş dolum fiyatına göre stop-loss | `CRYPTOBOT_STOP_LOSS_PCT` | `--stop-loss-pct` |
| `max_position_pct` | `90.0` | Tek pozisyona ayrılacak en fazla equity yüzdesi | `CRYPTOBOT_MAX_POSITION_PCT` | `--max-position-pct` |
| `max_open_positions` | `1` | Eşzamanlı açık pozisyon | `CRYPTOBOT_MAX_OPEN_POSITIONS` | `--max-open-positions` |
| `daily_loss_limit_pct` | `5.0` | Günlük zarar limiti; aşılırsa o gün yeni giriş yok | `CRYPTOBOT_DAILY_LOSS_LIMIT_PCT` | `--daily-loss-limit-pct` |
| `cooldown_minutes` | `60` | Zararlı işlemden sonra bekleme | `CRYPTOBOT_COOLDOWN_MINUTES` | `--cooldown-minutes` |
| `min_equity_usdt` | `10.0` | Bu equity'nin altında yeni giriş yok | `CRYPTOBOT_MIN_EQUITY_USDT` | — |
| `max_trades_per_day` | `8` | **Yeni giriş** sayısı tavanı (kapanışlar sayılmaz; UTC günü başına) | `CRYPTOBOT_MAX_TRADES_PER_DAY` | — |
| `pairs` | `[BTC/USDT, ETH/USDT]` | İzlenecek pariteler | `CRYPTOBOT_PAIRS` | `--pairs` |
| `timeframe` | `1h` | `1m…1d` | `CRYPTOBOT_TIMEFRAME` | `--timeframe` |
| `fee_pct` | `0.1` | Bacak başına taker komisyon (%) | `CRYPTOBOT_FEE_PCT` | `--fee-pct` |
| `slippage_pct` | `0.05` | Bacak başına tek yönlü slippage (%) | `CRYPTOBOT_SLIPPAGE_PCT` | `--slippage-pct` |
| `strategy.name` | `mean_reversion` | Strateji (registry) | — | — |
| `strategy.params.bb_period` | `20` | Bollinger periyodu | — | — |
| `strategy.params.bb_std` | `2.0` | Bant genişliği (std) | — | — |
| `strategy.params.rsi_period` | `14` | RSI periyodu | — | — |
| `strategy.params.rsi_oversold` | `35` | Alım için RSI eşiği (≤) | — | — |
| `strategy.params.trend_sma_period` | `200` | Trend filtresi (kapanış > SMA) | — | — |
| `strategy.params.exit_at_middle_band` | `true` | Orta banda dönüşte strateji çıkışı | — | — |
| `data.api_base` | `https://api.binance.com` | Halka açık veri sunucusu | `CRYPTOBOT_API_BASE` | — |
| `data.history_days` | `180` | İndirilecek/kullanılacak geçmiş | `CRYPTOBOT_HISTORY_DAYS` | `--days` |
| `data.request_timeout_seconds` | `20` | HTTP zaman aşımı | — | — |
| `data.max_retries` | `5` | Yeniden deneme sayısı | — | — |
| `data.backoff_initial_seconds` | `1.0` | İlk bekleme (2× artar) | — | — |
| `data.backoff_max_seconds` | `30.0` | Bekleme tavanı | — | — |
| `data.cache_dir` | `data/cache` | Mum cache klasörü | `CRYPTOBOT_CACHE_DIR` | — |
| `data.max_cache_age_bars` | `3` | **Yalnız gerçek zamanlı paper modda**: en yeni cache barı bu kadar bar'dan eskiyse cache bayat sayılır (`cache-stale`, `complete=False`, yeni giriş yok). Backtest/`--replay` etkilenmez | `CRYPTOBOT_MAX_CACHE_AGE_BARS` | — |
| `data.db_path` | `data/ledger.sqlite` | Defter dosyası | `CRYPTOBOT_DB_PATH` | `--db-path` |
| `reports_dir` | `reports` | Rapor klasörü | `CRYPTOBOT_REPORTS_DIR` | — |
| `logging.level` | `INFO` | `DEBUG…CRITICAL` | `CRYPTOBOT_LOG_LEVEL` | `--log-level` |
| `logging.dir` | `logs` | Log klasörü | — | — |

**Öncelik:** `config.yaml` < ortam değişkeni < CLI bayrağı.

### 5.1 Bildirim ayarları (`notifications:`)

Telefondan takip için `config.yaml` içindeki `notifications:` bölümü kullanılır. Tam kurulum
(Türkçe, adım adım) ve güvenlik notu: [`NOTIFICATIONS.md`](NOTIFICATIONS.md).

| Parametre | Varsayılan | Anlamı | Ortam değişkeni |
| --- | --- | --- | --- |
| `notifications.enabled` | `true` | Bildirim katmanı açık/kapalı | `CRYPTOBOT_NOTIFY_ENABLED` |
| `notifications.providers` | `[console, file, ntfy]` | Kullanılacak sağlayıcılar | `CRYPTOBOT_NOTIFY_PROVIDERS` |
| `notifications.ntfy_host` | `ntfy.sh` | ntfy sunucusu (başlatıcı erişilebilir `ntfy.envs.net` kullanır) | `CRYPTOBOT_NTFY_HOST` |
| `notifications.ntfy_topic` | `null` | ntfy konu adı — **sır**, ortamdan verin | `CRYPTOBOT_NTFY_TOPIC` |
| `notifications.min_severity` | `info` | Altındaki önem derecelerini gönderme | `CRYPTOBOT_NOTIFY_MIN_SEVERITY` |
| `notifications.dedupe_window_seconds` | `300` | Aynı bildirimi bu süre içinde bastır | `CRYPTOBOT_NOTIFY_DEDUPE_SECONDS` |
| `notifications.max_per_hour` | `20` | Saatlik **ağ** gönderim üst sınırı (yerel console/file sayılmaz) | `CRYPTOBOT_NOTIFY_MAX_PER_HOUR` |
| `notifications.quiet_hours` | `null` | Sessiz saatler, ör. `"22:00-07:00"` (critical geçer) | `CRYPTOBOT_NOTIFY_QUIET_HOURS` |
| `notifications.dry_run` | `false` | Gönderme, yalnızca göster/kaydet | `CRYPTOBOT_NOTIFY_DRY_RUN` |
| `notifications.timeout_seconds` | `10.0` | Sağlayıcı başına HTTP zaman aşımı | `CRYPTOBOT_NOTIFY_TIMEOUT_SECONDS` |
| `notifications.retry_max` | `2` | Yeniden deneme (toplam 1+bu; üst sınır 10) | `CRYPTOBOT_NOTIFY_RETRY_MAX` |
| `notifications.backoff_initial_seconds` | `1.0` | İlk bekleme (2× artar) | — |
| `notifications.backoff_max_seconds` | `8.0` | Bekleme tavanı | — |
| `notifications.equity_drop_pct` | `3.0` | Zirveden % düşüşte uyarı (0 = kapalı) | — |
| `notifications.notify_on` | `position_opened, position_closed` | Bildirilecek olay türleri (varsayılan: yalnızca işlem) | `CRYPTOBOT_NOTIFY_ON` |

Sırlar (`CRYPTOBOT_NTFY_TOPIC`, `CRYPTOBOT_NTFY_TOKEN`, `CRYPTOBOT_TELEGRAM_BOT_TOKEN`,
`CRYPTOBOT_TELEGRAM_CHAT_ID`, `CRYPTOBOT_WEBHOOK_URL`) **asla `config.yaml`'a yazılmaz**;
yalnızca ortam değişkenlerinden okunur ve kayıtlarda/loglarda maskelenir (`[REDACTED]`).

---

## 6. Net ↔ brüt take-profit matematiği

Hedef **net**tir: komisyon ve slippage düşüldükten sonra bakiyeye %2 eklemek. Bu yüzden brüt
seviye daha yüksektir. `cryptobot/execution/costs.py` içindeki saf fonksiyon:

```
gross_tp_pct = ((1 + f) * (1 + hedef)) / ((1 - s) * (1 - f)) - 1        (f = komisyon, s = slippage)
```

Varsayılan `f = 0.1%`, `s = 0.05%`, hedef `%2` için:

| Büyüklük | Değer |
| --- | --- |
| Gerekli **brüt** hareket | **%2.2553** |
| Maliyet yükü (cost drag) | **%0.2553** puan |
| `required_tp_price(entry_fill=100)` | `102.25533187…` |
| Bu seviyede gerçekleşen net getiri | `%2.000000` (fonksiyon bunu tam olarak tersine çevirir) |

Bu fonksiyon birim testle doğrulanır (`tests/test_costs.py`: brüt seviye → net getiri = hedef,
1e-9 hassasiyetle). Brüt hedefi elle sabitlemek isterseniz `gross_take_profit_pct` ayarlayın.

Stop-loss **net** değil referans fiyat üzerinden tanımlıdır: `stop_loss_pct = 2.5` iken gerçekleşen
net kayıp ~%2.7 olur (çıkışta da komisyon+slippage ödenir). Bu `RISK.md`'de ayrıca vurgulanır.

---

## 7. Strateji (mean-reversion)

Giriş için **üçü birden** gerekir (kapanmış bar üzerinde):

1. **Dip:** kapanış ≤ alt Bollinger bandı (`bb_period=20`, `bb_std=2.0`)
2. **Trend:** kapanış > uzun SMA (`trend_sma_period=200`) → düşen bıçak tutulmaz
3. **Aşırı satım:** RSI(14) ≤ `rsi_oversold=35`

Çıkış: strateji orta banda dönüşte çıkar (`exit_at_middle_band`); ek olarak risk yöneticisi
net take-profit ve stop-loss seviyelerini uygular. Yalnızca **long** (al) işlem yapılır; düzken nakit
tutulur. Sinyal üretimi tamamen aynı `decide()` fonksiyonunda olduğu için backtest ve paper mod
birebir aynı kararı verir.

---

## 8. Risk kuralları (hepsi logda ve defterde görünür)

| Kural | Davranış | Kod (`events.code`) |
| --- | --- | --- |
| Net take-profit | `required_tp_price` ile hesaplanan brüt seviye | `take_profit` (işlem çıkış nedeni) |
| Stop-loss | Bar low ≤ stop → stop fiyatından çıkış (önce stop kontrol edilir) | `stop_loss` |
| Pozisyon boyutu | `min(equity × max_position_pct, nakit)` + borsa min notional (5 USDT) | `below_min_notional` |
| Açık pozisyon tavanı | `max_open_positions` | `max_open_positions_reached` |
| Günlük zarar limiti | O gün realize net zarar ≥ limit → gün boyu yeni giriş yok | `daily_loss_limit_reached` |
| Cooldown | Zararlı işlemden sonra `cooldown_minutes` boyunca giriş yok | `cooldown_started`, `cooldown_active` |
| Günlük işlem tavanı | `max_trades_per_day` — **yalnızca yeni girişleri** sayar; kapanışlar sayılmaz | `max_trades_per_day_reached` |
| Minimum equity | `min_equity_usdt` | `equity_below_minimum` |
| Veri kesintisi / eksik veri | Eksik/bayat veri → yeni giriş yok (fail-safe), mevcut pozisyonun koruyucu çıkışı sürer | `pause_new_entries` |
| Bayat cache (gerçek zamanlı) | En yeni cache barı `data.max_cache_age_bars` bar'dan eskiyse cache tazelenir; ağ yoksa `complete=False` ile yeni giriş durur | `cache_stale`, `cache-stale` |

**Hangi limit neyi yapar (özet):**

* **Yeni girişi bloklar:** `daily_loss_limit_reached`, `cooldown_active`, `max_open_positions_reached`,
  `max_trades_per_day_reached`, `equity_below_minimum`, `no_cash`, `below_min_notional` ve veri
  fail-safe'i (`pause_new_entries` / `cache_stale`).
* **Yalnızca boyutlandırır (girişi engellemez):** `max_position_pct` — pozisyon miktarını küçültür.
* **Yalnızca mevcut pozisyonu kapatır:** stop-loss ve take-profit; yeni giriş kararını etkilemezler.

`max_trades_per_day` **yeni giriş** tavanıdır: `8` demek, UTC günü başına en fazla 8 giriş demektir
(kapanışlar sayaçı artırmaz, tek tur = 1 artış) ve sayaç UTC günü değişince sıfırlanır.

Bu kodlar hem `logs/*.log` JSON satırlarında hem `ledger.events` tablosunda bulunur; örnek:

```bash
python -c "import sqlite3;print(*sqlite3.connect('cryptobot/data/ledger.sqlite').execute(\"select code,count(*) from events group by code\").fetchall(),sep='\n')"
```

---

## 9. Defter, raporlar ve mutabakat

**Defter** (`data/ledger.sqlite`) tabloları:

* `ledger` — her **açılış (OPEN)** ve **kapanış (CLOSE)** satırı: zaman, parite, yön, referans fiyat,
  dolum fiyatı, miktar, notional, komisyon, slippage maliyeti, brüt PnL, net PnL, net %, neden.
* `trades` — kapanan her tur (giriş+çıkış aynı satırda).
* `orders` — reddedilen ve kısmi dolan emirler + nedeni.
* `equity` — zaman serisi equity anlık görüntüleri.
* `events` — risk/limit/feed/engine olayları.

**Raporlar** (`reports/`):

| Dosya | İçerik |
| --- | --- |
| `backtest_<pariteler>_<tf>.json` | Deterministik metrikler + config & veri özeti + `determinism_hash`. **Zaman damgası içermez** |
| `backtest_<pariteler>_<tf>.md` | Okunabilir rapor (metrik tablosu, işlem listesi, varsayımlar) |
| `backtest_<pariteler>_<tf>.png` | Equity + drawdown grafiği |
| `daily_<YYYY-MM-DD>.md` | Günlük özet: equity, işlemler, reddedilen emirler, risk olayları, mutabakat |
| `verify_all_summary.json` | `verify_all.py` adım adım kanıtları |
| `panel/index.html` | Operatör paneli: bildirimler + işlemler + genel başarı istatistikleri (tek dosya, çevrimdışı) |

**Mutabakat** (`python -m cryptobot verify`): nakit ve realize PnL'i defterden **sıfırdan** yeniden
hesaplar ve broker'in canlı değerleriyle karşılaştırır:

* `cash_from_ledger == broker.cash` (tolerans 1e-6 USDT)
* `realized_net_pnl` eşleşmesi
* açık pozisyon miktarları
* `equity == cash + işaretlenmiş pozisyonlar` (mark fiyat verilmişse; açık pozisyon değeri mark
  fiyatına bağlı olduğu için bu kontrol belgelenmiş bir istisnadır)

---

## 10. Veri dışa aktarma

```bash
python -m cryptobot export --out reports/exports
# reports/exports/ledger.csv, trades.csv, orders.csv, equity.csv, events.csv, ledger_<run_id>.json
```

---

## 10.1 Operatör paneli (tek dosya HTML)

```bash
python -m cryptobot panel                       # reports/panel/index.html yazar
python -m cryptobot panel --out reports/panel/index.html
python -m cryptobot panel --run-id paper-20260914T075546Z-20092   # yalnızca tek koşu
python -m cryptobot panel --limit 500           # tablo başına satır sınırı (0 = sınırsız)
python -m cryptobot panel --serve --port 8765   # 127.0.0.1 üzerinde yerel canlı görünüm
# bu ortamda: python cryptobot/scripts/paperbot.py panel ...
```

Panel, **zaten var olan** iki kayıttan tek bir kendi kendine yeten HTML üretir:

* `logs/notifications.jsonl` — her gönderim denemesi (durum, sebep, HTTP kodu, gecikme, hedef host),
* `data/ledger.sqlite` — `runs` / `trades` / `ledger` / `equity` / `orders` / `events`.

İçerik: koşu seçici + config özeti (SHA-256/12), **genel başarı istatistikleri** (bildirim
gönderildi/bastırıldı/hata/prova + teslim başarı oranı; açılan/kapanan/kazanç/zarar/başabaş işlem,
kazanma oranı, net-brüt PnL (USDT ve %), komisyon, slippage, işlem başına ortalama, en iyi/en kötü
işlem, profit factor, maks drawdown, ortalama tutma süresi), işlem tablosu (başarı durumu ve çıkış
türü sütunlarıyla), açık pozisyonlar, bildirim tablosu (sebep kodlarının Türkçe karşılıklarıyla) ve
inline SVG equity eğrisi + drawdown bandı.

Kurallar:

* **Tek dosya, çevrimdışı:** uzak `src`/`href`/`@import`, CDN, web font veya harici görsel yok
  (komut bunu her çalışmada tarar ve raporlar).
* **Salt okunur:** panel bildirim göndermez, emir vermez, deftere yazmaz.
* **Sırlar maskeli:** hedef host dışında konu/token/webhook yolu dosyaya **hiç** girmez; denetim
  kaydında düz metin bir sır varsa render sırasında `[REDACTED]` yapılır.
* **Deterministik:** aynı girdiler aynı dosyayı üretir; tek değişken alan üretim zamanı satırıdır.
* **Dürüst:** hesaplanamayan metrik `—` gösterilir; `--limit` ile kesilen satırlar tablo başlığında
  ve altbilgide açıkça yazılır (KPI sayıları her zaman tüm veriden hesaplanır).

**`--serve` nasıl durdurulur:** komutu çalıştırdığınız terminalde **Ctrl+C** (veya pencereyi
kapatın). Sunucu yalnızca `127.0.0.1` adresini dinler, sadece panel sayfasını sunar (`GET`/`HEAD`;
diğer yollar `404`, yazma metotları `501`) ve her istekte sayfayı yeniden üretir.

---

## 11. Test ve doğrulama

```bash
python cryptobot/scripts/verify_all.py            # her şey: tarama + testler + backtest + paper + mutabakat
python cryptobot/scripts/verify_all.py --quick    # sadece tarama + backtest + mutabakat
python cryptobot/scripts/notify_check.py          # bildirim katmanı: tarama + testler + yerel HTTP sink koşusu
```

Test paketi **çevrimdışıdır** (503 test): ağ erişimi yok, sahte transport ile besleme senaryoları,
bozuk/bayat cache, determinizm, komisyon matematiği, risk limitleri, defter mutabakatı, CLI çıkış
kodları ve **mobil bildirim katmanı** (bölümlü mesaj şablonları, Türkçe sayı biçimi, sağlayıcı gövde
şekilleri, yeniden deneme/backoff, hata izolasyonu, dedupe/saat limiti/sessiz saat filtreleri — kritik
olaylar saat limitini aşar —, sır redaksiyonu, dry-run, denetim kaydı).
Bildirim testleri gerçek bir `http.server` sink'ini `127.0.0.1` üzerinde kullanır; yani HTTP
yolu gerçekten çalışır, makineden dışarı çıkmaz. Test paketi ortam değişkenlerinden bağımsızdır:
`CRYPTOBOT_*` değişkenleri içe aktarımda temizlenir, böylece gerçek bir konu adı test sonucunu
değiştiremez. Operatör paneli için de aynı disiplin geçerlidir: KPI'lar elle hesaplanmış sahte bir
defterle karşılaştırılır, üretilen HTML'in kendine yeterliliği/kaçışlanması/redaksiyonu ve
determinizmi programatik olarak doğrulanır, `--serve` yalnızca `127.0.0.1` üzerinde test edilir.

---

## 12. Docker ve ücretsiz GitHub Actions

`.github/workflows/paper-trade.yml` **30 dakikada bir** çalışır: önce güncel halka açık mumları
indirir (soğuk bir checkout'ta cache boştur, `--offline` kullanmaz), sonra paper döngüsünü **sınırlı
bir canlı pencere** boyunca çalıştırır (`--duration-seconds` / `--interval-seconds`), ardından
mutabakat + rapor üretir ve `reports/` + `logs/` dosyalarını artifact olarak yükler. **Hiçbir secret
zorunlu değildir**; ntfy konu adını repo **Variable** (tercih edilen) veya **Secret** olarak
`CRYPTOBOT_NTFY_TOPIC` adıyla tanımlarsanız telefonunuza da bildirim gider (tanımlı değilse bildirim
katmanı yerel kalır: `console` + `logs/notifications.jsonl`).

> Bu, "bilgisayarım kapalıyken de çalışsın" isteğinin **ücretsiz** yoludur. Varsayılan olarak bot
> yerel bir süreçtir: makine kapalıysa çalışmaz. Cron gecikmesi, hareketsiz depo kısıtı, ücretsiz
> dakika limitleri ve koşular arasında durum kalıcılığı dahil tüm dürüst uyarılar ve alternatifler
> (küçük ücretsiz VM, PC'yi açık bırakma) için: [`NOTIFICATIONS.md` §3](NOTIFICATIONS.md).

```bash
docker build -t cryptobot-paper .
docker run --rm -v "$PWD/cryptobot/reports:/app/cryptobot/reports" cryptobot-paper \
  python -m cryptobot backtest --timeframe 1h
docker run --rm cryptobot-paper python -m cryptobot run --timeframe 1h --cycles 5 --offline --replay --replay-bars 300
```

---

## 13. Komutlar ve çıkış kodları

| Komut | Ne yapar |
| --- | --- |
| `download` | Halka açık mum verisini indirir/cache'i tazeler |
| `backtest` | Geçmiş üzerinde deterministik test + raporlar |
| `run` | Paper döngüsü (`--cycles`, `--duration-seconds`, `--replay`, `--faults demo`, `--notify-dry-run`, `--no-notify`) |
| `status` | Son/çalışan paper turunun durumu, risk limitleri, broker defteri |
| `notify status` | Bildirim sağlayıcıları, aktiflik durumu ve pasifse **nedeni** |
| `notify preview` | Örnek verilerle her olayın **tam metnini** yazdırır; `--send` verilmedikçe göndermez (`--event`, `--carrier markdown\|html`) |
| `notify test` | Etkin sağlayıcılara canlı test bildirimi (`--dry-run` ile göndermeden) |
| `notify log` | Son gönderim denemeleri (denetim; `--limit`, `--json`) |
| `notify export` | Bildirim kayıtlarını CSV/JSON olarak dışa aktarır |
| `report` | Defterden günlük Markdown raporu |
| `export` | Defteri CSV + JSON olarak dışa aktarır |
| `panel` | Bildirim + işlem paneli: tek dosya çevrimdışı HTML (`--run-id`, `--limit`, `--out`, `--serve --port`) |
| `verify` | Defter ↔ broker mutabakatı |
| `stop` | Çalışan döngüye kooperatif durma isteği |
| `safety` | Güvenlik duruşunu yazdırır (canlı mod engeli dahil) |

Çıkış kodları: `0` başarılı, `1` hata/başarısız mutabakat, `2` kullanım veya config hatası,
`3` **güvenlik ihlali** (canlı mod istendi), `4` **meşgul** — çalışan başka bir bot örneği
(`run/paper.pid` + `paper_state.json` ile tespit edilir) ya da defteri tutan başka bir süreç
(`database is locked`).

### Tek çalışma dizini, tek bot

`run` başlamadan önce pid/durum dosyalarını kontrol eder ve **çalışan bir örnek varsa** defteri
hiç açmadan durur:

```
Bu bot zaten calisiyor (pid 21992, run_id paper-..., dongu 7). Once durdurun: start-bot.cmd stop
```

Çıkış kodu `4` olur; deftere ve çalışma durumuna dokunulmaz. İki botu aynı anda çalıştırmayın:
ikinci örnek SQLite yazma kilidi yüzünden `database is locked` hatası alır — bu da artık ham bir
Python izi değil, ne yapılacağını söyleyen kısa bir Türkçe mesajdır (`start-bot.cmd stop`, ya da
`--db-path` ile başka bir defter). Süreç ölmüşse (pid canlı değilse) ya da durum dosyası
`stopped`/`finished` diyorsa eski kayıt otomatik temizlenir ve koşu normal başlar. `stop` çalışan
bir bot varken de çalışır: bot turunu bitirip raporlarını yazar.

Çıkış kodları: `0` başarılı, `1` hata/başarısız mutabakat, `2` kullanım veya config hatası,
`3` **güvenlik ihlali** (canlı mod istendi).

---

## 14. Sorun giderme

| Belirti | Çözüm |
| --- | --- |
| `No module named cryptobot` | Bu ortamda `python cryptobot/scripts/paperbot.py ...` kullanın (§3) veya `PYTHONPATH` yerine normal bir Python kurulumu kullanın |
| `VERI HATASI ... cache` | Önce `download`; ağ yoksa `--offline` yerine cache'i kopyalayın |
| `KONFIGURASYON HATASI: ... ` | Alan bazlı liste verir; `config.yaml`'ı düzeltin (§5) |
| `GUVENLIK IHLALI` | `mode`/`CRYPTOBOT_MODE`/`--mode` değeri canlı mod istiyor; `paper` veya `backtest` yapın |
| `multabakat FAIL` | Aynı `--run-id` ile tekrar yazılmış eski satırlar olabilir; yeni `--run-id` kullanın (tekrar kullanımda eski satırlar otomatik silinir) |
| İşlem hiç açılmıyor | Normal olabilir: trend filtresi (kapanış > SMA200) + Bollinger dibi + RSI ≤ 35 üçü birlikte gerekir; `status` ve `logs/` içindeki `blocked:*` nedenlerine bakın |
| Grafik üretilmiyor | `matplotlib` eksik olabilir; `--no-chart` ile devam edin |
| Telefona bildirim gelmiyor | `notify status` ile sağlayıcının **AKTIF** olduğunu doğrulayın; konu adı birebir aynı olmalı (bkz. `NOTIFICATIONS.md` §11) |
| Bildirim çok fazla / bastırılıyor | `dedupe_window_seconds`, `max_per_hour`, `quiet_hours`, `min_severity` ayarlarına bakın; `notify log` her bastırmanın nedenini yazar (kritik olaylar `max_per_hour`'u aşar) |
| Bilgisayar kapalıyken de çalışsın istiyorum | Varsayılan: çalışmaz (yerel süreç). Ücretsiz yol: GitHub Actions iş akışı; adımlar, uyarılar ve alternatifler: `NOTIFICATIONS.md` §3 |
| Bildirimler geç geliyor | Karar **kapanmış mumda** verilir (1h grafikte ~1 saat) ve döngü varsayılan 60 sn'dir; "her işlem hemen" tarifi `NOTIFICATIONS.md` §4.1 |
| Panel boş / "defter dosyası yok" diyor | Panel **salt okunur** bir görünümdür; `--db-path` ve `logs/notifications.jsonl` yollarını kontrol edin. Defter başka bir süreç (ör. çalışan bir `run` döngüsü) tarafından kilitliyse okuma yine çalışır ama **yazan** komutlar (`run`, `backtest`) `database is locked` verir |
| `Bu bot zaten calisiyor (pid ...)` ve çıkış kodu `4` | Aynı çalışma dizininde çalışan bir bot var; önce `start-bot.cmd stop` (raporlarını yazıp kapanır) — ayrıntı: §13 "Tek çalışma dizini, tek bot". Süreç gerçekten ölmüşse pid dosyası eski kalabilir: `run/paper.pid` silinirse koşu temiz başlar (durum dosyası `stopped`/`finished` ise bot zaten kendisi temizler) |
| `LEDGER KILITLI: ... database is locked` | Defteri başka bir süreç tutuyor (genelde çalışan ikinci bir bot; bazen defteri açık tutan bir araç/panel). Çalışan örneği durdurun (`start-bot.cmd stop`), araçları kapatın veya bu komutu başka bir deftere yönlendirin (`--db-path`) |
| Panel eski görünüyor | `--serve` her istekte yeniden üretir; dosya modunda komutu tekrar çalıştırın. `--limit` tablo satırlarını sınırlar (KPI'lar her zaman tüm veriden) |

---

## 15. Dosya listesi ve sorumluluklar

`HANDOVER.md` devir teslim, `RISK.md` sınırlamalar ve uyarılar, `NOTIFICATIONS.md` mobil bildirim
kurulumu içindir. Operatör paneli `panel/` paketindedir: `panel/model.py` (salt okunur okuma + KPI),
`panel/render.py` (tek dosya HTML/CSS/JS/inline SVG), `panel/server.py` (`--serve`, yalnızca
127.0.0.1). Gerçek parayla kullanım **ayrı ve açık onay** gerektirir ve bu kod tabanında
böyle bir yetenek yoktur.
