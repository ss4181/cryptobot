# HANDOVER.md — Devir Teslim, İşletme ve Geri Alma

Bu dosya botu **teslim alan** kişi içindir: nasıl çalıştırılır, nasıl izlenir, nasıl durdurulur,
nasıl geri alınır ve hangi noktalar henüz doğrulanmamıştır.

> Bot **paper trading** (simülasyon) modundadır. Canlı emir yolu yoktur, API anahtarı gerekmez.
> Sınırlamalar için [`RISK.md`](RISK.md), kullanım için [`README.md`](README.md).

---

## 1. Sistem özeti

| Alan | Değer |
| --- | --- |
| Sürüm | 1.0.0 (`cryptobot/__init__.py`) |
| Dil / çalışma zamanı | Python 3.13 (Windows'ta test edildi; Linux/Docker uyumlu) |
| Zorunlu paketler | `numpy`, `pandas`, `matplotlib`, `requests`, `PyYAML` (+ isteğe bağlı `ccxt`) |
| Veri kaynağı | Binance halka açık `GET /api/v3/klines` (anahtarsız, imzasız) |
| Durum deposu | `cryptobot/data/ledger.sqlite` (SQLite) + `cryptobot/run/paper_state.json` |
| Çıktılar | `cryptobot/reports/`, `cryptobot/logs/`, `cryptobot/logs/notifications.jsonl` |
| Bildirimler | Mobil uyarı katmanı (ntfy/telegram/webhook/console/file) — kurulum: [`NOTIFICATIONS.md`](NOTIFICATIONS.md) |
| Başlangıç bakiyesi | 50 USDT (sanal) |
| Giriş noktası | `python -m cryptobot <komut>` (bu ortamda `python cryptobot/scripts/paperbot.py <komut>`) |

---

## 2. Kurulum

### Yerel (bu makine)

```bash
cd C:\Users\serha\.openclaw-autoclaw\workspace
python cryptobot/scripts/verify_all.py --quick      # ortam + veri + backtest + mutabakat kontrolü
```

Bu makinedeki gömülü Python `python -m cryptobot` ile projeyi bulamaz (`python313._pth` kendi
dizinine göre çözülür ve `PYTHONPATH` yok sayılır). Bu yüzden CLI'ye **`scripts/paperbot.py`**
başlatıcısı üzerinden erişilir; işlevsel fark yoktur (aynı `cryptobot.cli.main`).

### Docker

```bash
docker build -t cryptobot-paper .
docker run --rm -v "%CD%/cryptobot/reports:/app/cryptobot/reports" cryptobot-paper \
  python -m cryptobot download --timeframe 1h
docker run --rm -v "%CD%/cryptobot/reports:/app/cryptobot/reports" cryptobot-paper \
  python -m cryptobot backtest --timeframe 1h
```

### GitHub Actions (ücretsiz, secretsız)

`.github/workflows/paper-trade.yml` — hafta içi 06:00 UTC + manuel tetikleme:
1. cache'i indirir, 2. `backtest` çalıştırır, 3. sınırlı `run --replay` yapar, 4. `verify` koşar,
5. `notify status` + `notify test --dry-run` + `notify log` çalıştırır, 6. `cryptobot/reports` +
`cryptobot/logs` dosyalarını artifact olarak yükler. **Hiçbir secret zorunlu değildir**; repo
secrets tanımlıysa (`CRYPTOBOT_NTFY_TOPIC` vb.) paper koşusu gerçek telefona da bildirim gönderir,
tanımlı değilse katman yerel kalır (console + `logs/notifications.jsonl`).

---

## 3. İşletme runbook'u (tatbik edilmiş)

Aşağıdaki dört adım bu depoda **fiilen çalıştırılmıştır**; beklenen çıktılar parantez içinde.

### 3.1 Başlat

```bash
# Sınırlı (önerilen, CI/demo):
python cryptobot/scripts/paperbot.py run --timeframe 1h --cycles 100 --interval-seconds 60
# Cache üzerinde ileri-yürüyüş (deterministik, ağsız, hızlı):
python cryptobot/scripts/paperbot.py run --timeframe 15m --offline --replay --replay-bars 400 --cycles 40
# Sınırsız (Ctrl+C veya `stop` ile durur):
python cryptobot/scripts/paperbot.py run --timeframe 1h --interval-seconds 60
```

Başlarken şunlar oluşur: `run/paper.pid`, `run/paper_state.json` (her turda güncellenir),
`logs/<tarih>_<run_id>.log`, `data/ledger.sqlite` içinde yeni `run_id`.
(Doğrulandı: 40 tur → `16000 bar işlendi, 64 işlem, mutabakat PASS`.)

**Tek çalışma dizininde tek bot.** `run` defteri açmadan önce `run/paper.pid` + `run/paper_state.json`
üzerinden çalışan bir örnek olup olmadığını kontrol eder. Varsa hiçbir şeye dokunmaz, çıkış kodu `4`
ile şu satırı yazıp durur (ham Python izi yok):

```
Bu bot zaten calisiyor (pid 21992, run_id paper-..., dongu 7). Once durdurun: start-bot.cmd stop
```

Eski kayıt (ölü süreç ya da `stopped`/`finished` durumu) otomatik temizlenir ve koşu normal başlar.
Defter yine de başka bir süreç tarafından tutuluyorsa (`database is locked`) komut kısa bir Türkçe
mesaj (`LEDGER KILITLI ...` → `start-bot.cmd stop` veya `--db-path`) ve çıkış kodu `4` ile biter;
hiçbir durumda `sqlite3.OperationalError` yığın izi basılmaz.

### 3.2 İzle

```bash
python cryptobot/scripts/paperbot.py status          # state, heartbeat, broker defteri, risk limitleri
tail -f cryptobot/logs/20260911_<run_id>.log         # JSON satır logu

# Mobil bildirim katmanı (isteğe bağlı; varsayılan ücretsiz ntfy)
python cryptobot/scripts/paperbot.py notify status   # sağlayıcılar + aktiflik nedenleri
python cryptobot/scripts/paperbot.py notify test     # telefona canlı test bildirimi
python cryptobot/scripts/paperbot.py notify log      # son gönderim denemeleri (denetim)
```

`status` şunları gösterir: `state` (`running/finished/stopped`), heartbeat yaşı, `cycles_run`,
`halted` + neden, nakit/equity/realize PnL, açık pozisyonlar, emir istatistikleri (filled/partial/
rejected + nedenler), tüm risk limitleri, güvenlik duruşu.
(Doğrulandı: `state: running`, `cycles_run: 28`, heartbeat 0 sn, `equity 48.3565`.)

Telefon bildirimleri için başlangıç: `CRYPTOBOT_NTFY_TOPIC` ortam değişkenine uzun/rastgele bir
konu adı verin, ntfy uygulamasından aynı konuya abone olun, `notify test` ile doğrulayın.
Sağlayıcı pasifse `notify status` **nedenini** yazar (eksik ortam değişkeni gibi). Ayrıntı ve
güvenlik notu: [`NOTIFICATIONS.md`](NOTIFICATIONS.md).

Sağlık kontrol listesi:

| Soru | Nereye bakılır | İyi durum |
| --- | --- | --- |
| Süreç yaşıyor mu? | `status` → heartbeat yaşı | < 2 × `interval_seconds` |
| Günlük limit takıldı mı? | `status` → `halted`, `events` tablosu `daily_loss_limit_reached` | teşhis edilmiş ve beklenen |
| Veri geliyor mu? | log `feed_unavailable` / `pause_new_entries` | yok veya kısa süreli |
| Defter tutarlı mı? | `verify` | `Ledger reconciliation: PASS` |
| Red oranı yüksek mi? | `status` → `rejected_reasons` | genelde `below_min_notional` dışında boş |

### 3.3 Durdur

```bash
python cryptobot/scripts/paperbot.py stop
```

`run/paper.stop` sentinel'i yazılır; döngü **mevcut turu bitirdikten sonra** temiz kapanır:
özet basılır, `state: stopped` olur, `paper.pid` ve sentinel silinir.
(Doğrulandı: 37. turda `stop_requested` (WARNING) → `runner_finished` → raporlar yazıldı → mutabakat PASS.)

Sert durdurma gerekirse: `Ctrl+C` (aynı `finally` temizliği çalışır) veya süreç sonlandırma +
`rm cryptobot/run/paper.stop` (kirli sentinel kalmasın).

### 3.4 Geri alma (rollback)

**Parametre geri alma** (tatbik edilmiş, kanıtlanmış):

```bash
python cryptobot/scripts/runbook_rollback.py --timeframe 1h
```

Bu komut: `config.yaml`'ı `cryptobot/.repro/config.baseline.yaml`'a yedekler → temel backtest'i
koşar → değişikliği "dağıtır" (`net_profit_target_pct=1.5`, `max_position_pct=50`) → farklı sonuç
aldığını doğrular → yedeği geri yükler → **JSON'un bayt bayt aynı** olduğunu doğrular.
(Doğrulanan çıktı: temel `sha256=01ac423a…` / `net=-2.5657`, dağıtılan `sha256=30519568…` /
`net=-1.6598`, geri alınmış `sha256=01ac423a…` → `ROLLBACK DRILL: PASS`.)

`finally` bloğu sayesinde komut hata ile bitse bile `config.yaml` temel değerlere döner.

**Kod geri alma:** depo git deposu değilse sürümü dosya kopyasıyla yönetin
(`cryptobot/.repro/` yedeği); git kullanıyorsanız `git revert <commit>` / `git checkout <etiket> -- cryptobot/`.
Geri aldıktan sonra **mutlaka** `verify_all.py --quick` koşup `determinism_hash`'in beklediğiniz
değere döndüğünü doğrulayın (geri alma ancak ölçülebiliyorsa geri almadır).

**Veri geri alma:** `data/cache/*.csv` ve `data/ledger.sqlite` üretilmiş dosyalardır; silinebilir.
Cache silinirse `download` ile yeniden üretilir (fiyatlar borsada değişmediği sürece aynı sonucu
verir; yeni barlar eklendiği için tarih aralığı kayabilir).

---

## 4. Gözlemlenebilirlik

| Kaynak | Yer | İçerik |
| --- | --- | --- |
| Yapılandırılmış log | `logs/<tarih>[_<run_id>].log` | Her satır JSON: `ts, level, logger, event, message` + alanlar (`pair`, `qty`, `net_pnl`, `code`…) |
| Konsol | stdout | Aynı olayların okunabilir hali |
| Defter | `data/ledger.sqlite` | `ledger` (OPEN/CLOSE), `trades`, `orders`, `equity`, `events` |
| Anlık durum | `run/paper_state.json` | Son turun tam görüntüsü (broker, risk, config, güvenlik) |
| Raporlar | `reports/` | backtest JSON/MD/PNG, `daily_*.md`, `verify_all_summary.json`, `exports/` |
| Bildirim denetimi | `logs/notifications.jsonl` | Her gönderim denemesi: zaman, olay, önem, sağlayıcı, durum (sent/suppressed/failed/dry_run), sebep, HTTP kodu, gecikme. Sırlar maskeli (`[REDACTED]`) |
| Bildirim özeti | `notify status` / `notify log` / `notify export` | Sağlayıcı aktifliği, son denemeler, CSV/JSON dışa aktarma |
| Sıfırlama | `data/cache/*.csv` | Mum verisi (bayt-kararlı yazılır) |

Log döndürme (rotation) bu sürümde **yoktur**; günlük dosya adı kullanıldığı için doğal olarak
günlük bölünür. Uzun süreli işletmede `logs/` ve `reports/` klasörlerini periyodik arşivleyin.

---

## 5. Sorun giderme

| Belirti | Neden / çözüm |
| --- | --- |
| `No module named cryptobot` | Gömülü Python; `scripts/paperbot.py` başlatıcısını kullanın |
| `state: running` ama heartbeat yaşlı | Süreç ölmüş olabilir: `run/paper_state.json`'a bakın, `paper.pid`'i kontrol edin, gerekirse sentinel bırakıp yeniden başlatın |
| `verify` FAIL | Aynı `run_id` ile karışmış eski satırlar (yeniden kullanımda otomatik silinir), elle eklenen satırlar, ya da farklı config ile üretilmiş defter |
| Hiç işlem yok | Normal: üç filtrenin (dip + trend + RSI) birlikte sağlanması gerekir; logda `blocked:*` nedenlerine bakın |
| `feed_unavailable` + `pause_new_entries` | Ağ/borsa kesintisi; cache varsa bayat veriyle devam eder, **yeni giriş açmaz** (fail-safe). Gerçek zamanlı modda cache `data.max_cache_age_bars` bar'dan eskiyse `cache_stale` olayı yazılır ve tazeleme denenir; ağ yoksa `complete=False` ile giriş durur |
| İşlem tavanı beklenenden erken doluyor | `max_trades_per_day` **yeni girişleri** sayar (kapanışlar sayılmaz); sayaç UTC günü değişince sıfırlanır |
| `below_min_notional` | Equity küçüldü; pozisyon 5 USDT altına düşüyor |
| `daily_loss_limit_reached` | Günlük limit; UTC günü değişince otomatik temizlenir |
| Aynı rapor dosyası üzerine yazıldı | `daily_<gün>.md` güne göre adlandırılır; aynı gün iki koşu son koşuyu bırakır (tasarım gereği) |
| `Bu bot zaten calisiyor (pid ...)` / çıkış kodu `4` | Çalışan bir örnek var (pid + `paper_state.json` `running`): `start-bot.cmd stop`. Süreç gerçekten ölmüşse kalıntı pid dosyasını silin (bkz. §3.1) |
| `LEDGER KILITLI ... database is locked` / çıkış kodu `4` | Defteri başka bir süreç tutuyor: çalışan örneği durdurun, defteri açık tutan araçları kapatın ya da `--db-path` ile başka bir defter kullanın |

---

## 6. Bilinen eksikler / doğrulanmamış noktalar (dürüst liste)

1. **Kârlılık yok.** Gerçek 180 günlük veride tüm parite/timeframe kombinasyonları net negatiftir
   (bkz. `RISK.md` §3). Bu bir altyapı teslimidir, kârlı bir strateji teslimi değildir.
2. **Uzun süreli gerçek zamanlı koşu yapılmamıştır.** Doğrulanan paper koşuları sınırlı sayıda tur
   (37–40 tur) ve `--replay` ileri-yürüyüş modundadır. Haftalarca kesintisiz gerçek zamanlı koşu
   (`--interval-seconds 60`) test edilmemiştir.
3. **Linux/macOS'ta çalıştırılmamıştır.** Testler ve koşular Windows + gömülü Python üzerindedir;
   Docker imajı oluşturulmuş ama **bu ortamda `docker build` çalıştırılmamıştır**.
4. **GitHub Actions workflow'u bu ortamda tetiklenmemiştir** (yerel doğrulama yapılmıştır).
5. **`ccxt` yedek transport'u birim test kapsamı dışındadır** (ağ gerektirir); canlı veri akışında
   hiç kullanılmamıştır (varsayılan `requests` tabanlı REST kullanılır).
6. **Log rotasyonu ve Prometheus metrikleri yoktur.** Mobil bildirim katmanı **vardır**
   (ntfy/telegram/webhook/console/file; `NOTIFICATIONS.md`); e-posta gönderimi yoktur.
   Bildirim katmanı doğrulanmıştır: yerel HTTP sink ile 183+ gerçek POST, ntfy.sh'a gerçek canlı
   yayın (HTTP 200) ve `notify_check.py` 4/4 adım PASS. Uzun süreli gerçek zamanlı koşuda
   bildirim davranışı (saatlik limit, sessiz saatler) birim testlerle doğrulanmıştır ancak
   haftalarca süren canlı koşuda gözlenmemiştir.
7. **Vergi, çoklu borsa, fon yönetimi, gerçek emir yürütme** kapsam dışıdır (ve canlı emir yolu
   kasten yoktur).
8. **Sharpe** 180 günlük veride gürültülüdür; yıllıklandırma varsayımına duyarlıdır.
9. **Mükerrer örnek koruması pid tabanlıdır.** Canlılık kontrolü taşınabilirdir (Windows'ta
   `OpenProcess`/`GetExitCodeProcess`, POSIX'te `os.kill(pid, 0)`; Windows'ta `os.kill` süreci
   öldüreceği için kasten kullanılmaz) ve Windows'ta gerçek süreçlerle doğrulanmıştır (`observed`).
   PID geri dönüşümünde (pid dosyası eski, pid artık başka bir sürece ait) pid "incelenemez"
   sayılır: koşu engellenmez, eski kayıt temizlenir ve son sözü gerçek kilit söyler
   (`database is locked` → kısa mesaj + çıkış kodu `4`). Yani koruma "kanıtlanmış canlı" durumda
   kesin, şüpheli durumda iyimserdir.

---

## 7. Teslim kontrol listesi

| Adım | Komut | Beklenen |
| --- | --- | --- |
| Güvenlik taraması | `verify_all.py` adım 1 | 0 bulgu, `no live-order code path` |
| Testler | `verify_all.py` adım 2 | 405 test, 0 hata |
| Veri | adım 3 | `BTCUSDT_1h.csv` 4320 satır, `15m` 17280 satır |
| Backtest tekrarlanabilirlik | adım 4 | `metrics JSON byte-identical: True` |
| Paper koşu + mutabakat | adım 5 | `mutabakat PASS`, 4/4 kontrol `ok` |
| Defter dışa aktarma | adım 6 | `ledger.csv`, `ledger_<run_id>.json` |
| Bildirim doğrulaması | `scripts/notify_check.py` | 4/4 adım PASS (yerel sink; gerçek HTTP POST'lar) |
| Telefon aboneliği | `notify status` + `notify test` | ntfy AKTIF, telefona test bildirimi düşer |
| Geri alma tatbikatı | `runbook_rollback.py` | `ROLLBACK DRILL: PASS` |
| Canlı emir yolu yok | `verify_all.py` adım 1 + `tests/test_no_live_orders.py` | 0 bulgu |
