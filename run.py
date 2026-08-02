import os
from dev_control import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "5000")), threaded=True)

