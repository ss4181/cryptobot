"""Self-contained HTML rendering of :class:`cryptobot.panel.model.PanelData`.

What this module guarantees
---------------------------
**One file, no network.**  The output carries inline CSS, inline JS and an inline
SVG chart -- no ``src``/``href``/``@import``, no CDN, no web font, no remote
image.  :func:`remote_references` proves it programmatically and the test suite
asserts on it.

**Escaped.**  Every value that comes out of a store goes through
:func:`_esc`, attribute values included.  A notification title containing
``<script>`` is text, not markup.

**Deterministic.**  Identical inputs produce byte-identical output except for the
single, clearly labelled generation line (:data:`GENERATED_MARKER`).  That is why
every collection is sorted with an explicit total order (no set iteration, no
dict-of-mutable order) before it reaches the template.

**Redacted.**  Every data field is redacted with
:func:`cryptobot.notify.redact.redact` before it reaches a template, and the
finished document is swept once more for the *verbatim* values of the configured
secrets.  The sweep replaces exact secret values only -- deliberately not the
heuristic patterns, because those would rewrite the panel's own JavaScript
(``event.key === "Enter"`` looks like ``key=<value>`` to a text-level regex).

**Honest.**  A metric that cannot be computed renders as ``—``; a table that
``--limit`` truncated says so in the table caption, above it and in the footer,
with the row counts.  Partial data is never presented as complete.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..notify.format import duration, integer, number, pct, price, qty, to_float, utc_stamp
from ..notify.redact import REDACTED, known_secrets
from .model import DASH, NotifyRow, PanelData, RunInfo, TradeRow, event_label, side_label

#: The one volatile line of the document.  Determinism checks drop the line that
#: carries this attribute and compare the rest byte for byte.
GENERATED_MARKER = "data-panel-generated"

#: Chart width/height budget (points are spaced evenly, so the SVG stays small
#: for a 17k-bar equity curve).
MAX_CHART_POINTS = 420

#: Default cap per table (``--limit``).
DEFAULT_LIMIT = 500

#: Remote-resource shapes: what could actually *load* something from the network.
#: A bare ``http://`` inside escaped text is data, not a reference, so it is
#: reported separately by :func:`scheme_references`.
_REMOTE_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r"<script[^>]*\ssrc\s*=", "<script src=...>"),
    (r"<link[^>]*\shref\s*=", "<link href=...>"),
    (r"@import", "@import"),
    (r"\burl\s*\(\s*['\"]?(?:https?:)?//", "CSS url(...)"),
    (r"\s(?:src|href|poster|action)\s*=\s*[\"']?(?:https?:)?//", "remote attribute"),
    (r"\bsrcset\s*=", "srcset"),
    (r"\b(?:fetch|XMLHttpRequest|WebSocket|EventSource)\s*\(", "network API in script"),
)

#: CSS custom properties: neutral grays, navy, one accent, tabular numerals.
_CSS = """
:root {
  --bg: #f4f5f7; --panel: #ffffff; --ink: #1d2129; --muted: #6b7280;
  --line: #d9dde3; --navy: #1b2f5b; --accent: #0f766e; --warn: #9a3412;
  --ok: #0f766e; --bad: #9a3412; --flat: #6b7280;
  --mono: ui-monospace, "Cascadia Mono", Consolas, "DejaVu Sans Mono", monospace;
  --sans: ui-sans-serif, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: var(--bg); color: var(--ink);
  font-family: var(--sans); font-size: 14px; line-height: 1.45; }
body { padding: 18px 20px 40px; max-width: 1500px; margin: 0 auto; }
h1 { font-size: 19px; margin: 0 0 2px; letter-spacing: .01em; }
h2 { font-size: 14px; text-transform: uppercase; letter-spacing: .08em;
  color: var(--navy); margin: 26px 0 10px; border-bottom: 1px solid var(--line);
  padding-bottom: 5px; }
h3 { font-size: 13px; margin: 16px 0 6px; color: var(--navy); }
p, li { margin: 4px 0; }
a { color: var(--accent); }
.sub { color: var(--muted); font-size: 12.5px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 6px;
  padding: 12px 14px; margin: 10px 0 0; }
.grid { display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); }
.kpi { background: var(--panel); border: 1px solid var(--line); border-radius: 6px;
  padding: 10px 12px; }
.kpi .label { color: var(--muted); font-size: 11.5px; text-transform: uppercase;
  letter-spacing: .06em; }
.kpi .value { font-family: var(--mono); font-variant-numeric: tabular-nums;
  font-size: 21px; margin-top: 3px; color: var(--navy); }
.kpi .value.small { font-size: 15px; }
.kpi .note { color: var(--muted); font-size: 11.5px; font-family: var(--mono); }
table.data { width: 100%; border-collapse: collapse; background: var(--panel);
  border: 1px solid var(--line); border-radius: 6px; margin-top: 8px; }
table.data caption { text-align: left; color: var(--muted); font-size: 12px;
  padding: 6px 2px; }
table.data th, table.data td { border-bottom: 1px solid var(--line); padding: 6px 8px;
  text-align: left; vertical-align: top; white-space: nowrap; }
table.data th { background: #eef0f3; color: var(--navy); font-size: 11.5px;
  text-transform: uppercase; letter-spacing: .05em; cursor: pointer; user-select: none; }
table.data th:focus-visible { outline: 2px solid var(--accent); }
table.data th[data-sort]::after { content: " \\2195"; color: var(--muted); font-size: 10px; }
table.data td.num, table.data th.num { text-align: right; font-family: var(--mono);
  font-variant-numeric: tabular-nums; }
table.data tr:hover td { background: #fafbfc; }
table.data .wrap { white-space: normal; max-width: 460px; }
tr[data-hidden="1"] { display: none; }
.tag { display: inline-block; border: 1px solid var(--line); border-radius: 3px;
  padding: 0 5px; font-size: 11.5px; font-family: var(--mono); }
.tag.ok { color: var(--ok); border-color: #b6d7d3; background: #eef7f6; }
.tag.bad { color: var(--bad); border-color: #e8cfc4; background: #fbf1ec; }
.tag.flat { color: var(--flat); }
.tag.navy { color: var(--navy); border-color: #c3cbdd; background: #eef1f8; }
.pos { color: var(--ok); } .neg { color: var(--bad); } .dim { color: var(--muted); }
.mono { font-family: var(--mono); font-variant-numeric: tabular-nums; }
.banner { border: 1px solid #e8cfc4; background: #fbf1ec; color: var(--bad);
  border-radius: 6px; padding: 8px 12px; margin: 10px 0 0; }
.banner ul { margin: 4px 0 0 18px; padding: 0; }
.controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: end; margin-top: 10px; }
.controls label { display: flex; flex-direction: column; font-size: 11.5px;
  color: var(--muted); gap: 3px; }
.controls input, .controls select { font: inherit; padding: 4px 6px; min-width: 170px;
  border: 1px solid var(--line); border-radius: 4px; background: #fff; color: var(--ink); }
.hint { color: var(--muted); font-size: 12px; margin-top: 6px; }
.footer { margin-top: 26px; border-top: 1px solid var(--line); padding-top: 10px;
  color: var(--muted); font-size: 12px; }
.legend { font-size: 12.5px; }
.legend code { font-family: var(--mono); background: #eef0f3; padding: 0 3px;
  border-radius: 3px; }
details { margin-top: 6px; }
svg.chart { width: 100%; height: auto; display: block; background: var(--panel);
  border: 1px solid var(--line); border-radius: 6px; margin-top: 8px; }
""".strip()

#: Small, dependency-free table UX.  Everything it does is an enhancement: with
#: JS off the tables render complete and readable (the ``<noscript>`` note says so).
_JS = """
(function () {
  "use strict";
  function rowsFor(table) { return Array.prototype.slice.call(table.tBodies[0].rows); }
  function applyFilter(controls, table) {
    var box = controls.querySelector("[data-role=text]");
    var sel = controls.querySelector("[data-role=status]");
    var needle = box ? box.value.trim().toLowerCase() : "";
    var want = sel ? sel.value : "";
    var shown = 0;
    rowsFor(table).forEach(function (row) {
      var text = (row.getAttribute("data-text") || row.textContent || "").toLowerCase();
      var status = row.getAttribute("data-status") || "";
      var ok = (needle === "" || text.indexOf(needle) !== -1) && (want === "" || status === want);
      row.setAttribute("data-hidden", ok ? "0" : "1");
      if (ok) { shown += 1; }
    });
    var out = document.getElementById(table.id + "-shown");
    if (out) { out.textContent = String(shown); }
  }
  function cellValue(cell, numeric) {
    var raw = cell.getAttribute("data-value");
    if (!numeric) { return (raw === null ? cell.textContent : raw).toLowerCase(); }
    // ``data-value`` is always a C-locale number; the visible text is Turkish
    // (1.234,56), so only the fallback needs the separator swap.
    if (raw !== null && raw !== "") {
      var direct = parseFloat(raw);
      return isNaN(direct) ? -Infinity : direct;
    }
    var text = (cell.textContent || "").replace(/\\s+/g, "").replace(/\\./g, "").replace(",", ".");
    var parsed = parseFloat(text);
    return isNaN(parsed) ? -Infinity : parsed;
  }
  function sortTable(table, index, numeric, dir) {
    var body = table.tBodies[0];
    rowsFor(table).sort(function (a, b) {
      var av = cellValue(a.cells[index], numeric), bv = cellValue(b.cells[index], numeric);
      if (av < bv) { return dir; }
      if (av > bv) { return -dir; }
      return 0;
    }).forEach(function (row) { body.appendChild(row); });
  }
  document.querySelectorAll("table.data").forEach(function (table) {
    var controls = document.querySelector("[data-controls=" + table.id + "]");
    if (controls) {
      controls.addEventListener("input", function () { applyFilter(controls, table); });
      controls.addEventListener("change", function () { applyFilter(controls, table); });
      var reset = controls.querySelector("[data-role=reset]");
      if (reset) {
        reset.addEventListener("click", function () {
          var box = controls.querySelector("[data-role=text]");
          var sel = controls.querySelector("[data-role=status]");
          if (box) { box.value = ""; }
          if (sel) { sel.value = ""; }
          applyFilter(controls, table);
        });
      }
    }
    var headers = table.tHead ? Array.prototype.slice.call(table.tHead.rows[0].cells) : [];
    headers.forEach(function (th, index) {
      if (!th.hasAttribute("data-sort")) { return; }
      var numeric = th.getAttribute("data-sort") === "num";
      th.setAttribute("tabindex", "0");
      th.setAttribute("role", "button");
      var state = 1;
      function toggle() {
        state = -state;
        sortTable(table, index, numeric, state);
        headers.forEach(function (other) { other.removeAttribute("aria-sort"); });
        th.setAttribute("aria-sort", state === 1 ? "ascending" : "descending");
      }
      th.addEventListener("click", toggle);
      th.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggle(); }
      });
    });
  });
}());
""".strip()

#: Suppression/failure codes are shown verbatim in a tooltip, so this list is a
#: convenience for the reader, not the only place the mapping exists.
_LEGEND_REASONS: Tuple[Tuple[str, str], ...] = (
    ("event_not_enabled", "kapsam dışı olay"),
    ("below_min_severity", "önem derecesi eşiğin altında"),
    ("quiet_hours", "sessiz saatler"),
    ("dedupe_window", "tekrar bastırıldı"),
    ("max_per_hour", "saatlik ağ bütçesi doldu"),
    ("no_active_provider", "etkin sağlayıcı yok"),
    ("harness_guard", "test/doğrulama koruması"),
    ("replay_no_push", "replay modunda gönderim kapalı"),
    ("offline_no_push", "offline modunda gönderim kapalı"),
    ("superseded_by_position_closed", "kapanış bildirimi ile geçersiz kılındı"),
)


# --------------------------------------------------------------------------- #
# primitives
# --------------------------------------------------------------------------- #
def _esc(value: Any) -> str:
    """HTML-escape any value (text *and* attribute-safe)."""
    return html.escape("" if value is None else str(value), quote=True)


def _dash(value: str) -> str:
    return DASH if value in ("n/a", "") else value


def _usdt(value: Any, digits: int = 4, *, signed: bool = True) -> str:
    return _dash(number(value, digits, signed=signed))


def _amount(value: Any, digits: int = 4, *, signed: bool = True, unit: str = "USDT") -> str:
    """``+20,6695 USDT`` -- and just ``—`` when the value is not computable."""
    text = _usdt(value, digits, signed=signed)
    return DASH if text == DASH else "{} {}".format(text, unit)


def _pct(value: Any, digits: int = 2, *, signed: bool = True) -> str:
    """A percentage in Turkish notation (``+2,00`` for a delta, ``45,81`` for a rate)."""
    return _dash(pct(value, digits, signed=signed))


def _pct_with_unit(value: Any, digits: int = 2, *, signed: bool = True) -> str:
    text = _pct(value, digits, signed=signed)
    return DASH if text == DASH else "{} %".format(text)


def _count(value: Any) -> str:
    return _dash(integer(value))


def _ratio(value: Any) -> str:
    """Profit factor: ``∞`` is a real result (no losing trade), ``—`` is unknown."""
    parsed = to_float(value)
    if parsed is None:
        return DASH
    if parsed == float("inf"):
        return "\u221e"
    return number(parsed, 2)


def _sign_class(value: Any) -> str:
    parsed = to_float(value)
    if parsed is None:
        return "dim"
    if parsed > 0:
        return "pos"
    if parsed < 0:
        return "neg"
    return "dim"


def _stamp(iso: Any, ts: Any) -> str:
    text = str(iso or "").strip()
    if text:
        return text.replace("T", " ").replace("Z", "")
    return _dash(utc_stamp(ts))


def _tag(label: str, cls: str, title: str = "") -> str:
    attr = ' title="{}"'.format(_esc(title)) if title else ""
    return '<span class="tag {}"{}>{}</span>'.format(cls, attr, _esc(label))


def _status_tag(row: NotifyRow) -> str:
    cls = {"sent": "ok", "failed": "bad", "suppressed": "flat", "dry_run": "navy"}.get(row.status, "flat")
    return _tag(row.status_label, cls, row.status)


def _trade_tag(row: TradeRow) -> str:
    cls = {"kazanc": "ok", "zarar": "bad", "basabas": "flat", "acik": "navy"}.get(row.status_code, "flat")
    return _tag(row.status_label, cls)


# --------------------------------------------------------------------------- #
# chart
# --------------------------------------------------------------------------- #
def _downsample(points: Sequence[Tuple[int, float]], budget: int = MAX_CHART_POINTS) -> List[Tuple[int, float]]:
    """Evenly spaced subset (first and last always kept) -- deterministic."""
    total = len(points)
    if total <= budget or budget < 3:
        return list(points)
    step = (total - 1) / float(budget - 1)
    picked = {int(round(index * step)) for index in range(budget)}
    picked.add(0)
    picked.add(total - 1)
    return [points[index] for index in sorted(picked)]


def _svg_chart(points: Sequence[Tuple[int, float]]) -> str:
    """Inline equity curve + drawdown band (peak line, filled gap, axis labels)."""
    if len(points) < 2:
        return ('<p class="hint">Equity eğrisi için yeterli veri yok '
                '(tek koşu seçilmedi ya da defterde equity satırı yok).</p>')
    series = _downsample(points)
    xs = [float(ts) for ts, _value in series]
    ys = [float(value) for _ts, value in series]
    span_x = (xs[-1] - xs[0]) or 1.0
    low, high = min(ys), max(ys)
    pad = (high - low) * 0.08 or (abs(high) * 0.01 or 1.0)
    low, high = low - pad, high + pad
    span_y = (high - low) or 1.0
    left, right, top, bottom = 74.0, 14.0, 14.0, 30.0
    width, height = 960.0, 300.0
    plot_w, plot_h = width - left - right, height - top - bottom

    def px(ts: float) -> float:
        return left + (ts - xs[0]) / span_x * plot_w

    def py(value: float) -> float:
        return top + (high - value) / span_y * plot_h

    equity_pts = " ".join("{:.2f},{:.2f}".format(px(ts), py(value)) for ts, value in series)
    peak = -float("inf")
    peak_line: List[str] = []
    band: List[str] = []
    for ts, value in series:
        peak = max(peak, value)
        peak_line.append("{:.2f},{:.2f}".format(px(float(ts)), py(peak)))
        band.append("{:.2f},{:.2f}".format(px(float(ts)), py(value)))
    band_path = "M " + " L ".join(band) + " L " + " L ".join(reversed(peak_line)) + " Z"

    grid: List[str] = []
    for fraction in (0.0, 0.5, 1.0):
        value = low + (high - low) * fraction
        y = py(value)
        grid.append('<line x1="{:.2f}" y1="{:.2f}" x2="{:.2f}" y2="{:.2f}" class="grid"/>'.format(
            left, y, width - right, y))
        grid.append('<text x="{:.2f}" y="{:.2f}" class="axis">{}</text>'.format(
            left - 8, y + 4, _esc(number(value, 2))))
    for index, ts in enumerate((series[0][0], series[-1][0])):
        anchor = "start" if index == 0 else "end"
        grid.append('<text x="{:.2f}" y="{:.2f}" class="axis" text-anchor="{}">{}</text>'.format(
            px(float(ts)), height - 10, anchor, _esc(str(utc_stamp(ts))[:10])))

    return (
        '<svg class="chart" viewBox="0 0 {w:.0f} {h:.0f}" role="img" '
        'aria-label="equity eğrisi">{css}'
        '<g>{grid}</g>'
        '<path d="{band}" class="band"/>'
        '<polyline points="{peak}" class="peak"/>'
        '<polyline points="{equity}" class="equity"/>'
        '<text x="{lx:.2f}" y="{ly:.2f}" class="axis label">{label}</text>'
        '</svg>'
    ).format(
        w=width, h=height,
        css=(
            "<style>"
            ".equity{fill:none;stroke:#1b2f5b;stroke-width:1.7}"
            ".peak{fill:none;stroke:#6b7280;stroke-width:1;stroke-dasharray:3 3}"
            ".band{fill:#0f766e;fill-opacity:0.13;stroke:none}"
            ".grid{stroke:#e6e9ee;stroke-width:1}"
            ".axis{fill:#6b7280;font-size:11px;font-family:ui-monospace,Consolas,monospace}"
            "</style>"
        ),
        grid="".join(grid), band=band_path, peak=" ".join(peak_line),
        equity=equity_pts, lx=left + 8, ly=top + 16, label="equity (USDT)",
    )


# --------------------------------------------------------------------------- #
# blocks
# --------------------------------------------------------------------------- #
def _generated_line(data: PanelData) -> str:
    return ('<p class="sub generated" {marker}="{iso}" style="margin:2px 0 0">'
            'Üretim zamanı (UTC): <span class="mono">{iso}</span> · veri: '
            '<span class="mono">{db}</span> + <span class="mono">{audit}</span></p>').format(
        marker=GENERATED_MARKER, iso=_esc(data.generated_iso),
        db=_esc(Path(data.ledger.path).name), audit=_esc(Path(data.audit.path).name))


def _header(data: PanelData) -> str:
    stats = data.stats["trades"]
    notif = data.stats["notifications"]
    pairs = sorted({pair for run in data.runs for pair in run.pairs})
    timeframes = sorted({run.timeframe for run in data.runs if run.timeframe})
    hashes = sorted({run.cfg_hash for run in data.runs if run.cfg_hash})
    modes = sorted({run.mode for run in data.runs if run.mode})
    return (
        '<h1>cryptobot · operatör paneli</h1>'
        '<p class="sub">Bildirimler, açılan/kapanan işlemler, başarı durumu ve genel '
        'başarı istatistikleri — tek dosya, çevrimdışı HTML.</p>'
        + _generated_line(data)
        + '<div class="card"><div class="grid">'
        + _kv("Seçili koşu", _esc(data.selection_label))
        + _kv("Koşu sayısı", _count(len(data.runs)))
        + _kv("Mod", _esc(", ".join(modes)) or DASH)
        + _kv("Zaman aralığı (timeframe)", _esc(", ".join(timeframes)) or DASH)
        + _kv("Pariteler", _esc(", ".join(pairs)) or DASH)
        + _kv("Config özeti (SHA-256/12)", _esc(", ".join(hashes)) or DASH,
              "her koşunun runs.config_json özeti; tabloda tam liste var")
        + _kv("Kapanan işlem", _count(stats["closed"]) if stats["readable"] else DASH,
              "açık: {}".format(_count(stats["open"]) if stats["readable"] else DASH))
        + _kv("Bildirim kaydı", _count(notif["total"]) if notif["readable"] else DASH,
              "denetim dosyası: {}".format("var" if notif["exists"] else "yok"))
        + '</div></div>'
    )


def _kv(label: str, value: str, note: str = "") -> str:
    note_html = '<div class="note">{}</div>'.format(_esc(note)) if note else ""
    return ('<div class="kpi"><div class="label">{}</div>'
            '<div class="value small">{}</div>{}</div>').format(_esc(label), value, note_html)


def _kpi(label: str, value: str, note: str = "", small: bool = False) -> str:
    cls = "value small" if small else "value"
    note_html = '<div class="note">{}</div>'.format(_esc(note)) if note else ""
    return ('<div class="kpi"><div class="label">{}</div><div class="{}">{}</div>{}</div>'
            ).format(_esc(label), cls, value, note_html)


def _warnings(data: PanelData) -> str:
    if not data.warnings:
        return ""
    items = "".join("<li>{}</li>".format(_esc(item)) for item in data.warnings)
    return ('<div class="banner"><strong>Uyarılar — eksik/okunamayan veri var, '
            'aşağıdaki sayılar buna göre okunmalıdır:</strong><ul>{}</ul></div>'
            ).format(items)


def _stats_block(data: PanelData) -> str:
    stats = data.stats["trades"]
    notif = data.stats["notifications"]
    readable = bool(stats["readable"])
    win_rate = _pct(stats["win_rate_pct"], signed=False) if readable else DASH
    dd_basis = data.drawdown_basis_label
    cards = [
        _kpi("Toplam bildirim üretildi", _count(notif["total"]) if notif["readable"] else DASH,
             "denetim satırı: {} (geçerli {})".format(_count(notif["file_lines"]),
                                                     _count(notif["parsed"]))),
        _kpi("Gönderildi (sent)", _count(notif["sent"]) if notif["readable"] else DASH),
        _kpi("Bastırıldı (suppressed)", _count(notif["suppressed"]) if notif["readable"] else DASH,
             "oran: {}".format(_pct(notif["suppress_rate_pct"], signed=False))),
        _kpi("Hata (failed)", _count(notif["failed"]) if notif["readable"] else DASH),
        _kpi("Prova (dry-run)", _count(notif["dry_run"]) if notif["readable"] else DASH),
        _kpi("Teslim başarı oranı", _pct(notif["delivery_rate_pct"], signed=False),
             "gönderildi / (gönderildi + hata)"),
        _kpi("Açılan işlem", _count(stats["opened"]) if readable else DASH,
             "kapanan + açık"),
        _kpi("Kapanan işlem", _count(stats["closed"]) if readable else DASH),
        _kpi("Kazanç / Zarar / Başabaş",
             "{} / {} / {}".format(_count(stats["winning"]), _count(stats["losing"]),
                                   _count(stats["breakeven"])) if readable else DASH,
             "kazanç: net > 0 · zarar: net < 0 · başabaş: net = 0"),
        _kpi("Kazanma oranı", win_rate, "kazanç / kapanan işlem"),
        _kpi("Net kâr/zarar", _amount(stats["net_pnl_usdt"]) if readable else DASH,
             "giriş+çıkış komisyonu ve slippage düşülmüş"),
        _kpi("Net kâr/zarar (%)", _pct_with_unit(stats["net_pnl_pct"]) if readable else DASH,
             "baz: bilinen başlangıç sermayesi {}".format(
                 _usdt(stats["capital_basis_usdt"], 2, signed=False) if stats["capital_known"] else DASH)),
        _kpi("Brüt kâr/zarar", _amount(stats["gross_pnl_usdt"]) if readable else DASH),
        _kpi("Toplam komisyon", _amount(stats["fees_usdt"], signed=False) if readable else DASH),
        _kpi("Toplam slippage", _amount(stats["slippage_usdt"], signed=False) if readable else DASH),
        _kpi("İşlem başına ortalama net",
             _amount(stats["avg_net_pnl_usdt"]) if readable else DASH,
             "ortalama net %: {}".format(_pct(stats["avg_net_pnl_pct"]))),
        _kpi("Profit factor", _ratio(stats["profit_factor"]) if readable else DASH,
             "brüt kazanç / brüt kayıp · ∞ = hiç kayıp yok · — = hesaplanamaz"),
        _kpi("Maks drawdown", _pct_with_unit(stats["max_drawdown_pct"], signed=False) if readable else DASH,
             "{} · baz: {}".format(_amount(stats["max_drawdown_usdt"], signed=False), dd_basis)),
        _kpi("Ortalama tutma süresi",
             _dash(duration(stats["avg_holding_seconds"])) if readable else DASH,
             "kapanan işlemlerin ortalaması"),
    ]
    best, worst = stats["best"], stats["worst"]
    cards.append(_best_worst("En iyi işlem", best, readable))
    cards.append(_best_worst("En kötü işlem", worst, readable))
    return '<div class="grid">' + "".join(cards) + "</div>"


def _best_worst(label: str, row: Optional[TradeRow], readable: bool) -> str:
    if row is None or not readable:
        return _kpi(label, DASH)
    value = '<span class="{}">{}</span>'.format(_sign_class(row.net_pnl), _esc(_usdt(row.net_pnl)))
    return _kpi(label, value + " USDT", "{} · {}{}".format(
        row.pair or DASH, _stamp(row.exit_iso, row.exit_ts),
        "" if row.run_id == "" else " · " + row.run_id))

def _runs_table(data: PanelData) -> str:
    if not data.runs:
        return ('<p class="hint">Defterde koşu kaydı yok (runs tablosu boş veya defter '
                'okunamadı).</p>')
    head = ("Koşu", "Mod", "Başlangıç (UTC)", "TF", "Pariteler", "Sermaye (USDT)",
            "Config hash", "Kapanan", "Net PnL (USDT)")
    headers = "".join('<th{}>{}</th>'.format(
        ' class="num"' if name in ("Sermaye (USDT)", "Kapanan", "Net PnL (USDT)") else "",
        _esc(name)) for name in head)
    rows = []
    for run in sorted(data.runs, key=lambda item: (item.started_ts or 0, item.run_id)):
        rows.append(
            '<tr><td class="mono">{}</td><td>{}</td><td class="mono">{}</td><td>{}</td>'
            '<td class="wrap">{}</td><td class="num">{}</td><td class="mono">{}</td>'
            '<td class="num">{}</td><td class="num {}">{}</td></tr>'.format(
                _esc(run.run_id), _esc(run.mode or DASH), _esc(_stamp(run.started_iso, run.started_ts)),
                _esc(run.timeframe or DASH), _esc(", ".join(run.pairs) or DASH),
                _esc(_usdt(run.initial_capital, 2, signed=False)),
                _esc(run.cfg_hash or DASH), _count(run.trades_closed),
                _sign_class(run.net_pnl), _esc(_usdt(run.net_pnl))))
    note = "" if all(run.in_runs_table for run in data.runs) else \
        '<p class="hint">Bazı koşular <code>runs</code> tablosunda yok; alanları — gösteriliyor.</p>'
    return ('<table class="data" id="tbl-runs"><caption>Defterdeki koşular '
            '(kaynak: <code>runs</code> tablosu).</caption>'
            '<thead><tr>{}</tr></thead><tbody>{}</tbody></table>{}'
            ).format(headers, "".join(rows), note)


def _trade_rows_html(rows: Sequence[TradeRow]) -> str:
    out = []
    for row in rows:
        net_class = _sign_class(row.net_pnl)
        out.append(
            '<tr data-status="{status}" data-text="{text}">'
            '<td class="mono">{entry}</td>'
            '<td class="mono">{exit}</td>'
            '<td>{pair}</td>'
            '<td>{side}</td>'
            '<td class="num" data-value="{entry_raw}">{entry_price}</td>'
            '<td class="num" data-value="{exit_raw}">{exit_price}</td>'
            '<td class="num" data-value="{qty_raw}">{qty}</td>'
            '<td class="num" data-value="{fees_raw}">{fees}</td>'
            '<td class="num" data-value="{gross_raw}">{gross}</td>'
            '<td class="num {net_class}" data-value="{net_raw}">{net}</td>'
            '<td class="num {net_class}" data-value="{pct_raw}">{pct}</td>'
            '<td class="num" data-value="{hold_raw}">{hold}</td>'
            '<td class="wrap">{exit_kind}<div class="dim mono wrap">{exit_reason}</div></td>'
            '<td>{status_cell}</td>'
            '<td><span class="dim mono">{run}</span></td>'
            '</tr>'.format(
                status=_esc(row.status_code),
                text=_esc(" ".join(str(item) for item in (
                    row.run_id, row.pair, row.side, row.status_label, row.close_label,
                    row.exit_reason, row.status_code))),
                entry=_esc(_stamp(row.entry_iso, row.entry_ts)),
                exit=_esc(_stamp(row.exit_iso, row.exit_ts)),
                pair=_esc(row.pair or DASH),
                side=_esc(_side(row.side)),
                entry_raw=_esc(_raw(row.entry_price)), entry_price=_esc(price(row.entry_price)),
                exit_raw=_esc(_raw(row.exit_price)), exit_price=_esc(price(row.exit_price)),
                qty_raw=_esc(_raw(row.qty)), qty=_esc(qty(row.qty)),
                fees_raw=_esc(_raw(row.fees)), fees=_esc(_dash(number(row.fees, 4))),
                gross_raw=_esc(_raw(row.gross_pnl)), gross=_esc(_usdt(row.gross_pnl)),
                net_class=net_class, net_raw=_esc(_raw(row.net_pnl)), net=_esc(_usdt(row.net_pnl)),
                pct_raw=_esc(_raw(row.net_pnl_pct)), pct=_esc(_pct(row.net_pnl_pct)),
                hold_raw=_esc(_raw(row.holding_seconds)), hold=_esc(_dash(duration(row.holding_seconds))),
                exit_kind=_esc(row.close_label),
                exit_reason=_esc(str(row.exit_reason or "")),
                status_cell=_trade_tag(row),
                run=_esc(row.run_id or DASH),
            ))
    return "".join(out)


def _side(value: str) -> str:
    return side_label(value)


def _raw(value: Any) -> str:
    """Machine-readable value for ``data-value`` (C locale, always parseable)."""
    parsed = to_float(value)
    if parsed is None:
        return ""
    return repr(float(parsed))


def _trades_table(data: PanelData, limit: int) -> str:
    closed = sorted(data.trades, key=lambda row: (row.exit_ts or 0, row.trade_id or 0), reverse=True)
    shown = closed if limit <= 0 else closed[:limit]
    truncated = len(closed) - len(shown)
    head = (("Açılış (UTC)", "num"), ("Kapanış (UTC)", "num"), ("Parite", ""), ("Yön", ""),
            ("Giriş fiyatı", "num"), ("Çıkış fiyatı", "num"), ("Miktar", "num"),
            ("Komisyon", "num"), ("Brüt PnL", "num"), ("Net PnL (USDT)", "num"),
            ("Net PnL (%)", "num"), ("Tutma", "num"), ("Çıkış türü / nedeni", ""),
            ("Başarı durumu", ""), ("Koşu", ""))
    headers = "".join('<th data-sort="{}"{}>{}</th>'.format(
        kind, ' class="num"' if kind == "num" else "", _esc(name)) for name, kind in head)
    caption = ("Kapanan {} işlem".format(integer(len(closed))) if not truncated else
               "Kapanan {} işlemden en yeni {} tanesi gösteriliyor "
               "(--limit {} ile kesildi)".format(integer(len(closed)), integer(len(shown)), limit))
    controls = _controls("tbl-trades", (("kazanc", "Kazanç"), ("zarar", "Zarar"),
                                        ("basabas", "Başabaş"), ("acik", "Açık")))
    return (
        '{controls}'
        '<table class="data" id="tbl-trades">'
        '<caption>{caption} · net PnL satır bazında; genel istatistikler bu tablodan bağımsız, '
        'tüm veriden hesaplanır (<span id="tbl-trades-shown">{shown}</span>/'
        '<span id="tbl-trades-total">{total}</span> satır görünür).</caption>'
        '<thead><tr>{headers}</tr></thead><tbody>{rows}</tbody></table>{note}'
    ).format(controls=controls, caption=_esc(caption), shown=len(shown), total=len(closed),
             headers=headers, rows=_trade_rows_html(shown), note=_truncation_note(truncated, limit))


def _open_table(data: PanelData, limit: int) -> str:
    rows_all = sorted(data.open_positions, key=lambda row: (row.entry_ts or 0, row.trade_id or 0),
                      reverse=True)
    shown = rows_all if limit <= 0 else rows_all[:limit]
    truncated = len(rows_all) - len(shown)
    if not rows_all:
        return ('<h3>Açık pozisyonlar</h3><p class="hint">Açık pozisyon yok: defterde '
                'eşleşmeyen OPEN satırı ve <code>trades</code> satırı bulunmuyor.</p>')
    head = (("Açılış (UTC)", "num"), ("Parite", ""), ("Yön", ""), ("Giriş fiyatı", "num"),
            ("Miktar", "num"), ("Giriş komisyonu", "num"), ("Giriş nedeni", ""),
            ("Durum", ""), ("Koşu", ""))
    headers = "".join('<th data-sort="{}"{}>{}</th>'.format(
        kind, ' class="num"' if kind == "num" else "", _esc(name)) for name, kind in head)
    body = []
    for row in shown:
        body.append(
            '<tr data-status="acik" data-text="{text}">'
            '<td class="mono">{entry}</td><td>{pair}</td><td>{side}</td>'
            '<td class="num" data-value="{p_raw}">{p}</td>'
            '<td class="num" data-value="{q_raw}">{q}</td>'
            '<td class="num" data-value="{f_raw}">{f}</td>'
            '<td class="wrap mono">{reason}</td><td>{status}</td>'
            '<td><span class="dim mono">{run}</span></td></tr>'.format(
                text=_esc(" ".join((row.run_id, row.pair, row.side, "acik"))),
                entry=_esc(_stamp(row.entry_iso, row.entry_ts)), pair=_esc(row.pair or DASH),
                side=_esc(_side(row.side)),
                p_raw=_esc(_raw(row.entry_price)), p=_esc(price(row.entry_price)),
                q_raw=_esc(_raw(row.qty)), q=_esc(qty(row.qty)),
                f_raw=_esc(_raw(row.fees)), f=_esc(_dash(number(row.fees, 4))),
                reason=_esc(str(row.exit_reason or "") or DASH),
                status=_tag("Açık", "navy", "acik"), run=_esc(row.run_id or DASH)))
    caption = ("Açık (kapanmamış) {} pozisyon — net PnL henüz yok, genel istatistiklere "
               "dahil edilmedi.".format(integer(len(rows_all))) if not truncated else
               "Açık {} pozisyondan en yeni {} tanesi gösteriliyor (--limit {}).".format(
                   integer(len(rows_all)), integer(len(shown)), limit))
    return ('<h3>Açık pozisyonlar</h3>'
            '<table class="data" id="tbl-open"><caption>{caption}</caption>'
            '<thead><tr>{headers}</tr></thead><tbody>{rows}</tbody></table>{note}').format(
        caption=_esc(caption), headers=headers, rows="".join(body),
        note=_truncation_note(truncated, limit))


def _notify_rows_html(rows: Sequence[NotifyRow]) -> str:
    out = []
    for row in rows:
        reason_cell = _esc(row.reason_label) if row.reason_label else DASH
        if row.reason_translated and row.reason:
            reason_cell += '<div class="dim mono wrap">{}</div>'.format(_esc(row.reason))
        label = '<div>{}</div><div class="dim mono">{}</div>'.format(
            _esc(row.event_label), _esc(row.event)) if row.event_label != row.event \
            else '<div class="mono">{}</div>'.format(_esc(row.event or DASH))
        title = row.title or DASH
        if row.body_excerpt:
            title = ('<span title="{}">{}</span>').format(_esc(row.body_excerpt), _esc(title))
        severity = row.severity or DASH
        cls = {"critical": "bad", "warning": "flat"}.get(severity, "navy")
        out.append(
            '<tr data-status="{status}" data-text="{text}">'
            '<td class="mono">{when}</td>'
            '<td class="wrap">{event}</td>'
            '<td>{sev}</td>'
            '<td>{provider}</td>'
            '<td>{status_cell}</td>'
            '<td class="wrap">{reason}</td>'
            '<td class="num">{http}</td>'
            '<td class="num" data-value="{lat_raw}">{lat}</td>'
            '<td class="mono wrap">{host}</td>'
            '<td class="wrap">{title}</td>'
            '<td><span class="dim mono">{run}</span></td>'
            '</tr>'.format(
                status=_esc(row.status),
                text=_esc(" ".join(str(item) for item in (
                    row.run_id, row.event, row.event_label, row.severity, row.provider,
                    row.status, row.status_label, row.reason, row.reason_label, row.title))),
                when=_esc(_stamp(row.iso, row.ts)),
                event=label, sev=_tag(severity, cls),
                provider=_esc(row.provider or DASH),
                status_cell=_status_tag(row),
                reason=reason_cell,
                http=_esc(str(row.http_status) if row.http_status is not None else DASH),
                lat_raw=_esc(_raw(row.latency_ms)),
                lat=_esc(DASH if row.latency_ms is None else number(row.latency_ms, 1)),
                # Only the host: the topic / bot token / webhook path part of the
                # target is never rendered (it is redacted in the model either way).
                host=_esc(row.target_host or DASH),
                title=title,
                run=_esc(row.run_id or DASH)))
    return "".join(out)


def _notifications_table(data: PanelData, limit: int) -> str:
    stats = data.stats["notifications"]
    rows_all = sorted(data.notifications, key=lambda row: (row.ts or 0, row.seq), reverse=True)
    shown = rows_all if limit <= 0 else rows_all[:limit]
    truncated = len(rows_all) - len(shown)
    head = (("Zaman (UTC)", "num"), ("Olay", ""), ("Önem", ""), ("Sağlayıcı", ""),
            ("Durum", ""), ("Sebep (Türkçe)", ""), ("HTTP", "num"), ("Gecikme (ms)", "num"),
            ("Hedef host", ""), ("Başlık", ""), ("Koşu", ""))
    headers = "".join('<th data-sort="{}"{}>{}</th>'.format(
        kind, ' class="num"' if kind == "num" else "", _esc(name)) for name, kind in head)
    caption = ("{} denetim satırı".format(integer(len(rows_all))) if not truncated else
               "{} denetim satırından en yeni {} tanesi gösteriliyor (--limit {} ile kesildi)"
               .format(integer(len(rows_all)), integer(len(shown)), limit))
    controls = _controls("tbl-notify", (("sent", "gönderildi"), ("suppressed", "bastırıldı"),
                                        ("failed", "hata"), ("dry_run", "prova")))
    return (
        '{controls}'
        '<table class="data" id="tbl-notify">'
        '<caption>{caption} · durum sayıları yukarıdaki KPI bloğunda tüm veriden hesaplanır '
        '(<span id="tbl-notify-shown">{shown}</span>/<span id="tbl-notify-total">{total}</span> satır görünür).</caption>'
        '<thead><tr>{headers}</tr></thead><tbody>{rows}</tbody></table>{note}'
    ).format(controls=controls, caption=_esc(caption), shown=len(shown), total=len(rows_all),
             headers=headers, rows=_notify_rows_html(shown), note=_truncation_note(truncated, limit))


def _controls(table_id: str, statuses: Sequence[Tuple[str, str]]) -> str:
    """Text + status filter and a reset button for one table (see :data:`_JS`)."""
    options = ['<option value="">tümü</option>']
    for value, label in statuses:
        options.append('<option value="{}">{}</option>'.format(_esc(value), _esc(label)))
    return (
        '<div class="controls" data-controls="{table}">'
        '<label>Metin filtresi'
        '<input type="search" data-role="text" placeholder="parite, koşu, sebep…" '
        'aria-label="tablo metin filtresi"></label>'
        '<label>Durum filtresi<select data-role="status" aria-label="durum filtresi">{options}</select></label>'
        '<label>&nbsp;<button type="button" data-role="reset">Filtreleri temizle</button></label>'
        '</div>'
    ).format(table=_esc(table_id), options="".join(options))


def _truncation_note(truncated: int, limit: int) -> str:
    if truncated <= 0:
        return ""
    return ('<p class="hint"><strong>Not:</strong> --limit {} nedeniyle {} satır gizlendi; '
            'bu tablo kısmi veri gösteriyor. Tümü için <code>--limit 0</code> kullanın.'
            ).format(limit, integer(truncated))


def _by_event_table(data: PanelData) -> str:
    stats = data.stats["notifications"]
    breakdown = list(stats["by_event"])
    if not breakdown:
        return ('<h3>Olay kırılımı</h3><p class="hint">Denetim kaydı yok, olay kırılımı '
                'hesaplanamadı.</p>')
    head = ("Olay", "Türkçe", "Toplam", "gönderildi", "bastırıldı", "hata", "prova")
    headers = "".join('<th{}>{}</th>'.format(' class="num"' if index > 1 else "", _esc(name))
                      for index, name in enumerate(head))
    body = []
    for event, counts in breakdown:
        body.append(
            '<tr><td class="mono">{}</td><td>{}</td><td class="num">{}</td>'
            '<td class="num">{}</td><td class="num">{}</td><td class="num">{}</td>'
            '<td class="num">{}</td></tr>'.format(
                _esc(event), _esc(event_label(event)), _count(sum(counts.values())),
                _count(counts["sent"]), _count(counts["suppressed"]),
                _count(counts["failed"]), _count(counts["dry_run"])))
    return ('<h3>Olay kırılımı (tüm veri, kısıtlamasız)</h3>'
            '<table class="data" id="tbl-events"><caption>Her olay türü için durum dağılımı.</caption>'
            '<thead><tr>{}</tr></thead><tbody>{}</tbody></table>').format(headers, "".join(body))


def _legend(data: PanelData) -> str:
    stats = data.stats["notifications"]
    reasons = "".join('<tr><td class="mono">{}</td><td>{}</td></tr>'.format(_esc(code), _esc(label))
                      for code, label in _LEGEND_REASONS)
    severities = ", ".join("{}: {}".format(_esc(key), _count(value))
                           for key, value in sorted(stats["severities"].items())) or DASH
    return (
        '<h2>Tanımlar ve yöntem</h2>'
        '<div class="legend">'
        '<h3>Başarı durumu kuralı (işlemler)</h3>'
        '<p><span class="tag ok">Kazanç</span> net PnL &gt; 0 · '
        '<span class="tag bad">Zarar</span> net PnL &lt; 0 · '
        '<span class="tag flat">Başabaş</span> net PnL <strong>tam olarak</strong> 0 '
        '(tolerans yok) · <span class="tag navy">Açık</span> kapanmamış pozisyon '
        '(net PnL yok, istatistiklere girmez). '
        'Net PnL, defterdeki <code>trades.net_pnl</code> alanıdır: brüt PnL eksi giriş+çıkış '
        'komisyonu; slippage maliyeti ayrıca raporlanır.</p>'
        '<h3>Çıkış türü</h3>'
        '<p><code>take_profit:</code> → Kâr al (take-profit) · <code>stop_loss:</code> → Zarar '
        'durdur (stop-loss) · <code>strategy:</code> → Strateji çıkışı · '
        '<code>time</code>/<code>timeout</code>/<code>time_exit</code> → Süre sonu (time exit) · '
        'başka bir önek → <em>Diğer</em> ve ham <code>exit_reason</code> satırda görünür.</p>'
        '<h3>Bildirim durumu ve teslim oranı</h3>'
        '<p><span class="tag ok">gönderildi</span> sağlayıcı <code>2xx</code> döndü · '
        '<span class="tag bad">hata</span> tüm denemeler başarısız · '
        '<span class="tag flat">bastırıldı</span> filtre/koruma nedeniyle hiç denenmedi · '
        '<span class="tag navy">prova</span> <code>dry-run</code>: ne gönderileceği kaydedildi, '
        'hiçbir şey gönderilmedi. <strong>Teslim başarı oranı</strong> = gönderildi / '
        '(gönderildi + hata); bastırılan satırlar bu orana girmez (kasıtlı olarak '
        'gönderilmemişlerdir) ve ayrı bir <em>bastırma oranı</em> olarak raporlanır.</p>'
        '<h3>Önem derecesi dağılımı</h3><p class="mono">{severities}</p>'
        '<h3>Bastırma/hata sebep kodları</h3>'
        '<table class="data" id="tbl-reasons"><thead><tr><th>Kod (denetimde saklanan)</th>'
        '<th>Türkçe karşılık</th></tr></thead><tbody>{reasons}</tbody></table>'
        '<h3>Hesaplama yöntemi</h3>'
        '<ul>'
        '<li><strong>Net kâr/zarar (%):</strong> toplam net PnL / seçili koşuların bilinen '
        'başlangıç sermayesi toplamı (<code>runs.config_json.initial_capital_usdt</code>). '
        'Sermaye bilinmiyorsa — gösterilir.</li>'
        '<li><strong>Kazanma oranı:</strong> kazanç sayısı / kapanan işlem sayısı.</li>'
        '<li><strong>Profit factor:</strong> brüt kazanç / brüt kayıp; hiç kayıp yoksa ∞, '
        'hiç işlem yoksa —.</li>'
        '<li><strong>Maks drawdown:</strong> {basis} üzerinden, pozitif büyüklük olarak '
        '(tepe→dip düşüş yüzdesi).</li>'
        '<li><strong>Ortalama tutma süresi:</strong> kapanan işlemler için '
        '(exit_ts − entry_ts) ortalaması.</li>'
        '<li><strong>Kısıtlama (<code>--limit</code>):</strong> yalnızca tablo satırlarını '
        'sınırlar. KPI sayıları her zaman <em>tüm</em> veriden hesaplanır; kesilen satırlar '
        'tablo başlığında açıkça yazılır.</li>'
        '<li><strong>Veri kaynakları (salt okunur):</strong> '
        '<code>{db}</code> + <code>{audit}</code>. Panel hiçbir bildirim göndermez, emir '
        'vermez, deftere yazmaz.</li>'
        '<li><strong>Tekrar üretilebilirlik:</strong> aynı girdiler aynı dosyayı üretir; '
        'tek değişken alan, hemen <em>cryptobot · operatör paneli</em> başlığının altındaki '
        'üretim zamanı satırıdır (sayfada <code>Üretim zamanı (UTC)</code> olarak işaretlidir).</li>'
        '</ul>'
        '</div>'
    ).format(severities=severities, reasons=reasons, basis=_esc(data.drawdown_basis_label),
             db=_esc(str(data.ledger.path)), audit=_esc(str(data.audit.path)))


# --------------------------------------------------------------------------- #
# document
# --------------------------------------------------------------------------- #
def remote_references(document: str) -> List[str]:
    """Every remote-resource shape found in ``document`` (empty == self-contained)."""
    found: List[str] = []
    for pattern, label in _REMOTE_PATTERNS:
        for match in re.finditer(pattern, document, re.IGNORECASE):
            snippet = document[max(0, match.start() - 20):match.end() + 20].replace("\n", " ")
            found.append("{} :: {}".format(label, snippet))
    return found


def scheme_references(document: str) -> List[str]:
    """Bare ``http://`` / ``https://`` occurrences anywhere -- stricter than needed.

    A URL that only appears inside escaped text is data, not a resource the page
    loads; the panel aims for zero of those too, so this is reported as extra
    evidence next to :func:`remote_references`.
    """
    return [document[max(0, match.start() - 30):match.end() + 30].replace("\n", " ")
            for match in re.finditer(r"https?://", document, re.IGNORECASE)]


def _scrub_secrets(document: str) -> str:
    """Replace every *configured* secret value appearing anywhere in ``document``.

    The last line of defence, and a deliberately narrow one: it only replaces the
    verbatim values of ``CRYPTOBOT_*`` secrets (what :func:`known_secrets`
    reports), never the heuristic patterns.  Running the pattern rules over the
    finished document would rewrite the panel's own code -- ``event.key === ...``
    reads as ``key=<value>`` -- which is exactly the bug this replaces.  A raw
    secret sitting in an audit row is already scrubbed field by field in the
    model, where the patterns belong.
    """
    for secret in known_secrets():
        if secret and secret in document:
            document = document.replace(secret, REDACTED)
    return document


def render_panel(data: PanelData, *, limit: int = DEFAULT_LIMIT) -> str:
    """Render ``data`` into one self-contained HTML document (already redacted)."""
    truncated_trades = max(0, len(data.trades) - (limit if limit > 0 else len(data.trades)))
    truncated_notify = max(0, len(data.notifications) - (limit if limit > 0 else len(data.notifications)))
    title = "cryptobot panel · {}".format(data.selection_label)
    document = "\n".join([
        "<!DOCTYPE html>",
        '<html lang="tr">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="generator" content="cryptobot panel (cikis: paperbot.py panel)">',
        '<meta name="referrer" content="no-referrer">',
        "<title>{}</title>".format(_esc(title)),
        "<style>",
        _CSS,
        "</style>",
        "</head>",
        "<body>",
        _header(data),
        _warnings(data),
        '<h2>Genel başarı istatistikleri</h2>',
        _stats_block(data),
        _summary_note(data, limit, truncated_trades, truncated_notify),
        "<h2>Koşular</h2>",
        _runs_table(data),
        "<h2>İşlemler</h2>",
        _trades_table(data, limit),
        _open_table(data, limit),
        "<h2>Bildirimler</h2>",
        _notifications_table(data, limit),
        _by_event_table(data),
        "<h2>Equity eğrisi</h2>",
        '<p class="sub">Baz: {} · {} nokta (grafik {} noktaya indirgendi; drawdown tüm '
        'seriden hesaplandı).</p>'.format(
            _esc(data.drawdown_basis_label), _count(len(data.equity_series)), MAX_CHART_POINTS),
        _svg_chart(data.equity_series),
        _legend(data),
        "<noscript><p class=\"banner\">JavaScript kapalı: tablolar tam ve okunabilir, "
        "ancak metin/durum filtreleri ve sütun sıralaması çalışmaz.</p></noscript>",
        "<script>",
        _JS,
        "</script>",
        '<div class="footer">cryptobot panel · salt okunur görünüm · tek dosya, çevrimdışı '
        '(harici script/font/görsel yok). Kısıtlama: <code>--limit {}</code> '
        '({} işlem satırı, {} bildirim satırı gizlendi). Bu sayfa hiçbir bildirim '
        'göndermez, emir vermez veya deftere yazmaz.</div>'.format(
            limit if limit > 0 else 0, integer(truncated_trades), integer(truncated_notify)),
        "</body>",
        "</html>",
        "",
    ])
    # Every field was redacted before it reached a template (see the model); this
    # narrows the last check to the values that actually are secrets.
    return _scrub_secrets(document)


def _summary_note(data: PanelData, limit: int, truncated_trades: int, truncated_notify: int) -> str:
    parts: List[str] = []
    if truncated_trades or truncated_notify:
        parts.append("Tablolar <code>--limit {}</code> ile kesildi: {} işlem ve {} bildirim satırı "
                     "gösterilmiyor — KPI sayıları yine de tüm veriden hesaplandı."
                     .format(limit, integer(truncated_trades), integer(truncated_notify)))
    else:
        parts.append("Tüm {} işlem ve {} bildirim satırı tablolarda gösteriliyor (kesilmedi)."
                     .format(integer(len(data.trades)), integer(len(data.notifications))))
    if len(data.runs) > 1:
        parts.append("DİKKAT: {} koşu birlikte gösteriliyor — toplamlar bağımsız koşuların "
                     "toplamıdır, tek bir hesabın sonucu değildir (aynı geçmiş aralığı birden çok "
                     "koşuda tekrar edebilir, ör. backtest + paper). Tek koşu için "
                     "<code>--run-id &lt;id&gt;</code> kullanın."
                     .format(integer(len(data.runs))))
    if not data.stats["notifications"]["exists"]:
        parts.append("Bildirim denetim dosyası bulunamadı; bildirim sayıları için okunacak "
                     "kayıt yok (sıfır değil, kayıtsız).")
    if not data.stats["trades"]["readable"]:
        parts.append("Defter okunamadı: işlem sayıları hesaplanamadı ve — olarak gösteriliyor.")
    return '<p class="hint">{}</p>'.format(" ".join(parts))


__all__ = ["DEFAULT_LIMIT", "GENERATED_MARKER", "MAX_CHART_POINTS", "REDACTED",
           "remote_references", "render_panel", "scheme_references"]
