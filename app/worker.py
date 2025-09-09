from huey.consumer import Consumer
from app.jobs.huey_app import huey
# Import tasks module to ensure Huey registers all tasks in the registry
from app.jobs import tasks  # noqa: F401
from app.core.logging import setup_logging
from loguru import logger


def main() -> None:
	setup_logging()
	logger.info("Huey worker starting...")
	consumer = Consumer(huey, workers=1, periodic=True)
	consumer.run()


if __name__ == "__main__":
	main()

