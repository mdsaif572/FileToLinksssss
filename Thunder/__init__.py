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
