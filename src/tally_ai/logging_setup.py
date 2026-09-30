import logging


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Third-party HTTP clients are noisy at INFO
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
