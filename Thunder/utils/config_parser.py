# Thunder/utils/config_parser.py

import os
import re
from typing import Dict, Optional
from Thunder.utils.logger import logger

# Only MULTI_TOKEN<digits> counts. A looser match let look-alike variables such
# as MULTI_TOKEN1_BACKUP or MULTI_TOKENEXTRA7 register as tokens and silently
# overwrite the real slot - the pool then ran with the wrong bot.
_TOKEN_KEY = re.compile(r"^MULTI_TOKEN(\d+)$")

class TokenParser:
    def __init__(self, config_file: Optional[str] = None):
        self.tokens: Dict[int, str] = {}
        self.config_file = config_file

    def parse_from_env(self) -> Dict[int, str]:
        try:
            multi_tokens: Dict[str, str] = {}
            for key, value in os.environ.items():
                if not key.startswith("MULTI_TOKEN"):
                    continue
                if not _TOKEN_KEY.match(key):
                    if value.strip():
                        logger.warning(
                            f"Ignoring {key}: only MULTI_TOKEN<number> is read as a bot token"
                        )
                    continue
                if value.strip():
                    multi_tokens[key] = value.strip()
            
            if not multi_tokens:
                return {}
            
            # Keep the MULTI_TOKEN number as the client id. Renumbering the
            # surviving tokens made every log line and every /status entry
            # unattributable: "Client ID 28" could be MULTI_TOKEN30 once a lower
            # slot was empty, so an operator could not tell which bot token a
            # reported client actually was.
            parsed: Dict[int, str] = {}
            for key, token in multi_tokens.items():
                ordinal = int(_TOKEN_KEY.match(key).group(1))
                if ordinal > 0:
                    parsed[ordinal] = token

            self.tokens = dict(sorted(parsed.items()))
            return self.tokens
        except Exception as e:
            logger.error(f"Error in parse_from_env: {e}", exc_info=True)
            return {}
