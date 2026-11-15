"""Typed boundaries only. Stage 1 intentionally contains no outbound API client."""
from dataclasses import dataclass
from typing import Protocol

@dataclass(frozen=True)
class RemoteCampaign:
    id: int
    name: str
    account_id: str
    campaign_type: str

class DirectReader(Protocol):
    async def campaigns(self, account_id: str) -> list[RemoteCampaign]: ...
    async def negative_keywords(self, account_id: str, campaign_id: int) -> list[str]: ...

class DirectWriter(Protocol):
    async def replace_negative_keywords(self, account_id: str, campaign_id: int, items: list[str]) -> None: ...

class MetrikaReader(Protocol):
    async def goals(self, counter_id: int) -> list[dict]: ...

class RecommendationProvider(Protocol):
    async def classify_queries(self, project_context: str, queries: list[dict]) -> list[dict]: ...
