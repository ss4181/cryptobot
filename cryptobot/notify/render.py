"""Sectioned, emoji-led, mobile-first rendering of notification events.

One shared content model, one rendering function per carrier
===========================================================
Every event is turned into a :class:`MessageContent` -- a headline plus a small
list of titled :class:`Section` blocks.  Each carrier (ntfy markdown, Telegram
HTML, webhook/console/file plain text) renders that same model with its own
function, so the wording and the numbers can never drift apart between channels.

Honesty rules baked into the templates
--------------------------------------
* A measurement block is titled ``ölçüm`` (measurement) and never ``olasılık``
  (probability): :mod:`cryptobot.notify.context` computes how far the signal
  cleared each filter; nothing here invents a confidence score.
* The historical block is only emitted when a real offline backtest over the
  cached candles produced it, and it always states the period, timeframe, pairs
  and the config hash plus a one-line "historical, not a promise" disclaimer.
* A value that could not be measured is omitted -- never replaced by a
  placeholder that would look like a measurement.
* Messages are capped at :data:`MAX_LINES` short lines so they stay readable on
  a phone lock screen.
"""

from __future__ import annotations

import html as _html
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import context as ctx
from . import format as fmt

#: Leading colour square per severity (info / warning / critical).
SEVERITY_SQUARE = {"info": "🟦", "warning": "🟨", "critical": "🟥"}

#: A message whose *content* lines (blank separators excluded) exceed this is
#: truncated with a marker.  Ordinary events are 3-8 lines; the richest layout
#: (a full entry block) is ~22, which keeps every message phone-readable.
MAX_LINES = 24

#: Carriers understood by :func:`render_body`.
CARRIERS = ("plain", "markdown", "html", "webhook", "console", "file")

#: Upward / downward scenario ladder for the "levels from entry" block, in percent.
UP_LADDER: Tuple[float, ...] = (1.0, 2.0, 3.0)
DOWN_LADDER: Tuple[float, ...] = (2.0, 5.0)

_MODE_LABEL = {"paper": "PAPER", "backtest": "BACKTEST"}

_TRIGGER_TR = {
    "take_profit": "hedef (take-profit) seviyesine ulaşıldı",
    "stop_loss": "stop-loss seviyesi tetiklendi",
    "strategy": "strateji çıkışı (fiyat orta banda döndü)",
}


# --------------------------------------------------------------------------- #
# content model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Section:
    """One titled block.  An empty ``title`` renders as a plain paragraph."""

    title: str = ""
    lines: Tuple[str, ...] = ()

    def as_lines(self) -> List[str]:
        out: List[str] = []
        if self.title:
            out.append(self.title)
        out.extend(line for line in self.lines if str(line).strip())
        return out


@dataclass(frozen=True)
class MessageContent:
    """Carrier-independent message: one headline + ordered sections."""

    severity: str
    headline: str
    sections: Tuple[Section, ...] = ()

    def as_lines(self, *, limit: int = MAX_LINES) -> List[str]:
        lines: List[str] = [self.headline]
        for section in self.sections:
            body = section.as_lines()
            if not body:
                continue
            lines.append("")
            lines.extend(body)
        return _cap(lines, limit)


def _cap(lines: List[str], limit: int) -> List[str]:
    """Truncate on **content** lines; blank separators do not consume the budget."""
    if not limit or limit <= 0:
        return lines
    filled = [index for index, line in enumerate(lines) if str(line).strip()]
    if len(filled) <= limit:
        return lines
    cut = filled[limit - 1]
    return lines[:cut] + ["… (mesaj kısaltıldı)"]


def _square(severity: str) -> str:
    return SEVERITY_SQUARE.get(str(severity or "info").lower(), SEVERITY_SQUARE["info"])


def _data(event: Any) -> Mapping[str, Any]:
    raw = getattr(event, "data", None)
    return raw if isinstance(raw, Mapping) else {}


def _text(data: Mapping[str, Any], key: str, default: str = "") -> str:
    value = data.get(key)
    if value is None:
        return default
    return str(value)


def _mode(data: Mapping[str, Any]) -> str:
    raw = str(data.get("mode") or "").strip().lower()
    return _MODE_LABEL.get(raw, raw.upper() if raw else "PAPER")


def _pair(event: Any, data: Mapping[str, Any]) -> str:
    return fmt.pair_compact(data.get("pair") or getattr(event, "pair", "") or "")


def _headline(event: Any, subject: str) -> str:
    return "{} {}".format(_square(getattr(event, "resolved_severity", lambda: "info")()), subject)


# --------------------------------------------------------------------------- #
# shared sub-blocks
# --------------------------------------------------------------------------- #
def _levels_block(entry_price: Any, net_target_pct: Any) -> Optional[Section]:
    entry = fmt.to_float(entry_price)
    if entry is None or entry <= 0:
        return None
    ups = sorted(set(UP_LADDER))
    up_line = " · ".join(
        "+%{}: {}".format(fmt.pct_label(step), fmt.price(entry * (1 + step / 100.0))) for step in ups)
    down_line = " · ".join(
        "-%{}: {}".format(fmt.pct_label(step), fmt.price(entry * (1 - step / 100.0))) for step in DOWN_LADDER)
    return Section("📌 Senaryo seviyeleri (girişe göre)", ("▲ " + up_line, "▼ " + down_line))


def _context_block(data: Mapping[str, Any]) -> Optional[Section]:
    """Measured signal context.  Omitted entirely when nothing could be measured."""
    indicators = data.get("indicators")
    params = data.get("strategy_params")
    margins = ctx.filter_margins(indicators, params)
    lines: List[str] = []
    if margins is not None:
        regime = ctx.regime_line(margins)
        if regime:
            lines.append("🧭 Rejim: {}".format(regime))
        volume_z = fmt.to_float(data.get("volume_log_z"))
        if volume_z is not None:
            lines.append("🔊 Hacim log-Z: {}".format(fmt.number(volume_z, 2, signed=True)))
        summary = ctx.margin_summary(margins)
        if summary:
            lines.append("📐 Filtre marjları: {}".format(summary))
    if not lines:
        return None
    return Section("📊 Sinyal bağlamı (ölçüm, olasılık değil)", tuple(lines))


def _history_block(data: Mapping[str, Any]) -> Optional[Section]:
    """Historical block from a real backtest; omitted when no stats are available."""
    history = data.get("history")
    if not isinstance(history, Mapping):
        return None
    trades = fmt.to_float(history.get("trades"))
    if trades is None:
        return None
    pairs = fmt.pairs_compact(history.get("pairs"))
    timeframe = _text(history, "timeframe", "n/a")
    days = fmt.to_float(history.get("days"))
    period = "{} gün".format(fmt.number(days, 0)) if days is not None else "dönem: {}".format(
        fmt.utc_date(history.get("start_ts")))
    scope = "{} · {} · {}".format(period, timeframe, pairs)
    header = "📚 Geçmiş ölçüm ({})".format(scope)

    lines: List[str] = []
    isabet = fmt.to_float(history.get("win_rate_pct"))
    median = fmt.to_float(history.get("median_net_pnl_pct"))
    trade_line = "🧪 İşlem: {}".format(fmt.integer(trades))
    if isabet is not None:
        trade_line += " · isabet %{}".format(fmt.number(isabet, 1))
    if median is not None:
        trade_line += " · medyan {}%".format(fmt.pct(median))
    lines.append(trade_line)

    tp = fmt.to_float(history.get("tp_exit_pct"))
    sl = fmt.to_float(history.get("sl_exit_pct"))
    touch_bits = []
    if tp is not None:
        target_label = fmt.pct_label(history.get("net_target_pct"))
        touch_bits.append("🎯 +%{} hedefe dokunma: %{}".format(target_label, fmt.number(tp, 0)))
    if sl is not None:
        stop_label = fmt.pct_label(history.get("stop_loss_pct"))
        touch_bits.append("🛑 -%{} stop: %{}".format(stop_label, fmt.number(sl, 0)))
    if touch_bits:
        lines.append("  |  ".join(touch_bits))

    config_hash = _text(history, "config_hash")
    if config_hash:
        lines.append("🗂️ Config referansı: {}".format(config_hash))
    lines.append("⚠️ Tarihsel ölçüm; kâr garantisi değil, tavsiye değildir.")
    return Section(header, tuple(lines))


def _reason_tr(data: Mapping[str, Any]) -> str:
    """Human Turkish explanation of why an entry fired (never invents a quantity)."""
    reason = _text(data, "reason")
    params = data.get("strategy_params") if isinstance(data.get("strategy_params"), Mapping) else {}
    if not reason:
        return ""
    if reason.startswith("entry:bollinger_dip") or "bollinger_dip" in reason:
        rsi = fmt.pct_label(params.get("rsi_oversold"))
        sma = params.get("trend_sma_period")
        rsi_text = "RSI < {}".format(rsi) if rsi != fmt.NA else "RSI eşiğin altında"
        sma_text = "fiyat SMA{} üstünde".format(sma) if sma else "fiyat trend SMA üstünde"
        return "Bollinger alt bandının altında kapanış + {} + {}.".format(rsi_text, sma_text)
    if reason.startswith("entry:"):
        return "Strateji giriş koşulu: {}.".format(reason.split("entry:", 1)[1])
    return reason


def _exit_reason_tr(trigger: str, exit_reason: str) -> str:
    known = _TRIGGER_TR.get(trigger)
    if known:
        detail = ""
        if exit_reason and exit_reason != trigger and not exit_reason.startswith(trigger + ":"):
            detail = " ({})".format(exit_reason)
        return known + detail
    return exit_reason or trigger or "n/a"


# --------------------------------------------------------------------------- #
# per-event templates
# --------------------------------------------------------------------------- #
def _build_bot_started(event: Any, data: Mapping[str, Any]) -> MessageContent:
    mode = _mode(data)
    pairs = fmt.pairs_compact(data.get("pairs"))
    timeframe = _text(data, "timeframe", "n/a")
    lines = [
        "🧪 Mod: {}".format(mode.lower()),
        "🔀 Pariteler: {}".format(pairs or "n/a"),
        "⏱️ Zaman aralığı: {}".format(timeframe),
        "💵 Başlangıç: {} USDT".format(fmt.usdt(data.get("initial_capital"), 2)),
    ]
    if data.get("dry_run"):
        lines.append("🧾 Dry-run: hiçbir şey gönderilmez, yalnızca gösterilir.")
    run_id = _text(data, "run_id") or str(getattr(event, "run_id", "") or "")
    if run_id:
        lines.append("🆔 Run: {}".format(run_id))
    return MessageContent(str(getattr(event, "resolved_severity", lambda: "info")()),
                          _headline(event, "BOT BAŞLADI · {}".format(mode)),
                          (Section("🚀 Başlatma", tuple(lines)),))


def _build_bot_stopped(event: Any, data: Mapping[str, Any]) -> MessageContent:
    net = fmt.to_float(data.get("net_pnl"))
    lines = [
        "💰 Final equity: {} USDT".format(fmt.usdt(data.get("final_equity"), 2)),
        "📉 Net: {} USDT ({}%)".format(fmt.usdt(net, 2, signed=True), fmt.pct(data.get("net_pnl_pct"))),
        "🧮 İşlem: {} · isabet %{}".format(fmt.integer(data.get("trades")), fmt.number(data.get("win_rate_pct"), 1)),
    ]
    run_id = _text(data, "run_id") or str(getattr(event, "run_id", "") or "")
    if run_id:
        lines.append("🆔 Run: {}".format(run_id))
    return MessageContent(str(getattr(event, "resolved_severity", lambda: "info")()),
                          _headline(event, "BOT DURDU · {}".format(_mode(data))),
                          (Section("🏁 Kapanış", tuple(lines)),))


def _build_position_opened(event: Any, data: Mapping[str, Any]) -> MessageContent:
    pair = _pair(event, data) or "?"
    entry = fmt.to_float(data.get("entry_price"))
    base = pair.replace("USDT", "") or "adet"
    sections: List[Section] = []

    # Optional lines are omitted -- never printed as "n/a" -- because a missing
    # field must not look like a measurement.
    order: List[str] = []
    if entry is not None:
        order.append("💰 Giriş: {}".format(fmt.price(entry)))
    if fmt.to_float(data.get("tp_price")) is not None:
        target_label = fmt.pct_label(data.get("net_target_pct"))
        if target_label != fmt.NA:
            order.append("🎯 Hedef (net +%{}): {}".format(target_label, fmt.price(data.get("tp_price"))))
        else:
            order.append("🎯 Hedef: {}".format(fmt.price(data.get("tp_price"))))
    if fmt.to_float(data.get("stop_price")) is not None:
        order.append("🛑 Stop: {}".format(fmt.price(data.get("stop_price"))))
    if fmt.to_float(data.get("qty")) is not None:
        notional = fmt.to_float(data.get("notional"))
        line = "📦 Miktar: {} {}".format(fmt.qty(data.get("qty")), base)
        if notional is not None:
            line += " · {} USDT".format(fmt.usdt(notional, 2))
        order.append(line)
    timeframe = _text(data, "timeframe")
    if timeframe:
        order.append("⏱️ Zaman aralığı: {}".format(timeframe))
    if order:
        sections.append(Section("", tuple(order)))

    reason = _reason_tr(data)
    if reason:
        sections.append(Section("💡 Neden geldi?", (reason,)))

    context_block = _context_block(data)
    if context_block is not None:
        sections.append(context_block)

    history_block = _history_block(data)
    if history_block is not None:
        sections.append(history_block)

    levels = _levels_block(data.get("entry_price"), data.get("net_target_pct"))
    if levels is not None:
        sections.append(levels)

    return MessageContent(str(getattr(event, "resolved_severity", lambda: "info")()),
                          _headline(event, "{} · LONG ({})".format(pair, _mode(data))),
                          tuple(sections))


def _build_position_closed(event: Any, data: Mapping[str, Any]) -> MessageContent:
    pair = _pair(event, data) or "?"
    net = fmt.to_float(data.get("net_pnl"))
    net_pct = fmt.to_float(data.get("net_pnl_pct"))
    lines = [
        "🚪 Giriş → Çıkış: {} → {}".format(fmt.price(data.get("entry_price")), fmt.price(data.get("exit_price"))),
        "📦 Miktar: {} {}".format(fmt.qty(data.get("qty")), pair.replace("USDT", "") or "adet"),
        "💸 Komisyon: {} USDT".format(fmt.usdt(data.get("fees"), 4)),
        "📈 Brüt: {} USDT".format(fmt.usdt(data.get("gross_pnl"), 4, signed=True)),
        "💰 Net: {} USDT ({}%)".format(fmt.usdt(net, 4, signed=True), fmt.pct(net_pct)),
    ]
    reason = _exit_reason_tr(_text(data, "trigger"), _text(data, "exit_reason"))
    if reason and reason != "n/a":
        lines.append("🧭 Neden: {}".format(reason))
    held = fmt.to_float(data.get("held_seconds"))
    bars_held = fmt.to_float(data.get("bars_held"))
    if held is not None:
        duration_text = fmt.duration(held)
        if bars_held is not None:
            duration_text += " ({} bar)".format(fmt.integer(bars_held))
        lines.append("⏱️ Süre: {}".format(duration_text))
    return MessageContent(str(getattr(event, "resolved_severity", lambda: "info")()),
                          _headline(event, "{} · KAPANDI ({})".format(pair, _mode(data))),
                          (Section("", tuple(lines)),))


def _build_protective_exit(event: Any, data: Mapping[str, Any], *, kind: str) -> MessageContent:
    pair = _pair(event, data) or "?"
    net = fmt.to_float(data.get("net_pnl"))
    lines = [
        "🚪 Giriş → Çıkış: {} → {}".format(fmt.price(data.get("entry_price")), fmt.price(data.get("exit_price"))),
        "💰 Net: {} USDT ({}%)".format(fmt.usdt(net, 4, signed=True), fmt.pct(data.get("net_pnl_pct"))),
    ]
    held = fmt.to_float(data.get("held_seconds"))
    if held is not None:
        lines.append("⏱️ Süre: {}".format(fmt.duration(held)))
    if kind == "take_profit":
        subject = "{} · HEDEF ✓ ({})".format(pair, _mode(data))
        title = "🎯 Hedef tuttu"
    else:
        subject = "{} · STOP ✗ ({})".format(pair, _mode(data))
        title = "🛑 Stop tetiklendi"
    return MessageContent(str(getattr(event, "resolved_severity", lambda: "info")()),
                          _headline(event, subject),
                          (Section(title, tuple(lines)),))


def _build_risk_halted(event: Any, data: Mapping[str, Any]) -> MessageContent:
    lines = [
        "🧯 Neden: {}".format(_text(data, "reason", "günlük zarar limiti")),
        "📉 Gün realize net: {} USDT".format(fmt.usdt(data.get("day_realized_net_pnl"), 4, signed=True)),
        "⛔ Yeni giriş yok; UTC günü değişince açılır.",
    ]
    return MessageContent(str(getattr(event, "resolved_severity", lambda: "info")()),
                          _headline(event, "RİSK DURDU · GÜNLÜK LİMİT"),
                          (Section("🧱 Risk kontrolü", tuple(lines)),))


def _build_cooldown_started(event: Any, data: Mapping[str, Any]) -> MessageContent:
    pair = _pair(event, data) or "?"
    lines = [
        "😖 Zararlı kapanış: {} USDT".format(fmt.usdt(data.get("net_pnl"), 4, signed=True)),
        "⏳ Bekleme: {} dk yeni giriş yok".format(fmt.number(data.get("cooldown_minutes"), 0)),
    ]
    return MessageContent(str(getattr(event, "resolved_severity", lambda: "info")()),
                          _headline(event, "{} · COOLDOWN".format(pair)),
                          (Section("🧊 Soğuma", tuple(lines)),))


def _build_feed_outage(event: Any, data: Mapping[str, Any]) -> MessageContent:
    pair = _pair(event, data) or "?"
    lines = [
        "⚠️ Hata: {}".format(_text(data, "error", "veri hatası")),
        "📄 Detay: {}".format(_text(data, "detail", "veri yok")),
        "⛔ Yeni giriş yok (fail-safe).",
    ]
    return MessageContent(str(getattr(event, "resolved_severity", lambda: "info")()),
                          _headline(event, "VERİ KESİNTİSİ · {}".format(pair)),
                          (Section("📡 Veri akışı", tuple(lines)),))


def _build_data_fail_safe(event: Any, data: Mapping[str, Any]) -> MessageContent:
    lines = [
        "🧯 Neden: {}".format(_text(data, "reason", "eksik/bayat veri")),
        "⛔ Yeni giriş yok; veri düzelince kendiliğinden açılır.",
    ]
    return MessageContent(str(getattr(event, "resolved_severity", lambda: "info")()),
                          _headline(event, "VERİ FAIL-SAFE · YENİ GİRİŞ YOK"),
                          (Section("🛡️ Fail-safe", tuple(lines)),))


def _build_daily_summary(event: Any, data: Mapping[str, Any]) -> MessageContent:
    net = fmt.to_float(data.get("net_pnl"))
    lines = [
        "💰 Equity: {} USDT".format(fmt.usdt(data.get("equity"), 2)),
        "🧮 İşlem: {} (kazanan {}) · isabet %{}".format(
            fmt.integer(data.get("trades")), fmt.integer(data.get("wins")), fmt.number(data.get("win_rate_pct"), 1)),
        "📊 Net: {} USDT".format(fmt.usdt(net, 4, signed=True)),
    ]
    return MessageContent(str(getattr(event, "resolved_severity", lambda: "info")()),
                          _headline(event, "GÜNLÜK ÖZET · {}".format(_text(data, "day", "n/a"))),
                          (Section("🗓️ Gün kapanışı", tuple(lines)),))


def _build_equity_drop(event: Any, data: Mapping[str, Any]) -> MessageContent:
    lines = [
        "📈 Zirve: {} USDT".format(fmt.usdt(data.get("peak_equity"), 2)),
        "📉 Şimdi: {} USDT".format(fmt.usdt(data.get("equity"), 2)),
        "📐 Düşüş: -%{} (eşik %{})".format(
            fmt.number(data.get("drop_pct"), 2), fmt.number(data.get("threshold_pct"), 2)),
    ]
    return MessageContent(str(getattr(event, "resolved_severity", lambda: "info")()),
                          _headline(event, "EQUITY DÜŞÜŞÜ"),
                          (Section("🩺 Sermaye uyarısı", tuple(lines)),))


def _build_test(event: Any, data: Mapping[str, Any]) -> MessageContent:
    message = _text(data, "message") or _text_from_body(event)
    lines = [
        "✅ Bildirim katmanı çalışıyor.",
        "📱 Bu mesajı görüyorsanız telefonunuz bağlı.",
    ]
    if message:
        lines.append("📝 {}".format(message))
    if data.get("dry_run"):
        lines.append("🧾 Dry-run: bu mesaj GÖNDERİLMEDİ.")
    return MessageContent(str(getattr(event, "resolved_severity", lambda: "info")()),
                          _headline(event, "TEST BİLDİRİMİ"),
                          (Section("", tuple(lines)),))


def _text_from_body(event: Any) -> str:
    return str(getattr(event, "body", "") or "")


_BUILDERS = {
    "bot_started": _build_bot_started,
    "bot_stopped": _build_bot_stopped,
    "position_opened": _build_position_opened,
    "position_closed": _build_position_closed,
    "take_profit_hit": lambda event, data: _build_protective_exit(event, data, kind="take_profit"),
    "stop_loss_hit": lambda event, data: _build_protective_exit(event, data, kind="stop_loss"),
    "risk_halted": _build_risk_halted,
    "cooldown_started": _build_cooldown_started,
    "feed_outage": _build_feed_outage,
    "data_fail_safe": _build_data_fail_safe,
    "daily_summary": _build_daily_summary,
    "equity_drop": _build_equity_drop,
    "test": _build_test,
}


def _generic(event: Any) -> MessageContent:
    """Fallback for a hand-made event with no structured template."""
    severity = str(getattr(event, "resolved_severity", lambda: "info")())
    headline = "{} {}".format(_square(severity), getattr(event, "title", "") or "")
    body = _text_from_body(event)
    sections: Tuple[Section, ...] = (Section("", (body,)),) if body.strip() else ()
    return MessageContent(severity, headline.strip(), sections)


def build_content(event: Any) -> MessageContent:
    """Build the content model for ``event``; never raises."""
    try:
        builder = _BUILDERS.get(str(getattr(event, "type", "")))
        if builder is not None:
            return builder(event, _data(event))
        return _generic(event)
    except Exception:  # pragma: no cover - rendering must never break delivery
        return _generic(event)


# --------------------------------------------------------------------------- #
# carrier renderers (one function per carrier, one shared model)
# --------------------------------------------------------------------------- #
def to_plain(content: MessageContent) -> str:
    """Clean plain text: webhook, console and audit-file carrier."""
    return "\n".join(content.as_lines())


def to_markdown(content: MessageContent) -> str:
    """ntfy carrier: ``**bold**`` headline and section titles."""
    lines: List[str] = ["**{}**".format(content.headline)]
    for section in content.sections:
        if not Section(section.title, section.lines).as_lines():
            continue
        lines.append("")
        if section.title:
            lines.append("**{}**".format(section.title))
        lines.extend(line for line in section.lines if str(line).strip())
    return "\n".join(_cap(lines, MAX_LINES))


def to_html(content: MessageContent) -> str:
    """Telegram carrier: HTML ``<b>`` headline and section titles."""
    lines: List[str] = ["<b>{}</b>".format(_html.escape(content.headline))]
    for section in content.sections:
        if not Section(section.title, section.lines).as_lines():
            continue
        lines.append("")
        if section.title:
            lines.append("<b>{}</b>".format(_html.escape(section.title)))
        lines.extend(_html.escape(line) for line in section.lines if str(line).strip())
    return "\n".join(_cap(lines, MAX_LINES))


_CARRIERS = {
    "plain": to_plain,
    "markdown": to_markdown,
    "html": to_html,
    "telegram": to_html,
    "webhook": to_plain,
    "console": to_plain,
    "file": to_plain,
}


def render_body(event: Any, carrier: str = "plain") -> str:
    """Render ``event`` for one carrier; falls back to plain text."""
    content = getattr(event, "content", None)
    if not isinstance(content, MessageContent):
        content = build_content(event)
    renderer = _CARRIERS.get(str(carrier or "plain").lower(), to_plain)
    try:
        return renderer(content)
    except Exception:  # pragma: no cover - defensive
        return to_plain(content)


def content_for(type_: str, data: Optional[Mapping[str, Any]] = None, *,
                title: str = "", body: str = "", severity: str = "info",
                pair: str = "", run_id: str = "", ts: int = 0) -> MessageContent:
    """Build a content model from a raw type + data dict (used by preview/samples)."""
    probe = _Probe(type_=type_, data=dict(data or {}), title=title, body=body, severity=severity,
                   pair=pair, run_id=run_id, ts=ts)
    return build_content(probe)


class _Probe:
    """Duck-typed stand-in for ``NotificationEvent`` (avoids an import cycle)."""

    def __init__(self, *, type_: str, data: Dict[str, Any], title: str, body: str,
                 severity: str, pair: str, run_id: str, ts: int) -> None:
        self.type = type_
        self.data = data
        self.title = title
        self.body = body
        self.severity = severity
        self.pair = pair
        self.run_id = run_id
        self.ts = ts

    def resolved_severity(self) -> str:
        return self.severity


def render_content(content: MessageContent, carrier: str = "plain") -> str:
    """Render an already-built content model for a carrier."""
    renderer = _CARRIERS.get(str(carrier or "plain").lower(), to_plain)
    try:
        return renderer(content)
    except Exception:  # pragma: no cover - defensive
        return to_plain(content)


__all__ = [
    "SEVERITY_SQUARE", "MAX_LINES", "CARRIERS", "UP_LADDER", "DOWN_LADDER",
    "Section", "MessageContent", "build_content", "render_body", "render_content",
    "content_for", "to_plain", "to_markdown", "to_html",
]
