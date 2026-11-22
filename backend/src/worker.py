import logging
import time
from pathlib import Path
from src.services.archives import claim as claim_archive, materialize_waiting, switch
from src.services.execution import claim, execute, materialize_due
from src.utils.wakeup import wait

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
heartbeat = Path('/tmp/analysis-heartbeat')


def main():
    while True:
        try:
            materialize_due()
            materialize_waiting()
            lease = claim()
            if lease:
                execute(lease)
            else:
                archive_lease = claim_archive()
                if archive_lease:
                    switch(archive_lease)
                else:
                    wait()
            heartbeat.touch()
        except Exception:
            log.exception('Worker cycle failed')
            time.sleep(2)


if __name__ == '__main__':
    main()
