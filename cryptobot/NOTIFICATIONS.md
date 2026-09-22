# NOTIFICATIONS.md — Telefondan Takip (Mobil Bildirim Katmanı)

Bu belge, paper bot'un **telefonunuza kısa bildirimler** göndermesini adım adım anlatır.
Varsayılan yol **ücretsizdir ve hesap gerektirmez**: [ntfy](https://ntfy.sh) uygulamasını kurup
bir "konu"ya (topic) abone olursunuz, bot da o konuya mesaj yayınlar.

> ✅ **Kapsam (varsayılan): yalnızca işlem bildirimleri.** Telefon yalnızca bir pozisyon
> **AÇILDIĞINDA** ve **KAPANDIĞINDA** çalar. Diğer tüm olaylar (`bot_started`,
> `risk_halted`, `daily_summary`, …) uygulanmış ve `notify preview --all` ile
> önizlenebilir kalır, ama varsayılan olarak **filtrelenir** — tek satırla geri açılır
> (§15.1). `notify status` hangi olayın aktif/pasif olduğunu açıkça yazar.

> ✅ **Otomasyon hiçbir zaman gerçek bildirim göndermez.** Test paketi, `verify_all.py`,
> `acceptance_check.py` ve `notify_check.py` yapısal olarak engellidir (*harness guard*),
> ve `run --replay` / `--offline` açık `--notify-send` bayrağı olmadan ağa göndermez
> (§16). Bu, geçmişteki "yüzlerce bildirim" olayının kalıcı çözümüdür (§15).

> ⚠️ Bildirim katmanı **yalnızca mesaj gönderir**. Borsaya emir göndermez, işlem kararlarını
> etkilemez ve bir sağlayıcı çökse bile paper döngüsü durmaz (hata yutulur, kayda geçer).
> Yatırım tavsiyesi değildir; sınırlamalar için [`RISK.md`](RISK.md).

---

## 1. 60 saniyede özet

```bash
# 1) Hazır başlatıcıyı kullanın -- konu adı ve erişilebilir ntfy host'u hazır gelir.
#    Başlatıcı, değişkenleri YALNIZCA başlattığı sürece verir (makine geneli gerekmez).

# 2) Doğrulayın: hangi olaylar aktif, gerçek gönderim mümkün mü?
cryptobot\start-bot.cmd notify status

# 3) Telefona gerçek bir test bildirimi gönderin
cryptobot\start-bot.cmd notify test

# 4) Botu paper modda başlatın (varsayılan: yalnızca işlem açılış/kapanış bildirimi)
cryptobot\start-bot.cmd
```

Elle (başlatıcı olmadan) yapmak isterseniz, değişkenleri **yalnızca o oturum için** verin:

```powershell
# PowerShell -- SADECE bu oturum. Kalıcı (User/System) TANIMLAMAYIN; bkz. §15.
$env:CRYPTOBOT_NTFY_HOST  = "ntfy.envs.net"                        # erişilebilir ücretsiz host (§2.5)
$env:CRYPTOBOT_NTFY_TOPIC = "cryptobot-paper-6c6de613c4-db65cc46fe"
python cryptobot/scripts/paperbot.py notify status
python cryptobot/scripts/paperbot.py notify test
```

Telefonda ntfy uygulamasında `cryptobot-paper-6c6de613c4-db65cc46fe` konusuna aboneyseniz
birkaç saniye içinde bildirimi görürsünüz. Mesajlar artık **bölümlü, emoji başlıklı**
biçimde gelir (bkz. §2.4); ntfy'ye `Markdown: yes` başlığı gönderilir, böylece başlıklar
telefonda **kalın** görünür.

Bu konuya daha önce yapılmış gerçek bir yayının kaydı (eski, tek satırlık biçim):

```
TOPIC = cryptobot-paper-6c6de613c4-db65cc46fe
HTTP  = 200
BODY  = {"id":"daYRz8AZTHol","time":1789198722,"expires":1789241922,"event":"message",
         "topic":"cryptobot-paper-6c6de613c4-db65cc46fe","title":"cryptobot test bildirimi",
         "message":"...","priority":3,"tags":["information_source"]}
```

Yeni biçimin tamamını **göndermeden** görmek için:

```bash
python cryptobot/scripts/paperbot.py notify preview --event position_opened
python cryptobot/scripts/paperbot.py notify preview --all          # 13 olayin tamami
```

---

## 2. ntfy kurulumu (önerilen, ücretsiz, hesap yok)

### 2.1 Uygulamayı kurun

| Platform | Yer |
| --- | --- |
| Android | Google Play → **ntfy** |
| iOS | App Store → **ntfy** |
| Masaüstü / tarayıcı | [ntfy.sh](https://ntfy.sh) adresinde konu adını yazıp **Subscribe** |

### 2.2 Konuya abone olun

1. Uygulamayı açın → **+** (Add subscription / Abone ol).
2. **Topic**: seçtiğiniz konu adını **birebir** yazın, örn. `cryptobot-paper-6c6de613c4-db65cc46fe`
   (büyük/küçük harf duyarlıdır).
3. Sunucu alanı boş kalsın (genel `ntfy.sh`) veya kendi sunucunuzun adresini yazın.
4. Kaydedin. Artık bu konuya yayınlanan her mesaj telefona düşer.

### 2.3 Botu konuya bağlayın

En kolay ve **önerilen** yol hazır başlatıcıdır; değişkenleri yalnızca başlattığı sürece verir:

```bat
cryptobot\start-bot.cmd                 :: paper modda başlatır (host/topic'i ekrana yazar)
cryptobot\start-bot.cmd notify status   :: kurulumu gösterir
cryptobot\start-bot.cmd notify test     :: telefona test bildirimi
```

Başlatıcının en üstünde tek bir ayar satırı vardır (gizlilik notu hemen yanındadır):

```bat
rem  AYAR: ntfy sunucusu. Degistirmek icin SADECE bu satiri duzenleyin.
set "NTFY_HOST_DEFAULT=ntfy.envs.net"
```

Elle yapmak isterseniz (yalnızca **o oturum** için):

```powershell
# PowerShell (bu oturum için) -- kalıcı YAPMAYIN, bkz. §15
$env:CRYPTOBOT_NTFY_HOST  = "ntfy.envs.net"
$env:CRYPTOBOT_NTFY_TOPIC = "cryptobot-paper-6c6de613c4-db65cc46fe"

# .env şablonu: cryptobot/.env.example
```

> 🚫 **Makine geneli (User/System) `CRYPTOBOT_NTFY_TOPIC` TANIMLAMAYIN.** Kalıcı bir
> değişken makinedeki **her** Python sürecine geçer: test paketi, `verify_all.py`,
> `acceptance_check.py` ve elle çalıştırdığınız her komut potansiyel bir **gerçek
> yayıncıya** dönüşür. Geçmişte telefona yüzlerce bildirim bu yüzden geldi — ayrıntı
> ve kalıcı çözüm: **§15**.

Kontrol:

```bash
cryptobot\start-bot.cmd notify status
#   notify_on      : 2 olay AKTIF -> position_opened, position_closed
#   filtrelenen    : 10 olay PASIF -> ...
#   [AKTIF ] ntfy  (ag)
#   GERCEK GONDERIM : MUMKUN
```

`ntfy_topic` boşsa ntfy **pasif** kalır ve `notify status` nedeni yazar; bu bir hata değildir,
yalnızca `console` + `file` çalışır.

### 2.4 Telefonda ne görürsünüz?

Her bildirim **bölümlere ayrılmış, emoji başlıklı** kısa satırlardan oluşur; amaç
telefon kilit ekranında tek bakışta okunmasıdır. Önem derecesi hem baştaki renk
karesine hem önceliğe yansır: `🟦 info`, `🟨 warning`, `🟥 critical`.

Bir pozisyon açılışı (gerçek backtest ölçümüyle birlikte):

```
🟦 BTCUSDT · LONG (PAPER)

💰 Giriş: 71.679,51
🎯 Hedef (net +%2): 73.296,12
🛑 Stop: 69.887,52
📦 Miktar: 0,00062779 BTC · 45,00 USDT
⏱️ Zaman aralığı: 1h

💡 Neden geldi?
Bollinger alt bandının altında kapanış + RSI < 35 + fiyat SMA200 üstünde.

📊 Sinyal bağlamı (ölçüm, olasılık değil)
🧭 Rejim: BULL (fiyat > SMA200)
🔊 Hacim log-Z: +1,84
📐 Filtre marjları: RSI 4,0 altı · bant 0,32σ altı · trend 3,1% üstü

📚 Geçmiş ölçüm (180 gün · 1h · BTCUSDT/ETHUSDT)
🧪 İşlem: 19 · isabet %52,6 · medyan +0,14%
🎯 +%2 hedefe dokunma: %10  |  🛑 -%2,5 stop: %21
🗂️ Config referansı: aa5725245536
⚠️ Tarihsel ölçüm; kâr garantisi değil, tavsiye değildir.

📌 Senaryo seviyeleri (girişe göre)
▲ +%1: 72.396,31 · +%2: 73.113,10 · +%3: 73.829,90
▼ -%2: 70.245,92 · -%5: 68.095,54
```

Önemli dürüstlük kuralları:

* **"Ölçüm" = ölçüm, "olasılık" değil.** `📐 Filtre marjları` bloğu, sinyalin
  eşikleri ne kadar aştığını söyler (RSI eşiğin kaç puan altında, fiyat alt bandın
  kaç sigma altında, trend SMA'nın yüzde kaç üstünde). Bu bir **kâr olasılığı
  değildir**; blok bunu başlığında açıkça yazar.
* **Geçmiş blok yalnızca gerçek bir ölçümden gelir.** `📚 Geçmiş ölçüm` bloğu,
  `data/cache` üzerinde gerçekten çalıştırılmış bir backtest'ten üretilir; dönem,
  zaman aralığı, pariteler ve **config hash**'i yazar ve "tarihsel, söz değil"
  uyarısını taşır. Önbellek yoksa blok **tamamen atlanır** (yerine uydurma sayı
  konmaz).
* **Ölçülemeyen değer yazılmaz.** Bir alan hesaplanamıyorsa o satır hiç
  gösterilmez; `n/a` gibi bir yer tutucu ölçüm gibi görünecek şekilde basılmaz.
* **Sayılar Türkçe biçimde:** binlik `.`, ondalık `,` (ör. `71.679,51`), PnL'de
  her zaman işaret (`+0,81` / `-1,24` USDT).

Kanal başına biçim: ntfy **Markdown** (`**kalın**` + `Markdown: yes` başlığı),
Telegram **HTML** (`<b>`), webhook/console/file ise **düz metin** alır. Hepsi aynı
içerik modelinden üretilir, yani sayılar kanallar arasında tutarlıdır. Gönderilen
`Title` başlığı kısa ve ASCII tutulur (bildirim başlıkları telefonda kısaltılır).

Diğer olaylar aynı düzeni kullanır: `bot_started`/`bot_stopped` (başlatma/kapanış),
`position_closed` (giriş → çıkış, komisyon, brüt, net USDT ve net %, neden, süre),
`take_profit_hit` / `stop_loss_hit`, `risk_halted`, `cooldown_started`,
`feed_outage`, `data_fail_safe`, `daily_summary`, `equity_drop`, `test`.
Hepsini görmek için: `notify preview --all`.

> ℹ️ **Tek çıkış = tek mesaj.** `take_profit_hit` ve `stop_loss_hit`, `position_closed`'in
> **ayrıntı varyantlarıdır**: aynı çıkışı anlatırlar. `position_closed` etkinken bu ikisi
> bir ağ sağlayıcısına **ikinci bir mesaj göndermez**; kayıt
> `suppressed / superseded_by_position_closed` olarak düşer (tek doğruluk kaynağı kapanış
> mesajıdır). Varyantı tek başına istiyorsanız `notify_on`'dan `position_closed`'i çıkarıp
> `take_profit_hit`/`stop_loss_hit` ekleyin.

### 2.5 Kendi ntfy sunucunuz / erişilebilir host / erişim jetonu (opsiyonel)

> 🌐 **Erişilebilirlik notu.** Genel `ntfy.sh` bazı ağlarda/ülkelerde **erişilemez** olabilir
> (bu makinede TCP 443 zaman aşımına uğradığı ölçülmüştür; DNS çözülür ama bağlantı kurulmaz).
> Bu durumda ntfy sağlayıcısı "gönderdim" sanmaz — `notify log` `failed` ve zaman aşımı yazar.
> Erişilebilir doğrulanmış ücretsiz bir yansı **`ntfy.envs.net`**'tir; başlatıcı varsayılanı budur.
> Kendi sunucunuzu kurmak hem erişilebilirlik hem **gizlilik** için en iyisidir.

> 🔒 **Gizlilik.** `ntfy.envs.net` gibi ücretsiz bir yansı (mirror), mesaj **içeriğini** görür;
> konu adını bilen herkes de okuyabilir. Kendi barındırdığınız ntfy sunucusu
> (`CRYPTOBOT_NTFY_HOST`) veya Telegram (`api.telegram.org`, yalnızca sizinle botunuz
> arasında) bu üçüncü tarafı ortadan kaldırır.

```powershell
$env:CRYPTOBOT_NTFY_HOST = "https://ntfy.example.org"   # şema dahil yazabilirsiniz
$env:CRYPTOBOT_NTFY_TOKEN = "tk_..."                    # Authorization: Bearer <jeton>
$env:CRYPTOBOT_NTFY_TAGS  = "chart_with_upwards_trend"  # varsayılan etiketi değiştirir
$env:CRYPTOBOT_NTFY_CLICK = "https://.../dashboard"     # bildirime tıklanınca açılacak adres
```

---

## 3. Bilgisayarım kapalıyken çalışır mı?

**Varsayılan cevap: hayır.** Bot bu depodaki bir Python sürecidir, uzaktaki bir
"sunucu" değil: bilgisayar kapalı, uykuda ya da oturum kapalıysa süreç çalışmaz,
hiçbir bar işlenmez ve hiçbir bildirim **üretilmez**. Bildirim katmanı yalnızca
botun ürettiği olayları gönderir; arka planda kendi başına çalışan bir parçası
yoktur.

Bunu ücretsiz çözmenin üç yolu var.

### 3.1 Ücretsiz bulut: GitHub Actions zamanlanmış koşu (önerilen)

Depoda hazır bir iş akışı var: [`.github/workflows/paper-trade.yml`](.github/workflows/paper-trade.yml).
GitHub, **sizin makineniz kapalıyken** bunu kendi sunucularında çalıştırır:
önce güncel halka açık mumları indirir, sonra paper döngüsünü **sınırlı bir
canlı pencere** boyunca çalıştırır (`--duration-seconds` / `--interval-seconds`),
sonra mutabakat + rapor üretir ve ntfy'ye bildirir. API anahtarı gerekmez.

Adım adım:

1. **Depo açın.** GitHub'da yeni bir depo oluşturun. *Public* depo = ücretsiz ve
   sınırsız Actions dakikası; *private* depo = aylık ücretsiz dakika kotası.
2. **Projeyi push edin** (depo kökünde `cryptobot/` klasörü görünmeli):
   ```bash
   git init && git add . && git commit -m "cryptobot" && git branch -M main
   git remote add origin https://github.com/<kullanici>/<depo>.git
   git push -u origin main
   ```
3. **Konuyu tanımlayın.** Depo → Settings → Secrets and variables → Actions →
   **Variables** → *New repository variable*:
   `CRYPTOBOT_NTFY_TOPIC = cryptobot-paper-<rastgele-uzun-ad>`
   (İsterseniz aynı adı **Secret** olarak da tanımlayabilirsiniz; iş akışı önce
   variable'a, yoksa secret'a bakar. İkisi de yoksa koşu yine çalışır, yalnızca
   bildirimler yerelde kalır.)
4. **İlk koşuyu elle başlatın.** Depo → Actions → `paper-trade` → **Run workflow**.
   Parametreler: `timeframe` (varsayılan `1h`), `duration_seconds` (300),
   `interval_seconds` (60), `days` (30).
5. **Telefonda** ntfy uygulamasında aynı konuya abone olun; koşu sırasında
   bildirimler düşer.

Ne sıklıkta çalışır? İş akışındaki `cron: "*/30 * * * *"` satırı **30 dakikada
bir** demektir. Değiştirmek için o satırı düzenleyip push edin.

Dürüst uyarılar (bunlar gerçek sınırlardır, özür değil):

* **Cron en iyi durumda ~5 dakikadır ve garanti değildir.** GitHub yoğun
  saatlerde zamanlanmış işleri dakikalarca — bazen daha uzun — geciktirebilir.
* **Hareketsiz depolarda zamanlanmış işler kapatılır.** GitHub, ~60 gün etkinlik
  olmayan depolarda `schedule` tetikleyicisini durdurur; ara sıra elle "Run
  workflow" çalıştırın.
* **Her tetikleme sınırlıdır.** Koşu 7/24 bir süreç değil, bir *snapshot*'tır:
  varsayılan 300 saniyelik pencerede birkaç döngü çalışır ve **durur**. Kaçırdığı
  barlar için sonradan karar üretilmez.
* **Durum koşular arasında kalıcı değildir.** Ledger
  (`cryptobot/data/ledger.sqlite`) ve bildirim kaydı her koşuda sıfırdan başlar.
  Devam eden bir equity geçmişi istiyorsanız koşu sonundaki
  `cryptobot-ledger-<run_number>` artifact'ini indirip depoya
  `cryptobot/data/ledger.sqlite` olarak koymanız gerekir (ileri düzey; küçük
  denemeler için gerekmez).
* **Ücretsiz dakika/limitler.** Public depoda Actions ücretsiz ve sınırsızdır;
  private depoda aylık ücretsiz dakika kotası vardır ve kota dolunca işler
  çalışmaz.
* **Konu adı sırdır gibi davranın.** `config.yaml`'a yazmayın; repo
  variable/secret veya ortam değişkeni kullanın (bkz. §12.1).

### 3.2 Alternatif: her zaman ücretsiz küçük bir VM

Oracle Cloud "Always Free", Google Cloud e2-micro ücretsiz katmanı veya bir
Raspberry Pi üzerinde botu `systemd`/`tmux` ile sürekli çalıştırabilirsiniz:

```bash
export CRYPTOBOT_NTFY_TOPIC="cryptobot-paper-<rastgele>"
python cryptobot/scripts/paperbot.py run --interval-seconds 60
```

* Artıları: gerçek 7/24 çalışma, cron gecikmesi yok, ledger kalıcı.
* Eksileri: kurulum/yönetim sizde; bazı "ücretsiz" katmanlar kredi kartı ister.

### 3.3 En basit: bilgisayarı açık bırakın

```powershell
# PowerShell -- bu depodaki hazir baslatici (konu adi hazir gelir)
.\start-bot.ps1

# veya elle:
$env:CRYPTOBOT_NTFY_TOPIC = "cryptobot-paper-<rastgele>"
python cryptobot/scripts/paperbot.py run --interval-seconds 60
```

Uyku modunu kapatın (Windows: *Güç ve uyku* → "Hiçbir zaman"), aksi halde süreç
askıya alınır ve bot durur.

---

## 4. Gecikme: "anında" ne demek?

Bildirim, **karar verildiği anda** gönderilir. Gecikmenin büyük kısmı bildirim
katmanından değil, kararın kendisinden gelir:

* **Strateji kapanmış mumla çalışır.** Sinyal ancak bar kapandıktan sonra
  oluşur: 1 saatlik grafikte en kötü durumda ~1 saat beklersiniz. Bu bir hata
  değil, "yeniden boyama" (repaint) yapmama tercihidir.
* **Gerçek mod döngüsü varsayılan 60 saniyedir** (`--interval-seconds 60`). Bir
  sinyal en fazla bir sonraki döngüye kadar (≤60 sn) bekler; `--interval-seconds 5`
  ile bu 5 saniyeye iner.
* **Ağ/sağlayıcı gecikmesi** tipik olarak 0,1–2 saniyedir (ntfy genelde <1 sn).
* Spam filtreleri bir mesajı **geciktirmez, bastırır**; neyin neden
  bastırıldığını `notify log` gösterir.

### 4.1 "Her işlem bildirimi hemen gelsin" tarifi

```powershell
# Tum olaylar, dedupe kapali, saatlik sinir cok yuksek, sessiz saat yok, esik info
$env:CRYPTOBOT_NOTIFY_ON = "bot_started,bot_stopped,position_opened,position_closed,take_profit_hit,stop_loss_hit,risk_halted,cooldown_started,feed_outage,data_fail_safe,daily_summary,equity_drop,test"
$env:CRYPTOBOT_NOTIFY_DEDUPE_SECONDS = "0"
$env:CRYPTOBOT_NOTIFY_MAX_PER_HOUR  = "100000"
$env:CRYPTOBOT_NOTIFY_MIN_SEVERITY  = "info"
# quiet_hours zaten config.yaml'da null; sessiz saat istemiyorsaniz dokunmayin.

python cryptobot/scripts/paperbot.py run --interval-seconds 60
```

Notlar:

* `max_per_hour` **kritik** olayları (`risk_halted`, `data_fail_safe`) asla
  bastırmaz: kritikler saatlik sınıra takılmadan gönderilir (yine de dedupe
  uygulanır ve kayda geçer).
* `max_per_hour` bir **ağ** bütçesidir: yalnızca başarılı ağ gönderimleri (ntfy /
  telegram / webhook, yani `requires_network = true`) sayılır. `console` ve `file`
  yerel yansımaları bütçeyi tüketmez; günlük geçmişteki yerel kayıtlar da bütçeyi
  dolduramaz (`notify status` → `ag butcesi` satırı).
* `dedupe = 0` aynı metnin tekrar tekrar gitmesine izin verir. Çok gürültülü
  olursa 30–60 saniye makul bir uzlaşmadır.
* `notify_on` listesine `position_closed` **ile birlikte** `take_profit_hit` /
  `stop_loss_hit` ekleseniz bile tek bir çıkış için **iki mesaj gelmez**: kapanış
  mesajı tek doğruluk kaynağıdır, varyant ağa gönderilmez
  (`suppressed / superseded_by_position_closed`).
* Daha sık sinyal için daha küçük zaman aralığı kullanın (ör. `--timeframe 15m`);
  komisyon + slippage'ın küçük hesapta sonucu domine ettiğini unutmayın
  ([`RISK.md`](RISK.md)).
* **`--replay` / `--offline` için bu tarif ağa göndermez** (bkz. §16): o modlarda
  gerçek gönderim için açıkça `--notify-send` gerekir.

---

## 5. Telegram kurulumu (opsiyonel)

1. Telegram'da **@BotFather** ile konuşun → `/newbot` → bot adı ve kullanıcı adı verin.
2. BotFather size bir **jeton** verir (`123456789:AA...`). Bunu kimseyle paylaşmayın.
3. Kendi Telegram hesabınızdan yeni botunuza **bir mesaj gönderin** (ör. "merhaba").
4. Sohbet kimliğinizi öğrenin: botunuza `https://api.telegram.org/bot<JETON>/getUpdates`
   adresini tarayıcıda açın; `"chat":{"id":123456789,...}` içindeki sayı **chat id**'dir.
   Alternatif: **@userinfobot** botuna mesaj atın.
5. Ortam değişkenlerini ayarlayın:

```powershell
$env:CRYPTOBOT_TELEGRAM_BOT_TOKEN = "123456789:AA..."
$env:CRYPTOBOT_TELEGRAM_CHAT_ID   = "123456789"
# Sağlayıcıyı listeye ekleyin (virgülle ayrılmış):
$env:CRYPTOBOT_NOTIFY_PROVIDERS   = "console,file,ntfy,telegram"
```

6. Doğrulayın:

```bash
python cryptobot/scripts/paperbot.py notify status   # telegram AKTIF görünmeli
python cryptobot/scripts/paperbot.py notify test
```

Her iki ortam değişkeni de tanımlı değilse telegram **pasif** kalır ve `notify status` eksik
değişkenin adını yazar.

---

## 6. Webhook (Discord / Slack / Zapier / n8n)

Genel bir JSON gövdesi POST edilir; hem `text` (Slack) hem `content` (Discord) alanı bulunur:

```powershell
$env:CRYPTOBOT_WEBHOOK_URL     = "https://discord.com/api/webhooks/..."   # veya Slack/Zapier/n8n URL'i
$env:CRYPTOBOT_NOTIFY_PROVIDERS = "console,file,ntfy,webhook"
```

Gövde şekli:

```json
{
  "source": "cryptobot", "event": "position_closed", "severity": "info",
  "title": "POZISYON KAPANDI: BTCUSDT",
  "text": "🟦 BTCUSDT · KAPANDI (PAPER)\n\n🚪 Giriş → Çıkış: 71.679,51 → 71.328,34\n📦 Miktar: 0,00062779 BTC\n💸 Komisyon: 0,0898 USDT\n📈 Brüt: -0,1756 USDT\n💰 Net: -0,3102 USDT (-0,69%)\n🧭 Neden: strateji çıkışı (fiyat orta banda döndü)\n⏱️ Süre: 20s (20 bar)",
  "content": "...(text ile aynı: düz metin)...",
  "ts": 1789198714000, "run_id": "paper-...", "pair": "BTC/USDT", "data": {"net_pnl": -0.3102, "fees": 0.0898}
}
```

> Webhook `text`/`content` alanları **düz metindir** (Markdown/HTML işaretlemesi
> içermez). Tüm alanlar aynı içerik modelinden üretilir; `data` alanı makine
> tarafından okunabilecek ham değerleri taşır.

> Webhook adresi bir sırdır; `config.yaml`'a **yazmayın**, yalnızca ortam değişkeni kullanın.

---

## 7. console + file (her zaman açık, ağ gerekmez)

Bu iki sağlayıcı hiçbir ayar gerektirmez ve **her zaman aktiftir**:

* `console` — paper döngüsünün konsoluna (ve `logs/*.log` içine) mesajın **düz
  metin** hâlini yazar; `logs/*.log` içinde şöyle görünür:

  ```
  🟨 BTCUSDT · STOP ✗ (PAPER)

  🛑 Stop tetiklendi
  🚪 Giriş → Çıkış: 71.679,51 → 69.887,52
  💰 Net: -1,2293 USDT (-2,73%)
  ⏱️ Süre: 3s
  ```
* `file` — her gönderim denemesini `logs/notifications.jsonl` dosyasına **bir JSON satırı** olarak ekler.
  Aynı dosya **denetim kaydıdır** (audit): gönderildi / bastırıldı / başarısız / dry_run.

İnceleme:

```bash
python cryptobot/scripts/paperbot.py notify log                # son 20 kayıt
python cryptobot/scripts/paperbot.py notify log --limit 100
python cryptobot/scripts/paperbot.py notify log --json
python cryptobot/scripts/paperbot.py notify export --out reports/notifications
```

---

## 8. Komutlar

| Komut | Ne yapar |
| --- | --- |
| `notify status` | Yapılandırılmış sağlayıcıları, aktiflik durumunu ve **pasifse nedenini** gösterir |
| `notify preview` | Örnek verilerle **her olayın tam metnini** yazdırır; varsayılan olarak **hiçbir şey göndermez** |
| `notify preview --event position_closed` | Yalnızca tek bir olayı gösterir |
| `notify preview --carrier markdown` | ntfy gövdesini (Markdown); `html` ise Telegram gövdesini gösterir |
| `notify preview --send` | **Gerçekten** gönderir (yalnızca bu bayrakla; elle test için) |
| `notify test` | Etkin sağlayıcılara **canlı** test bildirimi gönderir |
| `notify test --dry-run` | Hiçbir şey göndermeden tam olarak ne gönderileceğini gösterir/kaydeder |
| `notify log [--limit N] [--json]` | Son gönderim denemelerini yazdırır (varsayılan 20) |
| `notify export [--format csv\|json\|both]` | Kayıtları CSV/JSON olarak dışa aktarır |
| `run --notify-dry-run` | Paper koşusunu bildirimleri **göndermeden** çalıştırır (ne gönderileceğini gösterir) |
| `run --no-notify` | Bu koşu için bildirim katmanını tamamen kapatır |
| `run --replay` | Cache üzerinde geri-yürüyüş; **ağa göndermez** (varsayılan) — §16 |
| `run --offline` | Yalnızca cache'ten okur; **ağa göndermez** (varsayılan) — §16 |
| `run --replay --notify-send` | Replay/offline koşusunda ağ bildirimini **açıkça** açar |

---

## 9. Yapılandırma ve ortam değişkenleri

Tüm ayarlar `config.yaml` içindeki `notifications:` bölümündedir (Türkçe açıklamalı).
Öncelik: **`config.yaml` < ortam değişkeni < CLI bayrağı**.

| Ayar | Varsayılan | Anlamı | Ortam değişkeni |
| --- | --- | --- | --- |
| `enabled` | `true` | Ana anahtar | `CRYPTOBOT_NOTIFY_ENABLED` |
| `providers` | `console, file, ntfy` | Kullanılacak sağlayıcılar | `CRYPTOBOT_NOTIFY_PROVIDERS` |
| `ntfy_host` | `ntfy.sh` | ntfy sunucusu (başlatıcı erişilebilir `ntfy.envs.net` kullanır, §2.5) | `CRYPTOBOT_NTFY_HOST` |
| `ntfy_topic` | `null` | ntfy konu adı (sır) | `CRYPTOBOT_NTFY_TOPIC` |
| `min_severity` | `info` | Altındaki önem derecelerini gönderme | `CRYPTOBOT_NOTIFY_MIN_SEVERITY` |
| `dedupe_window_seconds` | `300` | Aynı bildirimi bu süre içinde tekrar gönderme | `CRYPTOBOT_NOTIFY_DEDUPE_SECONDS` |
| `max_per_hour` | `20` | Saatte en fazla **ağ** gönderimi (yerel console/file sayılmaz) | `CRYPTOBOT_NOTIFY_MAX_PER_HOUR` |
| `quiet_hours` | `null` | Sessiz saatler, ör. `"22:00-07:00"` (sadece critical geçer) | `CRYPTOBOT_NOTIFY_QUIET_HOURS` |
| `dry_run` | `false` | Gönderme, yalnızca göster/kaydet | `CRYPTOBOT_NOTIFY_DRY_RUN` |
| `timeout_seconds` | `10.0` | Sağlayıcı başına HTTP zaman aşımı | `CRYPTOBOT_NOTIFY_TIMEOUT_SECONDS` |
| `retry_max` | `2` | Yeniden deneme sayısı (toplam = 1 + bu; üst sınır 10) | `CRYPTOBOT_NOTIFY_RETRY_MAX` |
| `backoff_initial_seconds` | `1.0` | İlk bekleme (2× artar) | — |
| `backoff_max_seconds` | `8.0` | Bekleme tavanı | — |
| `equity_drop_pct` | `3.0` | Zirveden bu % düşüşte uyarı (0 = kapalı) | — |
| `notify_on` | `position_opened, position_closed` | Hangi olay türleri bildirilir (varsayılan: **yalnızca işlem**) | `CRYPTOBOT_NOTIFY_ON` |

**Sırlar (asla `config.yaml`'a yazılmaz):** `CRYPTOBOT_NTFY_TOPIC`, `CRYPTOBOT_NTFY_TOKEN`,
`CRYPTOBOT_TELEGRAM_BOT_TOKEN`, `CRYPTOBOT_TELEGRAM_CHAT_ID`, `CRYPTOBOT_WEBHOOK_URL`,
(`CRYPTOBOT_TELEGRAM_API_BASE` yalnızca kendi Bot API sunucunuz için).

Hazır şablon: [`cryptobot/.env.example`](.env.example).

> ⚠️ `CRYPTOBOT_NTFY_TOPIC`'i **kalıcı (User/System)** tanımlamak, makinedeki tüm Python
> süreçlerini potansiyel gerçek yayıncı yapar — §15'teki olayın kökeni budur.
> `.env` dosyası da bu yüzden yalnızca bilinçli, kapsamlı kullanım içindir.

---

## 10. Spam koruması nasıl çalışır?

Sırayla uygulanır ve her bastırma **nedeniyle birlikte** kaydedilir:

1. `enabled` kapalıysa hiç üretilmez.
2. `notify_on` listesinde olmayan olay → `event_not_enabled`.
3. `min_severity` altındaki olay → `below_min_severity` (`info < warning < critical`).
4. `quiet_hours` penceresinde `info`/`warning` → `quiet_hours` (**critical her zaman geçer**).
5. Aynı bildirim `dedupe_window_seconds` içinde tekrar → `dedupe_window`.
6. Son 60 dakikada `max_per_hour` **ağ** gönderim sınırına ulaşıldıysa → `max_per_hour`
   (yalnızca başarılı **ağ** gönderimleri sayılır: `requires_network = true` olan
   sağlayıcılar; `console`/`file` yerel yansımaları bütçeyi tüketmez, denetim
   dosyasından durum yeniden yüklenirken de sayılmaz. **kritik olaylar bu sınıra
   takılmaz**: `risk_halted` ve `data_fail_safe` her koşulda gönderilir; yine de
   dedupe uygulanır ve kayda geçer. Güncel kullanım/kalan bütçe `notify status`
   çıktısındaki `ag butcesi` satırında görünür).
7. Etkin sağlayıcı yoksa → `no_active_provider`.

Bunlara ek olarak, **ağ sağlayıcı başına** iki yapısal koruma vardır (§16):

8. Bir TP/SL varyantı (`take_profit_hit`/`stop_loss_hit`) ve `position_closed` etkinse
   → `superseded_by_position_closed` (tek çıkış, tek ağ mesajı).
9. Otomasyon ortamı (`harness_guard`) veya replay/offline modu (`replay_no_push` /
   `offline_no_push`) → istek **hiç yapılmaz**, satır `suppressed` olarak yazılır.

`notify log` çıktısındaki `durum` ve `sebep/hedef` kolonları bunu gösterir; örneğin:

```
2026-09-12T10:41:02Z position_opened  info  -      suppressed 20      0ms max_per_hour
2026-09-13T22:10:11Z position_closed  info  ntfy   suppressed -       0ms harness_guard
```

---

## 11. Sorun giderme

| Belirti | Neden / çözüm |
| --- | --- |
| Telefona hiçbir şey gelmiyor | `notify status` → `ntfy` **AKTIF** mi? Konu adı birebir aynı mı? Uygulamada abonelik kaydedildi mi? `notify test` çalıştırın |
| `notify status` ntfy'yi PASIF gösteriyor | `CRYPTOBOT_NTFY_TOPIC` tanımlı değil (veya `notify status` çalıştırdığınız kabukta tanımlı değil) |
| `notify test` "hiçbir sağlayıcı mesaj gönderemedi" | Yalnızca pasif sağlayıcılar var; konu/jeton ayarlayın veya `console,file` ekleyin |
| Bildirim geldi ama içerik boş | Gönderen tarafta `min_severity` yükseltilmiş veya `quiet_hours` açık olabilir; `notify log` sebebi gösterir |
| Telegram 401/404 | Bot jetonu yanlış ya da bota hiç mesaj atmadınız (sohbet başlatılmamış) |
| Telegram 400 "chat not found" | `CRYPTOBOT_TELEGRAM_CHAT_ID` yanlış; `getUpdates` ile doğrulayın |
| Çok fazla bildirim | Varsayılan artık yalnızca işlem açılış/kapanıştır. Yine fazlaysa `dedupe_window_seconds` artırın, `max_per_hour` düşürün, `quiet_hours` tanımlayın veya `notify_on`'dan olay çıkarın |
| Hiç bildirim istemiyorum | `CRYPTOBOT_NOTIFY_ENABLED=0` ya da `run --no-notify` |
| `bot_started` / `risk_halted` / `daily_summary` artık gelmiyor | **Beklenen**: varsayılan kapsam yalnızca işlem olaylarıdır. Geri açmak için bkz. §15.1 |
| Replay/offline çalıştırdım, bildirim gelmedi | **Beklenen**: replay/offline ağa göndermez (`replay_no_push`/`offline_no_push`). Açıkça istiyorsanız `--notify-send` (§16) |
| `notify log`'da `harness_guard` görüyorum | Bu bir otomasyon süreci (test/doğrulama); gerçek ağ gönderimi yapısal olarak engellendi (§16) |
| Gönderilenleri denetlemek istiyorum | `logs/notifications.jsonl` (JSON satır) + `notify log` / `notify export` |
| Windows'ta test betiği yavaş/hata | `python cryptobot/scripts/paperbot.py ...` başlatıcısını kullanın (bkz. `README.md` §3) |

---

## 12. Güvenlik notu (önemli)

### 12.1 Genel `ntfy.sh` üzerindeki konular gizli değildir

`ntfy.sh` üzerinde bir konuya yayınlanan mesajları, **konu adını bilen herkes** okuyabilir
(ve abone olan herkese düşer). Konu adı bir paroladır: tahmin edilebilir adlar
(`bitcoin`, `cryptobot`, `test`) **kullanmayın**.

Aynı kural, erişilebilirlik için kullanılan **ücretsiz yansılar** için de geçerlidir
(ör. `ntfy.envs.net`): mesaj **içeriği o sunucudan geçer** ve yansı operatörü tarafından
görülebilir. Gerçekten gizli tutmak istiyorsanız kendi ntfy sunucunuzu barındırın veya
Telegram kullanın (yalnızca sizinle botunuz arasında, üçüncü taraf yansı yok).

Öneriler:

1. **Uzun, rastgele bir konu adı** seçin: `cryptobot-paper-<10 hex>-<10 hex>` gibi.
   Bu ortamda doğrulanan örnek: `cryptobot-paper-6c6de613c4-db65cc46fe`.
2. **`CRYPTOBOT_NTFY_TOKEN`** ile korumalı (kendi barındırdığınız) ntfy sunucusu kullanın;
   genel sunucuda jeton doğrulaması yoktur.
3. Konu adını **yalnızca o sürece verilen** ortam değişkeninde tutun (başlatıcı bunu yapar);
   `config.yaml`'a veya ekran görüntülerine yazmayın ve **kalıcı (User/System) tanımlamayın** (§15).
4. Sızdığını düşünüyorsanız: yeni bir rastgele konuya geçin (eski konudan aboneliği silin).
5. Alternatif: kendi ntfy sunucunuzu barındırın (Docker) veya Telegram/webhook kullanın.

### 12.2 Sırlar asla dosyaya yazılmaz

* `config.yaml` yalnızca **açık** (gizli olmayan) ayarları içerir; jeton/konu/adres yazılmaz.
* Gönderim kaydı ve loglar, `cryptobot.notify.redact` ile **maskelenir**; jeton ve konu adı
  `logs/notifications.jsonl` içinde `[REDACTED]` olarak görünür.
* `notify export` çıktısı da maskelenmiştir.
* Bir konu adının sızıp sızmadığını hızlıca kontrol edin:

```bash
grep -c "CRYPTOBOT\|Bearer tk_\|/bot[0-9]" cryptobot/logs/notifications.jsonl || true
```

### 12.3 Bildirim katmanı işlem yapamaz

Bu paket (`cryptobot/notify/`) yalnızca HTTP POST ile **metin** gönderir; içinde hiçbir borsa
emri, imzalı istek veya API anahtarı okuma yolu yoktur. Bu, depodaki AST taraması
(`python cryptobot/tests/no_live_order_scan.py`) ile sürekli doğrulanır: **0 bulgu**.

Ayrıca:

* Gönderim başarısız olursa **paper döngüsü etkilenmez** (istisna yutulur, kayda geçer).
* Her denemenin **zaman aşımı** vardır ve yeniden deneme sayısı üstten sınırlıdır; bir sağlayıcı
  döngüyü asla askıda bırakamaz.
* `run --no-notify` ile katman tamamen kapatılabilir.

### 12.4 Ağ gönderimi başlamadan önce kaç kapıdan geçer?

Bir olay telefona ulaşmadan önce sırasıyla: `enabled`/`notify_on`/`min_severity`/`quiet_hours`/
`dedupe`/`rate` filtreleri (§10) → **`harness_guard`** (§16.1) → **`replay_no_push`** (§16.2) →
**`superseded_by_position_closed`** (§2.4) → sağlayıcı `active()` kontrolü. Her ret
`logs/notifications.jsonl` içine `status=suppressed` + `reason` olarak yazılır; hiçbiri sessiz değil.

---

## 13. Denetim ("takip") ve dışa aktarma

Her gönderim denemesi `logs/notifications.jsonl` dosyasına bir JSON satırı olarak yazılır:

```json
{"ts":1789198714000,"iso_utc":"2026-09-12T10:18:34Z","run_id":"paper-...","event":"position_closed",
 "severity":"info","provider":"ntfy","status":"sent","reason":"","http_status":200,
 "latency_ms":103.4,"attempts":1,"dry_run":false,"title":"POZISYON KAPANDI: BTC/USDT",
 "body":"...","dedupe_key":"...","target":"https://ntfy.sh/[REDACTED]"}
```

Neden SQLite defteri yerine JSONL? Çünkü bildirim gönderimi işlem döngüsünden **bağımsız ve
hataya dayanıklı** olmalıdır: aynı SQLite bağlantısına ikinci bir yazar, kilitlenme (`database is
locked`) riskiyle **işlem döngüsünü** etkileyebilirdi. JSONL tek satır eklemedir, çökmede
bozulmaz, ledger şemasına dokunmaz ve `notify log` / `notify export` ile kolayca incelenir.
Dosya aynı zamanda `file` sağlayıcısının çıktısıdır (tek yazma yolu, kopya yok).

---

## 14. İlgili dosyalar

| Dosya | İçerik |
| --- | --- |
| `config.yaml` | `notifications:` bölümü (Türkçe açıklamalı) |
| `.env.example` | Bildirim ortam değişkenleri şablonu |
| `cryptobot/notify/render.py` | İçerik modeli + olay şablonları + kanal başına render |
| `cryptobot/notify/format.py` | Türkçe sayı/tarih biçimlendirme (binlik `.`, ondalık `,`) |
| `cryptobot/notify/context.py` | Filtre marjları (ölçüm) + gerçek backtest geçmiş istatistiği |
| `cryptobot/notify/samples.py` | `notify preview` için gerçekçi örnek olaylar |
| `cryptobot/notify/providers.py` | Sağlayıcılar (ntfy/telegram/webhook/console/file) |
| `cryptobot/notify/dispatcher.py` | Filtreler, sınırlı yeniden deneme, hata izolasyonu |
| `cryptobot/notify/guard.py` | Harness guard + replay/offline no-push + tek-çıkış kuralı (§16) |
| `cryptobot/tests/test_notify_scope.py` | Kapsam + "otomasyon gerçek ağa çıkamaz" kanıt testleri |
| `cryptobot/notify/` | Denetim deposu (`store.py`), redaksiyon (`redact.py`), HTTP (`http.py`) |
| `cryptobot/tests/test_notify_*.py` | Çevrimdışı testler (yerel HTTP sink dahil) |
| `cryptobot/scripts/notify_check.py` | Tek komutla çevrimdışı doğrulama |
| `.github/workflows/paper-trade.yml` | Bilgisayar kapalıyken ücretsiz çalıştırma (GitHub Actions) |
| `logs/notifications.jsonl` | Gönderim denemesi kayıtları (denetim) |

---

## 15. Neden yüzlerce bildirim geldi ve artık gelmeyecek?

### 15.1 Ne oldu (kök neden)

1. Kullanıcı `CRYPTOBOT_NTFY_TOPIC` değişkenini **kalıcı (User seviyesi)** olarak tanımladı:

   ```powershell
   [Environment]::GetEnvironmentVariable("CRYPTOBOT_NTFY_TOPIC","User")
   # -> cryptobot-paper-6c6de613c4-db65cc46fe
   ```

2. Kalıcı bir değişken **makinedeki her Python sürecine** miras kalır. Bu yüzden unittest
   paketi, `verify_all.py`, `acceptance_check.py` ve elle çalıştırılan her komut konu adını
   görüyordu.
3. Test paketi o sırada bu değişkene karşı yalıtılmış (hermetic) değildi; ntfy sağlayıcısı
   "aktif" görünüyordu. 400+ testin tekrar tekrar çalıştırılması, **telefona yüzlerce gerçek
   push** gönderdi. Denetim dosyası `logs/notifications.jsonl` toplam **2318 satır**; bunların
   yalnızca **15'i** hedefi gerçek bir ağ host'u olan (ntfy.sh / ntfy.envs.net) satırdı —
   kalan gerçek gönderimler, kendi geçici (temp) denetim dosyalarına yazan **test koşularından**
   geliyordu. Yani satır sayısı, telefona giden bildirim sayısının yalnızca görünen kısmıydı.

### 15.2 Artık neden gelmeyecek (kalıcı çözüm)

1. **Test paketi hermetic:** `cryptobot/tests/__init__.py` ortamdan tüm `CRYPTOBOT_*`
   değişkenlerini temizler.
2. **Harness guard (yapısal):** test paketi, `verify_all.py`, `acceptance_check.py`,
   `notify_check.py`, `capture_evidence.py`, `runbook_rollback.py` bir **işaretçi** koyar
   (`CRYPTOBOT_NOTIFY_HARNESS=1`). Bu işaretçi varken dispatcher, hedefi **loopback olmayan**
   bir ağ sağlayıcısına isteği **hiç yapmaz** → `suppressed / harness_guard`. Yerel
   `127.0.0.1` sink (çevrimdışı öz-kontrol) çalışmaya devam eder.
3. **Replay/offline varsayılan olarak göndermez:** `run --replay`/`--offline` ağa göndermek
   için açık `--notify-send` ister (§16.2).
4. **Kapsam daraltıldı:** varsayılan `notify_on` yalnızca `position_opened` + `position_closed`.

### 15.3 Kalıcı değişkeni kaldırma (önerilir)

```powershell
# Kalıcı (kullanıcı seviyesi) değişkeni silin -- başlatıcı artık gerek duymuyor:
[Environment]::SetEnvironmentVariable("CRYPTOBOT_NTFY_TOPIC", $null, "User")
# (Yeni terminaller gerekir.) İsterseniz aynısını CRYPTOBOT_NTFY_HOST için yapın.
```

> Kalıcı değişkeni **bırakmak isteseniz bile** artık güvendesiniz: harness guard ve
> replay kuralı, otomasyonun gerçek ağa çıkmasını yapısal olarak engeller. Yine de en
> temizi değişkeni **başlatıcıya** (veya `start-bot.cmd` içindeki tek satıra) taşımaktır.

### 15.4 İşlem dışı olayları geri açma

`config.yaml` → `notifications.notify_on` listesine olay adını ekleyin (veya ortam değişkeni):

```yaml
notify_on:
  - position_opened
  - position_closed
  - risk_halted      # örnek: kritik risk durdurması da gelsin
  - daily_summary    # örnek: günlük özet
```

```powershell
# Tek seferlik, oturumluk alternatif:
$env:CRYPTOBOT_NOTIFY_ON = "position_opened,position_closed,risk_halted,daily_summary"
```

Tüm olaylar uygulanmış kalır ve `notify preview --all` ile gönderilmeden görülebilir.

---

## 16. Otomasyon güvenliği: harness guard ve replay kuralı

### 16.1 Harness guard (`cryptobot/notify/guard.py`)

* **İşaretçi:** `CRYPTOBOT_NOTIFY_HARNESS=1`.
* **Kural:** işaretçi varken `requires_network=True` olan bir sağlayıcının hedef host'u
  **loopback değilse**, dispatcher `provider.send()`'i **çağırmaz** ve
  `status=suppressed, reason=harness_guard` satırı yazar (denetimde görünür, sessiz değil).
* **Kapsam:** `cryptobot/tests/__init__.py`, `scripts/verify_all.py`,
  `scripts/acceptance_check.py` (alt süreçlerine de aktarır), `scripts/notify_check.py`,
  `scripts/capture_evidence.py`, `scripts/runbook_rollback.py`.
* **Neden loopback'e izin var?** Çevrimdışı öz-kontroller (`notify_check.py`, unittest sink'i)
  gerçek HTTP istemcisini, retry'i ve yükü `127.0.0.1`'e karşı çalıştırır; buradan dışarıya
  çıkılamaz.
* **Kanıt testi:** `cryptobot/tests/test_notify_scope.py::TestHarnessGuard` — ortamda gerçek
  görünen bir `CRYPTOBOT_NTFY_TOPIC` varken gönderim transport'u **hiç çağrılmaz**.

### 16.2 Replay/offline no-push

* `run --replay` veya `run --offline`: ağ sağlayıcıları varsayılan olarak **kapalıdır**.
  Denemeler `suppressed / replay_no_push` (offline'da `offline_no_push`) olarak kaydedilir.
* Gerçekten göndermek istiyorsanız açık bayrak: `run --replay --notify-send`.
* **Gerçek paper modu** (`run --mode paper`, ne replay ne offline) normal şekilde gönderir.
* Neden? Replay yüzlerce barı yürür; her biri gerçek bir push olsaydı, geçmişin tamamı
  telefona dökülürdü.
* Koruma, sağlayıcıya **hiç dokunulmadan** uygulanır: replay/offline bir koşuda
  `--notify-dry-run` de kullansanız, hedefi uzak olan bir ağ sağlayıcısı yine
  `replay_no_push` olarak kaydedilir (gönderim yok). Mesajın tam metnini görmek için
  `notify preview` komutunu kullanın; o komut dispatcher'a uğramaz ve hiçbir şey göndermez.

### 16.3 Başlangıçta ve `notify status`'ta görünürlük

Bot başlarken tek satır günlük kaydı düşer (`notify.send_capability`): gerçek gönderimin
**mümkün olup olmadığı** ve nedeni. `notify status` da aynı bilgiyi basar:

```
GERCEK GONDERIM : MUMKUN DEGIL (harness_guard)
GERCEK GONDERIM : MUMKUN        -> ntfy hedef=AG: ntfy.envs.net
```

---
