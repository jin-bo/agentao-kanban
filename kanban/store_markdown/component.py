from __future__ import annotations


class StoreComponent:
    def __init__(self, store) -> None:
        self.store = store

    def __getattr__(self, name: str):
        return getattr(self.store, name)
