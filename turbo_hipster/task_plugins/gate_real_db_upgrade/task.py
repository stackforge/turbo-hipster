# Copyright 2013 Rackspace Australia
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.


import gear
import json
import logging
import os
import re
import threading

from turbo_hipster.lib import utils

import turbo_hipster.task_plugins.gate_real_db_upgrade.handle_results\
    as handle_results

__worker_name__ = 'sql-migrate-test-runner-%s' % os.uname()[1]

# Regex for log checking
MIGRATION_START_RE = re.compile('([0-9]+) -&gt; ([0-9]+)\.\.\.$')
MIGRATION_END_RE = re.compile('^done$')


class Runner(threading.Thread):

    """ This thread handles the actual sql-migration tests.
        It pulls in a gearman job from the  build:gate-real-db-upgrade
        queue and runs it through _handle_patchset"""

    log = logging.getLogger("task_plugins.gate_real_db_upgrade.task.Runner")

    def __init__(self, config):
        super(Runner, self).__init__()
        self._stop = threading.Event()
        self.config = config

        # Set up the runner worker
        self.gearman_worker = None
        self.datasets = []

        self.job = None
        self.job_arguments = None
        self.job_datasets = []
        self.work_data = None
        self.cancelled = False

        # Define the number of steps we will do to determine our progress.
        self.current_step = 0
        self.total_steps = 4

        self.setup_gearman()

    def setup_gearman(self):
        self.log.debug("Set up real_db gearman worker")
        self.gearman_worker = gear.Worker(__worker_name__)
        self.gearman_worker.addServer(
            self.config['zuul_server']['gearman_host'],
            self.config['zuul_server']['gearman_port']
        )
        self.register_functions()

    def register_functions(self):
        """ Determine which functions to register based off available
        datasets """
        for dataset in self._get_datasets():
            self.gearman_worker.registerFunction(dataset['config']['gate'])

    def stop(self):
        self._stop.set()
        # Unblock gearman
        self.log.debug("Telling gearman to stop waiting for jobs")
        self.gearman_worker.stopWaitingForJobs()
        self.gearman_worker.shutdown()

    def stopped(self):
        return self._stop.isSet()

    def stop_worker(self, number):
        # Check the number is for this job instance
        # (makes it possible to run multiple workers with this task
        # on this server)
        if number == self.job.unique:
            self.log.debug("We've been asked to stop by our gearman manager")
            self.cancelled = True

    def run(self):
        while True and not self.stopped():
            try:
                # Reset job information:
                self.current_step = 0
                self.cancelled = False
                self.work_data = None
                # gearman_worker.getJob() blocks until a job is available
                self.log.debug("Waiting for job")
                self.job = self.gearman_worker.getJob()
                self._handle_job()
            except:
                self.log.exception('Exception retrieving log event.')

    def _handle_job(self):
        if self.job is not None:
            try:
                self.job_arguments = \
                    json.loads(self.job.arguments.decode('utf-8'))
                self.log.debug("Got job from ZUUL %s" % self.job_arguments)

                # Send an initial WORK_DATA and WORK_STATUS packets
                self._send_work_data()

                # Step 1: Figure out which datasets to run
                self._do_next_step()
                self.job_datasets = self._get_job_datasets()

                # Step 2: Checkout updates from git!
                self._do_next_step()
                git_path = self._grab_patchset(
                    self.job_arguments['ZUUL_PROJECT'],
                    self.job_arguments['ZUUL_REF']
                )

                # Step 3: Run migrations on datasets
                self._do_next_step()
                self._execute_migrations(git_path)

                # Step 4: Analyse logs for errors
                self._do_next_step()
                self._check_all_dataset_logs_for_errors()

                # Step 5: handle the results (and upload etc)
                self._do_next_step()
                self._handle_results()

                # Finally, send updated work data and completed packets
                self._send_work_data()

                if self.work_data['result'] is 'SUCCESS':
                    self.job.sendWorkComplete(
                        json.dumps(self._get_work_data()))
                else:
                    self.job.sendWorkFail()
            except Exception as e:
                self.log.exception('Exception handling log event.')
                if not self.cancelled:
                    self.job.sendWorkException(str(e).encode('utf-8'))

    def _handle_results(self):
        """ pass over the results to handle_results.py for post-processing """
        self.log.debug("Process the resulting files (upload/push)")
        index_url = handle_results.generate_push_results(
            self.job_datasets,
            self.job.unique,
            self.config['publish_logs']
        )
        self.log.debug("Index URL found at %s" % index_url)
        self.work_data['url'] = index_url

    def _check_all_dataset_logs_for_errors(self):
        self.log.debug("Check logs for errors")
        failed = False
        for i, dataset in enumerate(self.job_datasets):
            # Look for the beginning of the migration start
            result = \
                handle_results.check_log_for_errors(dataset['log_file_path'])
            self.job_datasets[i]['result'] = 'SUCCESS' if result else 'FAILURE'
            if not result:
                failed = True

        if failed:
            self.work_data['result'] = "Failed: errors found in dataset log(s)"
        else:
            self.work_data['result'] = "SUCCESS"

    def _get_datasets(self):
        self.log.debug("Get configured datasets to run tests against")
        if len(self.datasets) > 0:
            return self.datasets

        datasets_path = os.path.join(os.path.dirname(__file__),
                                     'datasets')
        for ent in os.listdir(datasets_path):
            dataset_dir = os.path.join(datasets_path, ent)
            if (os.path.isdir(dataset_dir) and os.path.isfile(
                    os.path.join(dataset_dir, 'config.json'))):
                dataset = {}
                with open(os.path.join(dataset_dir, 'config.json'),
                          'r') as config_stream:
                    dataset_config = json.load(config_stream)

                    dataset['name'] = ent
                    dataset['dataset_dir'] = dataset_dir
                    dataset['config'] = dataset_config

                    self.datasets.append(dataset)

        return self.datasets

    def _get_job_datasets(self):
        """ Take the applicable datasets for this job and set them up in
        self.job_datasets """

        job_datasets = []
        for dataset in self._get_datasets():
            # Only load a dataset if it is the right project and we
            # know how to process the upgrade
            if (self.job_arguments['ZUUL_PROJECT'] ==
                    dataset['config']['project'] and
                    self._get_project_command(dataset['config']['type'])):
                dataset['log_file_path'] = os.path.join(
                    self.config['jobs_working_dir'],
                    self.job.unique,
                    dataset['name'] + '.log'
                )
                dataset['result'] = 'UNTESTED'
                dataset['command'] = \
                    self._get_project_command(dataset['config']['type'])

                job_datasets.append(dataset)

        return job_datasets

    def _get_project_command(self, db_type):
        command = (self.job_arguments['ZUUL_PROJECT'].split('/')[-1] + '_' +
                   db_type + '_migrations.sh')
        command = os.path.join(os.path.dirname(__file__), command)
        if os.path.isfile(command):
            return command
        return False

    def _execute_migrations(self, git_path):
        """ Execute the migration on each dataset in datasets """

        self.log.debug("Run the db sync upgrade script")

        for dataset in self.job_datasets:

            cmd = dataset['command']
            # $1 is the unique id
            # $2 is the working dir path
            # $3 is the path to the git repo path
            # $4 is the db user
            # $5 is the db password
            # $6 is the db name
            # $7 is the path to the dataset to test against
            # $8 is the logging.conf for openstack
            # $9 is the pip cache dir

            cmd += (
                (' %(unique_id)s %(job_working_dir)s %(git_path)s'
                    ' %(dbuser)s %(dbpassword)s %(db)s'
                    ' %(dataset_path)s %(logging_conf)s %(pip_cache_dir)s')
                % {
                    'unique_id': self.job.unique,
                    'job_working_dir': os.path.join(
                        self.config['jobs_working_dir'],
                        self.job.unique
                    ),
                    'git_path': git_path,
                    'dbuser': dataset['config']['db_user'],
                    'dbpassword': dataset['config']['db_pass'],
                    'db': dataset['config']['database'],
                    'dataset_path': os.path.join(
                        dataset['dataset_dir'],
                        dataset['config']['seed_data']
                    ),
                    'logging_conf': os.path.join(
                        dataset['dataset_dir'],
                        dataset['config']['logging_conf']
                    ),
                    'pip_cache_dir': self.config['pip_download_cache']
                }
            )

            # Gather logs to watch
            syslog = '/var/log/syslog'
            sqlslo = '/var/log/mysql/slow-queries.log'
            sqlerr = '/var/log/mysql/error.log'
            if 'logs' in self.config:
                if 'syslog' in self.config['logs']:
                    syslog = self.config['logs']['syslog']
                if 'sqlslo' in self.config['logs']:
                    sqlslo = self.config['logs']['sqlslo']
                if 'sqlerr' in self.config['logs']:
                    sqlerr = self.config['logs']['sqlerr']

            utils.execute_to_log(
                cmd,
                dataset['log_file_path'],
                watch_logs=[
                    ('[syslog]', syslog),
                    ('[sqlslo]', sqlslo),
                    ('[sqlerr]', sqlerr)
                ],
            )

    def _grab_patchset(self, project_name, zuul_ref):
        """ Checkout the reference into config['git_working_dir'] """

        self.log.debug("Grab the patchset we want to test against")

        repo = utils.GitRepository(
            self.config['zuul_server']['git_url'] + project_name + '/.git',
            os.path.join(
                self.config['git_working_dir'],
                __worker_name__,
                project_name
            )
        )

        # reset to zuul's master
        repo.reset()

        # Fetch patchset and checkout
        repo.fetch(zuul_ref)
        repo.checkout('FETCH_HEAD')

        return repo.local_path

    def _get_work_data(self):
        if self.work_data is None:
            hostname = os.uname()[1]
            self.work_data = dict(
                name=__worker_name__,
                number=self.job.unique,
                manager='turbo-hipster-manager-%s' % hostname,
                url='http://localhost',
            )
        return self.work_data

    def _send_work_data(self):
        """ Send the WORK DATA in json format for job """
        self.log.debug("Send the work data response: %s" %
                       json.dumps(self._get_work_data()))
        self.job.sendWorkData(json.dumps(self._get_work_data()))

    def _do_next_step(self):
        """ Send a WORK_STATUS command to the gearman server.
        This can provide a progress bar. """

        # Each opportunity we should check if we need to stop
        if self.stopped():
            self.work_data['result'] = "Failed: Worker interrupted/stopped"
            self.job.sendWorkStatus(self.current_step, self.total_steps)
            raise Exception('Thread stopped')
        elif self.cancelled:
            self.work_data['result'] = "Failed: Job cancelled"
            self.job.sendWorkStatus(self.current_step, self.total_steps)
            self.job.sendWorkFail()
            raise Exception('Job cancelled')

        self.current_step += 1
        self.job.sendWorkStatus(self.current_step, self.total_steps)
