import datetime
import json
import logging
import math
import operator
import os
import re
import subprocess
import time
import unittest
import uuid
from collections import Counter
from datetime import datetime
from urllib.parse import urlparse

import dateutil.parser
import pytest
from pytz import utc
from retrying import retry

from tests.cook import reasons, mesos
from tests.cook import util


@pytest.mark.timeout(util.DEFAULT_TEST_TIMEOUT_SECS)  # individual test timeout
class CookTest(util.CookTest):

    @classmethod
    def setUpClass(cls):
        cls.cook_url = util.retrieve_cook_url()
        util.init_cook_session(cls.cook_url)

    def setUp(self):
        self.cook_url = type(self).cook_url
        self.mesos_url = util.retrieve_mesos_url()
        self.logger = logging.getLogger(__name__)
        self.cors_origin = os.getenv('COOK_ALLOWED_ORIGIN', 'http://cors.example.com')

    def test_scheduler_info(self):
        info = util.scheduler_info(self.cook_url)
        info_details = json.dumps(info, sort_keys=True)
        self.assertIn('authentication-scheme', info, info_details)
        self.assertIn('commit', info, info_details)
        self.assertIn('start-time', info, info_details)
        self.assertIn('version', info, info_details)
        self.assertEqual(len(info), 4, info_details)
        try:
            dateutil.parser.parse(info['start-time'])
        except:
            self.fail(f"Unable to parse start time: {info_details}")

    def test_basic_submit(self):
        job_executor_type = util.get_job_executor_type(self.cook_url)
        job_uuid, resp = util.submit_job(self.cook_url, executor=job_executor_type)
        self.assertEqual(resp.status_code, 201, msg=resp.content)
        self.assertEqual(resp.content, str.encode(f"submitted jobs {job_uuid}"))
        job = util.wait_for_job(self.cook_url, job_uuid, 'completed')
        self.assertIn('success', [i['status'] for i in job['instances']], json.dumps(job, indent=2))
        self.assertEqual(False, job['disable_mea_culpa_retries'])
        self.assertTrue(len(util.wait_for_output_url(self.cook_url, job_uuid)['output_url']) > 0)

        if util.should_expect_sandbox_directory_for_job(job):
            instance = util.wait_for_sandbox_directory(self.cook_url, job_uuid)
            message = json.dumps(instance, sort_keys=True)
            self.assertIsNotNone(instance['output_url'], message)
            self.assertIsNotNone(instance['sandbox_directory'], message)

            if instance['executor'] == 'cook':
                instance = util.wait_for_exit_code(self.cook_url, job_uuid)
                message = json.dumps(instance, sort_keys=True)
                self.assertEqual(0, instance['exit_code'], message)
            else:
                self.logger.info(f'Exit code not checked because cook executor was not used for {instance}')

    @unittest.skipUnless(util.docker_tests_enabled(),
                         'Requires setting the COOK_TEST_DOCKER_IMAGE environment variable')
    def test_docker_fields(self):
        docker_image = util.docker_image()
        self.assertIsNotNone(docker_image)
        container = {'type': 'docker',
                     'docker': {'image': docker_image,
                                'network': 'HOST',
                                'force-pull-image': False,
                                'parameters': [{'key': 'foo', 'value': 'bar'},
                                               {'key': 'baz', 'value': 'qux'}]},
                     'volumes': [{'host-path': '/var/lib/abc'},
                                 {'mode': 'RW',
                                  'host-path': '/var/lib/def'},
                                 {'host-path': '/var/lib/ghi',
                                  'container-path': '/var/lib/jkl'},
                                 {'mode': 'RW',
                                  'host-path': '/var/lib/mno',
                                  'container-path': '/var/lib/pqr'}]}
        job_uuid, resp = util.submit_job(self.cook_url, container=container)
        self.assertEqual(resp.status_code, 201, msg=resp.content)
        self.assertEqual(resp.content, str.encode(f"submitted jobs {job_uuid}"))
        job = util.load_job(self.cook_url, job_uuid)
        container = job['container']
        docker = container['docker']
        volumes = container['volumes']
        self.assertEqual('DOCKER', container['type'])
        self.assertEqual(docker_image, docker['image'])
        self.assertEqual('HOST', docker['network'])
        self.assertEqual(False, docker['force-pull-image'])
        self.assertEqual(2, len(docker['parameters']))
        self.assertEqual('bar', next(p['value'] for p in docker['parameters'] if p['key'] == 'foo'))
        self.assertEqual('qux', next(p['value'] for p in docker['parameters'] if p['key'] == 'baz'))
        self.assertEqual(4, len(volumes))
        self.assertIn({'host-path': '/var/lib/abc'}, volumes)
        self.assertIn({'mode': 'RW',
                       'host-path': '/var/lib/def'}, volumes)
        self.assertIn({'host-path': '/var/lib/ghi',
                       'container-path': '/var/lib/jkl'}, volumes)
        self.assertIn({'mode': 'RW',
                       'host-path': '/var/lib/mno',
                       'container-path': '/var/lib/pqr'}, volumes)

    def test_no_cook_executor_on_subsequent_instances(self):
        settings = util.settings(self.cook_url)
        retry_limit = util.get_in(settings, 'executor', 'retry-limit')
        max_job_retries = util.get_in(settings, 'task-constraints', 'retry-limit')
        if retry_limit is None or retry_limit is 0:
            pytest.skip('Cook executor is not configured (retry-limit is None)')
        elif retry_limit * 2 > max_job_retries:
            pytest.skip(f'Cannot set max_retries to {retry_limit * 2}, configured maximum is {max_job_retries}')
        else:
            self.logger.debug(f'Cook executor retry limit is {retry_limit}')
            num_hosts = len(util.get_mesos_state(self.mesos_url)['slaves'])
            if retry_limit >= num_hosts:
                pytest.skip(f'Skipping test as not enough agents to verify Mesos executor on subsequent '
                                  f'instances (agents = {num_hosts}, retry limit = {retry_limit})')

        # Should launch many instances
        job_uuid, resp = util.submit_job(self.cook_url, command='exit 1', max_retries=retry_limit * 2)
        self.assertEqual(resp.status_code, 201, msg=resp.content)
        try:
            # Try to get more than retry_limit instances
            util.wait_until(lambda: util.load_job(self.cook_url, job_uuid),
                            lambda job: len(job['instances']) > retry_limit)
        except BaseException as e:
            self.logger.debug(f"Didn't reach desired instance count: {e}")

        job = util.load_job(self.cook_url, job_uuid)
        self.logger.debug(f"Num job instances is {len(job['instances'])}")
        job_instances = sorted(job['instances'], key=operator.itemgetter('start_time'))
        for i, job_instance in enumerate(job_instances):
            message = f'Trailing instance {i}: {json.dumps(job_instance, sort_keys=True)}'
            self.assertNotEqual('success', job_instance['status'], message)

        if retry_limit >= len(job_instances):
            self.logger.debug('Not enough instances to verify Mesos executor on subsequent instances')
        else:
            later_job_instances = job_instances[retry_limit:]
            for i, job_instance in enumerate(later_job_instances):
                message = f'Trailing instance {i}: {json.dumps(job_instance, sort_keys=True)}'
                self.assertEqual('mesos', job_instance['executor'], message)

    @unittest.skipUnless(util.is_cook_executor_in_use(), 'Test assumes the Cook Executor is in use')
    def test_disable_mea_culpa(self):
        job_executor_type = util.get_job_executor_type(self.cook_url)
        self.assertEqual('cook', job_executor_type)
        uuid, resp = util.submit_job(self.cook_url, command='sleep 30', env={'EXECUTOR_TEST_EXIT': '1'},
                                     disable_mea_culpa_retries=True)
        try:
            instance = util.wait_for_instance(self.cook_url, uuid)
            self.assertEqual('cook', instance['executor'])
            job = util.wait_for_job(self.cook_url, uuid, 'completed')
            msg = json.dumps(job, indent=2)
            self.assertEqual(job['state'], 'failed', msg)
            self.assertEqual(job['retries_remaining'], 0, msg)
            instances = job['instances']
            instance = instances[0]
            self.assertEqual(1, len(instances), msg)
            self.assertEqual('Mesos executor terminated', instance['reason_string'], msg)
            self.assertTrue(instance['reason_mea_culpa'], msg)
        finally:
            util.kill_jobs(self.cook_url, [uuid])

    @unittest.skipUnless(util.is_cook_executor_in_use(), 'Test assumes the Cook Executor is in use')
    def test_mea_culpa_retries(self):
        job_executor_type = util.get_job_executor_type(self.cook_url)
        self.assertEqual('cook', job_executor_type)
        uuid, resp = util.submit_job(self.cook_url, command='sleep 30', env={'EXECUTOR_TEST_EXIT': '1'})
        try:
            instance = util.wait_for_instance(self.cook_url, uuid)
            self.assertEqual('cook', instance['executor'])
            job = util.wait_until(lambda: util.load_job(self.cook_url, uuid),
                                  lambda job: len(job['instances']) > 1 and any(
                                      [i['status'] == 'failed' for i in job['instances']]))
            self.assertEqual(job['retries_remaining'], 1, json.dumps(job, indent=2))

            failed_instances = [i for i in job['instances'] if i['status'] == 'failed']
            self.assertTrue(len(failed_instances) != 0, json.dumps(job, indent=2))
            for instance in failed_instances:
                msg = json.dumps(instance, indent=2)
                self.assertEqual('failed', instance['status'], msg)
                self.assertEqual('Mesos executor terminated', instance['reason_string'], msg)
                self.assertTrue(instance['reason_mea_culpa'], msg)
        finally:
            util.kill_jobs(self.cook_url, [uuid])

    def test_executor_flag(self):
        job_uuid, resp = util.submit_job(self.cook_url, executor='cook')
        self.assertEqual(201, resp.status_code, msg=resp.content)
        job = util.load_job(self.cook_url, job_uuid)
        self.assertEqual('cook', job['executor'])

        job_uuid, resp = util.submit_job(self.cook_url, executor='mesos')
        self.assertEqual(201, resp.status_code, msg=resp.content)
        job = util.load_job(self.cook_url, job_uuid)
        self.assertEqual('mesos', job['executor'])

    def test_job_environment_cook_job_and_instance_uuid_only(self):
        command = 'echo "Job environment:" && env && echo "Checking environment variables..." && ' \
                  'if [ ${#COOK_JOB_GROUP_UUID} -ne 0 ]; then echo "COOK_JOB_GROUP_UUID env is present"; exit 1; ' \
                  'else echo "COOK_JOB_GROUP_UUID env is missing as expected"; fi && ' \
                  'if [ ${#COOK_JOB_UUID} -eq 0 ]; then echo "COOK_JOB_UUID env is missing"; exit 1; ' \
                  'else echo "COOK_JOB_UUID env is present as expected"; fi && ' \
                  'if [ ${#COOK_INSTANCE_UUID} -eq 0 ]; then echo "COOK_INSTANCE_UUID env is missing"; exit 1; ' \
                  'else echo "COOK_INSTANCE_UUID env is present as expected"; fi'
        job_uuid, resp = util.submit_job(self.cook_url, command=command)
        self.assertEqual(201, resp.status_code, msg=resp.content)
        job = util.wait_for_job(self.cook_url, job_uuid, 'completed')
        self.assertEqual(1, len(job['instances']))
        message = json.dumps(job['instances'][0], sort_keys=True)
        self.assertEqual('success', job['instances'][0]['status'], message)

    def test_job_environment_cook_job_and_instance_and_group_uuid(self):
        command = 'echo "Job environment:" && env && echo "Checking environment variables..." && ' \
                  'if [ ${#COOK_JOB_GROUP_UUID} -eq 0 ]; then echo "COOK_JOB_GROUP_UUID env is missing"; exit 1; ' \
                  'else echo "COOK_JOB_GROUP_UUID env is present as expected"; fi && ' \
                  'if [ ${#COOK_JOB_UUID} -eq 0 ]; then echo "COOK_JOB_UUID env is missing"; exit 1; ' \
                  'else echo "COOK_JOB_UUID env is present as expected"; fi && ' \
                  'if [ ${#COOK_INSTANCE_UUID} -eq 0 ]; then echo "COOK_INSTANCE_UUID env is missing"; exit 1; ' \
                  'else echo "COOK_INSTANCE_UUID env is present as expected"; fi'
        group_uuid = str(uuid.uuid4())
        job_uuid, resp = util.submit_job(self.cook_url, command=command, group=group_uuid)
        self.assertEqual(201, resp.status_code, msg=resp.content)
        job = util.wait_for_job(self.cook_url, job_uuid, 'completed')
        self.assertEqual(1, len(job['instances']))
        message = json.dumps(job['instances'][0], sort_keys=True)
        self.assertEqual('success', job['instances'][0]['status'], message)

    def test_failing_submit(self):
        job_executor_type = util.get_job_executor_type(self.cook_url)
        job_uuid, resp = util.submit_job(self.cook_url, command='exit 1', executor=job_executor_type)
        self.assertEqual(201, resp.status_code, msg=resp.content)
        job = util.wait_for_job(self.cook_url, job_uuid, 'completed')
        self.assertEqual(1, len(job['instances']))
        instance = job['instances'][0]
        message = json.dumps(instance, sort_keys=True)
        self.assertEqual('failed', instance['status'], message)
        self.assertFalse(instance['reason_mea_culpa'], message)

        if util.should_expect_sandbox_directory(instance):
            instance = util.wait_for_sandbox_directory(self.cook_url, job_uuid)
            message = json.dumps(instance, sort_keys=True)
            self.assertIsNotNone(instance['output_url'], message)
            self.assertIsNotNone(instance['sandbox_directory'], message)

        if instance['executor'] == 'cook':
            instance = util.wait_for_exit_code(self.cook_url, job_uuid)
            message = json.dumps(instance, sort_keys=True)
            self.assertEqual(1, instance['exit_code'], message)
        else:
            self.logger.info(f'Exit code not checked because cook executor was not used for {instance}')

    @unittest.skipUnless(util.is_cook_executor_in_use(), 'Test assumes the Cook Executor is in use')
    def test_progress_update_submit(self):
        job_executor_type = util.get_job_executor_type(self.cook_url)
        progress_file_env = util.retrieve_progress_file_env(self.cook_url)

        line = util.progress_line(self.cook_url, 25, f'Twenty-five percent in ${{{progress_file_env}}}')
        command = f'echo "{line}" >> ${{{progress_file_env}}}; sleep 1; exit 0'
        job_uuid, resp = util.submit_job(self.cook_url, command=command,
                                         env={progress_file_env: 'progress.txt'},
                                         executor=job_executor_type, max_runtime=60000)
        self.assertEqual(201, resp.status_code, msg=resp.content)
        job = util.wait_for_job(self.cook_url, job_uuid, 'completed')
        message = json.dumps(job['instances'], sort_keys=True)
        self.assertIn('success', (i['status'] for i in job['instances']), message)

        instance = util.wait_for_sandbox_directory(self.cook_url, job_uuid)
        message = json.dumps(instance, sort_keys=True)
        self.assertIsNotNone(instance['output_url'], message)
        self.assertIsNotNone(instance['sandbox_directory'], message)
        self.assertEqual('cook', instance['executor'])
        util.sleep_for_publish_interval(self.cook_url)
        instance = util.wait_for_exit_code(self.cook_url, job_uuid)
        message = json.dumps(instance, sort_keys=True)
        self.assertEqual(0, instance['exit_code'], message)
        self.assertEqual(25, instance['progress'], message)
        self.assertEqual('Twenty-five percent in progress.txt', instance['progress_message'], message)

    @unittest.skipUnless(util.is_cook_executor_in_use(), 'Test assumes the Cook Executor is in use')
    def test_configurable_progress_update_submit(self):
        job_executor_type = util.get_job_executor_type(self.cook_url)
        command = 'echo "message: 25 Twenty-five percent" > progress_file.txt; sleep 1; exit 0'
        job_uuid, resp = util.submit_job(self.cook_url, command=command, executor=job_executor_type, max_runtime=60000,
                                         progress_output_file='progress_file.txt',
                                         progress_regex_string='message: (\d*) (.*)')
        self.assertEqual(201, resp.status_code, msg=resp.content)
        job = util.wait_for_job(self.cook_url, job_uuid, 'completed')
        message = json.dumps(job, sort_keys=True)
        self.assertEqual('progress_file.txt', job['progress_output_file'], message)
        self.assertEqual('message: (\d*) (.*)', job['progress_regex_string'], message)
        self.assertEqual(1, len(job['instances']))
        message = json.dumps(job['instances'][0], sort_keys=True)
        self.assertEqual('success', job['instances'][0]['status'], message)
        self.assertEqual('success', job['instances'][0]['status'], message)

        instance = util.wait_for_sandbox_directory(self.cook_url, job_uuid)
        message = json.dumps(instance, sort_keys=True)
        self.assertIsNotNone(instance['output_url'], message)
        self.assertIsNotNone(instance['sandbox_directory'], message)
        self.assertEqual('cook', instance['executor'])
        util.sleep_for_publish_interval(self.cook_url)
        instance = util.wait_for_exit_code(self.cook_url, job_uuid)
        message = json.dumps(instance, sort_keys=True)
        self.assertEqual(0, instance['exit_code'], message)
        self.assertEqual(25, instance['progress'], message)
        self.assertEqual('Twenty-five percent', instance['progress_message'], message)

    @unittest.skipUnless(util.is_cook_executor_in_use(), 'Test assumes the Cook Executor is in use')
    def test_multiple_progress_updates_submit(self):
        job_executor_type = util.get_job_executor_type(self.cook_url)
        line_1 = util.progress_line(self.cook_url, 25, 'Twenty-five percent')
        line_2 = util.progress_line(self.cook_url, 50, 'Fifty percent')
        line_3 = util.progress_line(self.cook_url, '', 'Sixty percent invalid format')
        line_4 = util.progress_line(self.cook_url, 75, 'Seventy-five percent')
        line_5 = util.progress_line(self.cook_url, '', 'Eighty percent invalid format')
        command = f'echo "{line_1}" && sleep 2 && echo "{line_2}" && sleep 2 && ' \
                  f'echo "{line_3}" && sleep 2 && echo "{line_4}" && sleep 2 && ' \
                  f'echo "{line_5}" && sleep 2 && echo "Done" && exit 0'
        job_uuid, resp = util.submit_job(self.cook_url, command=command, executor=job_executor_type, max_runtime=60000)
        self.assertEqual(201, resp.status_code, msg=resp.content)
        job = util.wait_for_job(self.cook_url, job_uuid, 'completed')
        self.assertEqual(1, len(job['instances']))
        message = json.dumps(job['instances'][0], sort_keys=True)
        self.assertEqual('success', job['instances'][0]['status'], message)

        instance = util.wait_for_sandbox_directory(self.cook_url, job_uuid)
        message = json.dumps(instance, sort_keys=True)
        self.assertIsNotNone(instance['output_url'], message)
        self.assertIsNotNone(instance['sandbox_directory'], message)
        self.assertEqual('cook', instance['executor'])
        util.sleep_for_publish_interval(self.cook_url)
        instance = util.wait_for_exit_code(self.cook_url, job_uuid)
        message = json.dumps(instance, sort_keys=True)
        self.assertEqual(0, instance['exit_code'], message)
        self.assertEqual(75, instance['progress'], message)
        self.assertEqual('Seventy-five percent', instance['progress_message'], message)

    @unittest.skipUnless(util.is_cook_executor_in_use(), 'Test assumes the Cook Executor is in use')
    def test_multiple_rapid_progress_updates_submit(self):
        job_executor_type = util.get_job_executor_type(self.cook_url)

        def progress_string(a):
            return util.progress_line(self.cook_url, a, f'{a}%')

        items = list(range(1, 100, 4)) + list(range(99, 40, -4)) + list(range(40, 81, 2))
        command = ''.join([f'echo "{progress_string(a)}" && ' for a in items]) + 'echo "Done" && exit 0'
        job_uuid, resp = util.submit_job(self.cook_url, command=command, executor=job_executor_type, max_runtime=60000)
        self.assertEqual(201, resp.status_code, msg=resp.content)
        job = util.wait_for_job(self.cook_url, job_uuid, 'completed')
        self.assertEqual(1, len(job['instances']))
        message = json.dumps(job['instances'][0], sort_keys=True)
        self.assertEqual('success', job['instances'][0]['status'], message)

        instance = util.wait_for_sandbox_directory(self.cook_url, job_uuid)
        message = json.dumps(instance, sort_keys=True)
        self.assertIsNotNone(instance['output_url'], message)
        self.assertIsNotNone(instance['sandbox_directory'], message)
        self.assertEqual('cook', instance['executor'])
        util.sleep_for_publish_interval(self.cook_url)
        instance = util.wait_for_exit_code(self.cook_url, job_uuid)
        message = json.dumps(instance, sort_keys=True)
        self.assertEqual(0, instance['exit_code'], message)
        self.assertEqual(80, instance['progress'], message)
        self.assertEqual('80%', instance['progress_message'], message)

    def test_max_runtime_exceeded(self):
        job_executor_type = util.get_job_executor_type(self.cook_url)
        settings_timeout_interval_minutes = util.get_in(util.settings(self.cook_url), 'task-constraints',
                                                        'timeout-interval-minutes')
        # the value needs to be a little more than 2 times settings_timeout_interval_minutes to allow
        # at least two runs of the lingering task killer
        job_sleep_seconds = (2 * settings_timeout_interval_minutes * 60) + 15
        job_sleep_ms = job_sleep_seconds * 1000
        max_runtime_ms = 5000
        assert max_runtime_ms < job_sleep_ms
        # schedule a job that will go over its max_runtime time limit
        job_uuid, resp = util.submit_job(self.cook_url,
                                         command=f'sleep {job_sleep_seconds}',
                                         executor=job_executor_type, max_runtime=max_runtime_ms)
        try:
            self.assertEqual(201, resp.status_code, msg=resp.content)
            # We wait for the job to start running, and only then start waiting for the max-runtime timeout,
            # otherwise we could get a false-negative on wait-for-job 'completed' because of a scheduling delay.
            # We wait for the 'end_time' attribute separately because there is another small delay
            # between the job being killed and that attribute being set.
            # Having three separate waits also disambiguates the root cause of a wait-timeout failure.
            util.wait_for_job(self.cook_url, job_uuid, 'running')
            util.wait_for_job(self.cook_url, job_uuid, 'completed', job_sleep_ms)
            job = util.wait_for_end_time(self.cook_url, job_uuid)
            job_details = f"Job details: {json.dumps(job, sort_keys=True)}"
            self.assertEqual(1, len(job['instances']), job_details)
            instance = job['instances'][0]
            # did the job fail as expected?
            self.assertEqual('failed', instance['status'], job_details)
            # We currently have three possible reason codes that we can observe
            # due to a race in the scheduler. See issue #515 on GitHub for more details.
            allowed_reasons = [
                # observed status update from when Cook killed the job
                reasons.MAX_RUNTIME_EXCEEDED,
                # observed status update received when exit code appears
                reasons.CMD_NON_ZERO_EXIT,
                # cook killed the job during setup, so the executor had an error
                reasons.EXECUTOR_UNREGISTERED]
            self.assertIn(instance['reason_code'], allowed_reasons, job_details)
            # was the actual running time consistent with running over time and being killed?
            actual_running_time_ms = instance['end_time'] - instance['start_time']
            self.assertGreater(actual_running_time_ms, max_runtime_ms, job_details)
            self.assertGreater(job_sleep_ms, actual_running_time_ms, job_details)

            # verify additional fields set when the cook executor is used
            if instance['executor'] == 'cook':
                instance = util.wait_for_sandbox_directory(self.cook_url, job_uuid)
                message = json.dumps(instance, sort_keys=True)
                self.assertIsNotNone(instance['output_url'], message)
                self.assertIsNotNone(instance['sandbox_directory'], message)

                instance = util.wait_for_exit_code(self.cook_url, job_uuid)
                message = json.dumps(instance, sort_keys=True)
                self.assertNotEqual(0, instance['exit_code'], message)
            else:
                self.logger.info(f'Exit code not checked because cook executor was not used for {instance}')
        finally:
            util.kill_jobs(self.cook_url, [job_uuid])

    @staticmethod
    def memory_limit_python_command():
        """Generates a python command that incrementally allocates large strings that cause the python process to
        request more memory than it is allocated."""
        command = 'python3 -c ' \
                  '"import resource; ' \
                  ' import sys; ' \
                  ' sys.stdout.write(\'Starting...\\n\'); ' \
                  ' one_mb = 1024 * 1024; ' \
                  ' [sys.stdout.write(' \
                  '   \'progress: {} iter-{}.{}-mem-{}mB-{}\\n\'.format(' \
                  '      i, ' \
                  '      i, ' \
                  '      len(\' \' * (i * 50 * one_mb)), ' \
                  '      int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / one_mb), ' \
                  '      sys.stdout.flush())) ' \
                  '  for i in range(100)]; ' \
                  ' sys.stdout.write(\'Done.\\n\'); "'
        return command

    @staticmethod
    def memory_limit_script_command():
        """Generates a script command that incrementally allocates large strings that cause the process to
        request more memory than it is allocated."""
        command = 'random_string() { ' \
                  '  base64 /dev/urandom | tr -d \'/+\' | dd bs=1048576 count=1024 2>/dev/null; ' \
                  '}; ' \
                  'R="$(random_string)"; ' \
                  'V=""; ' \
                  'echo "Length of R is ${#R}" ; ' \
                  'for p in `seq 0 99`; do ' \
                  '  for i in `seq 1 10`; do ' \
                  '    V="${V}.${R}"; ' \
                  '    echo "progress: ${p} ${p}-percent iter-${i}" ; ' \
                  '  done ; ' \
                  '  echo "Length of V is ${#V}" ; ' \
                  'done'
        return command

    def memory_limit_exceeded_helper(self, command, executor_type):
        """Given a command that needs more memory than it is allocated, when the command is submitted to cook,
        the job should be killed by Mesos as it exceeds its memory limits."""
        job_uuid, resp = util.submit_job(self.cook_url, command=command, executor=executor_type, mem=128)
        self.assertEqual(201, resp.status_code, msg=resp.content)
        instance = util.wait_for_instance(self.cook_url, job_uuid)
        self.logger.debug('instance: %s' % instance)
        job = instance['parent']
        try:
            job = util.wait_for_job(self.cook_url, job_uuid, 'completed')
            job_details = f"Job details: {json.dumps(job, sort_keys=True)}"
            self.assertEqual('failed', job['state'], job_details)
            self.assertLessEqual(1, len(job['instances']), job_details)
            instance = job['instances'][-1]
            instance_details = json.dumps(instance, sort_keys=True)
            self.logger.debug('instance: %s' % instance)
            # did the job fail as expected?
            self.assertEqual(executor_type, instance['executor'], instance_details)
            self.assertEqual('failed', instance['status'], instance_details)
            # Mesos chooses to kill the task (exit code 137) or kill the executor with a memory limit exceeded message
            if 2002 == instance['reason_code']:
                self.assertEqual('Container memory limit exceeded', instance['reason_string'], instance_details)
            elif 99003 == instance['reason_code']:
                # If the command was killed, it will have exited with either:
                # 137 (Fatal error signal of 128 + SIGKILL)
                # -9 (Negative return values are the signal number which was used to kill the process)
                self.assertEqual('Command exited non-zero', instance['reason_string'], instance_details)
                if executor_type == 'cook':
                    instance = util.wait_for_exit_code(self.cook_url, job_uuid)
                    self.logger.info(f'Exit code: {instance["exit_code"]}')
                    self.assertIn(instance['exit_code'], [137, -9], instance_details)
            else:
                self.fail('Unknown reason code {}, details {}'.format(instance['reason_code'], instance_details))
        finally:
            mesos.dump_sandbox_files(util.session, instance, job)
            util.kill_jobs(self.cook_url, [job_uuid])

    @pytest.mark.memlimit
    @unittest.skipUnless(util.is_cook_executor_in_use(), 'Test assumes the Cook Executor is in use')
    def test_memory_limit_exceeded_cook_python(self):
        command = self.memory_limit_python_command()
        self.memory_limit_exceeded_helper(command, 'cook')

    @pytest.mark.memlimit
    def test_memory_limit_exceeded_mesos_python(self):
        command = self.memory_limit_python_command()
        self.memory_limit_exceeded_helper(command, 'mesos')

    @pytest.mark.memlimit
    @unittest.skipUnless(util.is_cook_executor_in_use(), 'Test assumes the Cook Executor is in use')
    def test_memory_limit_exceeded_cook_script(self):
        command = self.memory_limit_script_command()
        self.memory_limit_exceeded_helper(command, 'cook')

    @pytest.mark.memlimit
    def test_memory_limit_exceeded_mesos_script(self):
        command = self.memory_limit_script_command()
        self.memory_limit_exceeded_helper(command, 'mesos')

    def test_get_job(self):
        # schedule a job
        job_spec = util.minimal_job()
        resp = util.session.post('%s/rawscheduler' % self.cook_url, json={'jobs': [job_spec]})
        self.assertEqual(201, resp.status_code, msg=resp.content)

        # query for the same job & ensure the response has what it's supposed to have
        job = util.wait_for_job(self.cook_url, job_spec['uuid'], 'completed')
        self.assertEqual(job_spec['mem'], job['mem'])
        self.assertEqual(job_spec['max_retries'], job['max_retries'])
        self.assertEqual(job_spec['name'], job['name'])
        self.assertEqual(job_spec['priority'], job['priority'])
        self.assertEqual(job_spec['uuid'], job['uuid'])
        self.assertEqual(job_spec['cpus'], job['cpus'])
        self.assertTrue('labels' in job)
        self.assertEqual(9223372036854775807, job['max_runtime'])
        # 9223372036854775807 is MAX_LONG(ish), the default value for max_runtime
        self.assertEqual('success', job['state'])
        self.assertTrue('env' in job)
        self.assertTrue('framework_id' in job)
        self.assertTrue('ports' in job)
        self.assertTrue('instances' in job)
        self.assertEqual('completed', job['status'])
        self.assertTrue(isinstance(job['submit_time'], int))
        self.assertTrue('uris' in job)
        self.assertTrue('retries_remaining' in job)
        instance = job['instances'][0]
        self.assertTrue(isinstance(instance['start_time'], int))
        self.assertTrue('executor_id' in instance)
        self.assertTrue('hostname' in instance)
        self.assertTrue('slave_id' in instance)
        self.assertTrue(isinstance(instance['preempted'], bool))
        self.assertTrue(isinstance(instance['end_time'], int))
        self.assertTrue(isinstance(instance['backfilled'], bool))
        self.assertTrue('ports' in instance)
        self.assertEqual('completed', job['status'])
        self.assertTrue('task_id' in instance)

    def determine_user(self):
        job_uuid, resp = util.submit_job(self.cook_url)
        self.assertEqual(resp.status_code, 201)
        return util.get_user(self.cook_url, job_uuid)

    def test_list_jobs_by_state(self):
        # schedule a bunch of jobs in hopes of getting jobs into different statuses
        job_specs = [util.minimal_job(command=f"sleep {i}") for i in range(1, 20)]
        try:
            _, resp = util.submit_jobs(self.cook_url, job_specs)
            self.assertEqual(resp.status_code, 201)

            # let some jobs get scheduled
            time.sleep(10)
            user = self.determine_user()

            for state in ['waiting', 'running', 'completed']:
                resp = util.list_jobs(self.cook_url, user=user, state=state)
                self.assertEqual(200, resp.status_code, msg=resp.content)
                jobs = resp.json()
                for job in jobs:
                    self.assertEquals(state, job['status'])
        finally:
            util.kill_jobs(self.cook_url, job_specs)

    def test_list_jobs_by_time(self):
        # schedule two jobs with different submit times
        job_specs = [util.minimal_job() for _ in range(2)]

        request_body = {'jobs': [job_specs[0]]}
        resp = util.session.post('%s/rawscheduler' % self.cook_url, json=request_body)
        self.assertEqual(resp.status_code, 201)

        time.sleep(5)

        request_body = {'jobs': [job_specs[1]]}
        resp = util.session.post('%s/rawscheduler' % self.cook_url, json=request_body)
        self.assertEqual(resp.status_code, 201)

        submit_times = [util.load_job(self.cook_url, job_spec['uuid'])['submit_time'] for job_spec in job_specs]

        user = self.determine_user()

        # start-ms and end-ms are exclusive

        # query where start-ms and end-ms are the submit times of jobs 1 & 2 respectively
        resp = util.list_jobs(self.cook_url, user=user, state='waiting+running+completed',
                              start_ms=submit_times[0] - 1, end_ms=submit_times[1] + 1)
        self.assertEqual(200, resp.status_code, msg=resp.content)
        jobs = resp.json()
        self.assertTrue(util.contains_job_uuid(jobs, job_specs[0]['uuid']))
        self.assertTrue(util.contains_job_uuid(jobs, job_specs[1]['uuid']))

        # query just for job 1
        resp = util.list_jobs(self.cook_url, user=user, state='waiting+running+completed',
                              start_ms=submit_times[0] - 1, end_ms=submit_times[1])
        self.assertEqual(200, resp.status_code, msg=resp.content)
        jobs = resp.json()
        self.assertTrue(util.contains_job_uuid(jobs, job_specs[0]['uuid']))
        self.assertFalse(util.contains_job_uuid(jobs, job_specs[1]['uuid']))

        # query just for job 2
        resp = util.list_jobs(self.cook_url, user=user, state='waiting+running+completed',
                              start_ms=submit_times[0] + 1, end_ms=submit_times[1] + 1)
        self.assertEqual(200, resp.status_code, msg=resp.content)
        jobs = resp.json()
        self.assertFalse(util.contains_job_uuid(jobs, job_specs[0]['uuid']))
        self.assertTrue(util.contains_job_uuid(jobs, job_specs[1]['uuid']))

        # query for neither
        resp = util.list_jobs(self.cook_url, user=user, state='waiting+running+completed',
                              start_ms=submit_times[0] + 1, end_ms=submit_times[1])
        self.assertEqual(200, resp.status_code, msg=resp.content)
        jobs = resp.json()
        self.assertFalse(util.contains_job_uuid(jobs, job_specs[0]['uuid']))
        self.assertFalse(util.contains_job_uuid(jobs, job_specs[1]['uuid']))

    def test_list_jobs_by_completion_state(self):
        name = str(uuid.uuid4())
        job_uuid_1, resp = util.submit_job(self.cook_url, command='true', name=name, max_retries=2)
        self.assertEqual(201, resp.status_code)
        job_uuid_2, resp = util.submit_job(self.cook_url, command='false', name=name)
        self.assertEqual(201, resp.status_code)
        job_uuid_3, resp = util.submit_job(self.cook_url, command='true', name=name, max_retries=2)
        self.assertEqual(201, resp.status_code)
        user = self.determine_user()
        start = util.wait_for_job(self.cook_url, job_uuid_1, 'completed')['submit_time']
        end = util.wait_for_job(self.cook_url, job_uuid_2, 'completed')['submit_time'] + 1

        # Test the various combinations of states
        resp = util.list_jobs(self.cook_url, user=user, state='completed', start_ms=start, end_ms=end)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        resp = util.list_jobs(self.cook_url, user=user, state='success+failed', start_ms=start, end_ms=end)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        resp = util.list_jobs(self.cook_url, user=user, state='completed+success+failed', start_ms=start, end_ms=end)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        resp = util.list_jobs(self.cook_url, user=user, state='completed+failed', start_ms=start, end_ms=end)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        resp = util.list_jobs(self.cook_url, user=user, state='completed+success', start_ms=start, end_ms=end)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        resp = util.list_jobs(self.cook_url, user=user, state='success', start_ms=start, end_ms=end)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        resp = util.list_jobs(self.cook_url, user=user, state='running+waiting+success', start_ms=start, end_ms=end)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        resp = util.list_jobs(self.cook_url, user=user, state='failed', start_ms=start, end_ms=end)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        resp = util.list_jobs(self.cook_url, user=user, state='running+waiting+failed', start_ms=start, end_ms=end)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)

        # Test with failed and a specified limit
        resp = util.list_jobs(self.cook_url, user=user, state='failed', start_ms=start, end_ms=end, limit=1, name=name)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)

        # Test with success and a specified limit
        start = end - 1
        end = util.wait_for_job(self.cook_url, job_uuid_3, 'completed')['submit_time'] + 1
        resp = util.list_jobs(self.cook_url, user=user, state='success', start_ms=start, end_ms=end, limit=1, name=name)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_3), job_uuid_3)

    def test_list_jobs_by_completion_state_with_jobs_endpoint(self):
        name = str(uuid.uuid4())
        job_uuid_1, resp = util.submit_job(self.cook_url, command='true', name=name, max_retries=2)
        self.assertEqual(201, resp.status_code)
        job_uuid_2, resp = util.submit_job(self.cook_url, command='false', name=name)
        self.assertEqual(201, resp.status_code)
        job_uuid_3, resp = util.submit_job(self.cook_url, command='true', name=name, max_retries=2)
        self.assertEqual(201, resp.status_code)
        user = self.determine_user()
        start = util.wait_for_job(self.cook_url, job_uuid_1, 'completed')['submit_time']
        end = util.wait_for_job(self.cook_url, job_uuid_2, 'completed')['submit_time'] + 1

        # Assert the job states
        job_1 = util.load_job(self.cook_url, job_uuid_1)
        job_2 = util.load_job(self.cook_url, job_uuid_2)
        self.logger.info(job_1)
        self.logger.info(job_2)
        self.assertEqual('success', job_1['state'])
        self.assertEqual('failed', job_2['state'])

        # Test the various combinations of states
        resp = util.jobs(self.cook_url, user=user, state='completed', start=start, end=end)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        resp = util.jobs(self.cook_url, user=user, state=['success', 'failed'], start=start, end=end)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        resp = util.jobs(self.cook_url, user=user, state=['completed', 'success', 'failed'], start=start, end=end)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        resp = util.jobs(self.cook_url, user=user, state=['completed', 'failed'], start=start, end=end)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        resp = util.jobs(self.cook_url, user=user, state=['completed', 'success'], start=start, end=end)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        resp = util.jobs(self.cook_url, user=user, state='success', start=start, end=end)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        resp = util.jobs(self.cook_url, user=user, state=['running', 'waiting', 'success'], start=start, end=end)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        resp = util.jobs(self.cook_url, user=user, state='failed', start=start, end=end)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        resp = util.jobs(self.cook_url, user=user, state=['running', 'waiting', 'failed'], start=start, end=end)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)

        # Test with failed and a specified limit
        resp = util.jobs(self.cook_url, user=user, state='failed', start=start, end=end, limit=1, name=name)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)

        # Test with success and a specified limit
        start = end - 1
        job_3 = util.wait_for_job(self.cook_url, job_uuid_3, 'completed')
        self.logger.info(job_3)
        self.assertEqual('success', job_3['state'])
        end = job_3['submit_time'] + 1
        resp = util.jobs(self.cook_url, user=user, state='success', start=start, end=end, limit=1, name=name)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_3), job_uuid_3)

    def test_list_jobs_by_name(self):
        job_uuid_1, resp = util.submit_job(self.cook_url, name='foo-bar-baz_qux')
        self.assertEqual(201, resp.status_code)
        job_uuid_2, resp = util.submit_job(self.cook_url, name='')
        self.assertEqual(201, resp.status_code)
        job_uuid_3, resp = util.submit_job(self.cook_url, name='.')
        self.assertEqual(201, resp.status_code)
        job_uuid_4, resp = util.submit_job(self.cook_url, name='foo-bar-baz__')
        self.assertEqual(201, resp.status_code)
        job_uuid_5, resp = util.submit_job(self.cook_url, name='ff')
        self.assertEqual(201, resp.status_code)
        job_uuid_6, resp = util.submit_job(self.cook_url, name='a')
        self.assertEqual(201, resp.status_code)
        user = self.determine_user()
        any_state = 'running+waiting+completed'

        resp = util.list_jobs(self.cook_url, user=user, state=any_state)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_3), job_uuid_3)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_4), job_uuid_4)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_5), job_uuid_5)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_6), job_uuid_6)
        resp = util.list_jobs(self.cook_url, user=user, state=any_state, name='*')
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_3), job_uuid_3)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_4), job_uuid_4)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_5), job_uuid_5)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_6), job_uuid_6)
        resp = util.list_jobs(self.cook_url, user=user, state=any_state, name='foo-bar-baz_qux')
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_3), job_uuid_3)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_4), job_uuid_4)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_5), job_uuid_5)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_6), job_uuid_6)
        resp = util.list_jobs(self.cook_url, user=user, state=any_state, name='foo-bar-baz_*')
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_3), job_uuid_3)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_4), job_uuid_4)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_5), job_uuid_5)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_6), job_uuid_6)
        resp = util.list_jobs(self.cook_url, user=user, state=any_state, name='f*')
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_3), job_uuid_3)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_4), job_uuid_4)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_5), job_uuid_5)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_6), job_uuid_6)
        resp = util.list_jobs(self.cook_url, user=user, state=any_state, name='')
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_3), job_uuid_3)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_4), job_uuid_4)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_5), job_uuid_5)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_6), job_uuid_6)
        resp = util.list_jobs(self.cook_url, user=user, state=any_state, name='*.*')
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_3), job_uuid_3)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_4), job_uuid_4)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_5), job_uuid_5)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_6), job_uuid_6)
        resp = util.list_jobs(self.cook_url, user=user, state=any_state, name='foo-bar-baz__*')
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_3), job_uuid_3)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_4), job_uuid_4)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_5), job_uuid_5)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_6), job_uuid_6)
        resp = util.list_jobs(self.cook_url, user=user, state=any_state, name='ff*')
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_3), job_uuid_3)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_4), job_uuid_4)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_5), job_uuid_5)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_6), job_uuid_6)
        resp = util.list_jobs(self.cook_url, user=user, state=any_state, name='a*')
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_3), job_uuid_3)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_4), job_uuid_4)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_5), job_uuid_5)
        self.assertTrue(util.contains_job_uuid(resp.json(), job_uuid_6), job_uuid_6)
        resp = util.list_jobs(self.cook_url, user=user, state=any_state, name='a-z0-9_-')
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_1), job_uuid_1)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_2), job_uuid_2)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_3), job_uuid_3)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_4), job_uuid_4)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_5), job_uuid_5)
        self.assertFalse(util.contains_job_uuid(resp.json(), job_uuid_6), job_uuid_6)

    def test_list_jobs_by_pool(self):
        # Submit two jobs to each active pool -- one that will be
        # running or waiting for a while, and another that will complete
        jobs = []
        name = str(uuid.uuid4())
        pools, _ = util.active_pools(self.cook_url)
        start = util.current_milli_time()
        sleep_command = 'sleep 600'
        for pool in pools:
            pool_name = pool['name']
            job_uuid, resp = util.submit_job(self.cook_url, pool=pool_name, name=name, command=sleep_command)
            self.assertEqual(201, resp.status_code)
            jobs.append(util.load_job(self.cook_url, job_uuid))
            job_uuid, resp = util.submit_job(self.cook_url, pool=pool_name, name=name, command='exit 0')
            self.assertEqual(201, resp.status_code)
            jobs.append(util.wait_for_job(self.cook_url, job_uuid, 'completed'))
        end = util.current_milli_time() + 1

        try:
            completed = ['completed']
            active = ['running', 'waiting']
            user = self.determine_user()

            # List jobs for each pool
            for pool in pools:
                pool_name = pool['name']

                # List running / waiting
                job_uuid = next(j['uuid'] for j in jobs if j['pool'] == pool_name and j['command'] == sleep_command)
                resp = util.jobs(self.cook_url, user=user, state=active,
                                 start=start, end=end, name=name, pool=pool_name)
                self.assertEqual(200, resp.status_code, resp.text)
                self.assertEqual(1, len(resp.json()))
                self.assertEqual(job_uuid, resp.json()[0]['uuid'])
                resp = util.jobs(self.cook_url, headers={'x-cook-pool': pool_name}, user=user,
                                 state=active, start=start, end=end, name=name)
                self.assertEqual(200, resp.status_code, resp.text)
                self.assertEqual(1, len(resp.json()))
                self.assertEqual(job_uuid, resp.json()[0]['uuid'])

                # List completed
                job_uuid = next(j['uuid'] for j in jobs if j['pool'] == pool_name and j['command'] == 'exit 0')
                resp = util.jobs(self.cook_url, user=user, state=completed,
                                 start=start, end=end, name=name, pool=pool_name)
                self.assertEqual(200, resp.status_code, resp.text)
                self.assertEqual(1, len(resp.json()), json.dumps(resp.json(), indent=2))
                self.assertEqual(job_uuid, resp.json()[0]['uuid'])
                resp = util.jobs(self.cook_url, headers={'x-cook-pool': pool_name}, user=user,
                                 state=completed, start=start, end=end, name=name)
                self.assertEqual(200, resp.status_code, resp.text)
                self.assertEqual(1, len(resp.json()), json.dumps(resp.json(), indent=2))
                self.assertEqual(job_uuid, resp.json()[0]['uuid'])

                # List all states
                job_uuids = [j['uuid'] for j in jobs if j['pool'] == pool_name]
                resp = util.jobs(self.cook_url, user=user, state=completed + active,
                                 start=start, end=end, name=name, pool=pool_name)
                self.assertEqual(200, resp.status_code, resp.text)
                self.assertEqual(2, len(resp.json()), json.dumps(resp.json(), indent=2))
                self.assertEqual(sorted(job_uuids), sorted(j['uuid'] for j in resp.json()))
                resp = util.jobs(self.cook_url, headers={'x-cook-pool': pool_name}, user=user,
                                 state=completed + active, start=start, end=end, name=name)
                self.assertEqual(200, resp.status_code, resp.text)
                self.assertEqual(2, len(resp.json()), json.dumps(resp.json(), indent=2))
                self.assertEqual(sorted(job_uuids), sorted(j['uuid'] for j in resp.json()))

            if len(pools) > 0:
                # List running / waiting with no pool should return all running / waiting
                resp = util.jobs(self.cook_url, user=user, state=active, start=start, end=end, name=name)
                job_uuids = [j['uuid'] for j in jobs if j['command'] == sleep_command]
                self.assertEqual(200, resp.status_code)
                self.assertEqual(sorted(job_uuids), sorted([j['uuid'] for j in resp.json()]))

                # List completed with no pool should return all completed
                resp = util.jobs(self.cook_url, user=user, state=completed, start=start, end=end, name=name)
                job_uuids = [j['uuid'] for j in jobs if j['command'] == 'exit 0']
                self.assertEqual(200, resp.status_code)
                self.assertEqual(sorted(job_uuids), sorted([j['uuid'] for j in resp.json()]))

                # List all states with no pool should return all jobs
                resp = util.jobs(self.cook_url, user=user, state=completed + active, start=start, end=end, name=name)
                self.assertEqual(200, resp.status_code)
                self.assertEqual(sorted(j['uuid'] for j in jobs), sorted([j['uuid'] for j in resp.json()]))
        finally:
            if len(jobs) > 0:
                util.kill_jobs(self.cook_url, jobs)

        # List running / waiting with a bogus pool
        resp = util.jobs(self.cook_url, user=user, state=active, start=start, end=end, pool=uuid.uuid4())
        self.assertEqual(200, resp.status_code)
        self.assertEqual(0, len(resp.json()))

        # List completed with a bogus pool
        resp = util.jobs(self.cook_url, user=user, state=completed, start=start, end=end, pool=uuid.uuid4())
        self.assertEqual(200, resp.status_code)
        self.assertEqual(0, len(resp.json()))

    def test_list_with_invalid_name_filters(self):
        user = self.determine_user()
        any_state = 'running+waiting+completed'
        resp = util.list_jobs(self.cook_url, user=user, state=any_state, name='^[a-z0-9_-]{3,16}$')
        self.assertEqual(400, resp.status_code)
        resp = util.list_jobs(self.cook_url, user=user, state=any_state, name='[a-z0-9_-]{3,16}')
        self.assertEqual(400, resp.status_code)
        resp = util.list_jobs(self.cook_url, user=user, state=any_state, name='[a-z0-9_-]')
        self.assertEqual(400, resp.status_code)
        resp = util.list_jobs(self.cook_url, user=user, state=any_state, name='\d+')
        self.assertEqual(400, resp.status_code)
        resp = util.list_jobs(self.cook_url, user=user, state=any_state, name='a+')
        self.assertEqual(400, resp.status_code)

    def test_cancel_job(self):
        job_uuid, _ = util.submit_job(self.cook_url, command='sleep 300')
        util.wait_for_job(self.cook_url, job_uuid, 'running')
        util.kill_jobs(self.cook_url, [job_uuid])
        job = util.load_job(self.cook_url, job_uuid)
        self.assertEqual('failed', job['state'])

    def test_change_retries(self):
        job_uuid, _ = util.submit_job(self.cook_url, command='sleep 60')
        try:
            util.wait_for_job(self.cook_url, job_uuid, 'running')
            util.kill_jobs(self.cook_url, [job_uuid])

            def instance_query():
                return util.query_jobs(self.cook_url, True, uuid=[job_uuid])

            # wait for the job (and its instances) to die
            util.wait_until(instance_query, util.all_instances_killed)
            # retry the job
            resp = util.retry_jobs(self.cook_url, retries=2, jobs=[job_uuid])
            self.assertEqual(201, resp.status_code, resp.text)
            job = util.load_job(self.cook_url, job_uuid)
            details = f"Job details: {json.dumps(job, sort_keys=True)}"
            self.assertIn(job['status'], ['waiting', 'running'], details)
            self.assertEqual(1, job['retries_remaining'], details)
        finally:
            util.kill_jobs(self.cook_url, [job_uuid])

    def test_change_retries_deprecated_post(self):
        job_uuid, _ = util.submit_job(self.cook_url, command='sleep 60')
        try:
            util.wait_for_job(self.cook_url, job_uuid, 'running')
            util.kill_jobs(self.cook_url, [job_uuid])

            def instance_query():
                return util.query_jobs(self.cook_url, True, uuid=[job_uuid])

            # wait for the job (and its instances) to die
            util.wait_until(instance_query, util.all_instances_killed)
            # retry the job
            resp = util.retry_jobs(self.cook_url, use_deprecated_post=True, retries=2, jobs=[job_uuid])
            self.assertEqual(201, resp.status_code, resp.text)
            job = util.load_job(self.cook_url, job_uuid)
            details = f"Job details: {json.dumps(job, sort_keys=True)}"
            self.assertIn(job['status'], ['waiting', 'running'], details)
            self.assertEqual(1, job['retries_remaining'], details)
        finally:
            util.kill_jobs(self.cook_url, [job_uuid])

    def test_change_failed_retries(self):
        job_specs = util.minimal_jobs(2, max_retries=1, command='sleep 60')
        try:
            jobs, resp = util.submit_jobs(self.cook_url, job_specs)
            self.assertEqual(resp.status_code, 201)
            # wait for first job to start running, and kill it
            job_uuid = jobs[0]
            util.wait_for_job(self.cook_url, job_uuid, 'running')
            util.kill_jobs(self.cook_url, [job_uuid])

            def instance_query():
                return util.query_jobs(self.cook_url, True, uuid=[job_uuid])

            # Wait for the job (and its instances) to die
            util.wait_until(instance_query, util.all_instances_killed)
            job = util.load_job(self.cook_url, job_uuid)
            self.assertEqual('failed', job['state'])
            # retry both jobs, but with the failed_only=true flag
            resp = util.retry_jobs(self.cook_url, retries=4, failed_only=True, jobs=jobs)
            self.assertEqual(201, resp.status_code, resp.text)
            jobs = util.query_jobs(self.cook_url, True, uuid=jobs).json()
            # We expect both jobs to be running now.
            # The first job (which we killed and retried) should have 3 retries remaining
            # (the attempt before resetting the total retries count is still included).
            job_details = f"Job details: {json.dumps(jobs[0], sort_keys=True)}"
            self.assertIn(jobs[0]['status'], ['waiting', 'running'], job_details)
            self.assertEqual(3, jobs[0]['retries_remaining'], job_details)
            # The second job (which started with the default 1 retries)
            # should have 1 remaining since the failed_only flag was set.
            job_details = f"Job details: {json.dumps(jobs[1], sort_keys=True)}"
            self.assertIn(jobs[1]['status'], ['waiting', 'running'], job_details)
            self.assertEqual(1, jobs[1]['retries_remaining'], job_details)
        finally:
            util.kill_jobs(self.cook_url, job_specs)

    def test_cancel_instance(self):
        job_uuid, _ = util.submit_job(self.cook_url, command='sleep 10', max_retries=2)
        job = util.wait_for_job(self.cook_url, job_uuid, 'running')
        task_id = job['instances'][0]['task_id']
        resp = util.session.delete('%s/rawscheduler?instance=%s' % (self.cook_url, task_id))
        self.assertEqual(204, resp.status_code, msg=resp.content)
        job = util.wait_for_job(self.cook_url, job_uuid, 'completed')
        self.assertEqual('success', job['state'], 'Job details: %s' % (json.dumps(job, sort_keys=True)))

    def test_no_such_group(self):
        group_uuid = str(uuid.uuid4())
        resp = util.query_groups(self.cook_url, uuid=[group_uuid])
        self.assertEqual(resp.status_code, 404, resp)
        resp_data = resp.json()
        resp_string = json.dumps(resp_data, sort_keys=True)
        self.assertIn('error', resp_data, resp_string)
        self.assertIn(group_uuid, resp_data['error'], resp_string)

    def test_implicit_group(self):
        group_uuid = str(uuid.uuid4())
        job_spec = {'group': group_uuid}
        jobs, resp = util.submit_jobs(self.cook_url, job_spec, 2)
        self.assertEqual(resp.status_code, 201)
        job_data = util.query_jobs(self.cook_url, uuid=jobs)
        self.assertEqual(200, job_data.status_code)
        job_data = job_data.json()
        self.assertEqual(group_uuid, job_data[0]['groups'][0]['uuid'])
        self.assertEqual(group_uuid, job_data[1]['groups'][0]['uuid'])
        util.wait_for_job(self.cook_url, jobs[0], 'completed')
        util.wait_for_job(self.cook_url, jobs[1], 'completed')

    def test_explicit_group(self):
        group_spec = util.minimal_group()
        group_uuid = group_spec['uuid']
        job_spec = {'group': group_uuid}
        jobs, resp = util.submit_jobs(self.cook_url, job_spec, 2, groups=[group_spec])
        self.assertEqual(resp.status_code, 201)
        job_data = util.query_jobs(self.cook_url, uuid=jobs)
        self.assertEqual(200, job_data.status_code)
        job_data = job_data.json()
        self.assertEqual(group_uuid, job_data[0]['groups'][0]['uuid'])
        self.assertEqual(group_uuid, job_data[1]['groups'][0]['uuid'])
        util.wait_for_job(self.cook_url, jobs[0], 'completed')
        util.wait_for_job(self.cook_url, jobs[1], 'completed')

    def test_straggler_handling(self):
        straggler_handling = {
            'type': 'quantile-deviation',
            'parameters': {
                'quantile': 0.5,
                'multiplier': 2.0
            }
        }
        slow_job_wait_seconds = 1200
        group_spec = util.minimal_group(straggler_handling=straggler_handling)
        job_fast = util.minimal_job(group=group_spec["uuid"])
        job_slow = util.minimal_job(group=group_spec["uuid"],
                                    command='sleep %d' % slow_job_wait_seconds)
        data = {'jobs': [job_fast, job_slow], 'groups': [group_spec]}
        resp = util.session.post('%s/rawscheduler' % self.cook_url, json=data)
        self.assertEqual(resp.status_code, 201)
        util.wait_for_job(self.cook_url, job_fast['uuid'], 'completed')
        util.wait_for_job(self.cook_url, job_slow['uuid'], 'completed',
                          slow_job_wait_seconds * 1000)
        jobs = util.query_jobs(self.cook_url, True, uuid=[job_fast, job_slow]).json()
        self.logger.debug('Loaded jobs %s', jobs)
        self.assertEqual('success', jobs[0]['state'], 'Job details: %s' % (json.dumps(jobs[0], sort_keys=True)))
        self.assertEqual('failed', jobs[1]['state'])
        self.assertEqual(2004, jobs[1]['instances'][0]['reason_code'])

    def test_expected_runtime_field(self):
        # Should support expected_runtime
        expected_runtime = 1
        job_uuid, resp = util.submit_job(self.cook_url, expected_runtime=expected_runtime)
        self.assertEqual(resp.status_code, 201)
        job = util.load_job(self.cook_url, job_uuid)
        self.assertEqual(expected_runtime, job['expected_runtime'])

        # Should disallow expected_runtime > max_runtime
        expected_runtime = 2
        max_runtime = expected_runtime - 1
        job_uuid, resp = util.submit_job(self.cook_url, expected_runtime=expected_runtime, max_runtime=max_runtime)
        self.assertEqual(resp.status_code, 400)

    def test_application_field(self):
        # Should support application
        application = {'name': 'foo-app', 'version': '0.1.0'}
        job_uuid, resp = util.submit_job(self.cook_url, application=application)
        self.assertEqual(resp.status_code, 201)
        job = util.load_job(self.cook_url, job_uuid)
        self.assertEqual(application, job['application'])

        # Should require application name
        _, resp = util.submit_job(self.cook_url, application={'version': '0.1.0'})
        self.assertEqual(resp.status_code, 400)

        # Should require application version
        _, resp = util.submit_job(self.cook_url, application={'name': 'foo-app'})
        self.assertEqual(resp.status_code, 400)

    def test_error_while_creating_job(self):
        job1 = util.minimal_job()
        job2 = util.minimal_job(uuid=job1['uuid'])
        resp = util.session.post('%s/rawscheduler' % self.cook_url,
                                 json={'jobs': [job1, job2]})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(f'Duplicate job uuids: ["{job1["uuid"]}"]', resp.json()['error'], resp.text)

    @unittest.skipIf(util.has_ephemeral_hosts(), util.EPHEMERAL_HOSTS_SKIP_REASON)
    def test_hostname_equals_job_constraint(self):
        hosts = util.hosts_to_consider(self.cook_url, self.mesos_url)[:10]

        bad_job_uuid, resp = util.submit_job(self.cook_url, constraints=[["HOSTNAME",
                                                                          "EQUALS",
                                                                          "lol won't get scheduled"]])
        self.assertEqual(resp.status_code, 201, resp.text)

        try:
            host_to_job_uuid = {}
            for host in hosts:
                hostname = host['hostname']
                constraints = [["HOSTNAME", "EQUALS", hostname]]
                job_uuid, resp = util.submit_job(self.cook_url, constraints=constraints, name=self.current_name())
                self.assertEqual(resp.status_code, 201, resp.text)
                host_to_job_uuid[hostname] = job_uuid

            try:
                for hostname, job_uuid in host_to_job_uuid.items():
                    self.logger.info(f'Waiting for job {job_uuid} to complete')
                    job = util.wait_for_job(self.cook_url, job_uuid, 'completed')
                    hostname_constrained = job['instances'][0]['hostname']
                    self.assertEqual(hostname, hostname_constrained)
                    self.assertEqual([["HOSTNAME", "EQUALS", hostname]], job['constraints'])
                # This job should have been scheduled since the job submitted after it has completed
                # however, its constraint means it won't get scheduled
                self.logger.info(f'Waiting for job {bad_job_uuid} to be waiting')
                util.wait_for_job(self.cook_url, bad_job_uuid, 'waiting', max_wait_ms=3000)
            finally:
                util.kill_jobs(self.cook_url, list(host_to_job_uuid.values()))
        finally:
            util.kill_jobs(self.cook_url, [bad_job_uuid])

    def test_allow_partial(self):
        def absent_uuids(response):
            return [part for part in response.json()['error'].split() if util.is_valid_uuid(part)]

        job_uuid_1, resp = util.submit_job(self.cook_url)
        self.assertEqual(201, resp.status_code, msg=resp.content)
        job_uuid_2, resp = util.submit_job(self.cook_url)
        self.assertEqual(201, resp.status_code, msg=resp.content)

        # Only valid job uuids
        resp = util.query_jobs(self.cook_url, uuid=[job_uuid_1, job_uuid_2])
        self.assertEqual(200, resp.status_code, msg=resp.content)

        # Mixed valid, invalid job uuids
        bogus_uuid = str(uuid.uuid4())
        resp = util.query_jobs(self.cook_url, uuid=[job_uuid_1, job_uuid_2, bogus_uuid])
        self.assertEqual(404, resp.status_code, msg=resp.content)
        self.assertEqual([bogus_uuid], absent_uuids(resp))
        resp = util.query_jobs(self.cook_url, uuid=[job_uuid_1, job_uuid_2, bogus_uuid], partial=False)
        self.assertEqual(404, resp.status_code, resp.json())
        self.assertEqual([bogus_uuid], absent_uuids(resp))

        # Partial results with mixed valid, invalid job uuids
        resp = util.query_jobs(self.cook_url, uuid=[job_uuid_1, job_uuid_2, bogus_uuid], partial=True)
        self.assertEqual(200, resp.status_code, resp.json())
        self.assertEqual(2, len(resp.json()))
        self.assertEqual([job_uuid_1, job_uuid_2].sort(), [job['uuid'] for job in resp.json()].sort())

        # Only valid instance uuids
        instance_uuid_1 = util.wait_for_instance(self.cook_url, job_uuid_1)['task_id']
        instance_uuid_2 = util.wait_for_instance(self.cook_url, job_uuid_2)['task_id']
        resp = util.query_instances(self.cook_url, uuid=[instance_uuid_1, instance_uuid_2])
        self.assertEqual(200, resp.status_code, msg=resp.content)
        self.assertEqual(2, len(resp.json()))
        self.assertEqual([job_uuid_1, job_uuid_2].sort(), [instance['job']['uuid'] for instance in resp.json()].sort())

        # Mixed valid, invalid instance uuids
        instance_uuids = [instance_uuid_1, instance_uuid_2, bogus_uuid]
        resp = util.query_instances(self.cook_url, uuid=instance_uuids)
        self.assertEqual(404, resp.status_code, msg=resp.content)
        self.assertEqual([bogus_uuid], absent_uuids(resp))
        resp = util.query_instances(self.cook_url, uuid=instance_uuids, partial=False)
        self.assertEqual(404, resp.status_code, msg=resp.content)
        self.assertEqual([bogus_uuid], absent_uuids(resp))

        # Partial results with mixed valid, invalid instance uuids
        resp = util.query_instances(self.cook_url, uuid=instance_uuids, partial=True)
        self.assertEqual(200, resp.status_code, msg=resp.content)
        self.assertEqual(2, len(resp.json()))
        self.assertEqual([job_uuid_1, job_uuid_2].sort(), [instance['job']['uuid'] for instance in resp.json()].sort())

    def test_ports(self):
        job_uuid, resp = util.submit_job(self.cook_url, ports=1)
        instance = util.wait_for_instance(self.cook_url, job_uuid)
        self.assertEqual(1, len(instance['ports']))
        job_uuid, resp = util.submit_job(self.cook_url, ports=10)
        instance = util.wait_for_instance(self.cook_url, job_uuid)
        self.assertEqual(10, len(instance['ports']))

    def test_allow_partial_for_groups(self):
        def absent_uuids(response):
            return [part for part in response.json()['error'].split() if util.is_valid_uuid(part)]

        group_uuid_1 = str(uuid.uuid4())
        group_uuid_2 = str(uuid.uuid4())
        _, resp = util.submit_job(self.cook_url, group=group_uuid_1)
        self.assertEqual(201, resp.status_code)
        _, resp = util.submit_job(self.cook_url, group=group_uuid_2)
        self.assertEqual(201, resp.status_code)

        # Only valid group uuids
        resp = util.query_groups(self.cook_url, uuid=[group_uuid_1, group_uuid_2])
        self.assertEqual(200, resp.status_code)

        # Mixed valid, invalid group uuids
        bogus_uuid = str(uuid.uuid4())
        resp = util.query_groups(self.cook_url, uuid=[group_uuid_1, group_uuid_2, bogus_uuid])
        self.assertEqual(404, resp.status_code)
        self.assertEqual([bogus_uuid], absent_uuids(resp))
        resp = util.query_groups(self.cook_url, uuid=[group_uuid_1, group_uuid_2, bogus_uuid], partial='false')
        self.assertEqual(404, resp.status_code, resp.json())
        self.assertEqual([bogus_uuid], absent_uuids(resp))

        # Partial results with mixed valid, invalid job uuids
        resp = util.query_groups(self.cook_url, uuid=[group_uuid_1, group_uuid_2, bogus_uuid], partial='true')
        self.assertEqual(200, resp.status_code, resp.json())
        self.assertEqual(2, len(resp.json()))
        self.assertEqual([group_uuid_1, group_uuid_2].sort(), [group['uuid'] for group in resp.json()].sort())

    def test_detailed_for_groups(self):
        detail_keys = ('waiting', 'running', 'completed')

        group_uuid = str(uuid.uuid4())
        _, resp = util.submit_job(self.cook_url, group=group_uuid)
        self.assertEqual(201, resp.status_code)

        # Cases with no details (default or detailed=false)
        for params in ({}, {'detailed': 'false'}):
            resp = util.query_groups(self.cook_url, uuid=[group_uuid], **params)
            self.assertEqual(200, resp.status_code)
            group_info = resp.json()[0]
            for k in detail_keys:
                self.assertNotIn(k, group_info)

        # Case with details (detailed=true)
        resp = util.query_groups(self.cook_url, uuid=[group_uuid], detailed='true')
        self.assertEqual(200, resp.status_code)
        group_info = resp.json()[0]
        for k in detail_keys:
            self.assertIn(k, group_info)

    def test_group_kill_simple(self):
        # Create and submit jobs in group
        slow_job_wait_seconds = 1200
        group_spec = util.minimal_group()
        group_uuid = group_spec['uuid']
        try:
            job_fast = util.minimal_job(group=group_uuid, priority=99, max_retries=2)
            job_slow = util.minimal_job(group=group_uuid, command=f'sleep {slow_job_wait_seconds}', max_retries=2)
            _, resp = util.submit_jobs(self.cook_url, [job_fast, job_slow], groups=[group_spec])
            self.assertEqual(resp.status_code, 201, resp.text)
            # Wait for the fast job to finish, and the slow job to start
            util.wait_for_job(self.cook_url, job_fast, 'completed')
            job = util.load_job(self.cook_url, job_fast['uuid'])
            self.assertEqual('success', job['state'], f"Job details: {json.dumps(job, sort_keys=True)}")
            util.wait_for_job(self.cook_url, job_slow, 'running')
            # Now try to cancel the group (just the slow job)
            util.kill_groups(self.cook_url, [group_uuid])

            # Wait for the slow job (and its instance) to die
            def query():
                return util.query_jobs(self.cook_url, True, uuid=[job_slow])

            util.wait_until(query, util.all_instances_killed)
            # The fast job should have Success, slow job Failed (because we killed it)
            jobs = util.query_jobs(self.cook_url, True, uuid=[job_fast, job_slow]).json()
            self.assertEqual('success', jobs[0]['state'], f"Job details: {json.dumps(jobs[0], sort_keys=True)}")
            slow_job_details = f"Job details: {json.dumps(jobs[1], sort_keys=True)}"
            self.assertEqual('failed', jobs[1]['state'], slow_job_details)
            valid_reasons = [
                # cook killed the job, so it exits non-zero
                reasons.CMD_NON_ZERO_EXIT,
                # cook killed the job during setup, so the executor had an error
                reasons.EXECUTOR_UNREGISTERED,
                # we've seen this happen in the wild
                reasons.UNKNOWN_MESOS_REASON
            ]
            self.assertIn(jobs[1]['instances'][0]['reason_code'], valid_reasons, slow_job_details)
        finally:
            # Now try to kill the group again
            # (ensure it still works when there are no live jobs)
            util.kill_groups(self.cook_url, [group_uuid])

    def test_group_kill_multi(self):
        # Create and submit jobs in group
        slow_job_wait_seconds = 1200
        group_spec = util.minimal_group()
        group_uuid = group_spec['uuid']
        job_spec = {'group': group_uuid, 'command': f'sleep {slow_job_wait_seconds}'}
        try:
            jobs, resp = util.submit_jobs(self.cook_url, job_spec, 10, groups=[group_spec])
            self.assertEqual(resp.status_code, 201)

            # Wait for some job to start
            def some_job_started(group_response):
                group = group_response.json()[0]
                running_count = group['running']
                self.logger.info(f"Currently {running_count} jobs running in group {group_uuid}")
                return running_count > 0

            def group_detail_query():
                response = util.query_groups(self.cook_url, uuid=[group_uuid], detailed='true')
                self.assertEqual(200, response.status_code)
                return response

            util.wait_until(group_detail_query, some_job_started)
            # Now try to kill the whole group
            util.kill_groups(self.cook_url, [group_uuid])

            # Wait for all the jobs to die
            # Ensure that each job Failed (because we killed it)
            def query():
                return util.query_jobs(self.cook_url, True, uuid=jobs)

            util.wait_until(query, util.all_instances_killed)
        finally:
            # Now try to kill the group again
            # (ensure it still works when there are no live jobs)
            util.kill_groups(self.cook_url, [group_uuid])

    def test_group_change_killed_retries(self):
        jobs = util.group_submit_kill_retry(self.cook_url, retry_failed_jobs_only=False)
        # ensure none of the jobs are still in a failed state
        for job in jobs:
            self.assertNotEqual('failed', job['state'], f'Job details: {json.dumps(job, sort_keys=True)}')

    def test_group_change_killed_retries_failed_only(self):
        jobs = util.group_submit_kill_retry(self.cook_url, retry_failed_jobs_only=True)
        # ensure none of the jobs are still in a failed state
        for job in jobs:
            self.assertNotEqual('failed', job['state'], f'Job details: {json.dumps(job, sort_keys=True)}')

    def test_group_change_retries(self):
        group_spec = util.minimal_group()
        group_uuid = group_spec['uuid']
        job_spec = {'group': group_uuid, 'command': 'sleep 1'}

        def group_query():
            return util.group_detail_query(self.cook_url, group_uuid)

        try:
            jobs, resp = util.submit_jobs(self.cook_url, job_spec, 10, groups=[group_spec])
            self.assertEqual(resp.status_code, 201)
            # wait for some job to start
            util.wait_until(group_query, util.group_some_job_started)
            # When this wait condition succeeds, we expect at least one job to be completed,
            # a couple of jobs to be running (sleeping for 1 second should be long enough
            # to dominate any lag in the test code), and some still waiting.
            # If this is run in a larger mesos cluster with many jobs executing in parallel,
            # then all of the jobs may complete around the same time. However, we would
            # expect the behavior described above in our usual scaled-down testing environments
            # (i.e., Travis-CI VMs and minimesos using local docker instances).
            util.wait_until(group_query, util.group_some_job_done)
            job_data = util.query_jobs(self.cook_url, uuid=jobs).json()
            # retry all jobs in the group
            util.retry_jobs(self.cook_url, retries=12, groups=[group_uuid], failed_only=False)
            # wait for the previously-completed jobs to restart
            prev_completed_jobs = [j for j in job_data if j['status'] == 'completed']
            assert len(prev_completed_jobs) >= 1

            def jobs_query():
                return util.query_jobs(self.cook_url, True, uuid=prev_completed_jobs)

            def all_completed_restarted(response):
                for job in response.json():
                    instance_count = len(job['instances'])
                    if job['status'] == 'completed' and instance_count < 2:
                        self.logger.debug(f"Completed job {job['uuid']} has fewer than 2 instances: {instance_count}")
                        return False
                return True

            util.wait_until(jobs_query, all_completed_restarted)
            # ensure that all of the jobs have an updated retries count (set to 12 above)
            job_data = util.query_jobs(self.cook_url, uuid=jobs)
            self.assertEqual(200, job_data.status_code)
            for job in job_data.json():
                job_details = f'Job details: {json.dumps(job, sort_keys=True)}'
                # Jobs that ran twice will have 10 retries remaining,
                # Jobs that ran once will have 11 retries remaining.
                # Jobs that were running during the reset and are still running
                # will still have all 12 retries remaining.
                self.assertIn(job['retries_remaining'], [10, 11, 12], job_details)
        finally:
            # ensure that we don't leave a bunch of jobs running/waiting
            util.kill_groups(self.cook_url, [group_uuid])

    @pytest.mark.xfail(reason="Test can fail if one of the 5 jobs fails, e.g. due to 'Invalid task'")
    def test_group_failed_only_change_retries_all_active(self):
        statuses = ['running', 'waiting']
        jobs = util.group_submit_retry(self.cook_url, command='sleep 120', predicate_statuses=statuses)
        for job in jobs:
            job_details = f'Job details: {json.dumps(job, sort_keys=True)}'
            self.assertIn(job['status'], statuses, job_details)
            self.assertEqual(job['retries_remaining'], 1, job_details)
            self.assertLessEqual(len(job['instances']), 1, job_details)

    def test_group_failed_only_change_retries_all_success(self):
        statuses = ['completed']
        jobs = util.group_submit_retry(self.cook_url, command='exit 0', predicate_statuses=statuses)
        for job in jobs:
            job_details = f'Job details: {json.dumps(job, sort_keys=True, indent=2)}'
            self.assertIn(job['status'], statuses, job_details)
            self.assertEqual(0, job['retries_remaining'], job_details)
            self.assertLessEqual(1, len(job['instances']), job_details)

    def test_group_failed_only_change_retries_all_failed(self):
        statuses = ['completed']
        jobs = util.group_submit_retry(self.cook_url, command='exit 1', predicate_statuses=statuses)
        for job in jobs:
            job_details = f'Job details: {json.dumps(job, sort_keys=True, indent=2)}'
            self.assertIn(job['status'], statuses, job_details)
            self.assertEqual(0, job['retries_remaining'], job_details)
            self.assertLessEqual(2, len(job['instances']), job_details)

    def test_400_on_group_query_without_uuid(self):
        resp = util.query_groups(self.cook_url)
        self.assertEqual(400, resp.status_code)

    def test_queue_endpoint(self):
        group = {'uuid': str(uuid.uuid4())}
        job_spec = {'group': group['uuid'],
                    'command': 'sleep 30',
                    'cpus': util.max_cpus(self.mesos_url, self.cook_url)}
        uuids, resp = util.submit_jobs(self.cook_url, job_spec, clones=100, groups=[group])
        self.assertEqual(201, resp.status_code, resp.content)
        try:
            default_pool = util.default_pool(self.cook_url)
            pool = default_pool or 'no-pool'
            self.logger.info(f'Checking the queue endpoint for pool {pool}')

            def query_queue():
                return util.query_queue(self.cook_url)

            def queue_predicate(resp):
                return any([job['job/uuid'] in uuids for job in resp.json()[pool]])

            resp = util.wait_until(query_queue, queue_predicate)
            job = [job for job in resp.json()[pool] if job['job/uuid'] in uuids][0]
            job_group = job['group/_job'][0]
            self.assertEqual(200, resp.status_code, resp.content)
            self.assertTrue('group/_job' in job.keys())
            self.assertEqual(group['uuid'], job_group['group/uuid'])
            self.assertTrue('group/host-placement' in job_group.keys())
            self.assertFalse('group/job' in job_group.keys())
        finally:
            util.kill_jobs(self.cook_url, uuids)

    @pytest.mark.docker
    def test_basic_docker_job(self):
        image = util.docker_image() or 'alpine:latest'
        self.logger.debug(f'Using docker image {image}')
        job_uuid, resp = util.submit_job(
            self.cook_url,
            command='cat /.dockerenv',
            container={'type': 'DOCKER',
                       'docker': {'image': image}})
        self.assertEqual(resp.status_code, 201)
        job = util.wait_for_job(self.cook_url, job_uuid, 'completed')
        self.assertEqual('success', job['instances'][0]['status'])

    @pytest.mark.docker
    @unittest.skipUnless(util.has_docker_service(), "Requires `docker inspect`")
    def test_docker_port_mapping(self):
        job_uuid, resp = util.submit_job(self.cook_url,
                                         command='python -m http.server 8080',
                                         ports=2,
                                         container={'type': 'DOCKER',
                                                    'docker': {'image': 'python:3.6',
                                                               'network': 'BRIDGE',
                                                               'port-mapping': [{'host-port': 0,  # first assigned port
                                                                                 'container-port': 8080},
                                                                                {'host-port': 1,  # second assigned port
                                                                                 'container-port': 9090,
                                                                                 'protocol': 'udp'}]}})
        self.assertEqual(resp.status_code, 201, resp.content)
        job = util.wait_for_job(self.cook_url, job_uuid, 'running')
        instance = job['instances'][0]
        self.logger.debug('instance: %s' % instance)
        try:
            # Get agent host/port
            state = util.get_mesos_state(self.mesos_url)
            agent = [agent for agent in state['slaves']
                       if agent['hostname'] == instance['hostname']][0]

            # Get container ID from agent
            def agent_query():
                return util.session.get(util.get_agent_endpoint(state, instance['hostname']))

            def contains_executor_predicate(agent_response):
                agent_state = agent_response.json()
                executor = util.get_executor(agent_state, instance['executor_id'])
                if executor is None:
                    self.logger.warning(f"Could not find executor {instance['executor_id']} in agent state")
                    self.logger.warning(f"agent_state: {agent_state}")
                return executor is not None

            agent_state = util.wait_until(agent_query, contains_executor_predicate).json()
            executor = util.get_executor(agent_state, instance['executor_id'])

            container_name = 'mesos-%s.%s' % (agent['id'], executor['container'])
            self.logger.debug(f'Container name: {container_name}')

            @retry(stop_max_delay=60000, wait_fixed=1000)  # Wait for docker container to start
            def get_docker_info():
                job = util.load_job(self.cook_url, job_uuid)
                self.logger.info(f'Job status is {job["status"]}: {job}')
                containers = subprocess.check_output(['docker', 'ps', '--all', '--last', '10']).decode('utf-8')
                self.logger.info(f'Last 10 containers: {containers}')
                docker_ps = ['docker', 'ps', '--all', '--filter', f'name={container_name}', '--format', '{{.ID}}']
                container_id = subprocess.check_output(docker_ps).decode('utf-8').strip()
                self.logger.debug(f'Container ID: [{container_id}]')
                container_json = subprocess.check_output(['docker', 'inspect', container_id]).decode('utf-8')
                self.logger.debug(f'Container JSON: {container_json}')
                return json.loads(container_json)

            docker_info = get_docker_info()
            ports = docker_info[0]['HostConfig']['PortBindings']
            self.logger.debug('ports: %s' % ports)
            self.assertTrue('8080/tcp' in ports)
            self.assertEqual(instance['ports'][0], int(ports['8080/tcp'][0]['HostPort']))
            self.assertTrue('9090/udp' in ports)
            self.assertEqual(instance['ports'][1], int(ports['9090/udp'][0]['HostPort']))
        finally:
            job = util.load_job(self.cook_url, job_uuid)
            self.logger.info(f'Job status is {job["status"]}: {job}')
            util.session.delete('%s/rawscheduler?job=%s' % (self.cook_url, job_uuid))
            mesos.dump_sandbox_files(util.session, instance, job)

    def test_unscheduled_jobs(self):
        job_spec = {'command': 'sleep 30',
                    'priority': 100,
                    'cpus': util.max_cpus(self.mesos_url, self.cook_url)}
        uuids, resp = util.submit_jobs(self.cook_url, job_spec, clones=100)
        self.assertEqual(resp.status_code, 201, resp.content)
        try:
            job_uuid_1, resp = util.submit_job(self.cook_url, command='ls', priority=1)
            self.assertEqual(resp.status_code, 201, resp.content)
            job_uuid_2, resp = util.submit_job(self.cook_url, command='ls', priority=1)
            self.assertEqual(resp.status_code, 201, resp.content)
            jobs, _ = util.unscheduled_jobs(self.cook_url, job_uuid_1, job_uuid_2)
            self.logger.info(f'Unscheduled jobs: {jobs}')
            # If the job from the test is submitted after another one, unscheduled_jobs will report "There are jobs
            # ahead of this in the queue" so we cannot assert that there is exactly one failure reason.
            self.assertTrue(any([reasons.UNDER_INVESTIGATION == reason['reason'] for reason in jobs[0]['reasons']]))
            self.assertTrue(any([reasons.UNDER_INVESTIGATION == reason['reason'] for reason in jobs[1]['reasons']]))
            self.assertEqual(job_uuid_1, jobs[0]['uuid'])
            self.assertEqual(job_uuid_2, jobs[1]['uuid'])

            @retry(stop_max_delay=60000, wait_fixed=5000)
            def check_unscheduled_reason():
                jobs, _ = util.unscheduled_jobs(self.cook_url, job_uuid_1, job_uuid_2)
                self.logger.info(f'Unscheduled jobs: {jobs}')
                pattern = re.compile('^You have (at least )?[0-9]+ other jobs ahead in the queue.$')
                self.assertTrue(any([pattern.match(reason['reason']) for reason in jobs[0]['reasons']]),
                                jobs[0]['reasons'])
                self.assertTrue(any([pattern.match(reason['reason']) for reason in jobs[1]['reasons']]),
                                jobs[1]['reasons'])
                self.assertEqual(job_uuid_1, jobs[0]['uuid'])
                self.assertEqual(job_uuid_2, jobs[1]['uuid'])

            check_unscheduled_reason()
        finally:
            util.kill_jobs(self.cook_url, uuids)

    def test_unscheduled_jobs_partial(self):
        job_uuid_1, resp = util.submit_job(self.cook_url, command='ls')
        self.assertEqual(resp.status_code, 201, resp.content)
        job_uuid_2 = uuid.uuid4()
        _, resp = util.unscheduled_jobs(self.cook_url, job_uuid_1, job_uuid_2, partial=None)
        self.assertEqual(404, resp.status_code)
        _, resp = util.unscheduled_jobs(self.cook_url, job_uuid_1, job_uuid_2, partial='false')
        self.assertEqual(404, resp.status_code)
        jobs, resp = util.unscheduled_jobs(self.cook_url, job_uuid_1, job_uuid_2, partial='true')
        self.assertEqual(200, resp.status_code)
        self.assertEqual(1, len(jobs))
        self.assertEqual(job_uuid_1, jobs[0]['uuid'])

    def test_unique_host_constraint(self):
        num_hosts = util.num_hosts_to_consider(self.cook_url, self.mesos_url)
        group = {'uuid': str(uuid.uuid4()),
                 'host-placement': {'type': 'unique'}}
        job_spec = {'group': group['uuid'], 'command': 'sleep 600'}
        # Don't submit too many jobs for the test. If the cluster is larger than 9 hosts, only submit 10 jobs.
        num_jobs = min(num_hosts + 1, 10)
        uuids, resp = util.submit_jobs(self.cook_url, job_spec, num_jobs, groups=[group])
        self.assertEqual(resp.status_code, 201, resp.content)
        try:
            def query():
                return util.query_jobs(self.cook_url, uuid=uuids).json()

            def num_running_predicate(response):
                num_jobs_total = len(response)
                num_running = len([j for j in response if j['status'] == 'running'])
                num_waiting = len([j for j in response if j['status'] == 'waiting'])
                self.logger.info(f'There are {num_jobs_total} total jobs, {num_running} running jobs, '
                                 f'and {num_waiting} waiting job(s)')
                if num_jobs_total == num_hosts + 1:
                    # One job should not be scheduled
                    return (num_running == num_jobs_total - 1) and (num_waiting == 1)
                else:
                    # All of the jobs should be running
                    return num_running == num_jobs_total

            jobs = util.wait_until(query, num_running_predicate)
            hosts = [job['instances'][0]['hostname'] for job in jobs
                     if job['status'] == 'running']
            # Only one job should run on each host
            self.assertEqual(len(set(hosts)), len(hosts))
            # If one job was not running, check the output of unscheduled_jobs
            if num_jobs == num_hosts + 1:
                waiting_jobs = [j for j in jobs if j['status'] == 'waiting']
                self.assertEqual(1, len(waiting_jobs))
                waiting_job = waiting_jobs[0]
                job_uuid = waiting_job['uuid']

                def query():
                    unscheduled = util.unscheduled_jobs(self.cook_url, job_uuid)[0][0]
                    self.logger.info(f"unscheduled_jobs response: {unscheduled}")
                    return unscheduled

                def check_unique_constraint(response):
                    return any([r['reason'] == reasons.COULD_NOT_PLACE_JOB or
                                r['reason'] == reasons.JOB_IS_RUNNING_NOW
                                for r in response['reasons']])

                unscheduled_jobs = util.wait_until(query, check_unique_constraint)
                unique_reasons = [r for r in unscheduled_jobs['reasons'] if r['reason'] == reasons.COULD_NOT_PLACE_JOB]
                running_reasons = [r for r in unscheduled_jobs['reasons'] if r['reason'] == reasons.JOB_IS_RUNNING_NOW]
                job = util.load_job(self.cook_url, job_uuid)
                if unique_reasons:
                    unique_reason = unique_reasons[0]
                    self.logger.info(f'Job could not be placed: {unique_reason}')
                    self.assertEqual("unique-host-placement-group-constraint",
                                     unique_reason['data']['reasons'][0]['reason'],
                                     unique_reason)
                elif running_reasons:
                    self.logger.info(f'Job is now running: {job}')
                    self.assertEqual('running', job['status'])
                    self.assertNotIn(job['instances'][0]['hostname'], hosts)
                else:
                    self.fail(f'Expected job to either not be possible to place or to be running: {job}')
        finally:
            util.kill_jobs(self.cook_url, uuids)

    def test_balanced_host_constraint_cannot_place(self):
        num_hosts = util.num_hosts_to_consider(self.cook_url, self.mesos_url)
        if num_hosts > 10:
            # Skip this test on large clusters
            pytest.skip(f"Skipping test due to cluster size of {num_hosts} greater than 10")
        if num_hosts == 0:
            pytest.skip(f"Skipping test due to no Mesos agents to consider")
        minimum_hosts = num_hosts + 1
        group = {'uuid': str(uuid.uuid4()),
                 'host-placement': {'type': 'balanced',
                                    'parameters': {'attribute': 'HOSTNAME',
                                                   'minimum': minimum_hosts}}}
        job_spec = {'command': 'sleep 600',
                    'cpus': 0.1,
                    'group': group['uuid'],
                    'mem': 100}
        num_jobs = minimum_hosts
        uuids, resp = util.submit_jobs(self.cook_url, job_spec, num_jobs, groups=[group])
        try:
            def query_list():
                return util.query_jobs(self.cook_url, uuid=uuids).json()

            def num_running_predicate(response):
                num_jobs_total = len(response)
                num_running = len([j for j in response if j['status'] == 'running'])
                num_waiting = len([j for j in response if j['status'] == 'waiting'])
                self.logger.info(f'There are {num_jobs_total} total jobs, {num_running} running jobs, '
                                 f'and {num_waiting} waiting job(s)')
                return num_running == num_hosts and num_waiting == 1

            jobs = util.wait_until(query_list, num_running_predicate)
            hosts = [job['instances'][0]['hostname'] for job in jobs if job['status'] == 'running']
            # Only one job should run on each host
            self.assertEqual(len(set(hosts)), len(hosts))
            waiting_jobs = [j for j in jobs if j['status'] == 'waiting']
            self.assertEqual(1, len(waiting_jobs), waiting_jobs)
            waiting_job = waiting_jobs[0]
            job_uuid = waiting_job['uuid']
            constraint = 'balanced-host-placement-group-constraint'

            def query_unscheduled():
                resp = util.unscheduled_jobs(self.cook_url, job_uuid)[0][0]
                placement_reasons = [reason for reason in resp['reasons']
                                     if (reason['reason'] == reasons.COULD_NOT_PLACE_JOB
                                         and any(r for r in reason['data']['reasons'] if r['reason'] == constraint)) or
                                     reason['reason'] == reasons.JOB_IS_RUNNING_NOW]
                self.logger.info(f"unscheduled_jobs response: {resp}")
                return placement_reasons

            placement_reasons = util.wait_until(query_unscheduled, lambda r: len(r) > 0)
            self.assertEqual(1, len(placement_reasons), placement_reasons)
            reason = placement_reasons[0]
            job = util.load_job(self.cook_url, job_uuid)
            if reason['reason'] == reasons.COULD_NOT_PLACE_JOB:
                balanced_reasons = [r for r in reason['data']['reasons'] if r['reason'] == constraint]
                self.logger.info(f'Job could not be placed: {balanced_reasons}')
                self.assertEqual(1, len(balanced_reasons), balanced_reasons)
            elif reason['reason'] == reasons.JOB_IS_RUNNING_NOW:
                self.logger.info(f'Job is now running: {job}')
                self.assertEqual('running', job['status'])
                self.assertNotIn(job['instances'][0]['hostname'], hosts)
            else:
                self.fail(f'Expected job to either not be possible to place or to be running: {job}')
        finally:
            util.kill_jobs(self.cook_url, uuids)

    def test_balanced_host_constraint_can_place(self):
        num_hosts = util.num_hosts_to_consider(self.cook_url, self.mesos_url)
        minimum_hosts = min(int(os.getenv('COOK_TEST_BALANCED_CONSTRAINT_NUM_HOSTS', 10)), num_hosts)
        self.logger.info(f'Setting minimum hosts to {minimum_hosts}')
        group = {'uuid': str(uuid.uuid4()),
                 'host-placement': {'type': 'balanced',
                                    'parameters': {'attribute': 'HOSTNAME',
                                                   'minimum': minimum_hosts}}}
        job_spec = {'group': group['uuid'],
                    'command': 'sleep 600',
                    'mem': 100,
                    'cpus': 0.1}
        max_jobs_per_host = 3
        num_jobs = minimum_hosts * max_jobs_per_host
        uuids, resp = util.submit_jobs(self.cook_url, job_spec, num_jobs, groups=[group])
        self.assertEqual(201, resp.status_code, resp.content)
        try:
            jobs = util.wait_for_jobs(self.cook_url, uuids, 'running')
            hosts = [j['instances'][0]['hostname'] for j in jobs]
            host_count = Counter(hosts)
            self.assertGreaterEqual(len(host_count), minimum_hosts, hosts)
            self.assertLessEqual(max(host_count.values()), max_jobs_per_host, host_count)
        finally:
            util.kill_jobs(self.cook_url, uuids)

    def test_attribute_equals_hostname_constraint(self):
        max_slave_cpus = util.max_slave_cpus(self.mesos_url)
        task_constraint_cpus = util.task_constraint_cpus(self.cook_url)
        # The largest job we can submit that actually fits on a slave
        max_cpus = min(max_slave_cpus, task_constraint_cpus)
        # The number of "big" jobs we need to submit before one will not be scheduled
        num_big_jobs = max(1, math.floor(max_slave_cpus / task_constraint_cpus))
        # Use the rest of the machine, plus one half CPU so one of the large jobs won't fit
        canary_cpus = max_slave_cpus - (num_big_jobs * max_cpus) + 0.5
        group = {'uuid': str(uuid.uuid4()),
                 'host-placement': {'type': 'attribute-equals',
                                    'parameters': {'attribute': 'HOSTNAME'}}}
        # First, num_big_jobs jobs each with max_cpus cpus which will sleep to fill up a single host:
        jobs = [util.minimal_job(group=group['uuid'],
                                 priority=100,
                                 cpus=max_cpus,
                                 command='sleep 600')
                for _ in range(num_big_jobs)]
        # Second, a canary job which uses canary_cpus cpus which will not fit on the host.
        # Due to priority, this should be scheduled after the other jobs
        canary = util.minimal_job(group=group['uuid'],
                                  priority=1,
                                  cpus=canary_cpus,
                                  command='sleep 600')
        jobs.append(canary)
        uuids, resp = util.submit_jobs(self.cook_url, jobs, groups=[group])
        self.assertEqual(201, resp.status_code, resp.content)
        try:
            reasons = {
                # We expect the reason to be either our attribute-equals constraint:
                "Host had a different attribute than other jobs in the group.",
                # Or, if there are no other offers, we simply don't have enough cpus:
                "Not enough cpus available."
            }

            def query():
                unscheduled_jobs, _ = util.unscheduled_jobs(self.cook_url, *[j['uuid'] for j in jobs])
                self.logger.info(f"unscheduled_jobs response: {json.dumps(unscheduled_jobs, indent=2)}")
                no_hosts = [reason for job in unscheduled_jobs for reason in job['reasons']
                            if reason['reason'] == "The job couldn't be placed on any available hosts."]
                for no_hosts_reason in no_hosts:
                    for sub_reason in no_hosts_reason['data']['reasons']:
                        if sub_reason['reason'] in reasons:
                            return sub_reason
                return None

            reason = util.wait_until(query, lambda r: r is not None)
            self.assertIn(reason['reason'], reasons)
        finally:
            util.kill_jobs(self.cook_url, uuids)

    def test_retrieve_jobs_with_deprecated_api(self):
        pools, _ = util.active_pools(self.cook_url)
        pool = pools[0]['name'] if len(pools) > 0 else None
        job_uuid_1, resp = util.submit_job(self.cook_url, pool=pool)
        self.assertEqual(201, resp.status_code, msg=resp.content)
        job_uuid_2, resp = util.submit_job(self.cook_url)
        self.assertEqual(201, resp.status_code, msg=resp.content)

        # Query by job uuid
        resp = util.query_jobs_via_rawscheduler_endpoint(self.cook_url, job=[job_uuid_1, job_uuid_2])
        self.assertEqual(200, resp.status_code, msg=resp.content)
        self.assertEqual(2, len(resp.json()))
        self.assertEqual(job_uuid_1, resp.json()[0]['uuid'])
        self.assertEqual(job_uuid_2, resp.json()[1]['uuid'])

        # Query by instance uuid
        instance_uuid_1 = util.wait_for_instance(self.cook_url, job_uuid_1)['task_id']
        instance_uuid_2 = util.wait_for_instance(self.cook_url, job_uuid_2)['task_id']
        resp = util.query_jobs_via_rawscheduler_endpoint(self.cook_url, instance=[instance_uuid_1, instance_uuid_2])
        instance_uuids = [i['task_id'] for j in resp.json() for i in j['instances']]
        self.assertEqual(200, resp.status_code, msg=resp.content)
        self.assertEqual(2, len(resp.json()))
        self.assertEqual(2, len(instance_uuids))
        self.assertIn(instance_uuid_1, instance_uuids)
        self.assertIn(instance_uuid_2, instance_uuids)

    def test_load_instance_by_uuid(self):
        job_uuid, resp = util.submit_job(self.cook_url)
        self.assertEqual(201, resp.status_code, msg=resp.content)
        instance_uuid = util.wait_for_instance(self.cook_url, job_uuid)['task_id']
        instance = util.load_instance(self.cook_url, instance_uuid)
        self.assertEqual(instance_uuid, instance['task_id'])
        self.assertEqual(job_uuid, instance['job']['uuid'])

    def test_user_usage_basic(self):
        job_resources = {'cpus': 0.1, 'mem': 123}
        job_uuid, resp = util.submit_job(self.cook_url, command='sleep 120', **job_resources)
        self.assertEqual(resp.status_code, 201, resp.content)
        pools, _ = util.all_pools(self.cook_url)
        try:
            user = util.get_user(self.cook_url, job_uuid)
            # Don't query until the job starts
            util.wait_for_job(self.cook_url, job_uuid, 'running')
            resp = util.user_current_usage(self.cook_url, user=user)
            self.assertEqual(resp.status_code, 200, resp.content)
            usage_data = resp.json()
            # Check that the response structure looks as expected
            if pools:
                self.assertEqual(list(usage_data.keys()), ['total_usage', 'pools'], usage_data)
            else:
                self.assertEqual(list(usage_data.keys()), ['total_usage'], usage_data)
            self.assertEqual(len(usage_data['total_usage']), 4, usage_data)
            # Since we don't know what other test jobs are currently running,
            # we conservatively check current usage with the >= operation.
            self.assertGreaterEqual(usage_data['total_usage']['mem'], job_resources['mem'], usage_data)
            self.assertGreaterEqual(usage_data['total_usage']['cpus'], job_resources['cpus'], usage_data)
            self.assertGreaterEqual(usage_data['total_usage']['gpus'], 0, usage_data)
            self.assertGreaterEqual(usage_data['total_usage']['jobs'], 1, usage_data)
        finally:
            util.kill_jobs(self.cook_url, [job_uuid])

    def test_user_usage_grouped(self):
        group_spec = util.minimal_group()
        group_uuid = group_spec['uuid']
        job_resources = {'cpus': 0.11, 'mem': 123}
        job_count = 2
        job_specs = util.minimal_jobs(job_count, command='sleep 120', group=group_uuid, **job_resources)
        job_uuids, resp = util.submit_jobs(self.cook_url, job_specs, groups=[group_spec])
        self.assertEqual(resp.status_code, 201, resp.content)
        pools, _ = util.all_pools(self.cook_url)
        try:
            user = util.get_user(self.cook_url, job_uuids[0])
            # Don't query until both of the jobs start
            util.wait_for_jobs(self.cook_url, job_uuids, 'running')
            resp = util.user_current_usage(self.cook_url, user=user, group_breakdown='true')
            self.assertEqual(resp.status_code, 200, resp.content)
            usage_data = resp.json()
            # Check that the response structure looks as expected
            if pools:
                self.assertEqual(set(usage_data.keys()), {'total_usage', 'grouped', 'ungrouped', 'pools'}, usage_data)
            else:
                self.assertEqual(set(usage_data.keys()), {'total_usage', 'grouped', 'ungrouped'}, usage_data)
            self.assertEqual(set(usage_data['ungrouped'].keys()), {'running_jobs', 'usage'}, usage_data)
            my_group_usage = next(x for x in usage_data['grouped'] if x['group']['uuid'] == group_uuid)
            self.assertEqual(set(my_group_usage.keys()), {'group', 'usage'}, my_group_usage)
            # The breakdown for our job group should contain exactly the two jobs we submitted
            self.assertEqual(set(job_uuids), set(my_group_usage['group']['running_jobs']), my_group_usage)
            # We know all the info about the jobs in our custom group
            self.assertEqual(my_group_usage['usage']['mem'], job_count * job_resources['mem'], my_group_usage)
            self.assertEqual(my_group_usage['usage']['cpus'], job_count * job_resources['cpus'], my_group_usage)
            self.assertEqual(my_group_usage['usage']['gpus'], 0, my_group_usage)
            self.assertEqual(my_group_usage['usage']['jobs'], job_count, my_group_usage)
            # Since we don't know what other test jobs are currently running
            # we conservatively check current usage with the >= operation
            self.assertGreaterEqual(usage_data['total_usage']['mem'], job_resources['mem'], usage_data)
            self.assertGreaterEqual(usage_data['total_usage']['cpus'], job_resources['cpus'], usage_data)
            self.assertGreaterEqual(usage_data['total_usage']['gpus'], 0, usage_data)
            self.assertGreaterEqual(usage_data['total_usage']['jobs'], job_count, usage_data)
            # The grouped + ungrouped usage should equal the total usage
            # (with possible rounding errors for floating-point cpu/mem values)
            breakdowns_total = Counter(usage_data['ungrouped']['usage'])
            for grouping in usage_data['grouped']:
                breakdowns_total += Counter(grouping['usage'])
            self.assertAlmostEqual(usage_data['total_usage']['mem'],
                                   breakdowns_total['mem'],
                                   places=4, msg=usage_data)
            self.assertAlmostEqual(usage_data['total_usage']['cpus'],
                                   breakdowns_total['cpus'],
                                   places=4, msg=usage_data)
            self.assertEqual(usage_data['total_usage']['gpus'], breakdowns_total['gpus'], usage_data)
            self.assertEqual(usage_data['total_usage']['jobs'], breakdowns_total['jobs'], usage_data)
            # Pool-specific checks
            for pool in pools:
                # There should be a sub-map under pools for each pool in the
                # system, since we didn't specify the pool in the usage request
                pool_usage = usage_data['pools'][pool['name']]
                self.assertEqual(set(pool_usage.keys()), {'total_usage', 'grouped', 'ungrouped'}, pool_usage)
                self.assertEqual(set(pool_usage['ungrouped'].keys()), {'running_jobs', 'usage'}, pool_usage)
            default_pool = util.default_pool(self.cook_url)
            if default_pool:
                # If there is a default pool configured, make sure that our jobs,
                # which don't have a pool, are counted as being in the default pool
                resp = util.user_current_usage(self.cook_url, user=user, group_breakdown='true', pool=default_pool)
                self.assertEqual(resp.status_code, 200, resp.content)
                usage_data = resp.json()
                my_group_usage = next(x for x in usage_data['grouped'] if x['group']['uuid'] == group_uuid)
                self.assertEqual(set(my_group_usage.keys()), {'group', 'usage'}, my_group_usage)
                self.assertEqual(set(job_uuids), set(my_group_usage['group']['running_jobs']), my_group_usage)
                self.assertEqual(my_group_usage['usage']['mem'], job_count * job_resources['mem'], my_group_usage)
                self.assertEqual(my_group_usage['usage']['cpus'], job_count * job_resources['cpus'], my_group_usage)
                self.assertEqual(my_group_usage['usage']['gpus'], 0, my_group_usage)
                self.assertEqual(my_group_usage['usage']['jobs'], job_count, my_group_usage)
                # If there is a non-default pool, make sure that
                # our jobs don't appear under that pool's usage
                non_default_pools = [p for p in pools if p['name'] != default_pool]
                if len(non_default_pools) > 0:
                    pool = non_default_pools[0]['name']
                    resp = util.user_current_usage(self.cook_url, user=user, group_breakdown='true', pool=pool)
                    self.assertEqual(resp.status_code, 200, resp.content)
                    usage_data = resp.json()
                    my_group_usage = [x for x in usage_data['grouped'] if x['group']['uuid'] == group_uuid]
                    self.assertEqual(0, len(my_group_usage))

                    resp = util.user_current_usage(self.cook_url, headers={'x-cook-pool': pool}, user=user,
                                                   group_breakdown='true')
                    self.assertEqual(resp.status_code, 200, resp.content)
                    usage_data = resp.json()
                    my_group_usage = [x for x in usage_data['grouped'] if x['group']['uuid'] == group_uuid]
                    self.assertEqual(0, len(my_group_usage))
                else:
                    self.logger.info('There is no pool that is not the default pool')
            else:
                self.logger.info('There is no configured default pool')
        finally:
            util.kill_jobs(self.cook_url, job_uuids)

    def test_user_usage_ungrouped(self):
        job_resources = {'cpus': 0.11, 'mem': 123}
        job_count = 2
        job_specs = util.minimal_jobs(job_count, command='sleep 120', **job_resources)
        job_uuids, resp = util.submit_jobs(self.cook_url, job_specs)
        self.assertEqual(resp.status_code, 201, resp.content)

        pools, _ = util.all_pools(self.cook_url)
        try:
            user = util.get_user(self.cook_url, job_uuids[0])
            # Don't query until both of the jobs start
            util.wait_for_jobs(self.cook_url, job_uuids, 'running')
            resp = util.user_current_usage(self.cook_url, user=user, group_breakdown='true')
            self.assertEqual(resp.status_code, 200, resp.content)
            usage_data = resp.json()
            # Check that the response structure looks as expected
            if pools:
                self.assertEqual(set(usage_data.keys()), {'total_usage', 'grouped', 'ungrouped', 'pools'}, usage_data)
            else:
                self.assertEqual(set(usage_data.keys()), {'total_usage', 'grouped', 'ungrouped'}, usage_data)
            ungrouped_data = usage_data['ungrouped']
            self.assertEqual(set(ungrouped_data.keys()), {'running_jobs', 'usage'}, ungrouped_data)
            # Our jobs should be included in the ungrouped breakdown
            for job_uuid in job_uuids:
                self.assertIn(job_uuid, ungrouped_data['running_jobs'], ungrouped_data)
            # Since we don't know what other test jobs are currently running,
            # we conservatively check current usage with the >= operation.
            self.assertGreaterEqual(ungrouped_data['usage']['mem'], job_count * job_resources['mem'], ungrouped_data)
            self.assertGreaterEqual(ungrouped_data['usage']['cpus'], job_count * job_resources['cpus'], ungrouped_data)
            self.assertGreaterEqual(ungrouped_data['usage']['gpus'], 0, ungrouped_data)
            self.assertGreaterEqual(ungrouped_data['usage']['jobs'], job_count, ungrouped_data)
            self.assertGreaterEqual(usage_data['total_usage']['mem'], job_resources['mem'], usage_data)
            self.assertGreaterEqual(usage_data['total_usage']['cpus'], job_resources['cpus'], usage_data)
            self.assertGreaterEqual(usage_data['total_usage']['gpus'], 0, usage_data)
            self.assertGreaterEqual(usage_data['total_usage']['jobs'], job_count, usage_data)
        finally:
            util.kill_jobs(self.cook_url, job_uuids)

    def test_user_limits_change(self):
        user = 'limit_change_test_user'
        # set user quota
        resp = util.set_limit(self.cook_url, 'quota', user, cpus=20)
        self.assertEqual(resp.status_code, 201, resp.text)
        # set user quota fails (malformed) if no reason is given
        resp = util.set_limit(self.cook_url, 'quota', user, cpus=10, reason=None)
        self.assertEqual(resp.status_code, 400, resp.text)
        # reset user quota back to default
        resp = util.reset_limit(self.cook_url, 'quota', user, reason=self.current_name())
        self.assertEqual(resp.status_code, 204, resp.text)
        # reset user quota fails (malformed) if no reason is given
        resp = util.reset_limit(self.cook_url, 'quota', user, reason=None)
        self.assertEqual(resp.status_code, 400, resp.text)
        # set user share
        resp = util.set_limit(self.cook_url, 'share', user, cpus=10)
        self.assertEqual(resp.status_code, 201, resp.text)
        # set user share fails (malformed) if no reason is given
        resp = util.set_limit(self.cook_url, 'share', user, cpus=10, reason=None)
        self.assertEqual(resp.status_code, 400, resp.text)
        # reset user share back to default
        resp = util.reset_limit(self.cook_url, 'share', user, reason=self.current_name())
        self.assertEqual(resp.status_code, 204, resp.text)
        # reset user share fails (malformed) if no reason is given
        resp = util.reset_limit(self.cook_url, 'share', user, reason=None)
        self.assertEqual(resp.status_code, 400, resp.text)

        default_pool = util.default_pool(self.cook_url)
        if default_pool is not None:
            for limit in ['quota', 'share']:
                # Get the default cpus limit
                resp = util.get_limit(self.cook_url, limit, "default")
                self.assertEqual(200, resp.status_code, resp.text)
                self.logger.info(f'The default limit in the {default_pool} pool is {resp.json()}')
                default_cpus = resp.json()['cpus']

                # Set a limit for the default pool
                resp = util.set_limit(self.cook_url, limit, user, cpus=100, pool=default_pool)
                self.assertEqual(resp.status_code, 201, resp.text)

                # Check that the limit is returned for no pool
                resp = util.get_limit(self.cook_url, limit, user)
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(100, resp.json()['cpus'], resp.text)

                # Check that the limit is returned for the default pool
                resp = util.get_limit(self.cook_url, limit, user, pool=default_pool)
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(100, resp.json()['cpus'], resp.text)

                # Delete the default pool limit (no pool argument)
                resp = util.reset_limit(self.cook_url, limit, user, reason=self.current_name())
                self.assertEqual(resp.status_code, 204, resp.text)

                # Check that the default is returned for the default pool
                resp = util.get_limit(self.cook_url, limit, user, pool=default_pool)
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(default_cpus, resp.json()['cpus'], resp.text)

                pools, _ = util.all_pools(self.cook_url)
                non_default_pools = [p['name'] for p in pools if p['name'] != default_pool]

                for pool in non_default_pools:
                    # Get the default cpus limit
                    resp = util.get_limit(self.cook_url, limit, "default", pool=pool)
                    self.assertEqual(200, resp.status_code, resp.text)
                    self.logger.info(f'The default limit in the {default_pool} pool is {resp.json()}')
                    default_cpus = resp.json()['cpus']

                    # delete the pool's limit
                    resp = util.reset_limit(self.cook_url, limit, user, pool=pool, reason=self.current_name())
                    self.assertEqual(resp.status_code, 204, resp.text)

                    # check that the default value is returned
                    resp = util.get_limit(self.cook_url, limit, user, pool=pool)
                    self.assertEqual(resp.status_code, 200, resp.text)
                    self.assertEqual(default_cpus, resp.json()['cpus'], resp.text)

                    # set a pool-specific limit
                    resp = util.set_limit(self.cook_url, limit, user, cpus=1000, pool=pool)
                    self.assertEqual(resp.status_code, 201, resp.text)

                    # check that the pool-specific limit is returned
                    resp = util.get_limit(self.cook_url, limit, user, pool=pool)
                    self.assertEqual(resp.status_code, 200, resp.text)
                    self.assertEqual(1000, resp.json()['cpus'], resp.text)

                    # now delete the pool limit with headers
                    resp = util.reset_limit(self.cook_url, limit, user, reason=self.current_name(),
                                            headers={'x-cook-pool': pool})
                    self.assertEqual(resp.status_code, 204, resp.text)

                    # check that the default value is returned
                    resp = util.get_limit(self.cook_url, limit, user, headers={'x-cook-pool': pool})
                    self.assertEqual(resp.status_code, 200, resp.text)
                    self.assertEqual(default_cpus, resp.json()['cpus'], resp.text)

                    # set a pool-specific limit
                    resp = util.set_limit(self.cook_url, limit, user, cpus=1000, headers={'x-cook-pool': pool})
                    self.assertEqual(resp.status_code, 201, resp.text)

                    # check that the pool-specific limit is returned
                    resp = util.get_limit(self.cook_url, limit, user, headers={'x-cook-pool': pool})
                    self.assertEqual(resp.status_code, 200, resp.text)

    def test_submit_with_no_name(self):
        # We need to manually set the 'uuid' to avoid having the
        # job name automatically set by the submit_job function
        job_with_no_name = {'uuid': str(uuid.uuid4()),
                            'command': 'ls',
                            'cpus': 0.1,
                            'mem': 16,
                            'max-retries': 1}
        job_uuid, resp = util.submit_job(self.cook_url, **job_with_no_name)
        self.assertEqual(resp.status_code, 201, msg=resp.content)
        scheduler_default_job_name = 'cookjob'
        job = util.load_job(self.cook_url, job_uuid)
        self.assertEqual(scheduler_default_job_name, job['name'])

    def test_submit_with_status(self):
        # The Java job client used to send the status field on job submission.
        # At some point, that changed, but we still allow it for backwards compat.
        job_uuid, resp = util.submit_job(self.cook_url, status='not a real status')
        self.assertEqual(resp.status_code, 201, msg=resp.content)
        job = util.load_job(self.cook_url, job_uuid)
        self.assertIn(job['status'], ['running', 'waiting', 'completed'])

    def test_instance_stats_running(self):
        name = str(uuid.uuid4())
        job_uuid_1, resp = util.submit_job(self.cook_url, command='sleep 300', name=name, max_retries=2)
        self.assertEqual(resp.status_code, 201, msg=resp.content)
        job_uuid_2, resp = util.submit_job(self.cook_url, command='sleep 300', name=name, max_retries=2)
        self.assertEqual(resp.status_code, 201, msg=resp.content)
        job_uuid_3, resp = util.submit_job(self.cook_url, command='sleep 300', name=name, max_retries=2)
        self.assertEqual(resp.status_code, 201, msg=resp.content)
        job_uuids = [job_uuid_1, job_uuid_2, job_uuid_3]
        try:
            instances = [util.wait_for_running_instance(self.cook_url, j) for j in job_uuids]
            start_time = min(i['start_time'] for i in instances)
            end_time = max(i['start_time'] for i in instances)
            stats, _ = util.get_instance_stats(self.cook_url,
                                               status='running',
                                               start=util.to_iso(start_time),
                                               end=util.to_iso(end_time + 1),
                                               name=name)
            user = util.get_user(self.cook_url, job_uuid_1)
            self.assertEqual(3, stats['overall']['count'])
            self.assertEqual(3, stats['by-reason']['']['count'])
            self.assertEqual(3, stats['by-user-and-reason'][user]['']['count'])
        finally:
            util.kill_jobs(self.cook_url, job_uuids)

    def test_instance_stats_failed(self):
        name = str(uuid.uuid4())
        job_uuid_1, resp = util.submit_job(self.cook_url, command='exit 1', name=name, cpus=0.1, mem=32)
        self.assertEqual(resp.status_code, 201, msg=resp.content)
        job_uuid_2, resp = util.submit_job(self.cook_url, command='sleep 1 && exit 1', name=name, cpus=0.2, mem=64)
        self.assertEqual(resp.status_code, 201, msg=resp.content)
        job_uuid_3, resp = util.submit_job(self.cook_url, command='sleep 2 && exit 1', name=name, cpus=0.4, mem=128)
        self.assertEqual(resp.status_code, 201, msg=resp.content)
        job_uuids = [job_uuid_1, job_uuid_2, job_uuid_3]
        try:
            jobs = util.wait_for_jobs(self.cook_url, job_uuids, 'completed')
            instances = []
            non_mea_culpa_instances = []
            for job in jobs:
                for instance in job['instances']:
                    instance['parent'] = job
                    instances.append(instance)
                    if not instance['reason_mea_culpa']:
                        non_mea_culpa_instances.append(instance)
            start_time = min(i['start_time'] for i in instances)
            end_time = max(i['start_time'] for i in instances)
            stats, _ = util.get_instance_stats(self.cook_url,
                                               status='failed',
                                               start=util.to_iso(start_time),
                                               end=util.to_iso(end_time + 1),
                                               name=name)
            self.logger.info(json.dumps(stats, indent=2))
            self.logger.info(f'Instances: {instances}')
            user = util.get_user(self.cook_url, job_uuid_1)
            stats_overall = stats['overall']
            exited_non_zero = 'Command exited non-zero'
            self.assertEqual(len(instances), stats_overall['count'])
            self.assertEqual(len(non_mea_culpa_instances), stats['by-reason'][exited_non_zero]['count'])
            self.assertEqual(len(non_mea_culpa_instances), stats['by-user-and-reason'][user][exited_non_zero]['count'])
            run_times = [(i['end_time'] - i['start_time']) / 1000 for i in instances]
            run_time_seconds = stats_overall['run-time-seconds']
            percentiles = run_time_seconds['percentiles']
            self.logger.info(f'Run times: {json.dumps(run_times, indent=2)}')
            self.assertEqual(util.percentile(run_times, 50), percentiles['50'])
            self.assertEqual(util.percentile(run_times, 75), percentiles['75'])
            self.assertEqual(util.percentile(run_times, 95), percentiles['95'])
            self.assertEqual(util.percentile(run_times, 99), percentiles['99'])
            self.assertEqual(util.percentile(run_times, 100), percentiles['100'])
            self.assertAlmostEqual(sum(run_times), run_time_seconds['total'])
            cpu_times = [((i['end_time'] - i['start_time']) / 1000) * i['parent']['cpus'] for i in instances]
            cpu_seconds = stats_overall['cpu-seconds']
            percentiles = cpu_seconds['percentiles']
            self.logger.info(f'CPU times: {json.dumps(cpu_times, indent=2)}')
            self.assertEqual(util.percentile(cpu_times, 50), percentiles['50'])
            self.assertEqual(util.percentile(cpu_times, 75), percentiles['75'])
            self.assertEqual(util.percentile(cpu_times, 95), percentiles['95'])
            self.assertEqual(util.percentile(cpu_times, 99), percentiles['99'])
            self.assertEqual(util.percentile(cpu_times, 100), percentiles['100'])
            self.assertAlmostEqual(sum(cpu_times), cpu_seconds['total'])
            mem_times = [((i['end_time'] - i['start_time']) / 1000) * i['parent']['mem'] for i in instances]
            mem_seconds = stats_overall['mem-seconds']
            percentiles = mem_seconds['percentiles']
            self.logger.info(f'Mem times: {json.dumps(mem_times, indent=2)}')
            self.assertEqual(util.percentile(mem_times, 50), percentiles['50'])
            self.assertEqual(util.percentile(mem_times, 75), percentiles['75'])
            self.assertEqual(util.percentile(mem_times, 95), percentiles['95'])
            self.assertEqual(util.percentile(mem_times, 99), percentiles['99'])
            self.assertEqual(util.percentile(mem_times, 100), percentiles['100'])
            self.assertAlmostEqual(sum(mem_times), mem_seconds['total'])
        finally:
            util.kill_jobs(self.cook_url, job_uuids)

    def test_instance_stats_success(self):
        name = str(uuid.uuid4())
        job_uuid_1, resp = util.submit_job(self.cook_url, command='exit 0', name=name, cpus=0.1, mem=32)
        self.assertEqual(resp.status_code, 201, msg=resp.content)
        job_uuid_2, resp = util.submit_job(self.cook_url, command='sleep 1', name=name, cpus=0.2, mem=64)
        self.assertEqual(resp.status_code, 201, msg=resp.content)
        job_uuid_3, resp = util.submit_job(self.cook_url, command='sleep 2', name=name, cpus=0.4, mem=128)
        self.assertEqual(resp.status_code, 201, msg=resp.content)
        job_uuids = [job_uuid_1, job_uuid_2, job_uuid_3]
        try:
            util.wait_for_jobs(self.cook_url, job_uuids, 'completed')
            instances = [util.wait_for_instance(self.cook_url, j) for j in job_uuids]
            try:
                for instance in instances:
                    self.assertEqual('success', instance['parent']['state'])
                start_time = min(i['start_time'] for i in instances)
                end_time = max(i['start_time'] for i in instances)
                stats, _ = util.get_instance_stats(self.cook_url,
                                                   status='success',
                                                   start=util.to_iso(start_time),
                                                   end=util.to_iso(end_time + 1),
                                                   name=name)
                user = util.get_user(self.cook_url, job_uuid_1)
                stats_overall = stats['overall']
                self.assertEqual(3, stats_overall['count'])
                self.assertEqual(3, stats['by-reason']['']['count'])
                self.assertEqual(3, stats['by-user-and-reason'][user]['']['count'])
                run_times = [(i['end_time'] - i['start_time']) / 1000 for i in instances]
                run_time_seconds = stats_overall['run-time-seconds']
                percentiles = run_time_seconds['percentiles']
                self.assertEqual(util.percentile(run_times, 50), percentiles['50'])
                self.assertEqual(util.percentile(run_times, 75), percentiles['75'])
                self.assertEqual(util.percentile(run_times, 95), percentiles['95'])
                self.assertEqual(util.percentile(run_times, 99), percentiles['99'])
                self.assertEqual(util.percentile(run_times, 100), percentiles['100'])
                self.assertAlmostEqual(sum(run_times), run_time_seconds['total'])
                cpu_times = [((i['end_time'] - i['start_time']) / 1000) * i['parent']['cpus'] for i in instances]
                cpu_seconds = stats_overall['cpu-seconds']
                percentiles = cpu_seconds['percentiles']
                self.assertEqual(util.percentile(cpu_times, 50), percentiles['50'])
                self.assertEqual(util.percentile(cpu_times, 75), percentiles['75'])
                self.assertEqual(util.percentile(cpu_times, 95), percentiles['95'])
                self.assertEqual(util.percentile(cpu_times, 99), percentiles['99'])
                self.assertEqual(util.percentile(cpu_times, 100), percentiles['100'])
                self.assertAlmostEqual(sum(cpu_times), cpu_seconds['total'])
                mem_times = [((i['end_time'] - i['start_time']) / 1000) * i['parent']['mem'] for i in instances]
                mem_seconds = stats_overall['mem-seconds']
                percentiles = mem_seconds['percentiles']
                self.assertEqual(util.percentile(mem_times, 50), percentiles['50'])
                self.assertEqual(util.percentile(mem_times, 75), percentiles['75'])
                self.assertEqual(util.percentile(mem_times, 95), percentiles['95'])
                self.assertEqual(util.percentile(mem_times, 99), percentiles['99'])
                self.assertEqual(util.percentile(mem_times, 100), percentiles['100'])
                self.assertAlmostEqual(sum(mem_times), mem_seconds['total'])
            except:
                for instance in instances:
                    mesos.dump_sandbox_files(util.session, instance, instance['parent'])
                raise
        finally:
            util.kill_jobs(self.cook_url, job_uuids)

    def test_instance_stats_supports_epoch_time_params(self):
        name = str(uuid.uuid4())
        job_uuid_1, resp = util.submit_job(self.cook_url, command='sleep 300', name=name, max_retries=2)
        self.assertEqual(resp.status_code, 201, msg=resp.content)
        job_uuid_2, resp = util.submit_job(self.cook_url, command='sleep 300', name=name, max_retries=2)
        self.assertEqual(resp.status_code, 201, msg=resp.content)
        job_uuid_3, resp = util.submit_job(self.cook_url, command='sleep 300', name=name, max_retries=2)
        self.assertEqual(resp.status_code, 201, msg=resp.content)
        job_uuids = [job_uuid_1, job_uuid_2, job_uuid_3]
        try:
            instances = [util.wait_for_running_instance(self.cook_url, j) for j in job_uuids]
            start_time = min(i['start_time'] for i in instances)
            end_time = max(i['start_time'] for i in instances)
            stats, _ = util.get_instance_stats(self.cook_url,
                                               status='running',
                                               start=start_time,
                                               end=end_time + 1,
                                               name=name)
            user = util.get_user(self.cook_url, job_uuid_1)
            self.assertEqual(3, stats['overall']['count'])
            self.assertEqual(3, stats['by-reason']['']['count'])
            self.assertEqual(3, stats['by-user-and-reason'][user]['']['count'])
        finally:
            util.kill_jobs(self.cook_url, job_uuids)

    def test_instance_stats_rejects_invalid_params(self):
        _, resp = util.get_instance_stats(self.cook_url, status='running', start='2018-02-20', end='2018-02-21')
        self.assertEqual(200, resp.status_code)
        _, resp = util.get_instance_stats(self.cook_url, status='running', start='2018-02-20')
        self.assertEqual(400, resp.status_code)
        _, resp = util.get_instance_stats(self.cook_url, status='running', end='2018-02-21')
        self.assertEqual(400, resp.status_code)
        _, resp = util.get_instance_stats(self.cook_url, start='2018-02-20', end='2018-02-21')
        self.assertEqual(400, resp.status_code)
        _, resp = util.get_instance_stats(self.cook_url, status='bogus', start='2018-02-20', end='2018-02-21')
        self.assertEqual(400, resp.status_code)
        _, resp = util.get_instance_stats(self.cook_url, status='running', start='2018-02-20',
                                          end='2018-02-21', name='foo')
        self.assertEqual(200, resp.status_code)
        _, resp = util.get_instance_stats(self.cook_url, status='running', start='2018-02-20',
                                          end='2018-02-21', name='?')
        self.assertEqual(400, resp.status_code)
        _, resp = util.get_instance_stats(self.cook_url, status='running', start='2018-01-01', end='2018-02-01')
        self.assertEqual(200, resp.status_code)
        _, resp = util.get_instance_stats(self.cook_url, status='running', start='2018-01-01', end='2018-02-02')
        self.assertEqual(400, resp.status_code)
        _, resp = util.get_instance_stats(self.cook_url, status='running', start='2018-01-01', end='2017-12-31')
        self.assertEqual(400, resp.status_code)

    def test_cors_request(self):
        resp = util.session.get(f"{self.cook_url}/settings", headers={"Origin": "http://bad.example.com"})
        self.assertEqual(403, resp.status_code)
        self.assertEqual(b"Cross origin request denied from http://bad.example.com", resp.content)

        def origin_allowed(cors_patterns, origin):
            return any([re.search(pattern, origin) for pattern in cors_patterns])

        resp = util.session.get(f"{self.cook_url}/settings", headers={"Origin": self.cors_origin})
        self.assertEqual(200, resp.status_code)
        self.assertTrue(origin_allowed(resp.json()["cors-origins"], self.cors_origin), resp.json())

        resp = util.session.get(f"{self.cook_url}/settings", headers={"Origin": self.cors_origin})
        self.assertEqual(200, resp.status_code)
        self.assertTrue(origin_allowed(resp.json()["cors-origins"], self.cors_origin), resp.json())

    def test_cors_preflight(self):
        resp = util.session.options(f"{self.cook_url}/settings", headers={"Origin": "http://bad.example.com"})
        self.assertEqual(403, resp.status_code)
        self.assertEqual(b"Origin http://bad.example.com not allowed", resp.content)

        resp = util.session.options(f"{self.cook_url}/settings", headers={"Origin": self.cors_origin,
                                                                          "Access-Control-Request-Headers": "Foo, Bar"})
        self.assertEqual(200, resp.status_code)
        self.assertEqual("true", resp.headers["Access-Control-Allow-Credentials"])
        self.assertEqual("Foo, Bar", resp.headers["Access-Control-Allow-Headers"])
        self.assertEqual(self.cors_origin, resp.headers["Access-Control-Allow-Origin"])
        self.assertEqual("86400", resp.headers["Access-Control-Max-Age"])
        self.assertEqual(b"", resp.content)

    def test_submit_with_pool(self):
        pools, _ = util.all_pools(self.cook_url)
        if len(pools) == 0:
            self.logger.info('There are no pools to submit jobs to')
        for pool in pools:
            self.logger.info(f'Submitting to {pool}')
            pool_name = pool['name']
            job_uuid, resp = util.submit_job(self.cook_url, pool=pool_name)
            if pool['state'] == 'active':
                self.assertEqual(resp.status_code, 201, msg=resp.content)
                job = util.load_job(self.cook_url, job_uuid)
                self.assertEqual(pool_name, job['pool'])
            else:
                self.assertEqual(resp.status_code, 400, msg=resp.content)

            job_uuid, resp = util.submit_job(self.cook_url, headers={'x-cook-pool': pool['name']})
            if pool['state'] == 'active':
                self.assertEqual(resp.status_code, 201, msg=resp.content)
                job = util.load_job(self.cook_url, job_uuid)
                self.assertEqual(pool_name, job['pool'])
            else:
                self.assertEqual(resp.status_code, 400, msg=resp.content)

        # Try submitting to a pool that doesn't exist
        job_uuid, resp = util.submit_job(self.cook_url, pool=str(uuid.uuid4()))
        self.assertEqual(resp.status_code, 400, msg=resp.content)

    def test_ssl(self):
        settings = util.settings(self.cook_url)
        if 'server-https-port' not in settings or settings['server-https-port'] is None:
            self.logger.info('SSL not configured: skipping test')
            return
        ssl_port = settings['server-https-port']

        host = urlparse(self.cook_url).hostname

        resp = util.session.get(f"https://{host}:{ssl_port}/settings", verify=False)
        self.assertEqual(200, resp.status_code)

    def test_pool_specific_quota_check_on_submit(self):
        constraints = util.settings(self.cook_url)['task-constraints']
        task_constraint_cpus = constraints['cpus']
        task_constraint_mem = constraints['memory-gb'] * 1024
        user = self.determine_user()
        pools, _ = util.active_pools(self.cook_url)
        for pool in pools:
            pool_name = pool['name']
            self.logger.info(f'Testing quota check for pool {pool_name}')
            quota = util.get_limit(self.cook_url, 'quota', user, pool_name).json()

            # cpus
            cpus_over_quota = quota['cpus'] + 0.1
            if cpus_over_quota > task_constraint_cpus:
                self.logger.info(f'Unable to check CPU quota on {pool_name} because the quota ({quota["cpus"]}) is '
                                 f'higher than the task constraint ({task_constraint_cpus})')
            else:
                self.assertLessEqual(cpus_over_quota, task_constraint_cpus)
                job_uuid, resp = util.submit_job(self.cook_url, pool=pool_name, cpus=0.1)
                self.assertEqual(201, resp.status_code, msg=resp.content)
                job_uuid, resp = util.submit_job(self.cook_url, pool=pool_name, cpus=cpus_over_quota)
                self.assertEqual(422, resp.status_code, msg=resp.content)

            # mem
            mem_over_quota = quota['mem'] + 1
            if mem_over_quota > task_constraint_mem:
                self.logger.info(f'Unable to check mem quota on {pool_name} because the quota ({quota["mem"]}) is '
                                 f'higher than the task constraint ({task_constraint_mem})')
            else:
                self.assertLessEqual(mem_over_quota, task_constraint_mem)
                job_uuid, resp = util.submit_job(self.cook_url, pool=pool_name, mem=32)
                self.assertEqual(201, resp.status_code, msg=resp.content)
                job_uuid, resp = util.submit_job(self.cook_url, pool=pool_name, mem=mem_over_quota)
                self.assertEqual(422, resp.status_code, msg=resp.content)

    def test_decrease_retries_below_attempts(self):
        uuid, resp = util.submit_job(self.cook_url, command='exit 1', max_retries=2)
        util.wait_for_job(self.cook_url, uuid, 'completed')
        resp = util.retry_jobs(self.cook_url, job=uuid, assert_response=False, retries=1)
        self.assertEqual(400, resp.status_code, msg=resp.content)
        self.assertEqual('Retries would be less than attempts-consumed',
                         resp.json()['error'])

    def test_retries_unchanged_conflict(self):
        uuid, resp = util.submit_job(self.cook_url, command='exit 0', max_retries=1, disable_mea_culpa_retries=True)
        util.wait_for_job(self.cook_url, uuid, 'completed')
        resp = util.retry_jobs(self.cook_url, job=uuid, assert_response=False, retries=1)
        self.assertEqual(409, resp.status_code, msg=resp.content)
        job = util.load_job(self.cook_url, uuid)
        self.assertEqual('completed', job['status'], json.dumps(job, indent=2))
        self.assertEqual(1, len(job['instances']), json.dumps(job, indent=2))

        attempts = 2
        uuid, resp = util.submit_job(self.cook_url, command='sleep 30',
                                     max_retries=attempts, disable_mea_culpa_retries=True)
        try:
            util.wait_for_job(self.cook_url, uuid, 'running')
            resp = util.retry_jobs(self.cook_url, job=uuid, assert_response=False, retries=attempts)
            self.assertEqual(409, resp.status_code, msg=resp.content)
        finally:
            util.kill_jobs(self.cook_url, [uuid])

    def test_retries_unchanged_conflict_group(self):
        group_spec = util.minimal_group()
        group_uuid = group_spec['uuid']
        jobs = [{'group': group_uuid,
                 'command': 'exit 1',
                 'max_retries': 2,
                 'disable_mea_culpa_retries': True},
                {'group': group_uuid,
                 'command': 'exit 1',
                 'max_retries': 1,
                 'disable_mea_culpa_retries': True}]
        job_uuids, resp = util.submit_jobs(self.cook_url, jobs)
        util.wait_for_jobs(self.cook_url, job_uuids, 'completed')
        resp = util.retry_jobs(self.cook_url, groups=[group_uuid], assert_response=False, retries=2)
        self.assertEqual(409, resp.status_code, resp.content)
        error = resp.json()['error']
        self.assertTrue(job_uuids[0] in error, resp.content)
        self.assertFalse(job_uuids[1] in error, resp.content)

    def test_set_retries_to_attempts_conflict(self):
        uuid, resp = util.submit_job(self.cook_url, command='sleep 30', max_retries=5, disable_mea_culpa_retries=True)
        util.wait_until(lambda: util.load_job(self.cook_url, uuid),
                        lambda j: (len(j['instances']) == 1) and (j['instances'][0]['status'] == 'running'))
        util.kill_jobs(self.cook_url, [uuid])

        def instances_complete(job):
            return all([i['status'] == 'failed' for i in job['instances']])

        job = util.wait_until(lambda: util.load_job(self.cook_url, uuid), instances_complete)
        self.assertEqual('completed', job['status'], json.dumps(job, indent=2))
        num_instances = len(job['instances'])
        self.assertEqual(5 - num_instances, job['retries_remaining'], json.dumps(job, indent=2))

        resp = util.retry_jobs(self.cook_url, job=uuid, retries=num_instances, assert_response=False)
        self.assertEqual(409, resp.status_code, msg=resp.content)
        job = util.load_job(self.cook_url, uuid)
        self.assertEqual('completed', job['status'])
        self.assertEqual(num_instances, len(job['instances']), json.dumps(job, indent=2))

    def test_pools_in_default_limit_response(self):
        pools, resp = util.all_pools(self.cook_url)
        pool_names = [p['name'] for p in pools]

        user = self.determine_user()
        for limit in ['share', 'quota']:
            resp = util.get_limit(self.cook_url, limit, user)
            self.assertEqual(200, resp.status_code)
            for pool in pool_names:
                self.assertTrue(pool in resp.json()['pools'])
                for resource in ['cpus', 'gpus', 'mem']:
                    self.assertTrue(resource in resp.json()['pools'][pool])

            if len(pool_names) > 0:
                resp = util.get_limit(self.cook_url, limit, user, pool_names[0])
                self.assertFalse('pools' in resp.json())
            else:
                resp = util.get_limit(self.cook_url, limit, user)
                self.assertFalse('pools' in resp.json())

    def test_submit_plugin(self):
        if not util.demo_plugin_is_configured(self.cook_url):
            self.skipTest("Requires demo plugin to be configured")
        job_uuids = []
        try:
            # Should succeed, demo-plugin accepts jobs by default.
            job_uuid1, resp = util.submit_job(self.cook_url)
            job_uuids.append(job_uuid1)
            self.assertEqual(resp.status_code, 201, msg=resp.content)
            self.assertEqual(resp.content, str.encode(f"submitted jobs {job_uuid1}"))
            job = util.wait_for_job_in_statuses(self.cook_url, job_uuid1, ['completed', 'running'])

            # This should now fail to submit (demo plugin looks at job name)
            job_uuid2, resp = util.submit_job(self.cook_url, name='plugin_test.submit_fail')
            job_uuids.append(job_uuid2)
            self.assertEqual(resp.status_code, 400, msg=resp.content)
            self.assertTrue(b"Message1- Fail to submit" in resp.content, msg=resp.content)
        finally:
            util.kill_jobs(self.cook_url, [job_uuids], assert_response=False)

    def test_launch_plugin(self):
        if not util.demo_plugin_is_configured(self.cook_url):
            self.skipTest("Requires demo plugin to be configured")
        job_uuid = None
        try:
            # Tell demo plugin server to defer launching jobs (special logic matches based on job name)
            job_uuid, resp = util.submit_job(self.cook_url, name='plugin_test.launch_defer')
            self.assertEqual(resp.status_code, 201, msg=resp.content)
            self.assertEqual(resp.content, str.encode(f"submitted jobs {job_uuid}"))

            # Validate job is still waiting and unscheduled.
            def query_unscheduled():
                resp = util.unscheduled_jobs(self.cook_url, job_uuid)[0][0]
                self.logger.info(f"unscheduled_jobs response: {resp}")
                return any([r['reason'] == reasons.PLUGIN_IS_BLOCKING
                            for r in resp['reasons']])

            util.wait_until(query_unscheduled, lambda r: r, 30000)
            job = util.load_job(self.cook_url, job_uuid)
            details = f"Job details: {json.dumps(job, sort_keys=True)}"
            self.assertEquals(job['status'], 'waiting', details)

            # Wait a bit and the demo plugin will mark it as launchable.
            # So, see if it is now running or completed.
            job = util.wait_for_job_in_statuses(self.cook_url, job_uuid, ['completed', 'running'])
        finally:
            util.kill_jobs(self.cook_url, [job_uuid], assert_response=False)


    @unittest.skipIf(os.getenv('COOK_TEST_SKIP_RECONCILE') is not None,
                     'Requires not setting the COOK_TEST_SKIP_RECONCILE environment variable')
    def test_reconciliation(self):
        """
        This test relies on 4 running jobs being seeded in Datomic *before* Cook Scheduler starts
        up, so that when it does start up and the reconciler runs, it can reconcile those tasks
        (i.e. discover that they are not actually running in Mesos and mark them as failed). See
        scheduler/datomic/data/seed_running_jobs.clj for the details of this job-seeding.
        """
        info = util.scheduler_info(self.cook_url)
        cook_start_dt = dateutil.parser.parse(info['start-time'])
        cook_start_ms = (cook_start_dt - datetime.fromtimestamp(0, tz=utc)).total_seconds() * 1000
        start = int(cook_start_ms - 240000)
        end = util.current_milli_time()
        resp = util.jobs(self.cook_url, user='seed_running_jobs_user',
                         state=['failed', 'running', 'waiting'],
                         start=start, end=end, limit=4, name='running_job_*')
        self.assertEqual(200, resp.status_code, msg=resp.content)
        jobs = resp.json()
        self.assertEqual(4, len(jobs))
        for job in jobs:
            instances = job['instances']
            self.assertEqual('completed', job['status'], job)
            self.assertEqual('failed', job['state'])
            self.assertEqual(1, len(instances))
            self.assertEqual('Mesos task reconciliation', instances[0]['reason_string'])

    @unittest.skipUnless(util.using_data_local_fitness_calculator(), "Requires the data local fitness calculator")
    def test_data_local_constraint_missing_data(self):
        job_uuid, resp = util.submit_job(self.cook_url, datasets=[{'dataset': {'uuid': str(uuid.uuid4())}}])
        self.assertEqual(201, resp.status_code, resp.text)

        # Because the job has no data in the data locality service, it shouldn't be scheduled for a period of time.
        def query_unscheduled():
            resp = util.unscheduled_jobs(self.cook_url, job_uuid)[0][0]
            placement_reasons = [reason for reason in resp['reasons']
                                 if reason['reason'] == reasons.COULD_NOT_PLACE_JOB]
            self.logger.info(f"unscheduled_jobs response: {resp}")
            return placement_reasons

        placement_reasons = util.wait_until(query_unscheduled, lambda r: len(r) > 0)
        self.assertEqual(1, len(placement_reasons), str(placement_reasons))
        reason = placement_reasons[0]
        data_locality_reasons = [r for r in reason['data']['reasons']
                                 if r['reason'] == 'data-locality-constraint']
        self.assertEqual(1, len(data_locality_reasons))

    @unittest.skipUnless(util.using_data_local_fitness_calculator() and util.data_local_service_is_set(), 'Requires the data local fitness calculator')
    @pytest.mark.serial
    def test_data_local_constrait_not_suitable(self):
        job_uuid, resp = util.submit_job(self.cook_url, datasets=[{'dataset': {'foo': str(uuid.uuid4())}}])
        try:
            self.assertEqual(201, resp.status_code, resp.text)
            hosts_to_consider = util.hosts_to_consider(self.cook_url, self.mesos_url)
            target = hosts_to_consider[0]
            costs = [{'node': target['hostname'], 'cost': 0.1, 'suitable': True}]
            for host in hosts_to_consider[1:]:
                costs.append({'node': host['hostname'], 'cost': 0.1, 'suitable': False})
            self.logger.info(f'Updating costs: {costs}')
            data_local_service = os.getenv('DATA_LOCAL_SERVICE')
            util.session.post(f'{data_local_service}/set-costs', json={str(job_uuid): costs})
            instance = util.wait_for_instance(self.cook_url, job_uuid)
            self.logger.info(f'Scheduled instance: {instance}')
            self.assertEqual(instance['hostname'], target['hostname'])
        finally:
            util.kill_jobs(self.cook_url, [job_uuid])


    @unittest.skipUnless(util.data_local_service_is_set(), "Requires a data local service")
    @pytest.mark.serial
    @pytest.mark.xfail(condition=util.continuous_integration(), reason="Sometimes fails on Travis")
    def test_data_local_debug_endpoint(self):
        job_uuid, resp = util.submit_job(self.cook_url, datasets=[{'dataset': {'foo': str(uuid.uuid4())}}])
        try:
            self.assertEqual(201, resp.status_code, resp.text)
            slaves = util.get_mesos_state(self.mesos_url)['slaves']
            costs = []
            for slave in slaves:
                costs.append({'node': slave['hostname'], 'cost': 0.1})
            data_local_service = os.getenv('DATA_LOCAL_SERVICE')
            util.session.post(f'{data_local_service}/set-costs', json={str(job_uuid): costs})

            def get_last_update_time():
                resp = util.session.get(f'{self.cook_url}/data-local')
                return resp.json()

            last_update = util.wait_until(get_last_update_time, lambda x: x.get(str(job_uuid)) is not None)
            # This will throw if the datetime is not formatted correctly
            datetime.strptime(last_update[str(job_uuid)], '%Y-%m-%dT%H:%M:%S.%fZ')

            cost_resp = util.session.get(f'{self.cook_url}/data-local/{str(job_uuid)}')
            self.assertEqual(200, cost_resp.status_code)
            expected_costs = {}
            for slave in slaves:
                expected_costs[slave['hostname']] = {'cost': 0.1, 'suitable': True}
            self.assertEqual(expected_costs, cost_resp.json())

            util.wait_for_jobs(self.cook_url, [job_uuid], 'completed')
            settings = util.settings(self.cook_url)
            timeout = settings['data-local-fitness-calculator']['cache-ttl-ms'] + 2 * \
                      settings['data-local-fitness-calculator']['update-interval-ms']

            def get_debug_status_code():
                resp = util.session.get(f'{self.cook_url}/data-local/{str(job_uuid)}')
                return resp.status_code

            util.wait_until(get_debug_status_code, lambda c: c == 404, timeout)

            missing_resp = util.session.get(f'{self.cook_url}/data-local/{str(uuid.uuid4())}')
            self.assertEqual(404, missing_resp.status_code, missing_resp.text)
        finally:
            util.kill_jobs(self.cook_url, [job_uuid], assert_response=False)


    @unittest.skipUnless(util.supports_mesos_containerizer_images(), "Requires support for docker images in mesos containerizer")
    def test_mesos_containerizer_image_support(self):
        job_uuid, resp = util.submit_job(self.cook_url, container={'type': 'mesos', 'mesos': {'image': 'alpine'}})
        try:
            self.assertEqual(201, resp.status_code, resp.text)
            instance = util.wait_for_instance(self.cook_url, job_uuid, status='success')
            slave_host = instance['hostname']
            master_state = util.get_mesos_state(util.retrieve_mesos_url())
            slave_state = util.session.get(util.get_agent_endpoint(master_state, slave_host)).json()

            executor = util.get_executor(slave_state, instance['executor_id'], True)

            self.logger.info(f'Executor id: {instance["executor_id"]}')
            self.assertTrue(executor is not None, slave_state)
            container = executor['completed_tasks'][0]['container']
            self.assertEqual('MESOS', container['type'], container)
            self.assertEqual('DOCKER', container['mesos']['image']['type'], container)
            self.assertEqual('alpine', container['mesos']['image']['docker']['name'], container)
        finally:
            util.kill_jobs(self.cook_url, [job_uuid], assert_response=False)
