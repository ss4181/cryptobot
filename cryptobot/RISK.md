# RISK.md — Sınırlamalar, Uyarılar ve Gerçekçi Beklentiler

> **Bu bir simülasyondur. Yatırım tavsiyesi değildir.**
> Gerçek parayla kullanım, ayrı ve açık bir onay gerektirir; bu kod tabanında gerçek emir gönderme
> yeteneği **yoktur** ve eklenmemiştir.

---

## 1. Kâr garantisi yoktur

Bu bot bir **araştırma ve yürütme altyapısıdır**, kazanç makinesi değildir. "İşlem başına net %2"
ifadesi bir **hedef seviyedir** (take-profit'in nereye konacağı), **beklenti veya garanti değildir**.
Take-profit'e ulaşmak zorunlu değildir; fiyat oraya hiç gitmeyebilir, stop-loss ile çıkılabilir veya
strateji orta banda dönüşte daha küçük bir kârla çıkabilir.

## 2. Geçmiş performans gelecek performansı göstermez

Backtest, indirilen 180 günlük veri üzerinde yapılan bir **tarihsel tekrardır**. Piyasa rejimi
değiştiğinde (trend, volatilite, likidite) aynı parametreler aynı davranışı göstermez. Ayrıca her
parametre seti bu geçmiş veriye göre "iyi" görünecek şekilde seçilirse (overfitting / curve fitting),
sonuçlar gerçek dünyada büyük olasılıkla tekrarlanmaz.

## 3. Ölçülen gerçek sonuçlar (küçük bir dürüstlük kanıtı)

Bu depodaki gerçek koşu (BTC/USDT + ETH/USDT, 180 gün, komisyon %0.1/bacak, slippage %0.05/bacak,
başlangıç 50 USDT) **net negatiftir**:

| Timeframe | İşlem | Kazanma | Net PnL | Net % | Maks drawdown | Profit factor |
| --- | --- | --- | --- | --- | --- | --- |
| 1h (BTC+ETH) | 18 | %50.0 | −2.57 USDT | −5.13% | %6.66 | 0.61 |
| 15m (BTC+ETH) | 66 | %42.4 | −6.24 USDT | −12.48% | %13.29 | 0.43 |
| 1h yalnız BTC | 15 | %53.3 | −0.67 USDT | −1.33% | %4.30 | 0.84 |
| 1h yalnız ETH | 9 | %55.6 | −0.21 USDT | −0.42% | %4.88 | 0.94 |
| 15m yalnız BTC | 40 | %40.0 | −1.16 USDT | −2.32% | %3.30 | 0.71 |
| 15m yalnız ETH | 50 | %50.0 | −4.60 USDT | −9.20% | %10.16 | 0.55 |

Bu sonuç **kasıtlı olarak değiştirilmemiştir**: parametreleri bu veriye göre "güzelleştirmek"
curve-fitting olurdu. En önemli çıkarım şudur: **işlem maliyetleri küçük hesaplarda belirleyicidir.**

* 1h koşusunda ödenen komisyon 1.58 USDT + slippage 0.79 USDT = **2.37 USDT**, yani 50 USDT'nin
  **%4.7**'si, sadece 18 işlemde.
* 15m koşusunda 5.47 + 2.73 = **8.20 USDT** (%16.4), 66 işlemde.
* İşlem başına maliyet ≈ 0.13–0.12 USDT; ölçülen ortalama net sonuç −0.14 / −0.09 USDT. Yani
  **brüt sonuç kabaca başabaş; maliyetler onu negatife çeviriyor.**

## 4. Backtest ile canlı (hatta paper ile gerçek) arasındaki sapmalar

Backtest modeli bilinçli olarak **iyimser olmayan** seçimler yapar, ancak yine de gerçek dünyadan
farklıdır:

| Konu | Bu projede model | Gerçek dünyada olabilecek |
| --- | --- | --- |
| Dolum fiyatı | Sinyal barının **kapanışı** + slippage | Emir gecikmesi, kuyruk, daha kötü fiyat |
| Slippage | Sabit `slippage_pct` (varsayılan %0.05) | Volatil anda %0.3–1+, özellikle ince emir kitabında |
| Komisyon | Sabit `fee_pct` (%0.1) | Kademe/BNB indirimi ile değişir; çekim ücreti ayrı |
| Bar içi stop/TP | Aynı bar hem stop hem TP'ye değerse **stop** işlenir (kötümser) | Gerçekte sıra şansa bağlı; bu seçim avantaj değil, dezavantaj yaratır |
| Gerçekleşmeyen bar verisi | Yalnızca **kapanmış** barlar kullanılır | — (bu doğru davranış) |
| Likidite | Sonsuz varsayılır (miktar büyüklüğü fiyatı etkilemez) | 50 USDT için gerçekçi; büyük bakiyede değil |
| Kısmi dolum | Yalnızca test/enjeksiyon ile simüle edilir | Gerçekte yaygın |
| Borsa kesintisi | Veri hatası → yeni giriş durur (fail-safe) | Emir reddi, bakım, sembol askıya alınması |
| Bayat cache | Gerçek zamanlı paper modda cache, en yeni bar `data.max_cache_age_bars` bar'dan daha eskise "bayat" sayılır (`cache-stale`, `complete=False`, `pause_new_entries`) → yeni giriş durur | Kesinti + eski cache birlikte sessizce işlem açma riskini ortadan kaldırır |
| Ağ/kayma | Cache ile tekrar üretilebilir | Canlıda internet kesintisi = kaçırılan fırsat |
| Vergi | Modellenmez | Bulunduğunuz ülkeye göre yükümlülük doğar |

Ayrıca **paper mod gerçek dolum değildir**: emirler gerçek emir kitabına hiç gitmez, dolayısıyla
gerçek borsada oluşabilecek red/gecikme/kısmi dolum davranışı ancak sonradan (gerçek para ile)
görülebilir.

## 5. Limitlerin gerçek davranışı (rakamlarla)

* **Stop-loss net değildir.** `stop_loss_pct = 2.5` referans fiyata göredir; çıkışta da komisyon ve
  slippage ödendiği için gerçekleşen net kayıp ≈ **%2.7**. Örnek: giriş fill 100.00 → stop 97.50 →
  net çıkış ≈ 97.50 × (1 − 0.0005) × (1 − 0.001) = 97.354 → net −%2.65.
* **Take-profit brüt %2.2553** olmalıdır ki net %2 kalsın (komisyon %0.1, slippage %0.05). Bu
  değer `net_profit_target_pct` değişince otomatik değişir.
* **Maliyet yükü 0.2553 puan** (2.2553 − 2.0). Her işlem başında piyasaya bu kadar borçlusunuz.
* **Borsa minimum notional 5 USDT** (`DEFAULT_MIN_NOTIONAL_USDT`): equity küçülürse (örn. 5 USDT
  altı) yeni pozisyon açılamaz ve `below_min_notional` olarak loglanır. 50 USDT ile
  `max_position_pct=90` → ~45 USDT, güvenli aralıkta.
* **Günlük zarar limiti** `daily_loss_limit_pct=5` → 50 USDT'de 2.5 USDT zarar o gün tüm yeni
  girişleri durdurur (`daily_loss_limit_reached`, WARNING/CRITICAL log). Limit, UTC günü değişince
  temizlenir.
* **Cooldown** `60 dk`: zararlı işlemden sonra bir saat giriş yok — düşen piyasada üst üste alımı
  engeller ama kaçırılan fırsat maliyeti de yaratır.
* **Maks 1 açık pozisyon** (varsayılan): iki parite aynı sinyali verse bile biri seçilir; bu yüzden
  "toplam" koşu, parite bazlı koşuların toplamından farklı işlem sayısı üretebilir.
* **Günlük işlem tavanı yalnızca yeni girişleri sayar.** `max_trades_per_day = 8`, UTC günü başına en
  fazla **8 yeni giriş** demektir; kapanışlar sayaçı artırmaz (tek tur = 1 artış). Sayaç `roll_day`
  ile UTC günü değişince sıfırlanır. Önceden kapanış da sayıldığı için tavan fiilen ~4 tura
  düşüyordu; bu düzeltilmiştir.
* **Hangi limit neyi yapar:** *Yeni girişi bloklayan* limitler — `daily_loss_limit_reached`,
  `cooldown_active`, `max_open_positions_reached`, `max_trades_per_day_reached`,
  `equity_below_minimum`, `no_cash`, `below_min_notional` (boyutlandırma sonucu), ve veri
  fail-safe'i `pause_new_entries` / `cache-stale`. *Yalnızca boyutlandıran* limit —
  `max_position_pct` (girişi engellemez, miktarı küçültür). *Yalnızca kapanışı yöneten* kurallar —
  `stop_loss` ve `take_profit` (mevcut pozisyonu kapatır, yeni girişi engellemez). Veri kesintisinde
  fail-safe yalnızca **yeni girişi** durdurur; açık pozisyonun koruyucu stop/TP çıkışı çalışmaya
  devam eder.

## 6. Küçük hesap matematiği

50 USDT gibi küçük bir bakiyede:

* Pozisyon ~45 USDT, brüt %2.2553 hedef → işlem başına brüt ~1.0 USDT, net ~0.9 USDT.
* Tek bir stop-loss (~%2.7 net) ≈ 1.2 USDT kaybeder; yani **kazandığınızdan daha hızlı kaybedersiniz**,
  kazanma oranı %50 civarındayken bile maliyetler yüzünden beklenti negatif olabilir.
* Bu nedenle bu ayarlar "para kazanma stratejisi" değil, **altyapı doğrulama** amaçlıdır.

## 7. Modelin bilinen zayıflıkları (şeffaflık listesi)

1. Göstergeler ve sinyaller yalnızca kapanmış barlar üzerinde hesaplanır; ancak **giriş aynı barın
   kapanış fiyatından** doldurulur. Gerçekte emir bir sonraki barın açılışında gerçekleşir; bu,
   backtest'i küçük ölçüde iyimser yapar.
2. Slippage sabit ve simetriktir (alışta yukarı, satışta aşağı); gerçek slippage volatiliteye bağlıdır.
3. Bar içi yol (path) bilinmez; stop/TP önceliği kötümser varsayılmıştır ama tek bir bar içindeki
   gerçek sıra modellenmez.
4. Emir reddi, kısmi dolum, gecikme yalnızca **enjekte edilerek** simüle edilir (test amaçlı);
   rastgele değildir (determinizm için bilinçli tercih).
5. Tek borsa (Binance) ve tek veri kaynağı varsayılır; borsa bazlı fiyat farkı (basis) yoktur.
6. Sharpe, 8 760 bar/yıl (1h) varsayımıyla yıllıklandırılır; 180 günlük veride bu istatistik
   gürültülüdür ve **tek başına karar dayanağı olmamalıdır**.
7. Açık kalan pozisyon kapanışta zorla kapatılmaz; mark-to-market edilir. Bu yüzden son equity,
   gerçekleşmemiş bir kâr/zarar içerebilir.

## 8. Gerçek parayla kullanım koşulu

* Bu depo **canlı emir göndermez** ve göndermek için gereken hiçbir kod yolu (imzalı istek, API
  anahtarı, `create_order`) içermez. `scripts/verify_all.py` bunu her koşuda statik olarak kanıtlar.
* Gerçek parayla işlem yapmak **ayrı bir mühendislik projesidir** ve en az: ayrı açık onay, borsa API
  anahtarı yönetimi ve saklama, emir hata yönetimi, pozisyon/risk sınırlarının sunucu tarafında
  zorlanması, izleme/alarm, denetim kaydı ve hukuki/vergi danışmanlığı gerektirir.
* Bu bot **bir yatırım danışmanı değildir**; hiçbir çıktısı yatırım tavsiyesi olarak yorumlanamaz.

## 9. Kabul edilebilir kullanım

Eğitim, araştırma, strateji altyapısı geliştirme, yürütme/muhasebe doğrulaması ve paper trading.
Kabul edilemez: gerçek emir göndermek için bu kodu "canlıya çevirmek" (kod buna izin vermez), başka
kişilerin fonlarını yönetmek, tavsiye satmak.
