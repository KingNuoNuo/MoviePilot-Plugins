"""宿主 app.sdk.logging 垫片：与宿主一致的 logger 接口。"""

import logging

logger = logging.getLogger("maoyanrank-stub")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)
