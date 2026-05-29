"""
run_bot.py — 4H Strategy Bot launcher.

Usage:
    python3 run_bot.py
"""
import os
import sys
import traceback as _tb
from datetime import datetime as _dt

# -- numpy compatibility shim (numpy 1.x on VPS) --
import numpy as _np
if not hasattr(_np, '_core'):
    import types as _types
    _np._core = _types.ModuleType('numpy._core')
    _np._core.multiarray = _np.core.multiarray
    sys.modules['numpy._core'] = _np._core
    sys.modules['numpy._core.multiarray'] = _np.core.multiarray

import warnings
warnings.filterwarnings('ignore', category=FutureWarning)
warnings.filterwarnings('ignore', category=DeprecationWarning)

# -- Load .env --
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# -- Crash alert --
def _send_crash_alert(error_text):
    webhook_url = os.getenv('DISCORD_WEBHOOK_URL', '').strip()
    if not webhook_url:
        return
    try:
        import urllib.request, json as _json
        msg = {'content': 'BOT CRASHED at %s UTC\n```%s```' % (
            _dt.utcnow().strftime('%H:%M'), error_text[:1800])}
        data = _json.dumps(msg).encode('utf-8')
        req = urllib.request.Request(webhook_url, data=data,
                                      headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass

# -- Launch --
print('=' * 50)
print('  4H MULTI-STRATEGY BOT (4 strategies, BTC-only)')
print('=' * 50)
print('  S1: BREAKOUT_4H      solo PF 2.27  45 trades')
print('  S2: VOL_MOMENTUM_4H  solo PF 2.03  41 trades')
print('  S3: PANIC_DIP_4H     solo PF 2.45  36 trades')
print('  S4: EMA_PULLBACK_4H  solo PF 1.99  52 trades')
print('  Combined: +$27,464, 174 trades, PF 2.01, DD 19%')
print('  Mode: PAPER | Symbol: BTCUSDT | TF: 4H')
print('  S5/S6 dropped Apr 17 audit. Heartbeat -> watch ch.')
print('=' * 50)

try:
    from bot.discord_bot import run_bot
    run_bot()
except Exception as e:
    crash_tb = _tb.format_exc()
    print('[CRASH] %s' % e)
    os.makedirs('logs', exist_ok=True)
    with open('logs/crash.log', 'a', encoding='utf-8') as f:
        f.write('\n%s\n%s\n%s\n' % ('=' * 50, _dt.utcnow().isoformat(), crash_tb))
    _send_crash_alert(crash_tb)
    raise
