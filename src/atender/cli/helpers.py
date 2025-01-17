# -*- coding: utf-8 -*-
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import sys
import importlib
import time
import os
from functools import partial

import quo
import redis
from redis import Redis
from redis.sentinel import Sentinel
from atender.defaults import (DEFAULT_CONNECTION_CLASS, DEFAULT_JOB_CLASS,
                         DEFAULT_QUEUE_CLASS, DEFAULT_WORKER_CLASS)
from atender.logutils import setup_loghandlers
from atender.utils import import_attribute
from atender.worker import WorkerStatus

red = partial(quo.style, fg='vred')
green = partial(quo.style, fg='vgreen')
yellow = partial(quo.style, fg='vyellow')


def read_config_file(module):
    """Reads all UPPERCASE variables defined in the given module file."""
    settings = importlib.import_module(module)
    return dict([(k, v)
                 for k, v in settings.__dict__.items()
                 if k.upper() == k])


def get_redis_from_config(settings, connection_class=Redis):
    """Returns a StrictRedis instance from a dictionary of settings.
       To use redis sentinel, you must specify a dictionary in the configuration file.
       Example of a dictionary with keys without values:
       SENTINEL: {'INSTANCES':, 'SOCKET_TIMEOUT':, 'PASSWORD':,'DB':, 'MASTER_NAME':}
    """
    if settings.get('REDIS_URL') is not None:
        return connection_class.from_url(settings['REDIS_URL'])

    elif settings.get('SENTINEL') is not None:
        instances = settings['SENTINEL'].get('INSTANCES', [('localhost', 26379)])
        socket_timeout = settings['SENTINEL'].get('SOCKET_TIMEOUT', None)
        password = settings['SENTINEL'].get('PASSWORD', None)
        db = settings['SENTINEL'].get('DB', 0)
        master_name = settings['SENTINEL'].get('MASTER_NAME', 'mymaster')
        sn = Sentinel(instances, socket_timeout=socket_timeout, password=password, db=db)
        return sn.master_for(master_name)

    ssl = settings.get('REDIS_SSL', False)
    if isinstance(ssl, str):
        if ssl.lower() in ['y', 'yes', 't', 'true']:
            ssl = True
        elif ssl.lower() in ['n', 'no', 'f', 'false', '']:
            ssl = False
        else:
            raise ValueError('REDIS_SSL is a boolean and must be "True" or "False".')

    kwargs = {
        'host': settings.get('REDIS_HOST', 'localhost'),
        'port': settings.get('REDIS_PORT', 6379),
        'db': settings.get('REDIS_DB', 0),
        'password': settings.get('REDIS_PASSWORD', None),
        'ssl': ssl,
        'ssl_ca_certs': settings.get('REDIS_SSL_CA_CERTS', None),
    }

    return connection_class(**kwargs)


def pad(s, pad_to_length):
    """Pads the given string to the given length."""
    return ('%-' + '%ds' % pad_to_length) % (s,)


def get_scale(x):
    """Finds the lowest scale where x <= scale."""
    scales = [20, 50, 100, 200, 400, 600, 800, 1000]
    for scale in scales:
        if x <= scale:
            return scale
    return x


def state_symbol(state):
    symbols = {
        WorkerStatus.BUSY: red('busy'),
        WorkerStatus.IDLE: green('idle'),
        WorkerStatus.SUSPENDED: yellow('suspended'),
    }
    try:
        return symbols[state]
    except KeyError:
        return state


def show_queues(queues, raw, by_queue, queue_class, worker_class):

    num_jobs = 0
    termwidth, _ = quo.terminalsize()
    chartwidth = min(20, termwidth - 20)

    max_count = 0
    counts = dict()
    for q in queues:
        count = q.count
        counts[q] = count
        max_count = max(max_count, count)
    scale = get_scale(max_count)
    ratio = chartwidth * 1.0 / scale

    for q in queues:
        count = counts[q]
        if not raw:
            chart = green('|' + '█' * int(ratio * count))
            line = '%-12s %s %d' % (q.name, chart, count)
        else:
            line = 'queue %s %d' % (q.name, count)
        quo.echo(line)

        num_jobs += count

    # print summary when not in raw mode
    if not raw:
        quo.echo('%d queues, %d jobs total' % (len(queues), num_jobs))


def show_workers(queues, raw, by_queue, queue_class, worker_class):
    workers = set()
    if queues:
        for queue in queues:
            for worker in worker_class.all(queue=queue):
                workers.add(worker)
    else:
        for worker in worker_class.all():
            workers.add(worker)

    if not by_queue:

        for worker in workers:
            queue_names = ', '.join(worker.queue_names())
            name = '%s (%s %s %s)' % (worker.name, worker.hostname, worker.ip_address, worker.pid)
            if not raw:
                quo.echo('%s: %s %s' % (name, state_symbol(worker.get_state()), queue_names))
            else:
                quo.echo('worker %s %s %s' % (name, worker.get_state(), queue_names))

    else:
        # Display workers by queue
        queue_dict = {}
        for queue in queues:
            queue_dict[queue] = worker_class.all(queue=queue)

        if queue_dict:
            max_length = max(len(q.name) for q, in queue_dict.keys())
        else:
            max_length = 0

        for queue in queue_dict:
            if queue_dict[queue]:
                queues_str = ", ".join(
                    sorted(
                        map(lambda w: '%s (%s)' % (w.name, state_symbol(w.get_state())), queue_dict[queue])
                    )
                )
            else:
                queues_str = '–'
            quo.echo('%s %s' % (pad(queue.name + ':', max_length + 1), queues_str))

    if not raw:
        quo.echo('%d workers, %d queues' % (len(workers), len(queues)))


def show_both(queues, raw, by_queue, queue_class, worker_class):
    show_queues(queues, raw, by_queue, queue_class, worker_class)
    if not raw:
        quo.echo('')
    show_workers(queues, raw, by_queue, queue_class, worker_class)
    if not raw:
        quo.echo('')
        import datetime
        quo.echo('Updated: %s' % datetime.datetime.now())


def refresh(interval, func, *args):
    while True:
        if interval:
            quo.clear()
        func(*args)
        if interval:
            time.sleep(interval)
        else:
            break


def setup_loghandlers_from_args(verbose, quiet, date_format, log_format):
    if verbose and quiet:
        raise RuntimeError("Flags --verbose and --quiet are mutually exclusive.")

    if verbose:
        level = 'DEBUG'
    elif quiet:
        level = 'WARNING'
    else:
        level = 'INFO'
    setup_loghandlers(level, date_format=date_format, log_format=log_format)


class CliConfig:
    """A helper class to be used with quo commands, to handle shared options"""
    def __init__(self, url=None, config=None, worker_class=DEFAULT_WORKER_CLASS,
                 job_class=DEFAULT_JOB_CLASS, queue_class=DEFAULT_QUEUE_CLASS,
                 connection_class=DEFAULT_CONNECTION_CLASS, path=None, *args, **kwargs):
        self._connection = None
        self.url = url
        self.config = config

        if path:
            for pth in path:
                sys.path.append(pth)

        try:
            self.worker_class = import_attribute(worker_class)
        except (ImportError, AttributeError) as exc:
            raise quo.BadParameter(str(exc), param_hint='--worker-class')
        try:
            self.job_class = import_attribute(job_class)
        except (ImportError, AttributeError) as exc:
            raise quo.BadParameter(str(exc), param_hint='--job-class')

        try:
            self.queue_class = import_attribute(queue_class)
        except (ImportError, AttributeError) as exc:
            raise quo.BadParameter(str(exc), param_hint='--queue-class')

        try:
            self.connection_class = import_attribute(connection_class)
        except (ImportError, AttributeError) as exc:
            raise quo.BadParameter(str(exc), param_hint='--connection-class')

    @property
    def connection(self):
        if self._connection is None:
            if self.url:
                self._connection = self.connection_class.from_url(self.url)
            elif self.config:
                settings = read_config_file(self.config) if self.config else {}
                self._connection = get_redis_from_config(settings,
                                                         self.connection_class)
            else:
                self._connection = get_redis_from_config(os.environ,
                                                         self.connection_class)
        return self._connection
