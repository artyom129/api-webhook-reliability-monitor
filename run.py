import os

import uvicorn


if __name__ == "__main__":
    # Listens on this machine only. Set HOST=0.0.0.0 to reach it from another
    # device, and put authentication in front of it first (see the README).
    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        reload=os.getenv("RELOAD", "1") == "1",
    )
