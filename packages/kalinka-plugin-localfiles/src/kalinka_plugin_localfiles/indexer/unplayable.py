class UnplayableFile(ValueError):
    """A file of a format the library reads that the renderer cannot play.

    Its message says why, phrased to follow "Not indexing <file>:".
    """
