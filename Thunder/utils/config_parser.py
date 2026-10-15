# Thunder/utils/config_parser.py

import os
from typing import Dict, Optional
from Thunder.utils.logger import logger

class TokenParser:
    def __init__(self, config_file: Optional[str] = None):
        self.tokens: Dict[int, str] = {}
        self.config_file = config_file

    def parse_from_env(self) -> Dict[int, str]:
        try:
            multi_tokens = {
                key: value.strip()
                for key, value in os.environ.items()
                if key.startswith("MULTI_TOKEN") and value.strip()
            }
            
            if not multi_tokens:
                return {}
            
            # Keep the MULTI_TOKEN number as the client id. Renumbering the
            # surviving tokens made every log line and every /status entry
            # unattributable: "Client ID 28" could be MULTI_TOKEN30 once a lower
            # slot was empty, so an operator could not tell which bot token a
            # reported client actually was.
            parsed: Dict[int, str] = {}
            for key, token in multi_tokens.items():
                ordinal = int(''.join(filter(str.isdigit, key)) or 0)
                if ordinal > 0:
                    parsed[ordinal] = token

            self.tokens = dict(sorted(parsed.items()))
            return self.tokens
        except Exception as e:
            logger.error(f"Error in parse_from_env: {e}", exc_info=True)
            return {}
