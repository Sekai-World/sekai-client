import threading
import queue
import logging

logger = logging.getLogger(__name__)

job_queue = queue.Queue(maxsize=1)
answer_queue = queue.Queue(maxsize=1)

def worker():
    while True:
        item = job_queue.get()
        logger.debug('Working on %s', item)
        try:
            res = item()
        except Exception as e:
            res = e
        logger.debug('Finished %s', item)
        answer_queue.put(res)
        job_queue.task_done()

# Turn-on the worker thread.
threading.Thread(target=worker, daemon=True).start()
