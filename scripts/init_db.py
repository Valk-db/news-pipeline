"""Initialize database tables and pgvector extension."""

import asyncio
from src.shared.database import init_db


async def main():
    print("Initializing database...")
    await init_db()
    print("Database initialized successfully.")


if __name__ == "__main__":
    asyncio.run(main())