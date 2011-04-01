#!/usr/bin/python
#
# Copyright (c) 2010 Red Hat, Inc.
#
#
# This software is licensed to you under the GNU General Public License,
# version 2 (GPLv2). There is NO WARRANTY for this software, express or
# implied, including the implied warranties of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. You should have received a copy of GPLv2
# along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.
#
# Red Hat trademarks are not licensed under GPLv2. No permission is
# granted to use or replicate Red Hat trademarks that are incorporated
# in this software or its documentation.

import os
import pprint
import sys
import time
import unittest
from datetime import datetime, timedelta

srcdir = os.path.abspath(os.path.dirname(__file__)) + "/../../src/"
sys.path.insert(0, srcdir)

commondir = os.path.abspath(os.path.dirname(__file__)) + '/../common/'
sys.path.insert(0, commondir)

from pulp.server.tasking.task import (
    Task, task_waiting, task_finished, task_error, task_timed_out,
    task_canceled, task_complete_states)
from pulp.server.tasking.queue.fifo import FIFOTaskQueue
from pulp.server.db.model.persistence import TaskSnapshot, restore_from_snapshot
from pulp.server.api.repo import RepoApi
from pulp.server import async

import testutil

def noop():
    pass

def args(*args):
    assert len(args) > 0

def kwargs(**kwargs):
    assert len(kwargs) > 0

def result():
    return True

def error():
    raise Exception('Aaaargh!')

def interrupt_me():
    while True:
        time.sleep(0.5)


class TaskTester(unittest.TestCase):

    def setUp(self):
        self.rapi = RepoApi()
        async.initialize()

    def tearDown(self):
        self.rapi.clean()
        testutil.common_cleanup()

    def test_task_create(self):
        task = Task(noop)
        self.assertTrue(task.state == task_waiting)
        snapshot = TaskSnapshot(task)
        restored_task = restore_from_snapshot(snapshot)
        self.assertTrue(restored_task.state == task_waiting)

    def test_task_noop(self):
        task = Task(noop)
        task.run()
        snapshot = TaskSnapshot(task)
        self.assertTrue(task.state == task_finished)
        restored_task = restore_from_snapshot(snapshot)
        self.assertTrue(restored_task.state == task_finished)
        
    def test_task_args(self):
        task = Task(args, args=[1, 2, 'foo'])
        task.run()
        snapshot = TaskSnapshot(task)
        restored_task = restore_from_snapshot(snapshot)
        self.assertTrue(restored_task.state == task_finished)
        self.assertTrue(task.state == task_finished)

    def test_task_kwargs(self):
        task = Task(kwargs, kwargs={'arg1':1, 'arg2':2, 'argfoo':'foo'})
        snapshot = TaskSnapshot(task)
        task.run()
        snapshot = TaskSnapshot(task)
        restored_task = restore_from_snapshot(snapshot)
        self.assertTrue(restored_task.state == task_finished)
        self.assertTrue(task.state == task_finished)

    def test_task_result(self):
        task = Task(result)
        task.run()
        self.assertTrue(task.state == task_finished)
        self.assertTrue(task.result is True)

    def test_task_error(self):
        task = Task(error)
        task.run()
        snapshot = TaskSnapshot(task)
        restored_task = restore_from_snapshot(snapshot)
        self.assertTrue(task.state == task_error)
        self.assertTrue(task.traceback is not None)
        self.assertTrue(restored_task.state == task_error)
        self.assertTrue(restored_task.traceback is not None)
        
    def __test_sync_task(self):
        repo = self.rapi.create('some-id', 'some name', 'i386', 
                                'yum:http://repos.fedorapeople.org/repos/pulp/pulp/fedora-14/x86_64/')
        self.assertTrue(repo is not None)
        
        task = self.rapi.sync(repo['id'])
        snapshot = TaskSnapshot(task)
        restored_task = restore_from_snapshot(snapshot)
        print restored_task.state

class QueueTester(unittest.TestCase):

    def _wait_for_task(self, task, timeout=timedelta(seconds=20)):
        start = datetime.now()
        while task.state not in task_complete_states:
            time.sleep(0.1)
            if datetime.now() - start >= timeout:
                raise RuntimeError('Task wait timed out after %d seconds, with state: %s' %
                                       (timeout.seconds, task.state))
        if task.state == task_error:
            pprint.pprint(task.traceback)


class FIFOQueueTester(QueueTester):

    def setUp(self):
        self.queue = FIFOTaskQueue()

    def tearDown(self):
        del self.queue

    def test_task_enqueue(self):
        task = Task(noop)
        self.queue.enqueue(task)
        self.assertTrue(task.state == task_waiting)

    def test_enqueue_duplicate_allowed(self):
        # Setup
        task1 = Task(noop)
        self.queue.enqueue(task1)

        self.assertEqual(1, len(list(self.queue._FIFOTaskQueue__storage.all_tasks())))

        # Test
        task2 = Task(noop)
        self.queue.enqueue(task2, unique=False)

        # Verify
        self.assertEqual(2, len(list(self.queue._FIFOTaskQueue__storage.all_tasks())))

    def test_enqueue_duplicate_no_args(self):
        # Setup
        task1 = Task(noop)
        self.queue.enqueue(task1)

        self.assertEqual(1, len(list(self.queue._FIFOTaskQueue__storage.all_tasks())))

        # Test
        task2 = Task(noop)
        self.queue.enqueue(task2, unique=True)

        # Verify
        self.assertEqual(1, len(list(self.queue._FIFOTaskQueue__storage.all_tasks())))

    def test_enqueue_duplicate_with_same_kw_args(self):
        # Setup
        task1 = Task(kwargs, kwargs={'foo':1, 'bar':2})
        self.queue.enqueue(task1)

        self.assertEqual(1, len(list(self.queue._FIFOTaskQueue__storage.all_tasks())))

        # Test
        task2 = Task(kwargs, kwargs={'foo':1, 'bar':2})
        self.queue.enqueue(task2, unique=True)

        # Verify
        self.assertEqual(1, len(list(self.queue._FIFOTaskQueue__storage.all_tasks())))

    def test_enqueue_duplicate_with_different_kw_args(self):
        # Setup
        task1 = Task(kwargs, kwargs={'foo':1, 'bar':2})
        self.queue.enqueue(task1)

        self.assertEqual(1, len(list(self.queue._FIFOTaskQueue__storage.all_tasks())))

        # Test
        task2 = Task(kwargs, kwargs={'foo':2, 'bar':3})
        self.queue.enqueue(task2, unique=True)

        # Verify
        self.assertEqual(2, len(list(self.queue._FIFOTaskQueue__storage.all_tasks())))

    def test_enqueue_duplicate_with_same_args(self):
        # Setup
        task1 = Task(args, args=[1, 2])
        self.queue.enqueue(task1)

        self.assertEqual(1, len(list(self.queue._FIFOTaskQueue__storage.all_tasks())))

        # Test
        task2 = Task(args, args=[1, 2])
        self.queue.enqueue(task2, unique=True)

        # Verify
        self.assertEqual(1, len(list(self.queue._FIFOTaskQueue__storage.all_tasks())))

    def test_enqueue_duplicate_with_different_args(self):
        # Setup
        task1 = Task(args, args=[1, 2])
        self.queue.enqueue(task1)

        self.assertEqual(1, len(list(self.queue._FIFOTaskQueue__storage.all_tasks())))

        # Test
        task2 = Task(args, args=[2, 3])
        self.queue.enqueue(task2, unique=True)

        # Verify
        self.assertEqual(2, len(list(self.queue._FIFOTaskQueue__storage.all_tasks())))

    def test_task_dispatch(self):
        task = Task(noop)
        self.queue.enqueue(task)
        self._wait_for_task(task)
        self.assertTrue(task.state == task_finished)

    def test_task_dispatch_with_scheduled_time(self):
        task = Task(noop)
        delay_seconds = 10
        task.scheduled_time = time.time() + delay_seconds
        self.queue.enqueue(task)
        start_time = time.time()
        self._wait_for_task(task, timeout=timedelta(seconds=2 * delay_seconds))
        end_time = time.time()
        self.assertTrue(task.state == task_finished)
        self.assertTrue(end_time - start_time > delay_seconds)


    def test_task_find(self):
        task1 = Task(noop)
        self.queue.enqueue(task1)
        task2 = self.queue.find(id=task1.id)[0]
        self.assertTrue(task1 is task2)

    def test_find_invalid_criteria(self):
        # Setup
        task1 = Task(noop)
        self.queue.enqueue(task1)

        # Test
        found = self.queue.find(foo=task1.id)

        # Verify
        self.assertTrue(not found)

    def test_find_empty_queue(self):
        # Test
        found = self.queue.find(id=1)

        # Verify
        self.assertTrue(not found)

    def test_find_multiple_criteria(self):
        # Setup
        task1 = Task(noop)
        self.queue.enqueue(task1)

        # Test
        found = self.queue.find(id=task1.id, state=task_waiting)

        # Verify
        self.assertTrue(found[0] is task1)

    def test_find_multiple_matching(self):
        # Setup
        task1 = Task(noop)
        task2 = Task(noop)

        self.queue.enqueue(task1)
        self.queue.enqueue(task2)

        # Test
        found = self.queue.find(state=task_waiting)

        # Verify
        self.assertTrue(found[0] is task2)

    def test_task_status(self):
        task = Task(noop)
        self.queue.enqueue(task)
        self._wait_for_task(task)
        status = self.queue.find(id=task.id)
        self.assertTrue(status[0].state == task.state)

    def test_exists_matching_criteria(self):
        # Setup
        task1 = Task(noop)
        self.queue.enqueue(task1)

        # Test
        task2 = Task(noop)
        task2.id = task1.id

        result = self.queue.exists(task2, ['id'])

        # Verify
        self.assertTrue(result)

    def test_exists_unmatching_criteria(self):
        # Setup
        task1 = Task(noop)
        self.queue.enqueue(task1)

        # Test
        task2 = Task(noop)

        result = self.queue.exists(task2, ['id'])

        # Verify
        self.assertTrue(not result)

    def test_exists_multiple_criteria(self):
        # Setup
        task1 = Task(args, args=[1, 2])
        task2 = Task(args, args=[2, 3])

        self.queue.enqueue(task1)
        self.queue.enqueue(task2)

        # Test
        find_me = Task(args, args=[2, 3])

        found = self.queue.exists(find_me, ['method_name', 'args'])

        # Verify
        self.assertTrue(found)

    def test_exists_invalid_criteria(self):
        # Setup
        look_for = Task(noop)

        # Test & Verify
        self.assertRaises(ValueError, self.queue.exists, look_for, ['foo'])


class InterruptFIFOQueueTester(QueueTester):

    def setUp(self):
        self.queue = FIFOTaskQueue()

    def tearDown(self):
        del self.queue

    def disable_task_timeout(self):
        task = Task(interrupt_me, timeout=timedelta(seconds=2))
        self.queue.enqueue(task)
        self._wait_for_task(task)
        self.assertTrue(task.state == task_timed_out)

    def disable_task_cancel(self):
        task = Task(interrupt_me)
        self.queue.enqueue(task)
        self.queue.cancel(task)
        self._wait_for_task(task)
        self.assertTrue(task.state == task_canceled)

# run the unit tests ----------------------------------------------------------

if __name__ == '__main__':
    unittest.main()
