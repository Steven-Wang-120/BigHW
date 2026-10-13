"""Read-only bootstrap health orchestration."""

from pathlib import Path

from 主程序.repositories.database import database_health


class HealthService:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)

    def check(self) -> dict[str, object]:
        return database_health(self.database_path)
