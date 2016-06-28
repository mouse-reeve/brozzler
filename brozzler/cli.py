#!/usr/bin/env python
'''
brozzler/cli.py - brozzler command line executables

Copyright (C) 2014-2016 Internet Archive

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
'''

import argparse
import brozzler
import brozzler.worker
import datetime
import json
import logging
import os
import pprint
import re
import requests
import rethinkstuff
import signal
import string
import sys
import threading
import time
import traceback
import warnings
import yaml

def _add_common_options(arg_parser):
    arg_parser.add_argument(
            '-v', '--verbose', dest='log_level',
            action='store_const', default=logging.INFO, const=logging.DEBUG)
    arg_parser.add_argument(
            '--version', action='version',
            version='brozzler %s - %s' % (
                brozzler.__version__, os.path.basename(sys.argv[0])))

def _add_rethinkdb_options(arg_parser):
    arg_parser.add_argument(
            '--rethinkdb-servers', dest='rethinkdb_servers',
            default='localhost', help=(
                'rethinkdb servers, e.g. '
                'db0.foo.org,db0.foo.org:38015,db1.foo.org'))
    arg_parser.add_argument(
            '--rethinkdb-db', dest='rethinkdb_db', default='brozzler',
            help='rethinkdb database name')

def _add_proxy_options(arg_parser):
    arg_parser.add_argument(
            '--proxy', dest='proxy', default=None, help='http proxy')
    arg_parser.add_argument(
            '--enable-warcprox-features', dest='enable_warcprox_features',
            action='store_true', help=(
                'enable special features that assume the configured proxy is '
                'warcprox'))

def _configure_logging(args):
    logging.basicConfig(
            stream=sys.stderr, level=args.log_level,
            format=(
                '%(asctime)s %(process)d %(levelname)s %(threadName)s '
                '%(name)s.%(funcName)s(%(filename)s:%(lineno)d) %(message)s'))
    logging.getLogger('requests.packages.urllib3').setLevel(logging.WARN)
    warnings.simplefilter(
            'ignore', category=requests.packages.urllib3.exceptions.InsecureRequestWarning)
    warnings.simplefilter(
            'ignore', category=requests.packages.urllib3.exceptions.InsecurePlatformWarning)

def brozzle_page():
    '''
    Command line utility entry point for brozzling a single page. Opens url in
    a browser, running some javascript behaviors, and prints outlinks.
    '''
    arg_parser = argparse.ArgumentParser(
            prog=os.path.basename(sys.argv[0]),
            description='brozzle-page - brozzle a single page',
            formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    arg_parser.add_argument('url', metavar='URL', help='page url')
    arg_parser.add_argument(
            '-e', '--executable', dest='chrome_exe', default='chromium-browser',
            help='executable to use to invoke chrome')
    arg_parser.add_argument(
            '--proxy', dest='proxy', default=None,
            help='http proxy')
    arg_parser.add_argument(
            '--enable-warcprox-features', dest='enable_warcprox_features',
            action='store_true', help=(
                'enable special features that assume the configured proxy '
                'is warcprox'))
    _add_common_options(arg_parser)

    args = arg_parser.parse_args(args=sys.argv[1:])
    _configure_logging(args)

    site = brozzler.Site(
            id=-1, seed=args.url, proxy=args.proxy,
            enable_warcprox_features=args.enable_warcprox_features)
    page = brozzler.Page(url=args.url, site_id=site.id)
    worker = brozzler.BrozzlerWorker(frontier=None)

    def on_screenshot(screenshot_png):
        OK_CHARS = (string.ascii_letters + string.digits)
        filename = '/tmp/{}-{:%Y%m%d%H%M%S}.png'.format(
                ''.join(ch if ch in OK_CHARS else '_' for ch in args.url),
                datetime.datetime.now())
        # logging.info('len(screenshot_png)=%s', len(screenshot_png))
        with open(filename, 'wb') as f:
            f.write(screenshot_png)
        logging.info('wrote screenshot to %s', filename)

    browser = brozzler.Browser(chrome_exe=args.chrome_exe)
    browser.start(proxy=site.proxy)
    try:
        outlinks = worker.brozzle_page(
                browser, site, page, on_screenshot=on_screenshot)
        logging.info('outlinks: \n\t%s', '\n\t'.join(sorted(outlinks)))
    except brozzler.ReachedLimit as e:
        logging.error('reached limit %s', e)
    finally:
        browser.stop()

def brozzler_new_job():
    '''
    Command line utility entry point for queuing a new brozzler job. Takes a
    yaml brozzler job configuration file, creates job, sites, and pages objects
    in rethinkdb, which brozzler-workers will look at and start crawling.
    '''
    arg_parser = argparse.ArgumentParser(
            prog=os.path.basename(sys.argv[0]),
            description='brozzler-new-job - queue new job with brozzler',
            formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    arg_parser.add_argument(
            'job_conf_file', metavar='JOB_CONF_FILE',
            help='brozzler job configuration file in yaml')
    _add_rethinkdb_options(arg_parser)
    _add_common_options(arg_parser)

    args = arg_parser.parse_args(args=sys.argv[1:])
    _configure_logging(args)

    r = rethinkstuff.Rethinker(
            args.rethinkdb_servers.split(','), args.rethinkdb_db)
    frontier = brozzler.RethinkDbFrontier(r)
    brozzler.job.new_job_file(frontier, args.job_conf_file)


def brozzler_new_site():
    '''
    Command line utility entry point for queuing a new brozzler site. 
    Takes a seed url and creates a site and page object in rethinkdb, which
    brozzler-workers will look at and start crawling.
    '''
    arg_parser = argparse.ArgumentParser(
            prog=os.path.basename(sys.argv[0]),
            description='brozzler-new-site - register site to brozzle',
            formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    arg_parser.add_argument('seed', metavar='SEED', help='seed url')
    _add_rethinkdb_options(arg_parser)
    _add_proxy_options(arg_parser)
    arg_parser.add_argument(
            '--time-limit', dest='time_limit', default=None,
            help='time limit in seconds for this site')
    arg_parser.add_argument(
            '--ignore-robots', dest='ignore_robots', action='store_true',
            help='ignore robots.txt for this site')
    arg_parser.add_argument(
            '--warcprox-meta', dest='warcprox_meta',
            help=(
                'Warcprox-Meta http request header to send with each request; '
                'must be a json blob, ignored unless warcprox features are '
                'enabled'))
    _add_common_options(arg_parser)

    args = arg_parser.parse_args(args=sys.argv[1:])
    _configure_logging(args)

    site = brozzler.Site(
            seed=args.seed, proxy=args.proxy,
            time_limit=int(args.time_limit) if args.time_limit else None,
            ignore_robots=args.ignore_robots,
            enable_warcprox_features=args.enable_warcprox_features,
            warcprox_meta=(
                json.loads(args.warcprox_meta) if args.warcprox_meta else None))

    r = rethinkstuff.Rethinker(
            args.rethinkdb_servers.split(","), args.rethinkdb_db)
    frontier = brozzler.RethinkDbFrontier(r)
    brozzler.new_site(frontier, site)

def brozzler_worker():
    '''
    Main entrypoint for brozzler, gets sites and pages to brozzle from
    rethinkdb, brozzles them.
    '''

    arg_parser = argparse.ArgumentParser(
            prog=os.path.basename(__file__),
            formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    _add_rethinkdb_options(arg_parser)
    arg_parser.add_argument(
            '-e', '--executable', dest='chrome_exe', default='chromium-browser',
            help='executable to use to invoke chrome')
    arg_parser.add_argument(
            '-n', '--max-browsers', dest='max_browsers', default='1',
            help='max number of chrome instances simultaneously browsing pages')
    _add_common_options(arg_parser)

    args = arg_parser.parse_args(args=sys.argv[1:])
    _configure_logging(args)

    def sigterm(signum, frame):
        raise brozzler.ShutdownRequested('shutdown requested (caught SIGTERM)')
    def sigint(signum, frame):
        raise brozzler.ShutdownRequested('shutdown requested (caught SIGINT)')

    def dump_state(signum, frame):
        pp = pprint.PrettyPrinter(indent=4)
        state_strs = []

        for th in threading.enumerate():
            state_strs.append(str(th))
            stack = traceback.format_stack(sys._current_frames()[th.ident])
            state_strs.append("".join(stack))

        logging.warn("dumping state (caught signal {})\n{}".format(
            signum, "\n".join(state_strs)))

    signal.signal(signal.SIGQUIT, dump_state)
    signal.signal(signal.SIGTERM, sigterm)
    signal.signal(signal.SIGINT, sigint)

    r = rethinkstuff.Rethinker(
            args.rethinkdb_servers.split(","), args.rethinkdb_db)
    frontier = brozzler.RethinkDbFrontier(r)
    service_registry = rethinkstuff.ServiceRegistry(r)
    worker = brozzler.worker.BrozzlerWorker(
            frontier, service_registry, max_browsers=int(args.max_browsers),
            chrome_exe=args.chrome_exe)

    worker_thread = worker.start()

    try:
        while worker_thread.is_alive():
            time.sleep(0.5)
        logging.critical("worker thread has died, shutting down")
    except brozzler.ShutdownRequested as e:
        pass
    finally:
        worker.shutdown_now()
        for th in threading.enumerate():
            if th != threading.current_thread():
                th.join()

    logging.info("brozzler-worker is all done, exiting")
