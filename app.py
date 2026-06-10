import logging
from commands import run_polling

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler("commands.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    run_polling()
