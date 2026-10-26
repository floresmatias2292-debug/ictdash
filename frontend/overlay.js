// Series primitive that draws boxes, segments and text by bar index (logical) and price.
// Items:
//   {kind:'box', l, r (null = extend to the right edge), top, bot, fill, stroke, dash, width, text, textColor}
//   {kind:'seg', l, r, price, color, dash, width, text, textColor, textPos:'above'|'below'}
class Overlay {
  constructor() { this.items = []; this._req = null; }
  setItems(items) { this.items = items; this._req && this._req(); }
  attached({ chart, series, requestUpdate }) { this.chart = chart; this.series = series; this._req = requestUpdate; }
  detached() { this._req = null; }
  updateAllViews() {}
  paneViews() {
    const self = this;
    return [{
      zOrder: () => 'bottom',
      renderer: () => ({
        draw: (target) => target.useBitmapCoordinateSpace((scope) => self._draw(scope)),
      }),
    }];
  }
  _draw({ context: ctx, horizontalPixelRatio: hr, verticalPixelRatio: vr, bitmapSize }) {
    if (!this.chart) return;
    const ts = this.chart.timeScale();
    const W = bitmapSize.width;
    const x = (i) => { const c = ts.logicalToCoordinate(i); return c == null ? null : c * hr; };
    const y = (p) => { const c = this.series.priceToCoordinate(p); return c == null ? null : c * vr; };
    const vis = ts.getVisibleLogicalRange();
    if (!vis) return;
    ctx.save();
    this._ctx = ctx;
    ctx.font = `600 ${11 * vr}px -apple-system, Segoe UI, sans-serif`;
    for (const it of this.items) {
      const r = it.r == null ? null : it.r;
      if (r != null && r < vis.from - 1) continue;
      if (it.l > vis.to + 1) continue;
      const x1 = x(it.l), x2 = r == null ? W : x(r);
      if (x1 == null || x2 == null) continue;
      ctx.setLineDash(it.dash ? it.dash.map((d) => d * hr) : []);
      if (it.kind === 'box') {
        const y1 = y(it.top), y2 = y(it.bot);
        if (y1 == null || y2 == null) continue;
        const top = Math.min(y1, y2), h = Math.max(1, Math.abs(y2 - y1));
        if (it.fill) { ctx.fillStyle = it.fill; ctx.fillRect(x1, top, x2 - x1, h); }
        if (it.stroke) { ctx.strokeStyle = it.stroke; ctx.lineWidth = (it.width || 1) * hr; ctx.strokeRect(x1, top, x2 - x1, h); }
        if (it.text) { ctx.textAlign = 'right'; ctx.textBaseline = 'middle'; this._label(it.text, Math.min(x2, W) - 4 * hr, top + h / 2, it.textColor || it.stroke, hr); }
      } else if (it.kind === 'seg') {
        const yy = y(it.price);
        if (yy == null) continue;
        ctx.strokeStyle = it.color; ctx.lineWidth = (it.width || 1) * hr;
        ctx.beginPath(); ctx.moveTo(x1, yy); ctx.lineTo(x2, yy); ctx.stroke();
        if (it.text) {
          ctx.textAlign = it.align || 'center';
          ctx.textBaseline = it.textPos === 'below' ? 'top' : 'bottom';
          const tx = it.align === 'left' ? Math.max(x1, 0) + 4 * hr : it.align === 'right' ? Math.min(x2, W) - 4 * hr : (Math.max(x1, 0) + Math.min(x2, W)) / 2;
          this._label(it.text, tx, yy + (it.textPos === 'below' ? 2 : -2) * vr, it.textColor || it.color, hr);
        }
      }
    }
    ctx.restore();
  }
  // Text with a white halo so it stays readable over candles and zones.
  _label(text, x, y, color, hr) {
    const ctx = this._ctx;
    ctx.setLineDash([]);
    ctx.lineJoin = 'round';
    ctx.lineWidth = 3 * hr;
    ctx.strokeStyle = window.OVERLAY_HALO || 'rgba(255,255,255,0.92)';
    ctx.strokeText(text, x, y);
    ctx.fillStyle = color;
    ctx.fillText(text, x, y);
  }
}
window.Overlay = Overlay;
