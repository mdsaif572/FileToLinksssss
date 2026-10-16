# Thunder/server/exceptions.py

class InvalidHash(Exception):
    pass

class FileNotFound(Exception):
    pass

class StreamUnavailable(FileNotFound):
    """The record exists but the media could not be read from Telegram.

    A subclass of :class:`FileNotFound` on purpose: callers that only care about
    "this file cannot be served" keep working, while the routes can tell the two
    apart. The distinction is the status code a viewer (or a media player) sees:

    * ``FileNotFound`` / ``InvalidHash``: the link does not resolve - ``404``.
    * ``StreamUnavailable``: the read failed after the retries (a stalled MTProto
      read, a FloodWait bigger than the stall budget, a dead session). That is
      retryable, and a retry usually lands on a different client, so it answers
      ``503`` + ``Retry-After`` instead of telling the player the file is gone.
    """
    pass
