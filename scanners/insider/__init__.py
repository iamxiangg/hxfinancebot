__all__ = [
    "MODEL_VERSION",
    "InsiderConfig",
    "InsiderTickerResult",
    "run_insider_scan",
]


def __getattr__(name: str):
    if name in __all__:
        from scanners.insider.engine import MODEL_VERSION, InsiderConfig, InsiderTickerResult, run_insider_scan

        exports = {
            "MODEL_VERSION": MODEL_VERSION,
            "InsiderConfig": InsiderConfig,
            "InsiderTickerResult": InsiderTickerResult,
            "run_insider_scan": run_insider_scan,
        }
        return exports[name]
    raise AttributeError(name)
