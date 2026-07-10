#!/usr/bin/env python3
"""MacroSage patch — adds NDX Dirty Sharpe (tile 19) + Tiered Entry System.
Run once from repo root: python apply_patch.py
Creates backup: short_dashboard.py.bak
"""
import os, sys, shutil

TARGET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "short_dashboard.py")
if not os.path.exists(TARGET):
    sys.exit("ERROR: short_dashboard.py not found")

shutil.copy2(TARGET, TARGET + ".bak")
print("Backup saved to short_dashboard.py.bak")

with open(TARGET, "r", encoding="utf-8") as f:
    src = f.read()

errors = []


# PATCH 1: bump TOTAL_TILES
OLD1 = "TOTAL_TILES = 18  # number of indicator tiles the engine computes"
NEW1 = "TOTAL_TILES = 19  # number of indicator tiles the engine computes"
if OLD1 in src:
    src = src.replace(OLD1, NEW1, 1)
    print("P1 TOTAL_TILES bumped to 19")
else:
    errors.append("P1: TOTAL_TILES line not found")

# PATCH 2: add fetch_ndx_dirty_sharpe() after fetch_spy_history
ANCHOR2 = '    log.warning("SPY history: ALL sources failed")\n    return None, None, None'
DS_FUNC = '''

# ---- NDX Dirty Sharpe (Tile 19, 2026-07-11) --------------------------------
def fetch_ndx_dirty_sharpe():
    """Returns (ratio, ret30_pct, rvol30_pct) or None on failure."""
    closes = _yahoo_closes_range("QQQ", "6mo")
    if not closes or len(closes) < 32:
        closes = _yahoo_closes_range("QQQ", "1y")
    if not closes or len(closes) < 32:
        log.warning("NDX Dirty Sharpe: insufficient QQQ history (%s)",
                    len(closes) if closes else 0)
        return None
    try:
        ret30 = (closes[-1] / closes[-22] - 1.0) * 100.0
        rets30 = [(closes[i] / closes[i-1] - 1.0)
                  for i in range(len(closes)-21, len(closes))
                  if closes[i-1]]
        if len(rets30) < 15:
            return None
        mean = sum(rets30) / len(rets30)
        variance = sum((r-mean)**2 for r in rets30) / (len(rets30)-1)
        rvol30 = (variance ** 0.5) * (252 ** 0.5) * 100.0
        if rvol30 <= 0:
            return None
        ratio = ret30 / rvol30
        log.info("NDX DS: QQQ ret30=%.1f%% rvol30=%.1f%% -> DS=%.3f",
                 ret30, rvol30, ratio)
        return ratio, ret30, rvol30
    except Exception as ex:
        log.warning("NDX DS calc error: %s", ex)
        return None
'''
if ANCHOR2 in src:
    src = src.replace(ANCHOR2, ANCHOR2 + DS_FUNC, 1)
    print("P2 fetch_ndx_dirty_sharpe() inserted")
else:
    errors.append("P2: fetch_spy_history anchor not found")

# PATCH 3: DS tile data block before p = []
ANCHOR3 = "\np = []\n"
DS_BLOCK = '''
# ---- NDX Dirty Sharpe tile data (tile 19) ----------------------------------
_ds_result = fetch_ndx_dirty_sharpe()
ds_ratio_val = None
ds_sub, ds_col = "no data", "gray"
if _ds_result:
    _dsr, _dret, _drvol = _ds_result
    ds_ratio_val, _ = keep("ndx_dirty_sharpe", _dsr, -10, 20)
    if ds_ratio_val is not None:
        _ds_tag = "[DS:%.2f ret30=%.1f%% rvol=%.1f%%]" % (ds_ratio_val, _dret, _drvol)
        if ds_ratio_val > 1.2:
            ds_col, ds_sub = "red", "%s STRETCHED" % _ds_tag
        elif ds_ratio_val < 0:
            ds_col, ds_sub = "green", "%s CORRECTING" % _ds_tag
        elif ds_ratio_val >= 0.5:
            ds_col, ds_sub = "amber", "%s ELEVATED" % _ds_tag
        else:
            ds_col, ds_sub = "green", "%s NORMAL" % _ds_tag
else:
    _dsc = CACHE.get("ndx_dirty_sharpe")
    if _dsc is not None:
        ds_ratio_val = _dsc
        ds_sub = "QQQ DS %.2f (last known)" % _dsc
        ds_col = "red" if _dsc > 1.2 else ("amber" if _dsc >= 0.5 else "green")
        log.warning("NDX DS: using cache (%.2f)", _dsc)

'''
if ANCHOR3 in src:
    src = src.replace(ANCHOR3, DS_BLOCK + ANCHOR3, 1)
    print("P3 DS tile data block inserted")
else:
    errors.append("P3: 'p = []' anchor not found")

# PATCH 4: add tile 19
ANCHOR4 = 'p.append(("18. Breadth proxy (RSP/SPY)", bp_sub, bp_col))'
TILE19 = '\np.append(("19. NDX Dirty Sharpe (QQQ 30d)", ds_sub, ds_col))'
if ANCHOR4 in src:
    src = src.replace(ANCHOR4, ANCHOR4 + TILE19, 1)
    print("P4 Tile 19 appended")
else:
    errors.append("P4: Tile 18 anchor not found")

# PATCH 5: Tiered Entry System before VERDICT ENGINE
ANCHOR5 = "# ---- 1. dual-red streak (session-guarded, persisted) ----"
TIER_BLOCK = '''# ---- TIERED ENTRY SYSTEM (2026-07-11) ----------------------------------------
# Tier 1 (30%): n_red >= 6 + L2 input + calendar clear
# Tier 2 (60%): Tier 1 + breadth_decay_streak >= 2
# Tier 3 (full): all gates confirmed (initiate_short)
_l2_any_input = any([gamma_flip, _vix_backwardation, mcclellan_divergence])
_cal_clear_tier = not (fomc_days is not None and fomc_days <= 5)
_tier1_met = bool(n_red >= 6 and _l2_any_input and _cal_clear_tier)
_tier2_met = bool(_tier1_met and breadth_decay_streak >= 2)
_prev_tier = int(CACHE.get("current_tier") or 0)

'''
if ANCHOR5 in src:
    src = src.replace(ANCHOR5, TIER_BLOCK + ANCHOR5, 1)
    print("P5 Tiered Entry block inserted")
else:
    errors.append("P5: VERDICT ENGINE anchor not found")

# PATCH 6: resolve tier after initiate_short
ANCHOR6 = "initiate_short = not blockers\n"
TIER_RESOLVE = """
# ---- Resolve current tier and persist ----
if initiate_short:
    _current_tier = 3
elif _tier2_met:
    _current_tier = 2
elif _tier1_met:
    _current_tier = 1
else:
    _current_tier = 0
if _current_tier < _prev_tier and not is_new_session(CACHE, "tier_session"):
    _current_tier = _prev_tier
CACHE.set("current_tier", _current_tier, RUN_TS)
if _current_tier > 0:
    mark_session(CACHE, "tier_session")
_TIER_LABELS = {0: "NO POSITION", 1: "TIER 1 -- 30%", 2: "TIER 2 -- 60%", 3: "TIER 3 -- FULL"}
_tier_label = _TIER_LABELS.get(_current_tier, "TIER %d" % _current_tier)
log.info("TIER: %s (n_red=%d, l2=%s, bd=%d, initiate=%s)",
         _tier_label, n_red, _l2_any_input, breadth_decay_streak, initiate_short)

"""
if ANCHOR6 in src:
    src = src.replace(ANCHOR6, ANCHOR6 + TIER_RESOLVE, 1)
    print("P6 Tier resolution inserted")
else:
    errors.append("P6: 'initiate_short = not blockers' not found")

# PATCH 7: tier-aware sizing
OLD7 = '    size_mult, size_txt = 0.5, "probe only (<8 red)"'
NEW7 = '''    size_mult, size_txt = 0.5, "probe only (<8 red)"
if _current_tier == 1:
    size_mult = min(size_mult, 0.30)
    size_txt = "TIER 1 -- 30%% (%d red tiles)" % n_red
elif _current_tier == 2:
    size_mult = min(size_mult, 0.60)
    size_txt = "TIER 2 -- 60%% (breadth streak %d)" % breadth_decay_streak
elif _current_tier == 3 and not initiate_short:
    _current_tier = 2
    size_mult = min(size_mult, 0.60)
    size_txt = "TIER 2 -- 60%% (full gates not yet met)"'''
if OLD7 in src:
    src = src.replace(OLD7, NEW7, 1)
    print("P7 Tier-aware sizing added")
else:
    errors.append("P7: size probe-only line not found")

# PATCH 8: tier status in WATCHING head
OLD8 = ('    if dual_red:\n'
        '        head = "WATCHING - Day %d of 3 (dual-red active)" % min(dual_red_streak, 3)\n'
        '    else:')
NEW8 = ('    if _current_tier in (1, 2):\n'
        '        head = ("%s ACTIVE -- %d red tiles | L2:%s | bd:%d | blocked: %s") % (\n'
        '            _tier_label, n_red,\n'
        '            "yes" if _l2_any_input else "no",\n'
        '            breadth_decay_streak,\n'
        '            "; ".join(blockers))\n'
        '    elif dual_red:\n'
        '        head = "WATCHING - Day %d of 3 (dual-red active)" % min(dual_red_streak, 3)\n'
        '    else:')
if OLD8 in src:
    src = src.replace(OLD8, NEW8, 1)
    print("P8 WATCHING head updated")
else:
    errors.append("P8: WATCHING head anchor not found")

# PATCH 9: layman text tier awareness
OLD9 = ('    elif _ll.startswith("ENTRY SIGNAL"):\n'
        '        layman = ("Small starter position.')
NEW9 = ('    elif _current_tier in (1, 2):\n'
        '        layman = ("%s active. %d red tiles + L2 signal. Sized at %s. "\n'
        '                  "Tier 3 needs all gates confirmed."\n'
        '                  % (_tier_label, n_red, size_txt))\n'
        '    elif _ll.startswith("ENTRY SIGNAL"):\n'
        '        layman = ("Small starter position.')
if OLD9 in src:
    src = src.replace(OLD9, NEW9, 1)
    print("P9 Layman text updated")
else:
    errors.append("P9: Layman ENTRY SIGNAL anchor not found")

# ── write or report ──────────────────────────────────────────────────────────
if errors:
    print("\nPATCH INCOMPLETE -- anchors not found:")
    for e in errors:
        print("  *", e)
    print("Backup intact at short_dashboard.py.bak — no changes written.")
    sys.exit(1)
else:
    with open(TARGET, "w", encoding="utf-8") as f:
        f.write(src)
    print("\nALL 9 PATCHES APPLIED.")
    print("  TOTAL_TILES=19 | fetch_ndx_dirty_sharpe() | Tile 19 | Tiered Entry")
    print("Next: git add short_dashboard.py && git commit -m 'feat: tile 19 + tiered entry' && git push")
