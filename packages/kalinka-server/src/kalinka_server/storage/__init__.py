"""What is attached to this machine, and how it is mounted.

The manager watches mountinfo and asks the root helper
(``/usr/lib/kalinka/media-mount``) for every mount and unmount, so the
server itself never needs a privilege.
"""
