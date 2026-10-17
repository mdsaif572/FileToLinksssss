# Thunder/__init__.py

import os
import time

# Optimize WZGram MTProto engine for multi-client file streaming:
# Prevent excessive parallel MTProto session creation per client that triggers Telegram DC connection resets
os.environ.setdefault("WZGRAM_DL_POOL_BOT", "1")
os.environ.setdefault("WZGRAM_DL_WORKERS_BOT", "1")
os.environ.setdefault("WZGRAM_MAX_READ_AHEAD", "256")
os.environ.setdefault("WZGRAM_DL_RATE_BOT", "64")
os.environ.setdefault("WZGRAM_DL_BURST_BOT", "32")
os.environ.setdefault("WZGRAM_UPLOAD_POOL_BOT", "1")
os.environ.setdefault("WZGRAM_UPLOAD_RATE_BOT", "64")

StartTime = time.time()
__version__ = "2.1.1"

# Polyfill ButtonStyle and InlineKeyboardButton if running on pyrogram/pyrofork without wzgram's ButtonStyle
try:
    import inspect
    import pyrogram.enums
    from pyrogram.types import InlineKeyboardButton
    if not hasattr(pyrogram.enums, "ButtonStyle"):
        class ButtonStyle:
            PRIMARY = "primary"
            SUCCESS = "success"
            DANGER = "danger"
        pyrogram.enums.ButtonStyle = ButtonStyle
    if "style" not in inspect.signature(InlineKeyboardButton.__init__).parameters:
        _orig_ikb_init = InlineKeyboardButton.__init__
        def _patched_ikb_init(self, *args, **kwargs):
            kwargs.pop("style", None)
            return _orig_ikb_init(self, *args, **kwargs)
        InlineKeyboardButton.__init__ = _patched_ikb_init
except Exception:
    pass
