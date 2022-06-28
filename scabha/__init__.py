import os
import logging
import subprocess

from .logging_utils import ConsoleColors, ColorizingFormatter, MultiplexingHandler
from .  import exceptions


def init_logger(name="SCABHA",
           fmt="{asctime}: {message}",
           col_fmt="{asctime}: %s{message}%s"%(ConsoleColors.BEGIN, ConsoleColors.END),
           datefmt="%Y-%m-%d %H:%M:%S", loglevel="INFO"):
    """Returns the global Stimela logger (initializing if not already done so, with the given values)"""
    global log
    if log is None:
        log = logging.getLogger(name)
        log.propagate = False

        level = os.environ.get('SCABHA_LOG_LEVEL') or 'INFO'
        log.setLevel(getattr(logging, level, logging.INFO))

        global log_console_handler, log_formatter, log_boring_formatter, log_colourful_formatter

        # this function checks if the log record corresponds to stdout/stderr output from a cab
        def _is_from_subprocess(rec):
            return hasattr(rec, 'stimela_subprocess_output')

        log_boring_formatter = logging.Formatter(fmt, datefmt, style="{")

        log_colourful_formatter = ColorizingFormatter(col_fmt, datefmt, style="{")

        log_formatter = log_colourful_formatter

        log_console_handler = MultiplexingHandler()
        log_console_handler.setFormatter(log_formatter)
        log_console_handler.setLevel(getattr(logging, loglevel))
        log.addHandler(log_console_handler)

        exceptions.set_logger(log)
    return log


def set_logger(logger):
    global log
    log = logger
    exceptions.set_logger(logger)

def logger():
    return init_logger()

def report_memory():
    """Reports memory status"""
    try:
        output = subprocess.check_output(["/usr/bin/free", "-h"]).decode().splitlines(keepends=False)
    except subprocess.CalledProcessError as exc:
        log.warning(f"/usr/bin/free -h exited with code {exc.returncode}")
        return
    for line in output:
        log.info(line)


def init():
    """Initiatlizes scabha standalone inside container"""
    log.info("Initial memory state:")
    report_memory()


log = None
init_logger()
