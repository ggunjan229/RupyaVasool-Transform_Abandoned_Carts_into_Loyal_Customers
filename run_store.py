# run_store.py
"""
Startup launcher for the AI Revenue Recovery storefront + API.
Loads .env config and boots FastAPI via Uvicorn on port 8000.
"""

import uvicorn
from app.config import settings


def main() -> None:
    print(f"Starting {settings.APP_NAME} (env={settings.ENV}, debug={settings.DEBUG})")
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=settings.DEBUG,
        reload_dirs=["app"],
    )


if __name__ == "__main__":
    main()