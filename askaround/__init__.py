"""Ask Around: governance for agents that ask people."""
import logging

__version__ = "0.1.0"

# Webhook URLs are secrets (the token is in the path). httpx/httpcore log full request URLs at INFO/DEBUG,
# so pin them to WARNING no matter how the host application configures logging.
for _name in ("httpx", "httpcore", "httpx2"):
    logging.getLogger(_name).setLevel(logging.WARNING)
