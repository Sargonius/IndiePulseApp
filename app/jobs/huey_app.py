import os
from huey import SqliteHuey

queue_db = os.getenv("QUEUE_DB", "queue.db")
huey = SqliteHuey(filename=queue_db)
