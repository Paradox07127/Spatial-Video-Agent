"""R14 UI audit: Playwright screenshot at 1440x900 (reduced motion) + DOM assertions for the ten rules.

  python pipeline/eval/ui_audit.py URL [--shot PNG] [--out JSON]

Runs Chromium through the app's @playwright/test (cd app && npm install). The page marks its parts with data attributes:
data-stage (3D, map, video canvas), data-card, data-focus, data-chart, data-timeline, data-kpi (+ data-unit),
data-badge (value = source or state), data-claim (+ a data-cite inside, or the text Not supported), data-empty, data-overlay.
Rules come from .notes/evidence/2026-10-08-r14-ui-design.md §6; each prints PASS/FAIL with the measured values.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SVA = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SVA))
from pipeline.common.index_build import OUT_ROOT  # noqa: E402

APP = SVA / "app"
MEASURE = r"""
() => {
  const vw = innerWidth, vh = innerHeight;
  const cs = (e) => getComputedStyle(e);
  const vis = [...document.querySelectorAll('body *')].filter((e) => {
    const r = e.getBoundingClientRect(), s = cs(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw;
  });
  const area = (e) => { const r = e.getBoundingClientRect(); return Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0)) * Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0)); };
  const stage = vis.filter((e) => e.matches('canvas, video, [data-stage]') && !(e.parentElement && e.parentElement.closest('[data-stage]')));
  const texts = vis.filter((e) => [...e.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim()));
  const family = (e) => cs(e).fontFamily.split(',')[0].trim().replace(/["']/g, '');
  const weights = {};
  texts.forEach((e) => { (weights[family(e)] = weights[family(e)] || new Set()).add(cs(e).fontWeight); });
  const px = (v) => parseFloat(v) || 0;
  const durations = vis.flatMap((e) => cs(e).transitionDuration.split(',').map((d) => px(d) * (d.trim().endsWith('ms') ? 1 : 1000)));
  const inStage = (e) => !!e.closest('[data-stage]');
  const claims = vis.filter((e) => e.matches('[data-claim]'));
  return {
    stage_ratio: stage.reduce((s, e) => s + area(e), 0) / (vw * vh),
    cards: vis.filter((e) => e.matches('[data-card]')).length,
    focus: vis.filter((e) => e.matches('[data-focus]')).length,
    charts: vis.filter((e) => e.matches('[data-chart]:not([data-timeline])')).length,
    kpis: vis.filter((e) => e.matches('[data-kpi]')).length,
    kpis_without_unit: vis.filter((e) => e.matches('[data-kpi]:not([data-unit])')).length,
    gradients: vis.filter((e) => !inStage(e) && cs(e).backgroundImage.includes('gradient')).length,
    badge_kinds: [...new Set(vis.filter((e) => e.matches('[data-badge]')).map((e) => e.dataset.badge))],
    ai_badges: vis.filter((e) => e.matches('[data-badge]') && /\bAI\b|✨/.test(e.textContent)).length,
    families: [...new Set(texts.map(family))],
    sizes: [...new Set(texts.map((e) => cs(e).fontSize))],
    max_weights: Math.max(0, ...Object.values(weights).map((s) => s.size)),
    max_radius_px: Math.max(0, ...vis.filter((e) => e.getBoundingClientRect().width > 16 && e.getBoundingClientRect().height > 16)
      .map((e) => px(cs(e).borderTopLeftRadius))),
    shadows_off_overlay: vis.filter((e) => cs(e).boxShadow !== 'none' && !e.closest('[data-overlay]') && !['fixed', 'absolute'].includes(cs(e).position)).length,
    nested_cards: document.querySelectorAll('[data-card] [data-card]').length,
    page_scroll: document.scrollingElement.scrollHeight > vh + 1 || document.scrollingElement.scrollWidth > vw + 1,
    infinite_animations: document.getAnimations().filter((a) => a.effect && a.effect.getTiming().iterations === Infinity).length,
    max_transition_ms: Math.max(0, ...durations),
    uncited_claims: claims.filter((e) => !e.querySelector('[data-cite]') && !e.textContent.includes('Not supported')).length,
    blank_empty_states: vis.filter((e) => e.matches('[data-empty]') && !e.textContent.trim()).length,
  };
}
"""
RUNNER = r"""
const { chromium } = require('@playwright/test');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' });
  await p.goto(process.env.UI_URL, { waitUntil: 'networkidle' });
  await p.screenshot({ path: process.env.UI_SHOT });
  console.log(JSON.stringify(await p.evaluate(MEASURE)));
  await b.close();
})().catch((e) => { console.error(e); process.exit(1); });
""".replace("MEASURE", MEASURE)


def rules(m: dict) -> list[tuple[str, bool, str]]:
    return [
        ("1 stage >= 60% of viewport", m["stage_ratio"] >= 0.6, f"{m['stage_ratio']:.0%}"),
        ("2 one focus, <= 3 peer cards", m["focus"] <= 1 and m["cards"] <= 3, f"focus={m['focus']} cards={m['cards']}"),
        ("3 <= 1 chart besides timeline, <= 3 KPIs with units", m["charts"] <= 1 and m["kpis"] <= 3 and m["kpis_without_unit"] == 0,
         f"charts={m['charts']} kpis={m['kpis']} no_unit={m['kpis_without_unit']}"),
        ("4 no decorative gradients in chrome", m["gradients"] == 0, f"gradients={m['gradients']}"),
        ("5 <= 7 badge kinds, no AI badges", len(m["badge_kinds"]) <= 7 and m["ai_badges"] == 0,
         f"kinds={m['badge_kinds']} ai={m['ai_badges']}"),
        ("6 <= 2 families, <= 5 sizes, <= 2 weights", len(m["families"]) <= 2 and len(m["sizes"]) <= 5 and m["max_weights"] <= 2,
         f"families={m['families']} sizes={len(m['sizes'])} weights={m['max_weights']}"),
        ("7 radius <= 6 px, shadows only on overlays, no nested cards",
         m["max_radius_px"] <= 6 and m["shadows_off_overlay"] == 0 and m["nested_cards"] == 0,
         f"radius={m['max_radius_px']} shadows={m['shadows_off_overlay']} nested={m['nested_cards']}"),
        ("8 no page scroll", not m["page_scroll"], f"page_scroll={m['page_scroll']}"),
        ("9 no looping animation, UI transitions <= 300 ms", m["infinite_animations"] == 0 and m["max_transition_ms"] <= 300,
         f"infinite={m['infinite_animations']} max_transition={m['max_transition_ms']:.0f} ms"),
        ("10 claims cited or Not supported, empty states written", m["uncited_claims"] == 0 and m["blank_empty_states"] == 0,
         f"uncited={m['uncited_claims']} blank_empty={m['blank_empty_states']}"),
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--shot", default=str(OUT_ROOT / "_logs" / "P-C" / "eval" / "ui_audit.png"))
    ap.add_argument("--out", default=str(OUT_ROOT / "_logs" / "P-C" / "eval" / "ui_audit.json"))
    a = ap.parse_args()
    Path(a.shot).parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["node", "-e", RUNNER], cwd=APP, capture_output=True, text=True,
                       env={**os.environ, "UI_URL": a.url, "UI_SHOT": a.shot})
    if r.returncode:
        sys.exit(f"ui_audit: playwright failed: {r.stderr[-800:]}")
    m = json.loads(r.stdout.strip().splitlines()[-1])
    res = rules(m)
    for name, ok, val in res:
        print(f"{'PASS' if ok else 'FAIL'} {name}: {val}")
    n_ok = sum(ok for _, ok, _ in res)
    Path(a.out).write_text(json.dumps({"url": a.url, "metrics": m, "rules": [{"rule": n, "pass": ok, "value": v} for n, ok, v in res],
                                       "rows": [{"metric": "ui_rules_pass", "column": a.url, "value": n_ok, "n": len(res),
                                                 "data_version": a.url}]}, indent=1, ensure_ascii=False))
    print(f"ui_audit: {n_ok}/{len(res)} rules pass; screenshot {a.shot}")


if __name__ == "__main__":
    main()
