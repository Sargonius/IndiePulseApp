from dotenv import load_dotenv

load_dotenv()  # same .env as the web app (DATABASE_URL, QUEUE_DB, ...)

from huey.consumer import Consumer
from app.jobs.huey_app import huey
# Import tasks module to ensure Huey registers all tasks in the registry
from app.jobs import tasks  # noqa: F401
from app.core.logging import setup_logging
from app.desktop.lifeline import on_supervisor_gone
from loguru import logger


def main() -> None:
	setup_logging()
	logger.info("Huey worker starting...")
	consumer = Consumer(huey, workers=1, periodic=True)

	def _stop_gracefully() -> None:
		# Same flags Huey's own SIGINT handler sets: the main loop then waits for the
		# task in progress (e.g. a publish) to finish before exiting.
		logger.info("Supervisor asked to stop: finishing the current task first")
		consumer._restart = False
		consumer._graceful = True
		consumer._received_signal = True

	on_supervisor_gone(_stop_gracefully)
	consumer.run()


if __name__ == "__main__":
	main()
