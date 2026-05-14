import threading
import queue
import logging

logger = logging.getLogger(__name__)

job_queue = queue.Queue(maxsize=1)

def worker():
    while True:
        job, response_queue = job_queue.get()
        logger.debug('Working on %s', job)
        try:
            res = job()
        except Exception as e:
            res = e
        logger.debug('Finished %s', job)
        try:
            response_queue.put(res, timeout=1)
        except queue.Full:
            logger.warning('Dropping stale worker result because caller timed out')
        job_queue.task_done()

# Turn-on the worker thread.
threading.Thread(target=worker, daemon=True).start()
