# DEPLOY — cryptobot'u ücretsiz GitHub Actions üzerinde çalıştırma

Bu belge, botu **bilgisayarınız kapalıyken** bulutta çalıştırmak için gereken her şeyi
anlatır: koşu davranışı, bildirim ayarları, elle tetikleme, cache ile durum kalıcılığı,
dürüst uyarılar ve public/private repo arasında geçiş.

Depo adresi: `<repo-url>`

---

## 1. Kurulum (tek seferlik)

1. Bu klasörü bir GitHub deposuna gönderin (kök klasör `cryptobot/` paketinin bir
   üst dizinidir; iş akışı kökteki `.github/workflows/paper-trade.yml` dosyasıdır).
2. Depo **Actions** sekmesini bir kez açıp iş akışlarının etkinleşmesini onaylayın.
3. (İsteğe bağlı) Bildirim ayarını yapın — bkz. bölüm 3.

Python kurulumu, bağımlılıklar ve testler iş akışının kendisi tarafından yapılır;
sunucu, kredi kartı veya yerel kurulum gerekmez.

---

## 2. Bir koşuda ne olur (per-run davranış)

İş akışı her tetiklendiğinde, taze (cold) bir Ubuntu runner üzerinde sırayla:

1. **Checkout** + Python 3.13 kurulumu + `cryptobot/requirements.txt` bağımlılıkları.
2. **Güvenlik duruşu:** `python -m cryptobot safety` ve
   `python cryptobot/tests/no_live_order_scan.py` — gerçek emir gönderen kod yolu
   olmadığı kanıtlanır.
3. **Çevrimdışı birim testleri** (ağ yok, bildirim env'i yok).
4. **Veri indirme:** Binance'in imzasız/herkese açık kline uç noktasından mumlar
   (`download --timeframe --days`). Taze runner'da cache boş olduğu için bu adım
   zorunludur.
5. **Durum geri yükleme (cache):** bir önceki koşudan kalan ledger ve bildirim
   günlüğü Actions cache'inden geri alınır (en iyi çaba; ilk koşuda boş).
6. **Paper koşusu (sınırlı pencere):** gerçek zamanlı modda
   `--duration-seconds` boyunca döner, sonra kendi kendine durur
   (`--offline` / `--replay` yok).
7. **Uzlaştırma ve çıktılar:** `verify`, `export` (CSV/JSON), günlük `report`.
8. **Bildirimler:** ntfy (ve tanımlıysa Telegram/webhook) üzerinden telefon bildirimi;
   ayrıca `cryptobot/logs/notifications.jsonl` denetim kaydı.
9. **Durum kaydetme (cache):** ledger + bildirim günlüğü cache'e yazılır.
10. **Artifact'ler:** `cryptobot/reports/` + `cryptobot/logs/` ve `ledger.sqlite`
    14 gün boyunca indirilebilir.
11. **KEEPALIVE:** `deploy/heartbeat.txt` bayat ise (içindeki tarih 20 günden eski)
    bugünün UTC tarihiyle güncellenip commit'lenip push'lanır (bkz. bölüm 6).

Her koşu **sınırlı bir anlık görüntüdür** — 7/24 kesintisiz bir süreç değildir.

---

## 3. Bildirim ayarları

GitHub'da: **Settings → Secrets and variables → Actions**

| Ad | Tür | Zorunlu mu | Açıklama |
|---|---|---|---|
| `CRYPTOBOT_NTFY_TOPIC` | **Variable** (Secret da olur) | Bildirim için evet | Uzun, rastgele ntfy konu adı; telefonunuzdaki ntfy uygulamasına aynı adı girin. |
| `CRYPTOBOT_NTFY_HOST` | **Variable** | Hayır | Varsayılan `ntfy.sh`. Kendi sunucunuz veya bir ayna (mirror) kullanmak isterseniz buraya yazın. |

- **Variable** önerilir: ntfy konu adı public `ntfy.sh` üzerinde bir kimlik bilgisi
  değil, bir kanal adıdır; Variable olarak döndürmek (rotate) daha kolaydır.
- Secret olarak da tanımlayabilirsiniz; bu durumda loglarda maskelenir.
- İş akışı `vars.X || secrets.X` sırasını dener, yani ikisinden biri yeterlidir.
- Hiçbiri tanımlı değilse koşu **yine başarılı olur**: bildirimler yalnızca konsola ve
  `cryptobot/logs/notifications.jsonl` dosyasına yazılır.

İsteğe bağlı diğer Secret'lar (tanımlıysa kullanılır): `CRYPTOBOT_NTFY_TOKEN`,
`CRYPTOBOT_TELEGRAM_BOT_TOKEN`, `CRYPTOBOT_TELEGRAM_CHAT_ID`, `CRYPTOBOT_WEBHOOK_URL`.

---

## 4. Elle (manuel) koşu

1. `<repo-url>` → **Actions** sekmesi.
2. Sol listede **paper-trade** iş akışını seçin.
3. Sağda **Run workflow** → dal (branch) seçin → **Run workflow**.
4. İsteğe bağlı girdiler: `timeframe` (1m..1d), `duration_seconds` (sınırlı pencere
   saniyesi), `interval_seconds` (döngüler arası saniye), `days` (indirilecek geçmiş
   gün sayısı). Boş bırakılırsa 1h / 300 / 60 / 30 kullanılır.
5. Koşu bitince özet ekranından artifact'leri indirebilirsiniz.

---

## 5. Cache ile durum kalıcılığı

Taze bir runner'da disk her seferinde boştur; bu yüzden ledger `actions/cache` ile
koşular arasında taşınır:

- **Yol (path):** `cryptobot/data/ledger.sqlite` ve
  `cryptobot/logs/notifications.jsonl`
- **Anahtar (key):** `cryptobot-state-${{ github.run_number }}` (her koşuda yeni)
- **Geri yükleme anahtarı (restore-keys):** `cryptobot-state-` → yani mevcut koşuya
  ait anahtar yoksa **en yeni** `cryptobot-state-*` girdisi kullanılır.
- **Sıra:** geri yükleme, paper koşusundan **önce**; kaydetme, koşu/export'lardan
  **sonra** ve artifact yüklemesinden **önce**.
- Her iki adım da **en iyi çaba**dır (`continue-on-error`): ilk koşuda dosya yoktur,
  cache boşalabilir (7 gün erişilmezse silinir veya repo cache kotası aşılırsa tahliye
  edilir). Bu durumda yalnızca **geçmiş** kaybedilir; koşu asla başarısız olmaz.
- Artifact'ler cache'ten bağımsız, elle inceleyebileceğiniz kopyadır.

**Durumu sıfırlamak** (örneğin bozuk bir ledger'dan sonra temiz başlangıç):

1. **Actions → Caches** (veya Settings → Actions → Caches) bölümünden `cryptobot-state-*`
   girdilerini silin. Silinen cache geri gelmez; bir sonraki koşu sıfırdan başlar.
2. İsterseniz depodaki `cryptobot/data/ledger.sqlite` dosyasını da silip commit'leyin.
   (Bu dosya `cryptobot/.gitignore` ile yok sayılır; bilinçli olarak takip etmek
   istiyorsanız `git add -f` gerekir.)
3. Cache girdileri 7 gün boyunca hiç erişilmezse GitHub tarafından kendiliğinden
   silinir; bu yüzden uzun bir aradan sonra ilk koşu geçmişi boş bulabilir.

---

## 6. Dürüst uyarılar (caveats)

- **Cron gecikmesi:** `schedule` girdisi `*/30 * * * *` olsa da GitHub zamanlanmış
  koşuları geciktirebilir; yüksek yük zamanlarında 30 dakika bir **alt sınır**tır,
  garanti değil. GitHub'ın pratik minimumu ~5 dakikadır.
- **Sınırlı pencere, 7/24 değil:** her koşu `duration_seconds` kadar çalışır ve durur.
  Bu bir **anlık görüntüdür**; bot 24 saat ayakta değildir, dolayısıyla mum kapanışları
  arasında kaçan sinyal olabilir.
- **60 gün hareketsizlik duraklaması:** GitHub, 60 gün boyunca hiç depo aktivitesi
  olmayan repolarda `schedule` tetikleyicisini duraklatır. Bu depo bunu **KEEPALIVE**
  adımıyla hafifletir: içindeki tarih 20 günden eskiyse `deploy/heartbeat.txt` bugünün
  UTC tarihiyle güncellenir ve push'lanır (yalnızca bu dosya commit'lenir).
  Push reddedilirse (fork, branch protection, salt-okunur token) iş **başarısız olmaz**;
  yalnızca duraklatma koruması devre dışı kalır.
- **Cache kalıcı depolama değildir:** bkz. bölüm 5; eviction durumunda geçmiş kaybolur.
- **Artifact süresi 14 gün:** indirmezseniz raporlar ve ledger 14 gün sonra silinir.
- **Ağ erişimi:** veri kaynağı Binance'in herkese açık uç noktasıdır; GitHub runner'ından
  erişilemezse koşu veri adımında başarısız olabilir. `CRYPTOBOT_NTFY_HOST` ile aynı
  mantıkla ağ erişimi engellenen ortamlarda bildirimler için ayna/self-host kullanın.
- **Paper only:** gerçek para/gerçek emir yok. Bu bir simülasyondur; sonuçlar yatırım
  tavsiyesi değildir.

---

## 7. Public ↔ Private geçişi ve ücretsiz dakikalar

| Repo türü | Actions dakikaları | Not |
|---|---|---|
| **Public** | **Sınırsız** (standart GitHub-hosted runner'larda) | Bu iş akışı için önerilen mod. |
| **Private** | Aylık **2.000** ücretsiz dakika (Free plan) | 30 dakikada bir, koşu başına ~5-10 dk: aylık ~1.440 koşu ile kota **kolayca aşılır**. |

Geçiş yapmak için:

1. `<repo-url>` → **Settings** → sayfanın en altı → **Danger Zone**.
2. **Change repository visibility** → **Make public** (veya **Make private**) → onay
   için depo adını yazıp onaylayın.

Notlar:

- Public'e almak ücretsiz dakika sınırını kaldırır; kod ve commit geçmişi herkese açık
  olur. **Bu depoda secret tutulmaz** (bildirim konu adı bir Variable'dır), ama yine de
  gizlilik beklentinizi gözden geçirin.
- Private'a alırsanız kotayı aşmamak için `schedule` sıklığını düşürün (örn.
  `0 * * * *` = saatte bir) veya `duration_seconds` değerini küçültün (`workflow_dispatch`
  varsayılanı 300 saniyedir; schedule koşuları da aynı girdileri kullanır).
- Kota aşımı durumunda GitHub, kotayı harcayan iş akışlarını durdurur; koşu **başarısız
  olarak değil**, çalıştırılmadan engellenmiş olarak görünür.
- Free plan private repo kotası 2.000 dakika/ay'dır; kurumsal/ücretli planlarda bu sayı
  değişir. Güncel değerleri GitHub'ın "Billing" sayfasından doğrulayın.
