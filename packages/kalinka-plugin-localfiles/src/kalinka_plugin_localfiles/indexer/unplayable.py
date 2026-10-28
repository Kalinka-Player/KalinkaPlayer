#: Incremented whenever a reader starts accepting audio it used to refuse,
#: so the files parked as unreadable are read again.
READERS_VERSION = 1


class UnplayableFile(ValueError):
    """A file of a format the library reads that the renderer cannot play.

    Its message says why, phrased to follow "Not indexing <file>:".
    """
