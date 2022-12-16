import threading
import queue

from time import sleep
from random import randint

job_queue = queue.Queue(maxsize=1)
answer_queue = queue.Queue(maxsize=1)

def worker():
    while True:
        item = job_queue.get()
        print(f'Working on {item}')
        try:
            res = item()
        except Exception as e:
            res = e
        print(f'Finished {item}')
        answer_queue.put(res)
        job_queue.task_done()

# Turn-on the worker thread.
threading.Thread(target=worker, daemon=True).start()
